"""Where a company's own website is: one lookup, cached, never invented.

The funnel's answer carries a ticker and a name - not a URL. The only source in
this stack that knows a company's site is the earnings provider (Finnhub), whose
key already supplies the report dates, so the same key answers this question too.

Promised here:

* ``web_url(symbol)`` returns an ``https://`` URL or ``None``. Never a guess,
  never a search-page stand-in, and never a downgrade to plain ``http``.
* No key, a refused answer, throttling, an unknown ticker or an unreadable
  payload: ``None`` and one line in the log. A detail page without a link is
  still a complete detail page; a 500 is not.
* Answers are cached for 12 h - a company's site changes about never, and the
  same detail gets opened many times in a session.
"""

import logging
import os
import time
import urllib.parse

import requests

PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
ENV_API_KEY = "FINNHUB_API_KEY"
#: A company's site changes about never; a day is short enough for a rebrand.
DEFAULT_TTL_SECONDS = 12 * 60 * 60
#: A FAILED lookup is cached too, but only this long: a throttled or briefly
#: unreachable source must not be asked again on every click, and must not hide
#: the link for the rest of the day either.
FAILURE_TTL_SECONDS = 5 * 60
#: Short: this call runs while somebody waits for a detail block.
DEFAULT_TIMEOUT_SECONDS = 4.0

logger = logging.getLogger(__name__)

#: ``symbol -> (fetched_at, url_or_None)``. ``None`` is cached too: an unknown
#: ticker stays unknown for the session instead of being asked every click.
_CACHE: dict = {}


def clear_cache():
    """Forget every answer - the tests need a cold start, the app does not."""
    _CACHE.clear()


def _token(token=None):
    """The key to use: the given one, else the environment, else nothing."""
    wert = token if token is not None else os.environ.get(ENV_API_KEY, "")
    return (wert or "").strip()


def usable_url(wert):
    """``wert`` as an ``https`` URL, or ``None``.

    Only ``https`` passes: an ``http`` site would be sent in clear text, and a
    ``javascript:``/``data:`` value in an ``href`` is script in this page's
    origin. A URL without a host is nothing to open.
    """
    if not isinstance(wert, str):
        return None
    text = wert.strip()
    if not text:
        return None
    teile = urllib.parse.urlsplit(text)
    if teile.scheme.lower() != "https" or not teile.netloc:
        return None
    if "@" in teile.netloc:            # credentials have no business in a link
        return None
    return text


def web_url(symbol, *, token=None, session=None, timeout=DEFAULT_TIMEOUT_SECONDS,
            ttl_seconds=DEFAULT_TTL_SECONDS, now_fn=time.monotonic):
    """The company's own website for ``symbol``, or ``None``.

    Never raises: every failure is ``None`` plus a log line. The answer of a
    cache hit costs no request at all - the cached value may be ``None``.
    """
    schluessel = str(symbol or "").strip().upper()
    if not schluessel:
        return None

    jetzt = now_fn()
    zwischengespeichert = _CACHE.get(schluessel)
    if zwischengespeichert is not None:
        geholt_am, wert, gueltig = zwischengespeichert
        if jetzt - geholt_am < gueltig:
            return wert

    key = _token(token)
    if not key:
        # No key is a state, not an error: the link is simply not offered.
        _CACHE[schluessel] = (jetzt, None, ttl_seconds)
        return None

    sitzung = session or requests
    try:
        # ``verify=True`` steht ausdruecklich da, obwohl es die Vorgabe von
        # requests ist: hier geht ein API-Schluessel ueber die Leitung, und eine
        # Zeile weiter unten soll niemand raten muessen, ob geprueft wird.
        antwort = sitzung.get(PROFILE_URL,
                              params={"symbol": schluessel, "token": key},
                              timeout=timeout, verify=True)
    except Exception as exc:                      # noqa: BLE001 - never a 500
        logger.warning("company profile: %s could not be read (%s)",
                       schluessel, type(exc).__name__)
        _CACHE[schluessel] = (jetzt, None, FAILURE_TTL_SECONDS)
        return None

    if antwort.status_code != 200:
        logger.warning("company profile: %s answered HTTP %s", schluessel,
                       antwort.status_code)
        _CACHE[schluessel] = (jetzt, None, FAILURE_TTL_SECONDS)
        return None
    try:
        daten = antwort.json()
    except Exception:                             # noqa: BLE001 - unreadable body
        logger.warning("company profile: %s answered unreadable JSON", schluessel)
        _CACHE[schluessel] = (jetzt, None, FAILURE_TTL_SECONDS)
        return None
    if not isinstance(daten, dict):
        _CACHE[schluessel] = (jetzt, None, FAILURE_TTL_SECONDS)
        return None

    url = usable_url(daten.get("weburl"))
    if url is None:
        logger.info("company profile: %s names no usable website", schluessel)
    _CACHE[schluessel] = (jetzt, url, ttl_seconds)
    return url
