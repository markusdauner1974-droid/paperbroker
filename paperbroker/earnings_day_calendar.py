"""The earnings day calendar: one Nasdaq day list -> ranked, labelled groups.

Three calendar days (yesterday / today / tomorrow) are shown at the top of the
start page. This module owns the meaning of such a list and nothing else: it
takes the decoded JSON the adapter fetched, it never reads a clock and it never
touches the network or the disk.

Why the market cap is parsed here and not in the template
---------------------------------------------------------
The source delivers ``marketCap`` as TEXT (``"$88,237,580,000"``). Sorting text
would put ``"$9,000"`` above ``"$88,237,580,000"`` and the Top 25 would be a
lie. So the text is parsed once, strictly, into an int.

Why an unparseable market cap is ``None`` and never ``0``
---------------------------------------------------------
``0`` is a value: it would sort as the smallest company and it would look like
a measurement. ``None`` says "the source did not give us a number", the row is
kept and shown after the ranked ones, and the group counts it
(``without_market_cap``). Nothing is dropped silently and nothing is invented.

Rows without a usable symbol (no string, empty, longer than six characters or
not A-Z) are skipped, because a row without a ticker cannot be clicked and
cannot be checked; the count is reported as ``skipped``.
"""

import re

from .earnings_calendar import SESSION_AFTER, SESSION_BEFORE, SESSION_UNKNOWN

# --- the three calendar days, in the order the page shows them ----------- #
DAY_PREV = "prev"
DAY_TODAY = "today"
DAY_NEXT = "next"
DAY_ORDER = (DAY_PREV, DAY_TODAY, DAY_NEXT)

DEFAULT_TOP_N = 25

# A ticker the screener can actually accept: 1-6 uppercase letters (same rule
# as the ticker field). Lowercase is not "normalised" into a symbol - the
# calendar answers in uppercase, so a lowercase value is a broken row.
_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}$")

# "$88,237,580,000" / "88237580000". Nothing else: no suffix, no decimals, no
# sign - a value in a shape we do not understand is not a market cap.
_MARKET_CAP_RE = re.compile(r"^\$?([0-9][0-9,]*)$")

# The source's own vocabulary for the release session. Anything else is
# UNKNOWN; a default of "before the open" would be an invented fact.
_SESSION_BY_TIME = {
    "time-pre-market": SESSION_BEFORE,
    "time-after-hours": SESSION_AFTER,
}


def _text(value):
    """A trimmed string, or None - never ``""`` and never a number as text."""
    if not isinstance(value, str):
        return None
    wert = value.strip()
    return wert or None


def parse_market_cap(text):
    """``"$88,237,580,000"`` -> ``88237580000``; anything else -> ``None``."""
    if not isinstance(text, str):
        return None
    treffer = _MARKET_CAP_RE.match(text.strip())
    if treffer is None:
        return None
    try:
        return int(treffer.group(1).replace(",", ""))
    except ValueError:               # pragma: no cover - the regex already pins digits
        return None


def _normalise_row(row):
    """One source row in our shape, or ``None`` when it has no usable symbol."""
    if not isinstance(row, dict):
        return None
    symbol = row.get("symbol")
    if not isinstance(symbol, str):
        return None
    symbol = symbol.strip()
    if not _SYMBOL_RE.match(symbol):
        return None
    zeit = row.get("time")
    session = _SESSION_BY_TIME.get(zeit.strip().lower(), SESSION_UNKNOWN) if isinstance(zeit, str) else SESSION_UNKNOWN
    return {
        "symbol": symbol,
        "name": _text(row.get("name")),
        "session": session,
        # The forecast stays the text the source wrote ("$1.41"). Turning it
        # into a number would silently drop the currency sign and the "n/a".
        "eps_forecast": _text(row.get("epsForecast")),
        "market_cap": parse_market_cap(row.get("marketCap")),
        "fiscal_quarter_ending": _text(row.get("fiscalQuarterEnding")),
    }


def parse_calendar_rows(payload, day):
    """Read one day's Nasdaq calendar answer.

    ``payload_ok`` separates "the source listed nothing" (a Sunday: an empty
    list) from "the answer is unusable" (no ``data.rows`` list at all). The
    caller must treat the second case as an outage, not as an empty day.
    """
    rows = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            rows = data.get("rows")
    if not isinstance(rows, list):
        return {"date": day, "rows": [], "skipped": 0, "payload_ok": False}

    sauber = []
    uebersprungen = 0
    for zeile in rows:
        normalisiert = _normalise_row(zeile)
        if normalisiert is None:
            uebersprungen += 1
            continue
        sauber.append(normalisiert)
    return {"date": day, "rows": sauber, "skipped": uebersprungen, "payload_ok": True}


def _market_cap(zeile):
    """A real int or None: bools and strings are not market caps."""
    wert = zeile.get("market_cap")
    if isinstance(wert, bool) or not isinstance(wert, int):
        return None
    return wert


def rank_rows(rows, top_n=DEFAULT_TOP_N):
    """Rank by market cap descending, ties by symbol, unknown caps last.

    The sort key is total: same input -> byte-identical order, on every run and
    on every machine. Rows without a cap are neither ranked as zero nor
    dropped - they are kept after the ranked ones and counted.
    """
    zeilen = [dict(z) for z in (rows or []) if isinstance(z, dict)]
    ohne_cap = sum(1 for z in zeilen if _market_cap(z) is None)

    def schluessel(zeile):
        cap = _market_cap(zeile)
        return (cap is None, -(cap or 0), str(zeile.get("symbol") or ""))

    geordnet = sorted(zeilen, key=schluessel)
    grenze = DEFAULT_TOP_N if top_n is None else max(0, int(top_n))
    ausgewaehlt = geordnet[:grenze]
    return {
        "rows": ausgewaehlt,
        "total": len(zeilen),
        "hidden": max(0, len(zeilen) - len(ausgewaehlt)),
        "without_market_cap": ohne_cap,
    }


def build_calendar(days, top_n=DEFAULT_TOP_N):
    """Assemble the three groups in ``DAY_ORDER``, whatever order they arrive in.

    A missing or malformed day is an empty group: the page then says "keine
    Einträge" for that day instead of dropping a column.
    """
    gruppen = []
    for key in DAY_ORDER:
        geparst = days.get(key) if isinstance(days, dict) else None
        if not isinstance(geparst, dict):
            geparst = {}
        geordnet = rank_rows(geparst.get("rows") or [], top_n)
        gruppen.append({
            "key": key,
            "date": geparst.get("date"),
            "rows": geordnet["rows"],
            "total": geordnet["total"],
            "hidden": geordnet["hidden"],
            "without_market_cap": geordnet["without_market_cap"],
            "payload_ok": bool(geparst.get("payload_ok")),
            "skipped": int(geparst.get("skipped") or 0),
        })
    return {"groups": gruppen}
