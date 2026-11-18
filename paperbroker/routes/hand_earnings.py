"""The hand-calendar route: one typed report date in, one honest answer out.

Only this route writes ``hand_earnings.json``. The body is small on purpose -
``symbol`` and ``date`` - because everything else in the file (``source``,
``notiert_am``) is either an optional short label or the server's own clock.

Rejections carry a stable code, not a sentence: the page turns ``invalid_date``
into its own language, and a code cannot drift into a different meaning by
being rephrased.
"""

from __future__ import annotations

from flask import current_app, jsonify, request

from ..hand_calendar import HandCalendarError
from ..view_support import _us_today
from .context import context

#: A source label is a short hint ("IR-Pressemitteilung"), not a citation. The
#: field is echoed in a badge, so it is cut rather than stored unbounded.
MAX_SOURCE_CHARS = 80


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
    ``{"status": "rejected", "code": ...}``; the HTTP status mirrors it (200/400)
    so a caller that only reads the status code is not misled either.
    """
    ctx = context()
    body = _body()
    symbol = str(body.get("symbol") or "").strip().upper()
    date_text = str(body.get("date") or "").strip()
    source = " ".join(str(body.get("source") or "").split())[:MAX_SOURCE_CHARS]
    heute = _us_today(ctx.now_fn)
    try:
        ergebnis = ctx.hand_calendar.save(symbol, date_text, source=source, day=heute)
    except HandCalendarError as exc:
        current_app.logger.info("hand earnings rejected for %r: %s", symbol, exc.code)
        return jsonify(status="rejected", code=exc.code, message=exc.message), 400
    except OSError:
        # The directory is mounted read-only or full: the caller must know that
        # nothing was stored, instead of a success that a reload would deny.
        current_app.logger.exception("hand earnings not written for %s", symbol)
        return jsonify(status="rejected", code="not_saved",
                       message="the file could not be written"), 500
    current_app.logger.info("hand earnings saved: %s -> %s", symbol,
                            ergebnis["entry"]["date"])
    eintrag = ergebnis["entry"]
    return jsonify(status="saved", symbol=ergebnis["symbol"], date=eintrag["date"],
                   source=eintrag["source"] or None,
                   notiert_am=eintrag["notiert_am"]), 200


def register(app) -> None:
    app.add_url_rule("/api/v1/hand-earnings", endpoint="api_hand_earnings",
                     view_func=post_hand_earnings, methods=["POST"])
