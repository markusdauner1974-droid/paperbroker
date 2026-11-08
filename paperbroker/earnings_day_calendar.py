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
from datetime import date

from .earnings_calendar import SESSION_AFTER, SESSION_BEFORE, SESSION_UNKNOWN
from .optionable_symbols import filter_optionable

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

# The source stamps an answer as English text: "Sun, Nov 8, 2026". Spelling the
# abbreviations out is deliberate - ``%a``/``%b`` in ``strptime`` are LOCALE
# dependent, so on a German machine "Sun" is not a weekday name at all and the
# very same stamp would either raise or land on a different date. These tables
# parse the same way on every locale.
_WEEKDAY_BY_TOKEN = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_MONTH_BY_TOKEN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "Wday, Mon DD, YYYY" with freely chosen whitespace and an optional leading
# zero on the day. Only that shape is a date stamp; nothing is guessed.
_AS_OF_RE = re.compile(
    r"^(?P<wday>[A-Za-z]{3})\s*,\s*(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s*,\s*(?P<year>\d{4})$"
)

# The requested day is a plain ISO date. Non-padded or otherwise "helpful"
# spellings are rejected: the caller builds those strings itself.
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def parse_as_of(text):
    """``"Sun, Nov 8, 2026"`` -> ``datetime.date(2026, 11, 8)``; else ``None``.

    Only the source's exact shape counts: a three-letter English weekday, a
    comma, a three-letter English month, a day of one or two digits, a comma
    and a four-digit year - case-insensitively and with free whitespace.

    Why not ``datetime.strptime(text, "%a, %b %d, %Y")``: ``%a`` and ``%b`` are
    resolved against the process locale. On a German machine "Sun" is not a
    weekday name, so the same stamp either raises or (with a locale that
    happens to share a spelling) parses into a date nobody asked for. The
    lookup tables above make the result identical on every machine.

    A known weekday token alone is not enough: the date must also exist, so
    "Sun, Feb 31, 2026" is ``None`` rather than a rolled-over 3 March. The
    weekday is checked as vocabulary, not recomputed - the day the source names
    is the month/day/year triple, and that triple is what the caller compares.
    """
    if not isinstance(text, str):
        return None
    treffer = _AS_OF_RE.match(text.strip())
    if treffer is None:
        return None
    if treffer.group("wday").lower() not in _WEEKDAY_BY_TOKEN:
        return None
    monat = _MONTH_BY_TOKEN.get(treffer.group("mon").lower())
    if monat is None:
        return None
    try:
        return date(int(treffer.group("year")), monat, int(treffer.group("day")))
    except ValueError:               # 31 February, day 0, year 0000
        return None


def _iso_day(value):
    """The requested day as a ``date``, or ``None`` when it is not ISO text."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _ISO_DAY_RE.match(text) is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
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

    ``payload_ok`` separates three cases, and the line between them is drawn on
    evidence rather than convenience:

    * a ``data.rows`` LIST - the day as the source listed it,
    * ``data.rows: null`` TOGETHER WITH the source's own date stamp
      (``data.asOf``) that names EXACTLY the requested day - a real, empty day.
      Measured against the live endpoint on a Sunday: Nasdaq answers HTTP 200
      with ``{"data": {"asOf": "Sun, Nov 8, 2026", "headers": null, "rows":
      null}}``. Nothing was skipped, the source simply listed nothing, so
      calling that an outage turns every weekend into "the source does not
      answer",
    * everything else (``{}``, ``{"data": {}}``, a non-list ``rows``, ``None``)
      - unusable, ``payload_ok`` False. A bare ``{"data": {"rows": null}}``
      without ``asOf`` belongs here on purpose: without the source's own stamp
      there is no proof that a day was answered at all, so "empty" is not
      established and the caller must keep treating it as an outage.

    Why the stamp is checked against ``day`` and not merely for presence
    -------------------------------------------------------------------
    ``asOf`` is the day the source ANSWERS FOR, so a stamp for another day is
    proof about a different question - not about this one. Accepting any
    non-empty stamp would let an answer for Monday certify Sunday as empty, and
    the adapter would then cache an empty Sunday that no query ever produced.
    Unparseable stamps (no year, German weekday, 31 February) and an
    unparseable ``day`` fall on the outage side for the same reason: fail
    closed beats inventing an empty day.
    """
    angefragt = _iso_day(day)
    rows = None
    # An empty day must be backed by the source's own date stamp, and that
    # stamp must name the day we asked about: `rows: null` alone could just as
    # well be a truncated answer, and a stamp for another day answers another
    # question.
    leer_bestaetigt = False
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            rows = data.get("rows")
            if rows is None and "rows" in data and angefragt is not None:
                leer_bestaetigt = parse_as_of(data.get("asOf")) == angefragt
    if not isinstance(rows, list):
        if leer_bestaetigt:
            return {"date": day, "rows": [], "skipped": 0, "payload_ok": True}
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


def rank_rows(rows, top_n=DEFAULT_TOP_N, optionable=None):
    """Rank by market cap descending, ties by symbol, unknown caps last.

    The sort key is total: same input -> byte-identical order, on every run and
    on every machine. Rows without a cap are neither ranked as zero nor
    dropped - they are kept after the ranked ones and counted.

    ``optionable`` is the CBOE symbol set. It is applied BEFORE the Top-N cut,
    so a title without listed options costs no place: the group is the 25
    largest OPTIONABLE titles, not the 25 largest titles of which some then
    disappear. ``None`` means "the list is not available": no filtering at all
    (the page says "ungefiltert"), and ``without_options`` is 0.
    """
    zeilen = [dict(z) for z in (rows or []) if isinstance(z, dict)]
    behalten, ohne_optionen = filter_optionable(zeilen, optionable)
    ohne_cap = sum(1 for z in behalten if _market_cap(z) is None)

    def schluessel(zeile):
        cap = _market_cap(zeile)
        return (cap is None, -(cap or 0), str(zeile.get("symbol") or ""))

    geordnet = sorted(behalten, key=schluessel)
    grenze = DEFAULT_TOP_N if top_n is None else max(0, int(top_n))
    ausgewaehlt = geordnet[:grenze]
    return {
        "rows": ausgewaehlt,
        "total": len(behalten),
        "hidden": max(0, len(behalten) - len(ausgewaehlt)),
        "without_market_cap": ohne_cap,
        "without_options": len(ohne_optionen),
    }


def build_calendar(days, top_n=DEFAULT_TOP_N, optionable=None):
    """Assemble the three groups in ``DAY_ORDER``, whatever order they arrive in.

    A missing or malformed day is an empty group: the page then says "keine
    Einträge" for that day instead of dropping a column. ``optionable`` is the
    CBOE symbol set and is passed to every group, so the Top-N selection is
    made among optionable titles only.
    """
    gruppen = []
    for key in DAY_ORDER:
        geparst = days.get(key) if isinstance(days, dict) else None
        if not isinstance(geparst, dict):
            geparst = {}
        geordnet = rank_rows(geparst.get("rows") or [], top_n, optionable=optionable)
        gruppen.append({
            "key": key,
            "date": geparst.get("date"),
            "rows": geordnet["rows"],
            "total": geordnet["total"],
            "hidden": geordnet["hidden"],
            "without_market_cap": geordnet["without_market_cap"],
            "without_options": geordnet["without_options"],
            "payload_ok": bool(geparst.get("payload_ok")),
            "skipped": int(geparst.get("skipped") or 0),
        })
    return {"groups": gruppen}
