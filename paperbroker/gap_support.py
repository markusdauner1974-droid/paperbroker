"""Gap helpers shared by the scan, the spreads list and the spread detail.

Moved 1:1 out of ``server.create_app``. All six are used by more than one gap
route, so they live next to each other instead of inside a route module.

Two of them read the factory state and take the context as a parameter:
``_gap_day(ctx)`` (the clock) and ``_gap_calendar(ctx, day, reaction)`` (the
earnings adapter). ``app.logger`` became ``current_app.logger``.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

import arrow
from flask import current_app, jsonify, request

from . import gap_funnel
from .assets import calendar_days_between
from .earnings_calendar import is_us_trading_day
from .earnings_day_calendar import DAY_PREV, DAY_TODAY
from .gap_scan import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MIN_PRICE,
    DEFAULT_MIN_TURNOVER_USD,
    DEFAULT_THRESHOLD_PCT,
    fetch_gaps,
    reaction_day_rows,
    select_candidates,
)
from .reachability import HISTORY_BARS
from .texts import t
from .view_support import _DATE_RE, _calendar_failure, _scan_limit, _us_today

GAP_SCAN_MODE = "post_earnings_gap"
_GAP_SCAN_PARAMETERS = ("gap_threshold_pct", "min_turnover_usd", "max_candidates",
                        "min_price")
_GAP_MAX_CANDIDATES_CEILING = 500
_GAP_MAX_PRICE_CEILING = 1_000_000.0
_GAP_DEADLINE_SECONDS = 120
_GAP_PREVIOUS_LOOKBACK_DAYS = 10
_GAP_PUBLICATION_GRACE_DAYS = 7
_US_CLOSE_HOUR_ET = 16


_GAP_SPREADS_PARAMETERS = ("max_required_move_pct",)
def _schranke_text(wert):
    """The bound as text, without rounding it.

    ``f"{wert:.0f}"`` was wrong here in two ways: 0.4 came out as "0" and 2.5 as
    "2" (half-to-even), while the body of the same answer carried 0.4 and 2.5. The
    note must name the number the reader sees, so it is rendered with no rounding
    at all - and a whole number simply loses its ".0".
    """
    text = repr(float(wert))
    return text[:-2] if text.endswith(".0") else text


def gap_spreads_notes(language=None, max_move_pct=None):
    """The methodology notes of the gap-spread list, in ``language``.

    A function and not a module constant: the sentences may not be frozen at
    import time, because this list is one of the page-facing answers that will
    follow the language switch.

    ``language=None`` means the default. That is what the route passes TODAY -
    there is no request language yet, ``resolve_language`` has no production
    caller until the switch lands. The parameter exists so that the switch is one
    line at the call site and not an edit to seven sentences; it is not a promise
    that the language already varies.

    ``max_move_pct`` is the bound the answer ACTUALLY applied, not the default:
    the route accepts it per request and writes it twice into the same body, so a
    note naming the default would contradict the field next to it. ``None`` means
    the default, which is what a request without the parameter gets.

    The two numbers travel as text variables - the German formatting stays where
    it was (``f"{x:.0f}"``), so a language switch cannot silently turn 0.5 into
    0,5.
    """
    schranke = (gap_funnel.DEFAULT_MAX_REQUIRED_MOVE_PCT
                if max_move_pct is None else max_move_pct)
    return [
        t("gap.notes.data_mode", language=language),
        t("gap.notes.rr", language=language),
        t("gap.notes.expiry", language=language),
        t("gap.notes.strike_pair", language=language),
        t("gap.notes.dte", language=language),
        t("gap.notes.required_move", language=language, max_move=_schranke_text(schranke)),
        t("gap.notes.hit_rate", language=language, bars=str(HISTORY_BARS)),
    ]
_GAP_SPREADS_SCHEMA_VERSION = "1.0"
_SPREAD_DETAIL_CODES = {
    gap_funnel.REASON_NO_NEXT_EARNINGS: "no_next_earnings",
    gap_funnel.REASON_NO_EXPIRY: "no_expiry_after_earnings",
    gap_funnel.REASON_NO_SPREAD: "no_spread_ge_rr",
    gap_funnel.REASON_NOT_OPTIONABLE: "chain_unavailable",
    # The spread exists but is too far away; the list would not show it
    # either, and the detail says the same thing with the same code.
    gap_funnel.REASON_MOVE_TOO_FAR: "move_too_far",
}
_STRUCTURE_PARAMS = ("expiration", "long", "short")
_SPREAD_DETAIL_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z0-9]{1,3})?$")


def _gap_scan_error(status, code, message=None, *, source_code=None, source_status=None):
    """Flat JSON error of this route: ``{"error": <code>}`` plus what is known.

    Flat on purpose and different from ``_earnings_error``: the agreed
    contract of this endpoint names the code in ``error`` itself, so a
    client switches on ONE field instead of unpacking ``error.code``. The
    upstream code and status of a calendar that BLOCKED the request travel
    next to it, for the same reason as in ``_calendar_failure``: the
    message may not say "did not answer" when the source answered 403.
    """
    koerper = {"error": code}
    if message:
        koerper["message"] = message
    if source_code:
        koerper["source_code"] = source_code
    if source_status is not None:
        koerper["http_status"] = source_status
    return jsonify(koerper), status



def _gap_previous_trading_day(reaction):
    """The last NYSE session before ``reaction``, or ``None``.

    A calendar day is NOT a session: Nasdaq answers a Sunday with an empty
    rows list, so ``reaction - 1 day`` would silently drop every Friday
    report released after the close - and the same for a holiday. The
    calendar decides here, not ``weekday() >= 5``: Thanksgiving is a closed
    day with a normal weekday.

    ``None`` means no session was found within the lookback window; the
    caller answers 503 instead of falling back to the calendar day, because
    a wrong "previous day" looks exactly like a quiet day.
    """
    kandidat = reaction - timedelta(days=1)
    for _schritt in range(_GAP_PREVIOUS_LOOKBACK_DAYS):
        if is_us_trading_day(kandidat):
            return kandidat
        kandidat -= timedelta(days=1)
    return None



def _gap_day(ctx):
    """``(day, reaction, error)`` of the request - one validation, three routes.

    The day is validated before it touches the calendar: a string that is
    no date would otherwise be asked for and come back as "no rows", i.e.
    as a quiet day instead of as a bad request.

    A day the market was CLOSED on is refused the same way, through the
    trading calendar rather than the weekday: the calendar answers such a
    day with ``rows: null``, so every symbol would end in
    ``no_reaction_bar`` and the answer would be a 200 that reads as a quiet
    day. A holiday is a weekday, which is why ``is_us_trading_day`` decides
    here and not ``date.weekday()``.

    A day that has not CLOSED yet is refused the same way. The scan measures
    a finished reaction session on both sides, and a day that is still
    running (or in the future) has no close to measure: answering 200 with
    ``selected: 0`` and every symbol in ``failures`` would be a quiet day
    nobody can tell apart from a real one. The test is the CLOSE, not the
    date: today is allowed once the New York clock is past
    ``_US_CLOSE_HOUR_ET`` - the evening scan of the day that just closed is
    the normal use, and a date-only check would forbid exactly that.
    """
    day = (request.args.get("day") or "").strip()
    # Exactly the agreed shape: the code alone, nothing to explain.
    if not day or not _DATE_RE.match(day):
        return None, None, _gap_scan_error(400, "invalid_day")
    try:
        reaction = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return None, None, _gap_scan_error(400, "invalid_day")

    # The calendar's own "today" (New York), never the UTC date: after
    # 19:00 ET a UTC date is already the next day.
    heute = _us_today(ctx.now_fn)
    if reaction > heute:
        return None, None, _gap_scan_error(
            400, "invalid_day", "the reaction day lies in the future"
        )
    # A closed market has no session to measure. Checked BEFORE the close
    # check below, because "Saturday, still open" is the wrong sentence for
    # a day that never opened at all.
    if not is_us_trading_day(reaction):
        return None, None, _gap_scan_error(
            400, "invalid_day", "the reaction day is no US trading day"
        )
    if reaction == heute:
        # Same date is not the same session: before the close the day has
        # no close to measure, from 16:00 ET on it does.
        jetzt_et = arrow.get(ctx.now_fn()).to("America/New_York")
        if jetzt_et.hour < _US_CLOSE_HOUR_ET:
            return None, None, _gap_scan_error(
                400, "invalid_day", "the reaction session has not closed yet"
            )
    return day, reaction, None



def _gap_calendar(ctx, day, reaction):
    """Both sessions of one gap day: ``(by_day, previous, error)``.

    The three gap routes (scan, spreads, one spread's detail) ask the same
    two questions of the calendar - the reaction day and its previous
    TRADING day - and every one of them has to fail the same way: a 503 when
    the calendar is not configured or not readable, never a quiet day built
    from half a candidate set. That rule is written here once.
    """
    previous = _gap_previous_trading_day(reaction)
    if previous is None:
        # No session inside the window is no answer: the calendar is not
        # believable, and a calendar-day fallback would hide Friday's rows.
        current_app.logger.error("gap calendar: no trading day before %s", day)
        return {}, None, _gap_scan_error(
            503, "calendar_unavailable",
            f"no US trading day within {_GAP_PREVIOUS_LOOKBACK_DAYS} days "
            f"before {day} - the trading calendar is unusable",
        )
    tage = {DAY_PREV: previous.isoformat(), DAY_TODAY: day}

    try:
        antwort = ctx.earnings_adapter.get_calendar_days(dict(tage)) or {}
    except Exception:                                    # noqa: BLE001 - classified here
        current_app.logger.exception("gap calendar: calendar failed for %s and %s",
                             tage[DAY_PREV], day)
        return {}, None, _gap_scan_error(
            503, "calendar_unavailable", "the earnings calendar could not be read"
        )
    roh = antwort.get("days") if isinstance(antwort, dict) else None
    roh = roh if isinstance(roh, dict) else {}
    gesamt = str((antwort.get("state") if isinstance(antwort, dict) else None) or "")
    if gesamt == "not_configured":
        # Setup fact, not an outage - and it is the same answer as no
        # calendar at all: there is no candidate set to build.
        return {}, None, _gap_scan_error(
            503, "calendar_unavailable",
            "no earnings source is configured - there is nothing to scan",
            source_code="not_configured",
        )

    # The reaction day is checked FIRST: it is the subject of the request,
    # so its failure is the one worth naming. Both days must be readable -
    # half a candidate set would look like a quiet day, not like a hole.
    gelesen = {}
    for schluessel in (DAY_TODAY, DAY_PREV):
        iso = tage[schluessel]
        eintrag = roh.get(iso)
        if not isinstance(eintrag, dict):
            # No entry for a requested day is no answer, not an empty day.
            quell_code, quell_status, message = _calendar_failure(
                antwort.get("error") if isinstance(antwort, dict) else None, iso
            )
            return {}, None, _gap_scan_error(
                503, "calendar_unavailable", message,
                source_code=quell_code, source_status=quell_status,
            )
        zustand = str(eintrag.get("state") or gesamt or "unavailable")
        zeilen = eintrag.get("rows") or []
        if zustand == "not_configured":
            return {}, None, _gap_scan_error(
                503, "calendar_unavailable",
                "no earnings source is configured - there is nothing to scan",
                source_code="not_configured",
            )
        if zustand == "unavailable" or not isinstance(zeilen, list):
            quell_code, quell_status, message = _calendar_failure(eintrag.get("error"), iso)
            return {}, None, _gap_scan_error(
                503, "calendar_unavailable", message,
                source_code=quell_code, source_status=quell_status,
            )
        gelesen[schluessel] = {"state": zustand, "rows": zeilen}
    return gelesen, previous, None



def _gap_scan_args(ctx, extra=()):
    """``day`` and the four numbers of one gap scan, or a ready JSON error.

    The day itself is validated by ``_gap_day`` (400 for everything that is
    not a finished US session); this function adds the four limits of the
    scan, each with its own error code. ``extra`` names the parameters a
    DERIVED route accepts on top (``/api/v1/gap-spreads`` adds the move cap)
    - without it a parameter the route cannot honour would be accepted
    silently, which is the same lie as ignoring it.
    """
    erlaubt = {"day"} | set(_GAP_SCAN_PARAMETERS) | set(extra)
    unbekannt = sorted(set(request.args) - erlaubt)
    if unbekannt:
        return None, _gap_scan_error(
            400, "unknown_parameter",
            f"unknown parameter(s): {', '.join(unbekannt)}. Allowed: day, "
            f"{', '.join(tuple(_GAP_SCAN_PARAMETERS) + tuple(extra))}",
        )
    doppelt = sorted(name for name in erlaubt if len(request.args.getlist(name)) > 1)
    if doppelt:
        return None, _gap_scan_error(
            400, "duplicate_parameter", f"duplicate parameter(s): {', '.join(doppelt)}"
        )

    day, reaction, fehler = _gap_day(ctx)
    if fehler is not None:
        return None, fehler

    limits = {}
    for name, default, ceiling, integer, minimum, code in (
        ("gap_threshold_pct", DEFAULT_THRESHOLD_PCT, 100.0, False, 0.0, "invalid_limit"),
        ("min_turnover_usd", DEFAULT_MIN_TURNOVER_USD, 1e12, False, 0.0, "invalid_limit"),
        ("max_candidates", DEFAULT_MAX_CANDIDATES, _GAP_MAX_CANDIDATES_CEILING, True, 1,
         "invalid_limit"),
        # The price hurdle has its OWN code: "invalid_limit" would send the
        # caller looking at the threshold, turnover or cap instead.
        ("min_price", DEFAULT_MIN_PRICE, _GAP_MAX_PRICE_CEILING, False, 0.0,
         "invalid_min_price"),
    ):
        wert, fehler = _scan_limit(name, request.args.get(name), default, ceiling,
                                   integer=integer, minimum=minimum)
        if fehler is not None:
            return None, _gap_scan_error(400, code, fehler)
        limits[name] = wert

    return {"day": day, "reaction": reaction, **limits}, None



def _required_move_cap(raw):
    """``max_required_move_pct`` of one spread request, or a readable reason.

    The bound is a percentage of the close, so it has to be positive and no
    larger than a whole move of the underlying (100 %). 0 is refused on
    purpose: it would exclude every spread that needs any move at all, which
    is not a filter but an emptied answer.
    """
    text = (raw or "").strip()
    if not text:
        return gap_funnel.DEFAULT_MAX_REQUIRED_MOVE_PCT, None
    try:
        wert = float(text)
    except ValueError:
        return None, f"max_required_move_pct: '{text}' is not a number"
    if not math.isfinite(wert):
        return None, f"max_required_move_pct: '{text}' is not a finite number"
    if wert <= 0 or wert > 100:
        return None, f"max_required_move_pct: must be > 0 and <= 100 (is: {text})"
    return wert, None

