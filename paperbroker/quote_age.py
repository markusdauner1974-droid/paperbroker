"""One rule for "how old is this quote?" - and the two modes that read it.

Why this module exists
======================
Three surfaces answered the same question three different ways:

* ``screener.filter_stale`` computed an age with
  ``arrow.get(ts).to("UTC")`` and dropped the contract when it was too old.
* ``server._newest_quote_age_min`` computed the same age for the traffic light
  and the header.
* ``eva_quality.assess_source_quality`` and ``option_chain_api`` refused to
  derive any age at all, because the CBOE payload names no timezone
  (``SOURCE_TIMEZONE_UNKNOWN``, ``source_timestamp_utc: null``).

So one and the same feed stamp was simultaneously "3 minutes old" (filter) and
"age unknown" (quality gate). This module makes that one rule instead:
:func:`assess` returns state, zone and age together, and every surface reads
that answer - the single-ticker page, ``/api/screen``, ``/api/v1/option-chain``
and the multi-ticker earnings scan.

The assumption, named instead of hidden
=======================================
``arrow`` 0.10 assigns ``tzutc()`` to a naive string, so ``.to("UTC")`` on
``"2026-11-11 16:11:19"`` converts nothing - it *looks* like a normalisation
and is really an assumption that the value already is UTC. Measured on
2026-11-11: the CBOE envelope answered ``"2026-11-11 16:11:19"`` for a request
made at ``16:12:25 UTC`` and ``"2026-11-11 16:14:19"`` 75 seconds later, so the
stamp tracks UTC wall clock and is not a US local clock (a New York stamp would
have been four to five hours behind). That measurement is the only reason this
module may compute an age for the feed at all.

It never promotes the assumption to a fact: an unzoned stamp gets
``zone == "assumed_utc"``, ``age_proven is False`` and
``source_timestamp_utc is None``. Only a stamp that carries ``Z`` or an explicit
offset returns ``zone == "stated"`` and a UTC value. A date without a time is
not an age either - it is ``unreadable``, not midnight.

The two modes
=============
``MODE_FRESHNESS`` ("Aktualitatsfilter") is the default and unchanged: a quote
older than ``max_quote_age_min`` is dropped with its own reason, and a quote
whose age cannot be established is dropped as well (fail-closed).

``MODE_LAST_AVAILABLE`` ("Letzter verfugbarer Marktstand") keeps the same age
rule as a *statement* instead of a rejection: an old quote may be used for a
provisional preparation, every other filter of the pipeline stays active, and
the answer is marked provisional. What it does NOT relax: a stamp that cannot be
dated at all (missing, unreadable, in the future) is still dropped - that is not
"old data", that is data whose state is unknown. The mode says "older data may be
used", not "undated data may be used".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------- #
# modes
# ---------------------------------------------------------------------- #

#: The default: the age limit decides what enters the result.
MODE_FRESHNESS = "fresh"
#: Preparation outside trading hours: old data may be used, labelled provisional.
MODE_LAST_AVAILABLE = "last_available"
AGE_MODES = (MODE_FRESHNESS, MODE_LAST_AVAILABLE)
MODE_LABELS = {
    MODE_FRESHNESS: "Aktualitätsfilter",
    MODE_LAST_AVAILABLE: "Letzter verfügbarer Marktstand",
}

# ---------------------------------------------------------------------- #
# states
# ---------------------------------------------------------------------- #

STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_FUTURE = "future"
STATE_MISSING = "missing"
STATE_UNREADABLE = "unreadable"
#: No age rule was applied at all (``max_quote_age_min is None``).
STATE_UNCHECKED = "unchecked"

# ---------------------------------------------------------------------- #
# zones
# ---------------------------------------------------------------------- #

#: The value carried ``Z`` or an explicit offset: the source stated the zone.
ZONE_STATED = "stated"
#: The value carried no zone. Read as UTC under FEED_ZONE_ASSUMED - an
#: assumption, reported as one.
ZONE_ASSUMED_UTC = "assumed_utc"
#: Nothing to read (no stamp, or unreadable).
ZONE_NONE = "none"

#: The zone this project reads an unzoned feed stamp in. See the module
#: docstring for the measurement behind it. It is a single constant so the
#: assumption has exactly one home and can be changed in one place.
FEED_ZONE_ASSUMED = "UTC"
ZONE_ASSUMED_NOTE = (
    "Quelle ohne Zeitzonenangabe - der Wert wird als UTC gelesen (Annahme, "
    "keine Angabe der Quelle)"
)

# ---------------------------------------------------------------------- #
# reason texts (German - they go to the page and therefore must not lie)
# ---------------------------------------------------------------------- #

REASON_MISSING = "Aktualität unbekannt (kein Zeitstempel)"
REASON_UNREADABLE = "Aktualität unbekannt (Zeitstempel unlesbar)"
REASON_CLOCK = "Aktualität unbekannt (Uhrfehler)"

#: Clock skew is seconds, not minutes. More than a minute in the FUTURE is a
#: broken feed, not a fast clock - a plain ``age > limit`` check can never see
#: it, because a future stamp makes the age negative. Shared with the freshness
#: lamp so both sides read one rule instead of two.
FUTURE_TOLERANCE_S = 60

# ``YYYY-MM-DD`` + a TIME + an optional zone. A bare date is rejected: it has no
# age, and reading it as midnight would invent one.
_STAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.(?P<frac>\d{1,6}))?)?"
    r"(?P<zone>Z|[+-]\d{2}:?\d{2})?$"
)


class StampError(ValueError):
    """The value is not a timestamp this project is willing to date."""


def _utc_offset(text):
    """``Z`` / ``+02:00`` / ``-0430`` -> a tzinfo. Raises StampError."""
    if text == "Z":
        return UTC
    rest = text[1:].replace(":", "")
    stunden, minuten = int(rest[:2]), int(rest[2:])
    if stunden > 23 or minuten > 59:            # not an offset any clock has
        raise StampError(f"invalid utc offset: {text!r}")
    versatz = timedelta(hours=stunden, minutes=minuten)
    return timezone(-versatz if text[0] == "-" else versatz)


def parse_source_stamp(value):
    """``(moment_in_utc, zone)`` for a quote timestamp. Raises :class:`StampError`.

    ``zone`` is :data:`ZONE_STATED` when the value carries ``Z`` or an explicit
    offset, and :data:`ZONE_ASSUMED_UTC` when it carries none. There is no third
    path: a value that is not a date plus a time is refused instead of guessed.
    """
    if value is None:
        raise StampError("no stamp")
    text = str(value).strip()
    if not text:
        raise StampError("empty stamp")
    treffer = _STAMP_RE.match(text)
    if treffer is None:
        raise StampError(text)
    try:
        moment = datetime(
            int(treffer["date"][0:4]), int(treffer["date"][5:7]), int(treffer["date"][8:10]),
            int(treffer["hour"]), int(treffer["minute"]),
            int(treffer["second"] or 0),
            int((treffer["frac"] or "0").ljust(6, "0")),
        )
    except ValueError as exc:                    # 2026-02-30, hour 25, ...
        raise StampError(f"{text}: {exc}") from exc
    zone_text = treffer["zone"]
    if zone_text is None:
        return moment.replace(tzinfo=UTC), ZONE_ASSUMED_UTC
    return moment.replace(tzinfo=_utc_offset(zone_text)).astimezone(UTC), ZONE_STATED


def clock_utc(now_fn=None):
    """The check clock as an aware UTC datetime.

    A clock without a zone is read as UTC - the same convention the stamps use,
    and the convention every existing test and caller already relies on. It is
    deliberately NOT "local time": a local clock would silently shift every age
    by the server's offset. Anything that is neither a datetime nor an arrow is
    a caller error and raises, so the caller can name it as a clock failure
    instead of an unreadable stamp (B-17).
    """
    if now_fn is None:
        return datetime.now(UTC)
    wert = now_fn()
    if isinstance(wert, datetime):
        moment = wert
    elif isinstance(getattr(wert, "datetime", None), datetime):     # arrow.Arrow
        moment = wert.datetime
    else:
        raise TypeError(f"unsupported clock value: {type(wert).__name__}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class QuoteAge:
    """What one quote stamp says: state, zone, age - always all three.

    ``age_minutes`` may be set while ``zone`` is :data:`ZONE_ASSUMED_UTC`: then it
    is a number computed under the documented assumption, not a measurement of
    the source. ``age_proven`` is the only thing a caller may present as fact.
    """

    state: str
    zone: str
    age_minutes: float | None
    source_timestamp_raw: str | None
    source_timestamp_utc: str | None
    reason: str | None

    @property
    def age_proven(self):
        """True only when the SOURCE stated a zone - an assumption is not proof."""
        return self.zone == ZONE_STATED and self.age_minutes is not None

    @property
    def usable_for_last_available(self):
        """Modes may use this quote: it is dated, merely old (or fresh)."""
        return self.state in (STATE_FRESH, STATE_STALE, STATE_UNCHECKED)

    def as_dict(self):
        return {
            "state": self.state,
            "zone": self.zone,
            "age_minutes": None if self.age_minutes is None else round(self.age_minutes, 1),
            "age_proven": self.age_proven,
            "source_timestamp_raw": self.source_timestamp_raw,
            "source_timestamp_utc": self.source_timestamp_utc,
            "reason": self.reason,
        }


def assess(stamp, clock, max_age_min):
    """Classify one quote timestamp against ``clock`` (an aware UTC datetime).

    The order of the checks is the order of the precedence: an unreadable value
    cannot be stale, and a future value cannot be old. ``max_age_min=None``
    means no age rule at all -> :data:`STATE_UNCHECKED`.
    """
    raw = None if stamp is None else str(stamp)
    if stamp is None or not str(stamp).strip():
        return QuoteAge(STATE_MISSING, ZONE_NONE, None, raw, None, REASON_MISSING)
    try:
        moment, zone = parse_source_stamp(stamp)
    except StampError:
        return QuoteAge(STATE_UNREADABLE, ZONE_NONE, None, raw, None, REASON_UNREADABLE)
    age_s = (clock - moment).total_seconds()
    age_min = age_s / 60.0
    # A UTC value is reported only when the source stated the zone. Under the
    # assumption the raw value stays the only honest rendition of the stamp.
    utc_text = moment.strftime("%Y-%m-%dT%H:%M:%SZ") if zone == ZONE_STATED else None
    if max_age_min is None:
        return QuoteAge(STATE_UNCHECKED, zone, age_min, raw, utc_text, None)
    if age_s < -FUTURE_TOLERANCE_S:
        # the wording keeps the rounding of the stale branch below: the existing
        # edge (30 min + 1 s reads "veraltet: 30 Min.") must not move
        return QuoteAge(STATE_FUTURE, zone, age_min, raw, utc_text,
                        f"Zeitstempel in der Zukunft (+{(-age_s) / 60:.0f} Min.)")
    if age_s > max_age_min * 60:
        return QuoteAge(STATE_STALE, zone, age_min, raw, utc_text,
                        f"veraltet: {age_min:.0f} Min.")
    return QuoteAge(STATE_FRESH, zone, age_min, raw, utc_text, None)


def stamp_of(quote):
    """The feed stamp of an OptionQuote (or a ScreenResult wrapping one)."""
    q = getattr(quote, "quote", quote)
    return getattr(q, "quote_timestamp", None)


def assess_chain(quotes, clock, max_age_min):
    """One :class:`QuoteAge` per quote in the chain, in chain order."""
    return [assess(stamp_of(q), clock, max_age_min) for q in quotes or ()]


def newest(ages):
    """The YOUNGEST dated stamp, or None when nothing could be dated.

    This is the value the header and the traffic light show: one chain, one feed
    stamp, so the youngest stamp is the state of that chain. Undated quotes
    contribute nothing (they are never evidence of freshness).
    """
    jung = None
    for age in ages or ():
        if age.age_minutes is None:
            continue
        if jung is None or age.age_minutes < jung.age_minutes:
            jung = age
    return jung


def freshness_verdict(ages, max_age_min):
    """``fresh`` / ``partial`` / ``stale`` / ``unchecked`` / ``unknown``.

    Exactly the verdict the earnings scan has always taken from
    ``filter_stale``'s two lists, now from the same assessments: no rule ->
    ``unchecked``; nothing to read -> ``unknown``; nothing fresh -> ``stale``;
    a mixed set -> ``partial``; else ``fresh``. Undated quotes count as "not
    fresh", which is what the fail-closed filter did before - the precise count
    of undated contracts is carried by :func:`summarise`, not by this word.
    """
    if max_age_min is None:
        return "unchecked"
    ages = list(ages or ())
    if not ages:
        return "unknown"
    if not any(a.state == STATE_FRESH for a in ages):
        return "stale"
    if any(a.state != STATE_FRESH for a in ages):
        return "partial"
    return "fresh"


# ---------------------------------------------------------------------- #
# market state (existing exchange calendar, no second holiday table)
# ---------------------------------------------------------------------- #

MARKET_OPEN = "open"
MARKET_CLOSED = "closed"
MARKET_UNKNOWN = "unknown"

_SESSION_OPEN = (9, 30)
_SESSION_CLOSE = (16, 0)
_MARKET_REASON_TEXT = {
    "weekend": "Wochenende",
    "holiday": "Börsenfeiertag",
    "before_open": "vor der Eröffnung",
    "after_close": "nach dem Schluss",
}


def market_state(clock):
    """Is the US cash session open at ``clock``? From the project's own calendar.

    The day comes from ``earnings_calendar.is_us_trading_day`` - the calendar
    that already exists for the earnings window - so weekends, NYSE holidays and
    the special closures have one definition in this project. Only the session
    hours (09:30-16:00 New York) are added here; they are the documented trading
    hours, not a new holiday rule. Nothing raises: an unknown state is reported
    as unknown.
    """
    try:
        from .earnings_calendar import is_us_trading_day

        lokal = clock.astimezone(ZoneInfo("America/New_York"))
        tag = lokal.date()
        if lokal.weekday() >= 5:
            return {"state": MARKET_CLOSED, "reason": "weekend",
                    "exchange_day": tag.isoformat(), "exchange_time": lokal.strftime("%H:%M")}
        if not is_us_trading_day(tag):
            return {"state": MARKET_CLOSED, "reason": "holiday",
                    "exchange_day": tag.isoformat(), "exchange_time": lokal.strftime("%H:%M")}
        jetzt = (lokal.hour, lokal.minute)
        if jetzt < _SESSION_OPEN:
            return {"state": MARKET_CLOSED, "reason": "before_open",
                    "exchange_day": tag.isoformat(), "exchange_time": lokal.strftime("%H:%M")}
        if jetzt >= _SESSION_CLOSE:
            return {"state": MARKET_CLOSED, "reason": "after_close",
                    "exchange_day": tag.isoformat(), "exchange_time": lokal.strftime("%H:%M")}
        return {"state": MARKET_OPEN, "reason": None,
                "exchange_day": tag.isoformat(), "exchange_time": lokal.strftime("%H:%M")}
    except Exception:                       # noqa: BLE001 - a label must not cost the page
        return {"state": MARKET_UNKNOWN, "reason": None,
                "exchange_day": None, "exchange_time": None}


def market_reason_text(reason):
    """German wording for :func:`market_state`'s reason (None stays None)."""
    return _MARKET_REASON_TEXT.get(reason, reason)


# ---------------------------------------------------------------------- #
# the block the page and the API share
# ---------------------------------------------------------------------- #

PROVISIONAL_NOTE = (
    "Vorläufig: angezeigt wird der letzte verfügbare Marktstand, nicht ein "
    "aktueller Kurs. Keine Aussage über aktuelle Handelbarkeit."
)


def summarise(ages, *, mode, max_age_min, clock):
    """One status block for the page, ``/api/screen`` and the scan.

    Machine-readable on purpose: the raw stamp, the UTC value (only when the
    source stated a zone), the age with its ``age_proven`` flag, the fetch time,
    the counts per state, the market state and one German sentence. A consumer
    therefore never has to re-derive any of it - which is what keeps the four
    surfaces on the same rule.
    """
    ages = list(ages or ())
    zaehler = {
        "total": len(ages),
        STATE_FRESH: sum(1 for a in ages if a.state == STATE_FRESH),
        STATE_STALE: sum(1 for a in ages if a.state == STATE_STALE),
        STATE_FUTURE: sum(1 for a in ages if a.state == STATE_FUTURE),
        "undated": sum(1 for a in ages if a.state in (STATE_MISSING, STATE_UNREADABLE)),
        STATE_UNCHECKED: sum(1 for a in ages if a.state == STATE_UNCHECKED),
    }
    neueste = newest(ages)
    markt = market_state(clock)
    # Only OLD or UNDATED data justifies the provisional label. A stamp in the
    # future is a broken feed, not an old market state - it is dropped in both
    # modes and must not be reported as "older than the limit" (Kody finding on
    # PR #77, 2026-11-11: the earlier "everything not fresh" rule did exactly
    # that for a chain whose only stamps lay in the future).
    unproven = zaehler[STATE_STALE] + zaehler["undated"] > 0
    provisional = bool(mode == MODE_LAST_AVAILABLE and unproven)
    if not provisional:
        note = None
    elif markt["state"] == MARKET_CLOSED:
        note = (f"Markt geschlossen ({market_reason_text(markt['reason'])}) - die Kurse "
                f"sind der letzte verfügbare Stand.")
    else:
        note = "Die Kurse sind älter als die Altersgrenze - angezeigt wird der letzte verfügbare Stand."
    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "provisional": provisional,
        "provisional_note": PROVISIONAL_NOTE if provisional else None,
        "note": note,
        # fetch time and source time are two different facts and are reported
        # as two different facts
        "fetched_at_utc": clock.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_timestamp_raw": None if neueste is None else neueste.source_timestamp_raw,
        "source_timestamp_utc": None if neueste is None else neueste.source_timestamp_utc,
        "source_timestamp_zone": None if neueste is None else neueste.zone,
        "source_timestamp_zone_note":
            None if neueste is None or neueste.zone != ZONE_ASSUMED_UTC else ZONE_ASSUMED_NOTE,
        "source_age_minutes": None if neueste is None or neueste.age_minutes is None
        else round(neueste.age_minutes, 1),
        "age_proven": bool(neueste is not None and neueste.age_proven),
        "max_age_min": max_age_min,
        "counts": zaehler,
        "market": markt,
    }
