"""Earnings dates, the option-expiry deadline rule and the US trading calendar.

Three things live here and nowhere else:

* the JSON contract for one ticker's next earnings date (``schema_version 1.0``):
  the date, the release session, whether the date is confirmed or estimated,
  where it came from and when the source was really checked,
* the deterministic rule that combines an earnings date with the expirations a
  chain REALLY lists and yields the planned last exit day plus one usable
  expiration - or a named reason why there is none,
* the US trading calendar the exit day is checked against (weekends, the ten
  NYSE holidays and the documented special closures).

Everything here is pure: no I/O, no clock, no network. The adapter
(``adapters/earnings/NasdaqEarningsAdapter.py``) owns source access and cache;
this module owns the meaning.

Why the dates are never guessed
-------------------------------
A missing date stays ``None``. There is no "+3 months" roll-forward anywhere in
this module: an earnings date that nobody confirmed is worse than no date,
because it would silently authorise a trade window. The same applies to
expirations - only dates that appear in the chain's own symbol list can ever be
selected, and if none of them lies after the earnings date the answer is
"blocked", never the next best date.
"""

import re
from datetime import date, timedelta

SCHEMA_VERSION = "1.0"

# --- release session ------------------------------------------------------ #
SESSION_BEFORE = "before_market"
SESSION_AFTER = "after_market"
SESSION_UNKNOWN = "unknown"
SESSIONS = (SESSION_BEFORE, SESSION_AFTER, SESSION_UNKNOWN)

# --- how well the date is established ------------------------------------- #
CONFIRMATION_CONFIRMED = "confirmed"
CONFIRMATION_ESTIMATED = "estimated"
CONFIRMATION_UNKNOWN = "unknown"
CONFIRMATIONS = (CONFIRMATION_CONFIRMED, CONFIRMATION_ESTIMATED, CONFIRMATION_UNKNOWN)

# --- state of the record itself (query/verification state, NOT the domain
# --- confirmation above - the two are deliberately separate fields) -------- #
STATUS_FRESH = "fresh"            # valid date, checked within the freshness limit
STATUS_STALE = "stale"            # valid date, but the proof is too old to authorise
STATUS_UNKNOWN = "unknown"        # source answered, but has no date for this ticker
STATUS_INVALID = "invalid"        # source answered with something unusable/contradictory
STATUS_UNAVAILABLE = "unavailable"  # source could not be reached and no usable cache
STATUSES = (STATUS_FRESH, STATUS_STALE, STATUS_UNKNOWN, STATUS_INVALID, STATUS_UNAVAILABLE)

# --- quality flags -------------------------------------------------------- #
FLAG_ESTIMATED = "source_marks_date_as_estimate"
FLAG_SESSION_UNKNOWN = "release_session_unknown"
FLAG_CACHE_USED = "served_from_cache"
FLAG_LIVE_FAILED = "live_query_failed"
FLAG_CALENDAR_CONFIRMED = "calendar_lists_symbol_on_date"
FLAG_CALENDAR_NOT_LISTED = "calendar_does_not_list_symbol_on_date"
FLAG_CALENDAR_UNAVAILABLE = "calendar_cross_check_unavailable"
FLAG_SESSION_CONFLICT = "session_conflict_between_sources"
FLAG_DATE_CONFLICT = "date_conflict_between_sources"
FLAG_SOURCE_NOT_CONFIGURED = "earnings_source_not_configured"
FLAG_DATE_IN_PAST = "earnings_date_in_past"
FLAG_CACHE_TIMESTAMP_FUTURE = "cache_timestamp_in_future"

# --- decision reasons (stable codes for machines, text for humans) -------- #
REASON_TEXT = {
    "earnings_source_not_configured": "Keine Earnings-Quelle konfiguriert - es wird nichts geschätzt.",
    "earnings_date_unknown": "Kein nächster Earnings-Termin bekannt.",
    "earnings_source_unavailable": "Die Earnings-Quelle ist nicht erreichbar und kein Cache-Eintrag liegt vor.",
    "earnings_data_invalid": "Die Earnings-Antwort ist unbrauchbar oder widersprüchlich.",
    "earnings_data_stale": "Der Earnings-Nachweis ist zu alt - er darf keinen Handel freigeben.",
    "earnings_status_unknown": "Der Prüfstatus des Earnings-Eintrags ist unbekannt - ohne belegten Status keine Freigabe.",
    "earnings_status_unrecognised": "Der Prüfstatus ist ein unbekannter Wert - keine Freigabe.",
    "earnings_date_in_past": "Der Earnings-Termin liegt in der Vergangenheit.",
    "date_confirmation_unknown": "Der Termin ist weder bestätigt noch als Schätzung belegt.",
    "expirations_unavailable": "Die gelisteten Verfallstermine konnten nicht gelesen werden.",
    "no_expirations_listed": "Die Optionskette nennt keinen Verfallstermin.",
    "no_listed_expiration_after_earnings": "Kein gelisteter Verfall liegt nach dem Earnings-Termin.",
    "exit_day_not_resolvable": "Vor dem Earnings-Termin ist kein US-Handelstag bestimmbar.",
    "exit_day_in_past": "Der geplante Ausstiegstag liegt in der Vergangenheit.",
    "expiration_not_after_earnings": "Der Verfall liegt nicht nach dem Earnings-Termin.",
}

# Reasons that mean "we do not know", as opposed to "we know it does not fit".
UNKNOWN_REASONS = frozenset({
    "earnings_source_not_configured",
    "earnings_date_unknown",
    "earnings_source_unavailable",
    "earnings_data_invalid",
    "earnings_status_unknown",
    "earnings_status_unrecognised",
    "date_confirmation_unknown",
    "expirations_unavailable",
    "no_expirations_listed",
    "exit_day_not_resolvable",
})

EXIT_RULE = "letzter US-Handelstag strikt vor dem Earnings-Termin"

_US_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ESTIMATE_RE = re.compile(r"expected\s*\*|\*\s*(?:for|to)\b|is\s+estimated|are\s+estimated|derived\s+from\s+an\s+algorithm")
_CONFIRMED_RE = re.compile(r"\bwill\s+report\b|\bis\s+scheduled\s+to\s+report\b|\bhas\s+confirmed\b|\bconfirmed\s+to\s+report\b")
_NO_DATE_RE = re.compile(r"hasn't\s+provided|has\s+not\s+provided|no\s+upcoming\s+earnings|not\s+available")


def empty_earnings(ticker):
    """The contract with every field present and nothing invented."""
    return {
        "next_earnings_date": None,
        "release_session": SESSION_UNKNOWN,
        "date_confirmation": CONFIRMATION_UNKNOWN,
        "source": None,
        "source_checked_at_utc": None,
        "retrieved_at_utc": None,
        "status": STATUS_UNKNOWN,
        "quality_flags": [],
    }


def earnings_body(ticker, earnings, cache=None):
    """Wrap one earnings record into the versioned response body.

    ``cache`` is optional and only describes WHY the record is what it is; it
    never changes the domain fields, so a caller cannot mistake a cached answer
    for a live check.
    """
    body = {
        "schema_version": SCHEMA_VERSION,
        "ticker": ticker,
        "earnings": earnings,
    }
    if cache is not None:
        body["cache"] = cache
    return body


# ---------------------------------------------------------------------- #
# Source parsing (pure: the adapter hands in the decoded JSON)            #
# ---------------------------------------------------------------------- #
def _first_text(payload):
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    for key in ("reportText", "announcement"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def session_from_text(text):
    """'before market open' / 'after market close' -> a named session, else unknown.

    No timezone is attached and no default is assumed: a text without a session
    phrase yields UNKNOWN even when the date itself is fine.
    """
    lowered = text.lower()
    if "before market open" in lowered or "before the open" in lowered or "pre-market" in lowered:
        return SESSION_BEFORE
    if "after market close" in lowered or "after the close" in lowered or "after-hours" in lowered:
        return SESSION_AFTER
    return SESSION_UNKNOWN


def parse_nasdaq_earnings_date(payload):
    """Read Nasdaq's ``analyst/<symbol>/earnings-date`` answer.

    Returns a dict with ``parse_status`` in {"ok", "no_date", "invalid"}, the
    normalised date (US ``MM/DD/YYYY`` in the source, ISO here), the session and
    the confirmation level. The source never gets the benefit of the doubt: a
    date that does not exist as a calendar date is ``invalid``, and an
    unmarked date is ``unknown`` rather than ``confirmed``.
    """
    text = _first_text(payload)
    raw = {
        "provider": "nasdaq",
        "headline": text or None,
        "date_text": None,
        "session_text": None,
    }
    if not text:
        return {
            "parse_status": "invalid",
            "reason": "source answer carries no report text",
            "raw": raw,
        }
    if _NO_DATE_RE.search(text) and not _US_DATE_RE.search(text):
        return {
            "parse_status": "no_date",
            "reason": "the source states that it has no date for this symbol",
            "raw": raw,
        }

    treffer = _US_DATE_RE.search(text)
    if treffer is None:
        return {
            "parse_status": "invalid",
            "reason": "no MM/DD/YYYY date in the source text",
            "raw": raw,
        }

    monat, tag, jahr = (int(treffer.group(1)), int(treffer.group(2)), int(treffer.group(3)))
    raw["date_text"] = treffer.group(0)
    try:
        termin = date(jahr, monat, tag)
    except ValueError:
        return {
            "parse_status": "invalid",
            "reason": f"source date {treffer.group(0)} is not a real calendar date",
            "raw": raw,
        }

    # The phrase that carries the session sits around the date; taking the whole
    # sentence is safe here because the text has exactly one sentence about the
    # upcoming report.
    session = session_from_text(text)
    raw["session_text"] = {
        SESSION_BEFORE: "before market open",
        SESSION_AFTER: "after market close",
        SESSION_UNKNOWN: None,
    }[session]

    if _ESTIMATE_RE.search(text):
        confirmation = CONFIRMATION_ESTIMATED
    elif _CONFIRMED_RE.search(text):
        confirmation = CONFIRMATION_CONFIRMED
    else:
        confirmation = CONFIRMATION_UNKNOWN

    return {
        "parse_status": "ok",
        "next_earnings_date": termin.isoformat(),
        "release_session": session,
        "date_confirmation": confirmation,
        "reason": None,
        "raw": raw,
    }


def parse_nasdaq_calendar_row(payload, ticker, expected_date):
    """Read the daily earnings calendar for one symbol on one date.

    The calendar is a second, independently structured answer from the same
    provider. It can CONFIRM the date and the session; it never upgrades a date
    to ``confirmed`` and it never replaces a date.
    """
    symbol = (ticker or "").strip().upper()
    rows = (((payload or {}).get("data") or {}).get("rows")) if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    for zeile in rows:
        if not isinstance(zeile, dict):
            continue
        if str(zeile.get("symbol") or "").strip().upper() != symbol:
            continue
        zeit = str(zeile.get("time") or "").strip().lower()
        session = {
            "time-pre-market": SESSION_BEFORE,
            "time-after-hours": SESSION_AFTER,
            "time-not-supplied": SESSION_UNKNOWN,
        }.get(zeit, SESSION_UNKNOWN)
        return {
            "listed": True,
            "date": expected_date,
            "release_session": session,
            "raw_time": zeile.get("time"),
            "fiscal_quarter_ending": zeile.get("fiscalQuarterEnding"),
        }
    return {"listed": False, "date": expected_date, "release_session": SESSION_UNKNOWN, "raw_time": None}


def merge_calendar_check(earnings, check):
    """Fold a calendar cross-check into a record - without ever improving it.

    * the calendar listing the symbol on the same date only sets a flag,
    * a different date is a contradiction and makes the record invalid,
    * a different session is a contradiction about the SESSION only: the date
      stays, the session becomes UNKNOWN and the conflict is visible.
    """
    if check is None:
        earnings["quality_flags"] = sorted(set(list(earnings.get("quality_flags") or []) + [FLAG_CALENDAR_UNAVAILABLE]))
        return earnings, FLAG_CALENDAR_UNAVAILABLE
    flags = list(earnings.get("quality_flags") or [])
    if not check.get("listed"):
        flags.append(FLAG_CALENDAR_NOT_LISTED)
        earnings["quality_flags"] = sorted(set(flags))
        return earnings, FLAG_CALENDAR_NOT_LISTED

    if check.get("date") and earnings.get("next_earnings_date") != check["date"]:
        earnings["status"] = STATUS_INVALID
        earnings["quality_flags"] = sorted(set(flags + [FLAG_DATE_CONFLICT]))
        return earnings, FLAG_DATE_CONFLICT

    flags.append(FLAG_CALENDAR_CONFIRMED)
    kalender_session = check.get("release_session") or SESSION_UNKNOWN
    eigene = earnings.get("release_session") or SESSION_UNKNOWN
    if kalender_session != SESSION_UNKNOWN and eigene != SESSION_UNKNOWN and kalender_session != eigene:
        earnings["release_session"] = SESSION_UNKNOWN
        flags.append(FLAG_SESSION_CONFLICT)
    elif eigene == SESSION_UNKNOWN and kalender_session != SESSION_UNKNOWN:
        # The calendar is a structured field, not a sentence: filling an UNKNOWN
        # session from it is evidence, not a guess. The confirmation is untouched.
        earnings["release_session"] = kalender_session
    earnings["quality_flags"] = sorted(set(flags))
    return earnings, FLAG_CALENDAR_CONFIRMED


# ---------------------------------------------------------------------- #
# US trading calendar                                                     #
# ---------------------------------------------------------------------- #
def _nth_weekday(year, month, weekday, n):
    erster = date(year, month, 1)
    versatz = (weekday - erster.weekday()) % 7
    return erster + timedelta(days=versatz + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    naechster = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    letzter = naechster - timedelta(days=1)
    return letzter - timedelta(days=(letzter.weekday() - weekday) % 7)


def _observed(day):
    """NYSE observance: Saturday -> Friday before, Sunday -> Monday after."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def easter_sunday(year):
    """Gregorian Easter Sunday (anonymous algorithm, no tables)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    monat = (h + ell - 7 * m + 114) // 31
    tag = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, monat, tag)


# Closures that are not derivable from a rule. Each one is a documented NYSE
# decision; keeping them here means the calendar answers correctly for the
# years the screener can still see in a chain.
_SPECIAL_CLOSURES = frozenset({
    date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
    date(2004, 6, 11),   # Ronald Reagan, national day of mourning
    date(2007, 1, 2),    # Gerald Ford, national day of mourning
    date(2012, 10, 29), date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),   # George H. W. Bush, national day of mourning
    date(2025, 1, 9),    # Jimmy Carter, national day of mourning
})


def us_market_holidays(year):
    """NYSE holidays whose OBSERVED date falls into ``year``.

    New Year's Day is the awkward one: 1 Jan 2022 was a Saturday, so the market
    was closed on 31 Dec 2021. The observed date is what the market closes on,
    and that is what this set contains.
    """
    tage = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),                # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                # Washington's Birthday
        easter_sunday(year) - timedelta(days=2),    # Good Friday
        _last_weekday(year, 5, 0),                  # Memorial Day
        _observed(date(year, 7, 4)),                # Independence Day
        _nth_weekday(year, 9, 0, 1),                # Labor Day
        _nth_weekday(year, 11, 3, 4),               # Thanksgiving
        _observed(date(year, 12, 25)),              # Christmas
    }
    if year >= 2022:
        tage.add(_observed(date(year, 6, 19)))      # Juneteenth, NYSE since 2022
    tage.update(t for t in _SPECIAL_CLOSURES if t.year == year)
    return frozenset(tage)


def is_us_trading_day(day):
    """Monday-Friday, not a holiday, not a special closure."""
    if not isinstance(day, date):
        return False
    if day.weekday() >= 5:
        return False
    if day in _SPECIAL_CLOSURES:
        return False
    # The observed date of 1 Jan can fall into the previous year, so the
    # neighbouring years are consulted as well - but only those that exist.
    # Without the guard, 31 Dec 9999 makes us_market_holidays(10000) raise.
    jahre = [jahr for jahr in (day.year - 1, day.year, day.year + 1) if 1 <= jahr <= 9999]
    return all(day not in us_market_holidays(jahr) for jahr in jahre)


def last_trading_day_before(day, max_lookback_days=10):
    """The last US trading day strictly before ``day``, or None.

    Deliberately conservative: this is the exit rule, and it is the same answer
    whether the report is before the open or after the close. Exiting one day
    early never holds a position into the numbers; exiting on the reporting day
    could, depending on the session.
    """
    if not isinstance(day, date):
        return None
    kandidat = day - timedelta(days=1)
    for _ in range(max_lookback_days):
        if is_us_trading_day(kandidat):
            return kandidat
        kandidat -= timedelta(days=1)
    return None


def remaining_trading_days(today, exit_day, max_calendar_days=400):
    """Trading days from ``today`` (exclusive) to ``exit_day`` (inclusive).

    ``None`` when either side is not a real date - an unknown "today" must not
    be replaced by the system clock, and an exit day that could not be resolved
    cannot be counted. An exit day already behind us counts 0; whether that also
    blocks the window is the caller's decision, not this function's.

    The walk is bounded to ``max_calendar_days`` (400, a bit over a trading
    year). A range wider than that is not a calendar question we can answer, and
    an unbounded loop is reachable: the source accepts any four-digit year, so a
    bogus ``01/01/9999`` would otherwise walk ~2.9 million days and rebuild the
    holiday sets on every one of them.

    The count walks the calendar day by day and asks ``is_us_trading_day``, so
    weekends, the ten NYSE holidays and the documented special closures are all
    honoured. A Monday-to-Friday loop would report one day too many in every
    holiday week.
    """
    if not isinstance(today, date) or not isinstance(exit_day, date):
        return None
    if exit_day < today:
        return 0
    if (exit_day - today).days > max_calendar_days:
        return None
    anzahl = 0
    # Offsets, not `tag += 1`: after the last day that addition would compute
    # exit_day + 1, which overflows for an exit day at date.max.
    for versatz in range(1, (exit_day - today).days + 1):
        if is_us_trading_day(today + timedelta(days=versatz)):
            anzahl += 1
    return anzahl


# ---------------------------------------------------------------------- #
# Earnings + listed expirations -> trade window                           #
# ---------------------------------------------------------------------- #
def _reason(code):
    return {"code": code, "message": REASON_TEXT.get(code, code)}


def _clean_expirations(expirations):
    """ISO dates only, unique, sorted; everything else is reported, not dropped."""
    sauber, ungueltig = set(), []
    for wert in expirations or []:
        if isinstance(wert, date):
            sauber.add(wert)
            continue
        text = str(wert or "").strip()
        try:
            sauber.add(date.fromisoformat(text))
        except ValueError:
            if text:
                ungueltig.append(text)
    return sorted(sauber), ungueltig


def combine_earnings_and_expirations(earnings, expirations, *, today):
    """Decide whether ONE listed expiration after the earnings date exists.

    ``expirations`` is the list the chain really carries (``None`` means "could
    not be read"). ``today`` is the current US/Eastern trading date, supplied by
    the caller so this function stays clock-free.

    The decision is positive only when every hard rule holds. A listed
    expiration after the earnings date is necessary but NOT sufficient: an
    estimated date, a stale proof, an unknown confirmation or a past date each
    block it on their own.
    """
    earnings = dict(earnings or {})
    status = earnings.get("status") or STATUS_UNKNOWN
    flags = list(earnings.get("quality_flags") or [])
    confirmation = earnings.get("date_confirmation") or CONFIRMATION_UNKNOWN
    session = earnings.get("release_session") or SESSION_UNKNOWN
    roh_datum = earnings.get("next_earnings_date")

    termin = None
    if isinstance(roh_datum, str) and roh_datum.strip():
        try:
            termin = date.fromisoformat(roh_datum.strip())
        except ValueError:
            termin = None
            status = STATUS_INVALID
            if "earnings_date_unparseable" not in flags:
                flags.append("earnings_date_unparseable")

    if expirations is None:
        verfaelle, ungueltig = [], []
        verfaelle_bekannt = False
    else:
        verfaelle, ungueltig = _clean_expirations(expirations)
        verfaelle_bekannt = True

    gruende = []
    if termin is None:
        if status == STATUS_UNAVAILABLE:
            gruende.append("earnings_source_unavailable")
        elif status == STATUS_INVALID:
            gruende.append("earnings_data_invalid")
        elif FLAG_SOURCE_NOT_CONFIGURED in flags:
            gruende.append("earnings_source_not_configured")
        else:
            gruende.append("earnings_date_unknown")
    else:
        if FLAG_SOURCE_NOT_CONFIGURED in flags:
            gruende.append("earnings_source_not_configured")
        # Fail closed: a date alone proves nothing. Only a fresh proof may
        # authorise; every other state - and every value the contract does not
        # define - blocks. Before this, a record marked unknown/invalid or one
        # with no status at all still returned eligible=True with a selected
        # expiry, because only "stale" was named here.
        if status == STATUS_FRESH:
            pass
        elif status == STATUS_STALE:
            gruende.append("earnings_data_stale")
        elif status == STATUS_INVALID:
            gruende.append("earnings_data_invalid")
        elif status == STATUS_UNAVAILABLE:
            gruende.append("earnings_source_unavailable")
        elif status == STATUS_UNKNOWN:
            gruende.append("earnings_status_unknown")
        else:
            gruende.append("earnings_status_unrecognised")
        if today is not None and termin < today:
            gruende.append("earnings_date_in_past")
        if confirmation == CONFIRMATION_UNKNOWN:
            gruende.append("date_confirmation_unknown")

    ausstieg = last_trading_day_before(termin) if termin is not None else None
    if termin is not None and ausstieg is None:
        gruende.append("exit_day_not_resolvable")
    if ausstieg is not None and today is not None and ausstieg < today:
        gruende.append("exit_day_in_past")

    nach_earnings, davor, ausgeschlossen = [], [], []
    for verfall in verfaelle:
        # Without an earnings date NOTHING can lie "after" it. Classifying the
        # expirations anyway produced a "selected" date for a ticker whose
        # earnings date is unknown - the one field a caller would act on.
        if termin is None:
            break
        if verfall > termin:
            nach_earnings.append(verfall)
        else:
            davor.append(verfall)
            ausgeschlossen.append({
                "date": verfall.isoformat(),
                "reason": "expiration_on_or_before_earnings",
            })

    ausgewaehlt = None
    if not verfaelle_bekannt:
        gruende.append("expirations_unavailable")
    elif not verfaelle:
        gruende.append("no_expirations_listed")
    elif termin is not None and not nach_earnings:
        gruende.append("no_listed_expiration_after_earnings")
    elif nach_earnings:
        ausgewaehlt = nach_earnings[0]

    # Stable order, no duplicates: the list is rendered and asserted on.
    eindeutig = []
    for code in gruende:
        if code not in eindeutig:
            eindeutig.append(code)

    eligible = not eindeutig
    if eligible:
        entscheidung = "ok"
    elif any(code in UNKNOWN_REASONS for code in eindeutig):
        entscheidung = "unknown"
    else:
        entscheidung = "blocked"

    return {
        "schema_version": SCHEMA_VERSION,
        "earnings": earnings,
        "exit": {
            "planned_last_exit_date": ausstieg.isoformat() if ausstieg else None,
            "rule": EXIT_RULE,
            "is_trading_day": bool(ausstieg is not None and is_us_trading_day(ausstieg)),
            # Informational only: it never feeds ``eligible``. An exit day in the
            # past counts 0 and is still blocked by its own reason below.
            "remaining_trading_days": remaining_trading_days(today, ausstieg),
        },
        "expirations": {
            "listed": [v.isoformat() for v in verfaelle],
            "after_earnings": [v.isoformat() for v in nach_earnings],
            "on_or_before_earnings": [v.isoformat() for v in davor],
            "selected": ausgewaehlt.isoformat() if ausgewaehlt else None,
            "excluded": ausgeschlossen,
            "unparseable": ungueltig,
        },
        "decision": {
            "eligible": eligible,
            "status": entscheidung,
            "reasons": [_reason(code) for code in eindeutig],
            "notes": _notes(entscheidung, ausgewaehlt, termin, session, confirmation),
        },
        "as_of": today.isoformat() if isinstance(today, date) else None,
    }


def _notes(entscheidung, ausgewaehlt, termin, session, confirmation):
    """Plain-language additions; empty when there is nothing to add."""
    texte = []
    if ausgewaehlt is not None:
        texte.append(
            f"Erster gelisteter Verfall nach dem Earnings-Termin: {ausgewaehlt.isoformat()}."
        )
    if termin is not None and session == SESSION_UNKNOWN:
        texte.append("Veröffentlichungszeit unbekannt - der Ausstiegstag ist deshalb der letzte Handelstag davor.")
    if termin is not None and confirmation == CONFIRMATION_ESTIMATED:
        texte.append("Der Termin ist eine Schätzung der Quelle, keine bestätigte Ankündigung.")
    if entscheidung == "ok":
        texte.append("Der Verfall nach Earnings ist eine notwendige, keine hinreichende Bedingung für einen Trade.")
    return texte
