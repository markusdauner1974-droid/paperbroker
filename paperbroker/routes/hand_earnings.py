"""The hand-calendar route: one typed report date in, one honest answer out.

This is the ONLY writer of the in-memory hand calendar and the first POST of the
app, so it carries three guards the read routes do not need:

* **Content type.** A body has to arrive as ``application/json``; anything else
  is ``415``. A browser will not send that header cross-origin without a
  successful CORS preflight, which this app never grants - so a foreign page
  cannot post here by accident. (A form post is refused with a code, not with a
  vague validation error.)
* **A token, when the operator sets one.** ``HAND_EARNINGS_TOKEN`` in the
  environment turns the route into a locked door: the header
  ``X-Hand-Earnings-Token`` has to carry the same value, otherwise ``403``. The
  page receives the token from the server (``data-hand-token`` on the funnel
  block) and sends it along, so the dashboard keeps working. Without the
  variable the route stays as open as every other route of this app - a private
  LAN tool with one authenticated route and thirty open ones would be a lock on
  the wrong door.
* **A rate limit.** Thirty saves per minute per client, then ``429``. Each save
  rewrites the whole document, so the limit is what keeps a runaway loop from
  turning the disk into the bottleneck.

Rejections carry a stable code, not a sentence: the page turns ``invalid_date``
into its own language, and a code cannot drift into a different meaning by
being rephrased.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections import deque

from flask import current_app, jsonify, request

from ..hand_calendar import HandCalendarError, valid_symbol
from ..view_support import _us_today
from .context import context

#: A source label is a short hint ("IR-Pressemitteilung"), not a citation. The
#: field is echoed in a badge, so it is cut rather than stored unbounded.
MAX_SOURCE_CHARS = 80

#: The optional lock: set it and every write has to present it.
ENV_TOKEN = "HAND_EARNINGS_TOKEN"
TOKEN_HEADER = "X-Hand-Earnings-Token"

#: Saves one client may make per minute. A human needs one; a loop would need
#: the disk.
MAX_SAVES_PER_MINUTE = 30
_RATE_WINDOW_SECONDS = 60.0

#: How many clients the limiter may remember. Bounded because the table would
#: otherwise be a place an unauthenticated caller can allocate in.
MAX_TRACKED_CLIENTS = 512

_SAVES: dict = {}
_SAVES_LOCK = threading.Lock()


def configured_token():
    """The token from the environment, or ``None`` when the route is open."""
    token = (os.environ.get(ENV_TOKEN) or "").strip()
    return token or None


def _client_key():
    """Who is asking: the SOCKET address - never a header the caller controls.

    ``X-Forwarded-For`` is written by the client. Keying on it would let anyone
    mint unlimited fresh buckets by sending a new value per request - a limiter
    that the limited party can reset is not a limiter. Behind a reverse proxy all
    clients then share one bucket: the stricter direction, and for a human-driven
    save (one per quarter at most) the right one.
    """
    return request.remote_addr or "local"


def _too_many(client):
    """``True`` when this client has used up its minute of saves."""
    jetzt = time.monotonic()
    with _SAVES_LOCK:
        zeiten = _SAVES.get(client)
        if zeiten is None:
            _make_room(jetzt)
            zeiten = _SAVES[client] = deque()
        while zeiten and jetzt - zeiten[0] > _RATE_WINDOW_SECONDS:
            zeiten.popleft()
        if len(zeiten) >= MAX_SAVES_PER_MINUTE:
            return True
        zeiten.append(jetzt)
        return False


def _make_room(jetzt):
    """Keep the tracking table bounded - called with ``_SAVES_LOCK`` held.

    First the buckets that are already dead, then - if a caller kept inventing
    clients - the whole table. Forgetting a used-up counter can cost one extra
    save per client; an unbounded table would be a place to allocate in.
    """
    if len(_SAVES) < MAX_TRACKED_CLIENTS:
        return
    for schluessel in [k for k, v in _SAVES.items()
                       if not v or jetzt - v[-1] > _RATE_WINDOW_SECONDS]:
        del _SAVES[schluessel]
    if len(_SAVES) >= MAX_TRACKED_CLIENTS:
        _SAVES.clear()


def _rejected(code, message, status):
    return jsonify(status="rejected", code=code, message=message), status


def optionable_symbols(ctx):
    """The CBOE symbol set as a ``frozenset``, or ``None`` when it is not available.

    ``None`` is the honest "no list": the page then shows no hand-calendar field
    at all, because a list that failed to load must not look like "no options".
    The adapter behind this call owns the 24 h cache and never raises for a dead
    source, but this route must not depend on that - a broken adapter is still
    "no list", not a broken scan.
    """
    try:
        answer = ctx.earnings_adapter.get_optionable_symbols()
    except Exception:                       # noqa: BLE001 - a missing list is a state
        current_app.logger.warning("optionable symbols unavailable", exc_info=True)
        return None
    symbols = answer.get("symbols") if isinstance(answer, dict) else None
    if not symbols:
        return None
    return frozenset(str(symbol).strip().upper() for symbol in symbols)


def _body():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def post_hand_earnings():
    """``POST /api/v1/hand-earnings`` - store one curated report date.

    ``symbol`` must be a US ticker, ``date`` a strict ISO day between the scan
    day and 270 days ahead. The answer is ``{"status": "saved", ...}`` or
    ``{"status": "rejected", "code": ...}``; the HTTP status mirrors it so a
    caller that only reads the status code is not misled either.
    """
    token = configured_token()
    # Konstantzeit-Vergleich: ein simples != verraet ueber die Laufzeit, wie
    # viele Zeichen stimmen - ein Orakel auf das Token. compare_digest kostet
    # nichts und schliesst das aus.
    #
    # Verglichen werden BYTES: compare_digest nimmt bei str nur ASCII an und
    # wirft sonst TypeError - ein Header mit Umlauten (Werkzeug dekodiert
    # latin-1) wuerde daraus einen 500er machen statt eines 403.
    gegeben = (request.headers.get(TOKEN_HEADER) or "").encode("utf-8")
    erwartet = token.encode("utf-8") if token is not None else b""
    if token is not None and not hmac.compare_digest(gegeben, erwartet):
        current_app.logger.warning("hand earnings write refused: token missing or wrong")
        return _rejected("forbidden", "a valid write token is required", 403)

    # The one header a foreign page cannot send without a preflight this app
    # never allows: a form post, an <img> or a plain link cannot fake it.
    if not request.is_json:
        return _rejected("unsupported_media_type",
                         "the body has to be application/json", 415)

    client = _client_key()
    if _too_many(client):
        current_app.logger.warning("hand earnings rate limit hit by %s", client)
        return _rejected("too_many_requests", "too many saves; wait a minute", 429)

    ctx = context()
    body = _body()
    symbol = str(body.get("symbol") or "").strip().upper()
    # "Kein Feld" ist ein Fehler, "leeres Feld" ist der Loeschauftrag: die Seite
    # schickt beim Leeren ausdruecklich "", waehrend ein Aufruf ohne Datum
    # einfach unvollstaendig ist. Beides zu verwechseln hiesse, dass ein
    # vergessenes Feld einen Eintrag loescht.
    if not valid_symbol(symbol):
        return _rejected("invalid_symbol",
                         "a symbol is one to six capital letters", 400)
    if "date" not in body:
        return _rejected("invalid_date", "the date is not YYYY-MM-DD", 400)
    # Nur ein AUSDRUECKLICH leeres Feld loescht. Leerzeichen sind ein Tippfehler,
    # kein Loeschauftrag.
    roh = body.get("date")
    date_text = "" if roh is None else str(roh).strip()
    source = " ".join(str(body.get("source") or "").split())[:MAX_SOURCE_CHARS]
    heute = _us_today(ctx.now_fn)

    # Ein LEERES Datum loescht den Eintrag. Dieselbe Taste, dieselbe Anfrage:
    # ein falsch eingetragenes Datum muss weggehen koennen, ohne Neustart und
    # ohne zweiten Knopf.
    if roh is None or roh == "":
        try:
            gab_es = ctx.hand_calendar.delete(symbol)
        except HandCalendarError as exc:
            return _rejected(exc.code, exc.message, 400)
        return jsonify(status="deleted", symbol=symbol.upper(), existed=gab_es), 200

    try:
        ergebnis = ctx.hand_calendar.save(symbol, date_text, source=source, day=heute)
    except HandCalendarError as exc:
        current_app.logger.info("hand earnings rejected for %r: %s", symbol, exc.code)
        return _rejected(exc.code, exc.message, 400)
    eintrag = ergebnis["entry"]
    return jsonify(status="saved", symbol=ergebnis["symbol"], date=eintrag["date"],
                   source=eintrag["source"] or None,
                   notiert_am=eintrag["notiert_am"]), 200


def register(app) -> None:
    app.add_url_rule("/api/v1/hand-earnings", endpoint="api_hand_earnings",
                     view_func=post_hand_earnings, methods=["POST"])
