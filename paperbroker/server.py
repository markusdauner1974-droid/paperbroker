# ---------------------------------------------------------------------- #
# Web display for the options screener (Phase 5).                         #
#                                                                          #
# Design (debate panel, 2026-10-07):                                       #
# - SSR with Jinja2 + exactly one JSON endpoint, NO htmx, no frontend      #
#   build chain (single-user LAN on a QNAP; maintainability first).        #
# - GET /                SSR page: ticker/expiration form + results table  #
# - GET /api/screen      JSON (stable interface for Phase 6 Telegram bot)  #
# - GET /healthz         liveness ONLY (no CBOE/xang calls - a coupled     #
#                        healthcheck turns unhealthy when an external      #
#                        service is down)                                  #
# - ticker + expiration are validated BEFORE they touch the CBOE URL     #
#   (a raw ticker would allow path injection against cdn.cboe.com).      #
# ---------------------------------------------------------------------- #

import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlencode

import arrow
from flask import Flask, abort, jsonify, render_template, request

from . import exclusions, gap_funnel, quote_age, spread_detail
from .adapters.earnings.NasdaqEarningsAdapter import NasdaqEarningsAdapter, NullEarningsAdapter
from .adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter
from .assets import calendar_days_between
from .earnings_calendar import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_ESTIMATED,
    SESSION_AFTER,
    SESSION_BEFORE,
    combine_earnings_and_expirations,
    is_us_trading_day,
    last_trading_day_before,
    next_trading_day_after,
)
from .earnings_day_calendar import (
    DAY_NEXT,
    DAY_PREV,
    DAY_TODAY,
    build_calendar,
)
from .earnings_scan import (
    CONCURRENCY_CEILING,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_EXPIRATIONS,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_SYMBOLS,
    MAX_EXPIRATIONS_CEILING,
    MAX_SECONDS_CEILING,
    MAX_SYMBOLS_CEILING,
    ScanLimits,
    scan_day,
)
from .eva_quality import assess_source_quality, snapshot_identity
from .exclusions import LEVEL_CONTRACT, LEVEL_EXPIRY, LEVEL_PAIR, LEVEL_SYMBOL
from .gap_scan import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MIN_PRICE,
    DEFAULT_MIN_TURNOVER_USD,
    DEFAULT_THRESHOLD_PCT,
    BoundedCache,
    NasdaqHistoryAdapter,
    fetch_gaps,
    reaction_day_rows,
    select_candidates,
)

# The numbers of a run have ONE definition (``gap_scan._counts``), so this route
# cannot drift from ``scan_gaps``: it imports the helper instead of re-deriving
# "selected" or "turnover_passed" from the rows a second time.
from .gap_scan import _counts as gap_run_counts
from .gap_support import (
    _GAP_DEADLINE_SECONDS,
    _gap_calendar,
    _gap_day,
    _gap_previous_trading_day,
    _gap_scan_args,
    _gap_scan_error,
    _required_move_cap,
)
from .next_earnings import NextEarningsAdapter
from .PaperBroker import PaperBroker
from .quote_age import AGE_MODES, MODE_FRESHNESS, MODE_LAST_AVAILABLE
from .reachability import HISTORY_BARS
from .routes import chain as chain_routes
from .routes import earnings as earnings_routes
from .routes import gap_scan as gap_scan_routes
from .routes import gap_spreads as gap_spreads_routes
from .routes import health as health_routes
from .routes.context import CONTEXT_KEY, FactoryContext
from .screener import (
    DEFAULT_STRATEGY,
    FUTURE_TOLERANCE_S,
    LEGACY_OPTION_TYPES,
    PAIR_SEARCH_LABELS,
    PAIR_SEARCH_MODES,
    STRATEGIES,
    STRATEGY_IDS,
    OptionScreener,
    ScreenerCriteria,
    TrendSignal,
    _trend_label,
    pair_search_report,
    resolve_strategy,
    screen,
    strategy_option_type,
    vertical_spreads,
)
from .view_support import (
    _CRITERIA_BOOL_FIELDS,
    _CRITERIA_CHOICE_FIELDS,
    _CRITERIA_ECHO_FIELDS,
    _CRITERIA_FIELDS,
    _CRITERIA_FLOAT_FIELDS,
    _CRITERIA_INT_FIELDS,
    _DATE_RE,
    _FRESH_GREEN_MIN,
    _FRESH_YELLOW_MIN,
    _IGNORED_PARAMS,
    _REASON_NUM_RE,
    _SCAN_LIMIT_PARAMS,
    _SPREAD_TYPES,
    _TICKER_RE,
    _age_within_limit,
    _calendar_failure,
    _criteria_echo,
    _criteria_from_request,
    _data_state,
    _drop_reason_groups,
    _exclusion_blocks,
    _feed_stamp,
    _freshness_lamp,
    _in_dte_window,
    _lamp_state,
    _newest_quote_age_min,
    _option_symbols,
    _pair_search_block,
    _parse_args,
    _parse_criteria_value,
    _same_strike,
    _scan_limit,
    _selectable_dates,
    _strategy_error_body,
    _strategy_label,
    _strategy_selection,
    _us_today,
)

logger = logging.getLogger(__name__)

# fetch-age thresholds for the freshness lamp (design decision, not
# measurement): the CBOE delayed feed is ~15 min behind by nature, so the
# lamp measures data age; the feed delay itself is communicated as a
# static banner, not as a red state.

# The gap scan answers one shape of run and says so: the closing gap after an
# earnings release, measured on the CLOSE of the reaction session.

# UX-1 (Ausbau-Slice 1): the screener hands back one free-text reason per
# dropped contract ("veraltet: 257 Min.", "OI 7 < 500"). Counting the raw texts
# would give roughly one line per contract (measured: 48 distinct texts for
# 97 drops with the age guard off), so numbers are folded to a placeholder.
# It is a NUMBER TOKEN, not a single digit: folding digit by digit would
# split "7.41" and "10.2" into different groups although both are "spread
# too wide". A group whose raw texts are all identical keeps the raw text,
# so the measured value stays on the page ("veraltet: 257 Min.", not "veraltet: # Min.").


# ---------------------------------------------------------------------- #
# Ausbau-Slice 2 (A): operator-settable filter criteria.                  #
#                                                                          #
# A request value overrides the injected base only when it is present and  #
# not empty, so a request without parameters keeps whatever the app was    #
# built with (the picker tests inject their own criteria).                  #
# The switch ignore_quote_age=1 is the ONLY way to disable the age guard:   #
# 0 is a valid, sharp bound ("everything older than 0 s"), not "off".       #
# ---------------------------------------------------------------------- #
# Slice I: every field ScreenerCriteria honours is settable from the form.
# Three input shapes share one parser: numbers (int/float), a switch with an
# explicit "unset" state (bool), and a choice of names (iv_pref). An empty
# field keeps the dataclass default - that is why "unset" must not be spelled
# as 0: min_bid_size=0 demands a size >= 0 and drops every quote with size 0.


def scan_view_data():
    """What the earnings-overview panel needs to run the day scan from the page.

    One source for both sides of the same contract:

    * ``strategies`` - the ids and the short column headers. The full labels
      name both legs ("Bull Call Spread (Debit)") and do not fit above a
      two-digit count, so the header prints the part before " Spread".
    * ``fields`` - the form fields the browser may copy into the scan request,
      built from the criteria table the route parses. A new filter therefore
      reaches the scan automatically; the alternative (a second list in the
      JavaScript) would silently drop it and the scan would then answer hits
      for filters the page did not ask for. ``strategy`` and the age switch are
      not criteria fields but ARE part of the request, so they are named here.
    * ``api`` - the route, spelled once.

    ``ticker``, ``expiration`` and ``sort`` are deliberately absent: they belong
    to the single-ticker view and the route refuses them as unknown parameters.
    """
    return {
        "api": "/api/v1/earnings-scan",
        "strategies": [
            {
                "id": sid,
                "label": STRATEGIES[sid]["label"],
                "short": STRATEGIES[sid]["label"].split(" Spread")[0],
            }
            for sid in STRATEGY_IDS
        ],
        "fields": list(_CRITERIA_FIELDS) + ["ignore_quote_age", "strategy"],
    }










_SESSION_TEXT = {SESSION_BEFORE: "vorbörslich", SESSION_AFTER: "nachbörslich"}
_SESSION_UNKNOWN_TEXT = "Zeitpunkt unbekannt"
_CONFIRMATION_TEXT = {CONFIRMATION_CONFIRMED: "bestätigt", CONFIRMATION_ESTIMATED: "geschätzt"}


def _german_date(iso_text):
    """``2026-11-08`` -> ``08.11.2026``; an unusable value stays ``None``."""
    try:
        return datetime.strptime(str(iso_text), "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return None


def _german_stamp(iso_text):
    """``2026-11-08T06:00:00Z`` -> ``08.11.2026 06:00 UTC`` (source stamp, no guess)."""
    try:
        wert = datetime.strptime(str(iso_text), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return wert.strftime("%d.%m.%Y %H:%M UTC")


# German weekday abbreviations for the day tabs. ``%a`` would be locale
# dependent - the same reason parse_as_of spells its tables out.
_WEEKDAY_SHORT_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def _short_day(iso_text):
    """``2026-11-08`` -> ``So 08.11.``; an unusable value stays ``None``."""
    try:
        wert = datetime.strptime(str(iso_text), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return f"{_WEEKDAY_SHORT_DE[wert.weekday()]} {wert.strftime('%d.%m.')}"


def _calendar_day_label(iso_text, today_iso):
    """The tab text for a day: ``gestern · Do 12.11.`` / ``heute · Fr 13.11.``.

    Date-based on purpose. With trading-day tabs the group after a Friday is a
    Monday, so the word "morgen" would promise a day two nights away and
    "gestern" would name a Friday as yesterday on a Monday. The word is used
    only where it is literally true (the calendar neighbour); every other day
    names itself with weekday and date. ``None`` when the date is unusable -
    the caller then renders no date rather than a guessed one.
    """
    kurz = _short_day(iso_text)
    if kurz is None:
        return None
    try:
        heute = datetime.strptime(str(today_iso), "%Y-%m-%d").date()
        tag = datetime.strptime(str(iso_text), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return kurz
    versatz = (tag - heute).days
    wort = {-1: "gestern", 0: "heute", 1: "morgen"}.get(versatz)
    return f"{wort} · {kurz}" if wort else kurz




def _option_note(applied, hidden, stale):
    """One German sentence for the filter: a number, or honestly no filter.

    ``hidden`` is the block-wide count of titles the list removed. An
    unavailable list must not name a number: a "0" would read like a result.
    """
    if not applied:
        return "Options-Liste nicht abrufbar - ungefiltert"
    satz = (f"{hidden} Titel ohne gelistete Optionen ausgeblendet" if hidden
            else "kein Titel ohne gelistete Optionen ausgeblendet")
    if stale:
        satz += " (Options-Liste aus letztem Stand)"
    return satz


_FAILURE_REASONS = {
    "source_blocked": "Quelle blockt die Anfrage",
    "source_throttled": "Quelle drosselt die Anfragen",
    "source_unreachable": "Quelle nicht erreichbar",
    "invalid_payload": "Quelle liefert unbrauchbare Daten",
    "invalid_json": "Quelle liefert kein JSON",
    "recent_failure": "letzter Versuch gerade gescheitert",
}


def _failure_reason(error):
    """One German clause naming WHY a source query failed, or ``None``.

    A block is not a silent source: the source answered 403, and a note that
    says only "does not answer" would describe the wrong event. The code stays
    visible next to the wording so the log and the page agree.
    """
    if not isinstance(error, dict):
        return None
    code = str(error.get("code") or "").strip()
    if not code:
        return None
    text = _FAILURE_REASONS.get(code, f"Quelle meldet {code}")
    status = error.get("http_status")
    if status:
        text = f"{text} (HTTP {status})"
    return text




def _earnings_source_from_env():
    """Which earnings source create_app wires when none is injected.

    Default is OFF (fail-closed): deploying the code must not silently start
    calling a new external service. The compose stack sets
    PAPERBROKER_EARNINGS_SOURCE=nasdaq; tests and a bare `flask run` keep the
    Null adapter and say so on the page.
    """
    return (os.environ.get("PAPERBROKER_EARNINGS_SOURCE") or "").strip().lower()






def create_app(quote_adapter=None, screener=None, now_fn=None, earnings_adapter=None,
               gap_adapter=None, next_earnings_adapter=None):
    """now_fn: injectable clock for tests (default: real arrow.get).

    The screener's 30-min-stale guard compares quote_timestamp against
    now() - a hardcoded fixture timestamp goes stale in real time, so
    tests MUST pin the clock instead of freezing the fixtures.

    ``gap_adapter`` is the SECOND source: daily closes for the closing-gap
    scan (``GET /api/v1/gap-scan``). Separate from ``earnings_adapter`` on
    purpose - the calendar says WHO reacted, the history says HOW FAR, and the
    two fail for different reasons. ``None`` means the real Nasdaq history
    adapter, built ONCE here with a bounded cache: a past close never changes,
    so a second request for the same day must not load the whole day again.
    An injected ``gap_adapter`` wins over that default (tests, other sources).

    ``next_earnings_adapter`` is the THIRD source: the next report date per
    title for ``GET /api/v1/gap-spreads`` (Finnhub's calendar, see
    ``next_earnings.py`` - Nasdaq's per-symbol endpoint answers no date at all).
    ``None`` builds the real adapter once, with a 12 h file cache under
    ``/app/.cache``; an injected one wins (tests, another vendor). The key is
    read from ``FINNHUB_API_KEY`` at query time, never at import time, and a run
    without it answers ``no_next_earnings`` instead of guessing a date.
    """
    app = Flask(__name__, template_folder="templates")
    if now_fn is None:
        now_fn = arrow.get

    if quote_adapter is None:
        quote_adapter = CBOEQuoteAdapter()
    if screener is None:
        screener = OptionScreener(ScreenerCriteria())
    if gap_adapter is None:
        # One adapter for the lifetime of the app, one cache with a ceiling -
        # not a fresh, memoryless adapter per request.
        gap_adapter = NasdaqHistoryAdapter(cache=BoundedCache())
    if earnings_adapter is None:
        # Fail-closed default: no source configured -> UNKNOWN, never a guess.
        if _earnings_source_from_env() == "nasdaq":
            earnings_adapter = NasdaqEarningsAdapter(now_fn=now_fn)
        else:
            earnings_adapter = NullEarningsAdapter(now_fn=now_fn)
    if next_earnings_adapter is None:
        # One adapter (and one cache file) for the lifetime of the app: the
        # funnel asks up to six titles in parallel and must not re-query a
        # symbol it already knows.
        next_earnings_adapter = NextEarningsAdapter(now_fn=now_fn)
    broker = PaperBroker(quote_adapter=quote_adapter)

    # One context for the route modules: the state that used to live in the
    # closures of this factory. A route reads it instead of closing over it.
    app.extensions[CONTEXT_KEY] = FactoryContext(
        broker=broker,
        screener=screener,
        earnings_adapter=earnings_adapter,
        gap_adapter=gap_adapter,
        next_earnings_adapter=next_earnings_adapter,
        now_fn=now_fn,
        trend_for_screener=_trend_for_screener,
    )

    # JSON error contract for the API (CodeRabbit finding): abort() would
    # send Flask's HTML error page, which a JSON client cannot parse
    @app.errorhandler(400)
    def _bad_request(err):
        if request.path.startswith("/api/"):
            return jsonify(error=getattr(err, "description", "bad request")), 400
        return err

    health_routes.register(app)
    chain_routes.register(app)
    earnings_routes.register(app)
    gap_scan_routes.register(app)
    gap_spreads_routes.register(app)


    # Unfiltered, read-only option-chain view for Hermes. Deliberately NOT a
    # screener: no criteria object, no trend provider, no spread engine. The
    # route validates its arguments, fetches the raw snapshot through the
    # facade and hands it to a pure serializer.

    # ------------------------------------------------------------------ #
    # Earnings calendar (read-only, fail-closed).                        #
    #                                                                    #
    # Two routes, one contract: /api/v1/earnings answers "what does the   #
    # source say about this ticker", /api/v1/earnings-window adds the     #
    # expirations this chain really lists and the deterministic decision. #
    # Neither route places an order, and neither invents a date.          #
    # ------------------------------------------------------------------ #





    # ------------------------------------------------------------------ #
    # One scan across a whole earnings day (GET /api/v1/earnings-scan).   #
    #                                                                     #
    # Deliberately NOT wired into GET /: one chain request per title is    #
    # far too expensive for a page view, and the page's Top-25 cut is      #
    # exactly the limit this scan exists to lift. It therefore runs on an  #
    # explicit request only, behind a hard budget, and answers with its    #
    # own coverage report instead of pretending to be complete.           #
    # ------------------------------------------------------------------ #




    # ------------------------------------------------------------------ #
    # The closing gap of one reaction day (GET /api/v1/gap-scan).         #
    #                                                                     #
    # The scan above positions BEFORE the numbers. This one measures what #
    # the market did AFTER them: the close of the reaction session against #
    # the close before it. Two sources, two questions - the earnings      #
    # calendar says WHO released (its rows of the previous day and the    #
    # pre-market rows of the day), the daily closes say HOW FAR. Nothing  #
    # here prices an option, reads a chain or places an order.            #
    # ------------------------------------------------------------------ #
    # The spread route's own fifth number: an upper bound on the DISTANCE to
    # break-even. It is NOT a gap-scan parameter (that route knows no spread),
    # so it is allowed only where it means something.
    # The output cap is bounded like every other limit: 5000 titles in one
    # answer is not a bigger result, it is a response nobody reads. The
    # MEASUREMENT is never capped - every candidate is fetched and checked.
    # The price hurdle is a filter, not a universe: a "price" of a million USD
    # would empty every day without saying so. The ceiling keeps the number a
    # price, and it is validated like the other limits instead of trusted.
    # The whole run's budget, in seconds. Not a cap on the titles - every
    # candidate is still asked - but a request may not hold a worker for the
    # worst case (220 titles x 6 s / 8 workers = ~165 s). What did not finish
    # is named ``deadline_exceeded`` in ``failures``, never dropped.
    # How far back the previous TRADING day may lie: ten calendar days cover a
    # weekend plus the longest NYSE closure (a holiday and a broken bridge).
    # Beyond that the calendar is not believable and the answer is a 503.
    # How long the daily table may legitimately lag behind the calendar: a
    # session's closes are published late on that session's day, so a day within
    # this many calendar days can still be missing for that reason. Older days
    # are not "late" any more - the answer names the missing row instead of a
    # publication that will not come.
    # The regular US session closes at 16:00 in New York. A reaction day is only
    # measurable once its close exists, so today is allowed from this hour on -
    # refusing the whole day would make the evening scan of the day that just
    # closed impossible, which is the normal use.








    # ------------------------------------------------------------------ #
    # The gap candidates, priced as debit spreads (GET /api/v1/gap-spreads).#
    #                                                                     #
    # The route above answers WHO moved how far. This one answers what   #
    # that move would cost to trade: next report date (Finnhub), the     #
    # first listed expiry strictly after it, one chain request per        #
    # symbol, the best debit spread of the gap's direction - and NOTHING  #
    # for a title that cannot be priced, named with the step that stopped #
    # it. The gap stage is not re-implemented: it is the same three calls  #
    # (reaction_day_rows -> fetch_gaps -> select_candidates) the gap scan  #
    # makes, with the same defaults, so both answers describe one day.    #
    # ------------------------------------------------------------------ #
    # The version of THIS answer's shape. Bumped when a field changes
    # meaning, so a consumer can switch instead of guessing.


    # ------------------------------------------------------------------ #
    # ONE spread of that list, in full (GET /api/v1/gap-spreads/detail).  #
    #                                                                     #
    # The list answers WHICH spread a candidate trades and what it costs. #
    # This route answers what that ONE spread looks like: both legs with  #
    # their quotes, the four numbers of the position, the model value     #
    # today beside the exact payout at expiration, and the seven scenarios. #
    #                                                                     #
    # The spread is re-derived through the SAME stages as the list        #
    # (calendar -> gap -> direction -> report date -> chain -> expiry ->  #
    # qualifying pair) instead of being taken from the client: the page   #
    # may only name a symbol and a day, so a strike sent by a browser     #
    # could never move the answer away from the row it claims to describe.#
    # Cost is one gap request and one chain request, not the whole day.   #
    # ------------------------------------------------------------------ #
    #: The exclusion of the funnel -> the code this route answers with. A
    #: title that never had a chain is ``chain_unavailable`` from here: the
    #: missing thing is the chain, and a made-up second code would only be a
    #: synonym with a different spelling.
    #: A ticker as the calendar spells it - six characters, one dot for a class.
    #: The three parameters that name ONE listed structure instead of the
    #: funnel's pick. All three or none: half a structure is a different
    #: spread, not half of one.







    def _earnings_view(ticker, expirations, offered):
        """What the page shows - derived from the same calls the API makes.

        ``offered`` is the DTE-filtered list the expiration picker shows. Only
        dates from that list are printed: the page has a standing promise not to
        name a date outside the screener window (the picker test pins it), while
        the API still returns the full list. What is left out is named as a
        count, not silently dropped.
        """
        try:
            koerper = earnings_adapter.get_earnings(ticker)
            fenster = combine_earnings_and_expirations(
                koerper["earnings"], expirations, today=_us_today(now_fn)
            )
        except Exception:                                    # noqa: BLE001 - page must render
            app.logger.exception("earnings view failed for %s", ticker)
            return {"failed": True}
        eintrag = koerper["earnings"]
        cache = koerper.get("cache") or {}
        sichtbar = [d for d in fenster["expirations"]["after_earnings"] if d in set(offered or [])]
        return {
            "failed": False,
            "date": eintrag.get("next_earnings_date"),
            "session": _SESSION_TEXT.get(eintrag.get("release_session"), "UNBEKANNT"),
            "confirmation": _CONFIRMATION_TEXT.get(eintrag.get("date_confirmation"), "unbekannt"),
            "status": eintrag.get("status"),
            "source": eintrag.get("source"),
            "checked": eintrag.get("source_checked_at_utc"),
            "flags": eintrag.get("quality_flags") or [],
            "exit": fenster["exit"]["planned_last_exit_date"],
            "after": sichtbar,
            "after_hidden": len(fenster["expirations"]["after_earnings"]) - len(sichtbar),
            "selected": fenster["expirations"]["selected"],
            "eligible": fenster["decision"]["eligible"],
            "reasons": [grund["message"] for grund in fenster["decision"]["reasons"]],
            "notes": fenster["decision"]["notes"],
            "cache_state": cache.get("state"),
            "age_seconds": cache.get("age_seconds"),
            "checked_live": cache.get("state") == "live",
        }

    def _calendar_view():
        """Yesterday/today/tomorrow for the start page - degrades, never raises.

        The three days are US calendar dates, because the calendar is a US one
        (a German date would shift the whole block by the night offset). Every
        failure mode ends in a dict the template can render: a missing source
        is a plain sentence, not an exception and not an empty day pretending
        there is nothing to report.

        Each group therefore carries the answer state the adapter reported
        (``unavailable``: no content, ``stale``: last known rows) so the page
        can tell "nobody could ask" apart from "the source answered: nothing
        on that day". The header note names one failure per sentence, which is
        the only way a single failed day stays visible while its two siblings
        are live.

        One group is flagged ``is_default`` and is the tab the page opens on:
        ``today`` when it has rows, else the first day in ``DAY_ORDER``
        (prev, today, next) that has rows, else ``today`` again when no day
        has rows. A blind ``today`` default opens an empty tab on a weekend
        while the next day lists the only titles of the page - the content
        would be reachable but hidden behind a tab nobody clicks. Rows, not
        state, decide: an ``unavailable`` day has no rows and therefore never
        becomes the default while another day answered with titles.
        """
        try:
            heute = _us_today(now_fn)
            heute_iso = heute.isoformat() if heute is not None else ""
            # The three tabs are TRADING days, never calendar neighbours: on a
            # Friday the next tab used to be a Saturday with 0 rows the UI could
            # not leave. Each day is asked of the one US calendar; a day it
            # cannot name (``None``) falls out of the answer instead of being
            # replaced by ``heute ± 1``. The keys stay DAY_PREV/DAY_TODAY/
            # DAY_NEXT - only the dates they point at change.
            tag_prev = last_trading_day_before(heute)
            tag_today = heute if is_us_trading_day(heute) else next_trading_day_after(heute)
            tag_next = next_trading_day_after(tag_today) if tag_today is not None else None
            tage = {
                key: tag.isoformat()
                for key, tag in ((DAY_PREV, tag_prev), (DAY_TODAY, tag_today), (DAY_NEXT, tag_next))
                if tag is not None
            }
            antwort = earnings_adapter.get_calendar_days(dict(tage)) or {}
            roh = antwort.get("days") if isinstance(antwort, dict) else None
            roh = roh if isinstance(roh, dict) else {}
            tagesdaten = {}
            for key, tag in tage.items():
                eintrag = roh.get(tag)
                if not isinstance(eintrag, dict):
                    # No entry for this day: the group stays empty and says so.
                    eintrag = {"date": tag, "rows": [], "payload_ok": False, "skipped": 0}
                tagesdaten[key] = eintrag

            # Per-day answer state, taken from the adapter's own labelling and
            # never inferred from "the rows are empty": a live day may honestly
            # list nothing (a weekend answers `rows: null` with its own date
            # stamp), while an unavailable one has no content at all. A day the
            # answer does not mention is unavailable too - an absent answer is
            # no answer, so the page must not read it as an empty day.
            tageszustand = {}
            for key, tag in tage.items():
                meldung = roh.get(tag)
                gemeldet = meldung.get("state") if isinstance(meldung, dict) else None
                tageszustand[key] = gemeldet if gemeldet in ("live", "stale") else "unavailable"

            symbole, option_state, option_fetched = _option_symbols(earnings_adapter)
            kalender = build_calendar(tagesdaten, optionable=symbole)
            gruppen = []
            for gruppe in kalender["groups"]:
                zeilen = [dict(zeile, session_text=_SESSION_TEXT.get(zeile.get("session"), _SESSION_UNKNOWN_TEXT))
                          for zeile in gruppe["rows"]]
                zustand = tageszustand.get(gruppe["key"], "unavailable")
                meldung = roh.get(tage.get(gruppe["key"]))
                grund = None
                if zustand == "unavailable" and isinstance(meldung, dict):
                    grund = _failure_reason(meldung.get("error"))
                gruppen.append(dict(
                    gruppe,
                    rows=zeilen,
                    label=_calendar_day_label(gruppe["date"], heute_iso) or "",
                    date_text=_german_date(gruppe["date"]),
                    short_day=_short_day(gruppe["date"]),
                    counter=f"{len(zeilen)} von {gruppe['total']}",
                    # Tab counter: how many titles the day lists (after the
                    # option filter). A failed day has no number - "0" would
                    # read like "nothing reported".
                    tab_counter=None if zustand == "unavailable" else gruppe["total"],
                    unavailable=zustand == "unavailable",
                    stale=zustand == "stale",
                    # WHY the day is missing, when the source said so (a block
                    # and a timeout are different events and must not read
                    # alike). Empty when the answer carries no reason.
                    failure_reason=grund,
                ))

            # The page opens on exactly ONE day, so that day must be the one
            # with something to show: a hard-wired "today" opens an empty tab on
            # a Sunday while tomorrow lists 191 titles, which leaves the only
            # content of the block behind a tab nobody sees. Rule, in
            # DAY_ORDER: "today" wins when it has rows; otherwise the first day
            # (prev, then next) that has rows. Has no day rows at all, "today"
            # stays the default - an empty calendar must still open on the tab
            # a reader expects, not on an arbitrary one. A day whose state is
            # "unavailable" has no rows by definition, so it can never become
            # the default while any other day answered with titles.
            standard_key = DAY_TODAY
            if not any(gruppe["key"] == DAY_TODAY and gruppe["rows"] for gruppe in gruppen):
                for gruppe in gruppen:
                    if gruppe["rows"]:
                        standard_key = gruppe["key"]
                        break
            for gruppe in gruppen:
                gruppe["is_default"] = gruppe["key"] == standard_key

            angewendet = symbole is not None
            ohne_optionen = sum(gruppe["without_options"] for gruppe in gruppen)
            option_stale = bool(angewendet and option_state == "stale")

            state = str(antwort.get("state") or "unavailable")
            zustaende = list(tageszustand.values())
            vorhanden = any(z in ("live", "stale") for z in zustaende)
            ausgefallen = sum(1 for z in zustaende if z == "unavailable")
            letzter_stand = "stale" in zustaende or state == "stale"
            if state == "not_configured":
                note = "keine Earnings-Quelle konfiguriert"
            else:
                # One sentence per failure mode, because a partial outage and a
                # stale answer are different promises: the first says a day is
                # missing, the second says what is shown is old. "Nothing was
                # answered at all" stays the block-wide sentence - it is the
                # honest wording for three missing days, and it keeps the
                # weekend's two empty-but-answered days free of any warning.
                saetze = []
                if not vorhanden:
                    saetze.append("Quelle antwortet nicht")
                elif ausgefallen == 1:
                    saetze.append("ein Tag konnte nicht abgerufen werden")
                elif ausgefallen:
                    saetze.append(f"{ausgefallen} Tage konnten nicht abgerufen werden")
                if letzter_stand:
                    saetze.append("letzter Stand, Quelle antwortet nicht")
                # One reason per distinct failure, named next to the sentence:
                # "does not answer" alone hides that the source DID answer 403.
                gruende = []
                for key, tag in tage.items():
                    if tageszustand.get(key) != "unavailable":
                        continue
                    meldung = roh.get(tag)
                    grund = _failure_reason(
                        meldung.get("error") if isinstance(meldung, dict) else None
                    )
                    if grund and grund not in gruende:
                        gruende.append(grund)
                if gruende:
                    saetze.append("Grund: " + " · ".join(gruende))
                note = " · ".join(saetze) or None

            return {
                "available": bool(vorhanden),
                # Groups are rendered unless no source is configured at all: a
                # failed day must stay a visible, labelled day instead of a
                # column the page silently drops - and a not-configured source
                # is a setup fact, not three outages.
                "show_groups": bool(gruppen) and state != "not_configured",
                "state": state,
                "note": note,
                "stand": _german_stamp(antwort.get("fetched_at_utc")),
                "groups": gruppen,
                # The option filter is a block-wide statement: which list was
                # used (or not), how old it is, and one sentence naming what it
                # did. The per-group "without_options" says the same for a day.
                "option_filter": {"applied": angewendet, "stale": option_stale,
                                  "unavailable": not angewendet},
                "option_stamp": _german_stamp(option_fetched) if angewendet else None,
                "option_note": _option_note(angewendet, ohne_optionen, option_stale),
            }
        except Exception:                                    # noqa: BLE001 - page must render
            app.logger.exception("earnings calendar view failed")
            return {
                "available": False,
                "state": "unavailable",
                "note": "Quelle nicht lesbar - Details im Server-Log.",
                "stand": None,
                "groups": [],
                "option_filter": {"applied": False, "stale": False, "unavailable": True},
                "option_stamp": None,
                "option_note": "Options-Liste nicht abrufbar - ungefiltert",
            }

    @app.get("/")
    def index():
        ticker = request.args.get("ticker", "").upper().strip()
        expiration = request.args.get("expiration", "").strip()
        # The page shows no calendar and no per-ticker earnings panel, so it
        # asks no source for either: a plain load spends no adapter call (and
        # no network round trip). `?no_calendar` stays a known parameter only
        # so an old bookmark is not reported as an unknown one.
        error = None
        hint = None
        results = dropped = dates = selectable = quotes = None
        vertical_kept = vertical_dropped = None
        pair_coverage = {}
        criteria, criteria_error, unknown = _criteria_from_request(
            screener.criteria, request.args)
        strategy, spread_type, strategy_error, strategy_message = _strategy_selection(request.args)
        gewuenschte = (request.args.get("strategy") or "").strip().lower()
        if strategy_error == "unknown_option_type" and not gewuenschte:
            # Legacy behaviour, unchanged (D14) for the BARE old address: the page
            # names what is allowed and keeps computing the default. Only here -
            # the caller named nothing but a side.
            error = strategy_message
            strategy = DEFAULT_STRATEGY
            spread_type = strategy_option_type(strategy)
            strategy_error = None
        elif strategy_error:
            # A named strategy is NEVER replaced by a default: the request cannot be
            # honoured (unknown value, or a malformed legacy alias next to it), so the
            # page says so and computes nothing - the same answer /api/screen gives
            # with its 400. The radio keeps the id the caller asked for, so the form
            # does not silently show a different strategy than the request carried.
            error = strategy_message
            strategy = gewuenschte if gewuenschte in STRATEGIES else None
            spread_type = None if strategy is None else strategy_option_type(strategy)
        if unknown:
            hint = ("Unbekannte Parameter ignoriert: " + ", ".join(unknown)
                    + " (bedienbar sind: " + ", ".join(_CRITERIA_FIELDS)
                    + ", ignore_quote_age, sort)")
        if criteria_error:
            error = criteria_error
        try:
            if ticker and not criteria_error and not strategy_error:
                if not _TICKER_RE.match(ticker):
                    error = "Ungültiger Ticker - 1-6 Buchstaben A-Z."
                else:
                    dates = broker.get_expiration_dates(ticker)
                    selectable = _selectable_dates(dates, criteria, now_fn)
                    if expiration:
                        if not _DATE_RE.match(expiration):
                            error = "Ungültiges Verfallsdatum - JJJJ-MM-TT."
                        elif not dates or expiration not in dates:
                            error = "Verfallsdatum ist für diesen Ticker nicht gelistet."
                        else:
                            quotes = broker.get_options(ticker, expiration)
                            results, dropped = screen(
                                quotes, criteria, trend=_trend_for_screener(ticker),
                                now_fn=now_fn)
                            # the RAW chain, not the screened candidates (B)
                            vertical_kept, vertical_dropped = vertical_spreads(
                                quotes, criteria, strategy=strategy, now_fn=now_fn,
                                coverage=pair_coverage)
        except Exception:
            # no exception text in the page (CodeRabbit finding CWE-209:
            # CboeRequestError carries raw requests messages) - log it,
            # show a generic box; the server log holds the details
            app.logger.exception("screen failed for %s %s", ticker, expiration)
            error = "Marktdaten-Fehler - Details im Server-Log."
        lamp_source = quotes if quotes is not None else results
        # B-14: the <select> and the validation must read the same list. The
        # offer is the DTE-filtered list, but a bookmarked date that is real
        # yet filtered out must stay in it - otherwise the form renders one
        # date while screening another. Only dates that are really in the
        # chain qualify, so a made-up date still cannot get in.
        offered = list(selectable) if selectable else []
        if expiration and dates and expiration in dates and expiration not in offered:
            offered = [expiration] + offered
        # Darstellung (Slice K): die Reihenfolge der ANZEIGE, nicht die der
        # Schnittstelle. /api/screen liefert unveraendert die Kettenreihenfolge -
        # dort haengen Zusicherungen dran; sortiert wird nur, was die Seite zeigt.
        # Unbekannte Werte behandelt _vertical_sort_key wie die Voreinstellung;
        # eine zweite Pruefung hier waere wirkungslos (als Mutant M5 gemessen).
        sort_mode = request.args.get("sort", "rendite_desc")
        vertical_rows = None if vertical_kept is None else [
            (v, _spread_return_pct(v), _spread_be_gap_pct(v))
            for v in sorted(vertical_kept, key=_vertical_sort_key(sort_mode, criteria))
        ]
        age_min = _newest_quote_age_min(lamp_source, now_fn=now_fn)
        # Datenmodus + Datenstand + Vorlaeufig-Marker. Dasselbe Objekt liefert
        # /api/screen - die Seite rechnet nichts nach.
        zustand = _data_state(lamp_source, criteria, now_fn)
        zustand["fetched_text"] = _german_stamp(zustand["fetched_at_utc"])
        # D (Ausbau-Slice 3): the row order is a property of the RESULT, not
        # of the template. Sorting here keeps the order checkable in the test
        # client (there is no JS runner) and it is the order every consumer
        # sees. Tie order stays EXACTLY what the template's `|sort|reverse`
        # produced: stable ascending sort, then reversed. `sorted(reverse=True)`
        # would flip contracts with EQUAL scores - a silent behaviour change.
        if results is not None:
            results = list(reversed(sorted(results, key=lambda r: r.score)))
        # the header shows the DTE the DTE filter used (screener.filter_dte
        # reads quote.days_to_expiration) - the same field, not a second
        # computation. No rows -> no DTE claim.
        # D8: two numbers, both named. The feed stamp is what the data says, the DTE
        # is what this scan sees - they used to disagree silently (the picker offered a
        # date the filter then dropped).
        top = results[0].quote if results else None
        dte = top.asset.get_days_to_expiration(now_fn()) if top is not None else None
        feed_time = getattr(top, "quote_timestamp", None) if top is not None else None
        # L: label and the two legs are printed from the same table the calculation
        # used - one source for the wording, no second copy in the template.
        spez = STRATEGIES.get(strategy or "")
        return render_template(
            "screen.html", ticker=ticker, expiration=expiration,
            feed_time=feed_time,
            dates=offered, dates_loaded=dates is not None,
            results=results, dropped_count=len(dropped) if dropped else 0,
            dropped_reasons=_drop_reason_groups(dropped),
            spread_type=spread_type,
            strategy=strategy,
            strategy_label=None if spez is None else spez["label"],
            strategy_kind=None if spez is None else spez["kind"],
            strategy_buy=None if spez is None else spez["buy"],
            strategy_sell=None if spez is None else spez["sell"],
            strategy_direction=None if spez is None else spez["direction"],
            verticals=vertical_rows,
            sort_links=_sort_links(request.args),
            sort_mode=sort_mode,
            sort_modes=SORT_MODES,
            sort_labels=SORT_LABELS,
            vertical_drops=_drop_reason_groups(vertical_dropped),
            # Suchart, Suchumfang und die Ausschlussuebersicht (je Einheit)
            pair_search=_pair_search_block(criteria, pair_coverage),
            exclusions=_exclusion_blocks(dropped, vertical_dropped, criteria),
            criteria=criteria, age_off=criteria.max_quote_age_min is None,
            hint=hint,
            error=error, freshness=_lamp_state(age_min),
            # E3: die Seite braucht dieselben zwei Antworten wie das JSON - die
            # allgemeine Lampe oben und die Nutzergrenze, getrennt benannt.
            age_within_limit=_age_within_limit(lamp_source, criteria, now_fn=now_fn),
            feed_stamp=_feed_stamp(lamp_source),
            freshness_age=None if age_min is None else int(round(age_min)),
            # the limit shown in the header is the EFFECTIVE one (A): it is the
            # value the operator picked, not the app default
            freshness_limit=criteria.max_quote_age_min,
            dte=dte,
            trend=_trend_display(ticker) if ticker else None,
            # The day scan is the page's second action: what it may send and
            # what its table is called come from one server-side table.
            scan=scan_view_data(),
            feed_note="CBOE-Kursdaten zeitversetzt – ca. 15 Minuten hinter Echtzeit",
            data_state=zustand)

    return app


class _TrendInfo:
    """UI-facing trend snapshot (direction + availability, no crash)."""

    def __init__(self, direction, as_of=None, source_ok=True):
        self.direction = direction
        self.as_of = as_of
        self.source_ok = source_ok


def _load_trend(ticker):
    """Trend via provider if configured, else None (screener ignores it).

    Failure of the trend source must NOT look like a real 'flat' market:
    the UI receives source_ok=False and shows 'Trend nicht verfügbar'.
    """
    provider = _get_trend_provider()
    if provider is None:
        return None
    try:
        return provider.get_trend(ticker)
    except Exception as exc:  # noqa: BLE001 - any source failure = no trend
        # visible in the log: "source down" must not be indistinguishable
        # from "no provider configured" (the UI text is the same by design)
        logger.warning("trend source failed (%s)", type(exc).__name__)
        return _TrendInfo("flat", source_ok=False)


_STAGE_DIRECTION = {1: "flat", 2: "up", 3: "flat", 4: "down"}
_TREND_TIMEOUT_DEFAULT = 2.0


def _direction_from_stage(stage):
    """Weinstein stage -> trend direction.

    Anything that is not one of the four integers raises: a silent "flat"
    on a real "up" would be a confident wrong trend, which is worse for the
    side annotation than a visible source failure.
    """
    if type(stage) is not int or stage not in _STAGE_DIRECTION:
        raise ValueError("stage ungueltig")
    return _STAGE_DIRECTION[stage]


def _urllib_transport(url, headers, body, timeout):
    """The real transport (stdlib only).

    Returns (status, body) instead of raising on HTTP status, so the
    provider has ONE place that decides what counts as a failure.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class _MCPTrendProvider:
    """Trend from the xang1234 MCP endpoint (JSON-RPC 2.0 over HTTP).

    Only the direction is used (it picks the call/put side, never IV).
    Envelope is the documented MCP shape: result.content[0].text carries a
    JSON string (not a nested object). Every failure raises - _load_trend
    turns it into source_ok=False, so a dead source never looks calm.
    """

    def __init__(self, url, token, timeout=_TREND_TIMEOUT_DEFAULT, transport=None):
        # /mcp without the trailing slash answers 307; normalise once, so
        # callers may pass either form.
        self.url = url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self._transport = transport or _urllib_transport

    def get_trend(self, symbol):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "stock_snapshot",
                       "arguments": {"symbol": symbol}},
        }).encode("utf-8")
        headers = {"Authorization": "Bearer " + self.token,
                   "Content-Type": "application/json"}
        status, raw = self._transport(self.url, headers, body, self.timeout)
        if status != 200:
            raise RuntimeError(f"trend source http {status}")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("trend source body") from exc
        result = payload.get("result") or {}
        if "error" in payload or result.get("isError"):
            raise RuntimeError("trend source rpc")
        try:
            snapshot = json.loads(result["content"][0]["text"])
            # MEASURED 2026-10-12 against the live source (smoke test with the
            # secret): the numbers live under "technicals". The top level carries
            # breadth/summary/stock/themes - reading it made every lookup fail
            # closed, so the page only ever said "Trend nicht verfügbar".
            stage = snapshot["technicals"]["stage"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("trend source payload") from exc
        # the REQUESTED symbol is echoed: the screener matches trend.symbol
        # against the quote's underlying symbol.
        return TrendSignal(symbol, _direction_from_stage(stage))


def _get_trend_provider():
    """Provider only when the config is COMPLETE, else None.

    None is today's behaviour, byte for byte: no trend input, no exception,
    no badge. An empty string counts as missing.
    """
    url = os.environ.get("PAPERBROKER_TREND_MCP_URL", "")
    token = os.environ.get("PAPERBROKER_TREND_TOKEN", "")
    if not url or not token:
        return None
    raw = os.environ.get("PAPERBROKER_TREND_TIMEOUT_S", "")
    try:
        timeout = float(raw) if raw else _TREND_TIMEOUT_DEFAULT
    except (TypeError, ValueError):
        timeout = _TREND_TIMEOUT_DEFAULT
    if timeout <= 0:
        timeout = _TREND_TIMEOUT_DEFAULT
    return _MCPTrendProvider(url, token, timeout)


def _trend_for_screener(ticker):
    """The signal the SCREENER may use: a real TrendSignal, never the UI snapshot.

    _load_trend() answers "what should the badge show" and falls back to
    _TrendInfo(source_ok=False) when the source is down - an object without
    .symbol. Handing that to screen() raised AttributeError, which the page
    turned into a "Marktdaten-Fehler" box and the API into an HTML/500 page.
    A source outage must cost the trend annotation, not the result.
    """
    sig = _load_trend(ticker)
    return sig if (sig is not None and getattr(sig, "source_ok", True)) else None


def _trend_display(ticker):
    sig = _load_trend(ticker)
    if sig is None:
        return None
    if not getattr(sig, "source_ok", True):
        # "source down" (signal here) and "no provider configured" (sig None,
        # no badge at all) are different situations - only this one needs an
        # explanation, and it must not read like a calm flat market.
        return {"direction": "unavailable", "label": "Trend nicht verfügbar",
                "note": ("Trend-Quelle nicht erreichbar - die Liste steht "
                         "ohne Trend-Prüfung da.")}
    return {"direction": sig.direction, "label": _trend_label(sig.direction)}






def _spread_return_pct(v):
    """Rendite eines Spreads in Prozent - None, wenn der Nenner fehlt.

    Der Nenner ist der maximale Verlust. Er kann 0 sein (Debit 0); dann wird
    nicht gerechnet: eine Division durch Null waere kein Ergebnis, die Seite
    zeigt an dieser Stelle einen Strich.
    """
    return v.max_profit / v.max_loss * 100.0 if v.max_loss else None


def _spread_be_gap_pct(v):
    """Abstand des Kurses zum Break-even in Prozent - None, wenn kein Kurs.

    D20: this is a DISTANCE, always positive, and the direction is shown next to it. It used
    to be signed like a call - "positive means the underlying has to rise" - but a put spread
    breaks even BELOW the spot, so its value came out negative, and the ascending sort
    ("BE-Abstand", nearest first) put the LARGEST required move upward on top, which for a
    put is the least favourable position in the list.

    `underlying_price` is optional; without a price nothing is computed.
    """
    spot = v.long.underlying_price
    if not spot:
        return None
    return abs(v.breakeven - spot) / spot * 100.0


#: The sort orders the page offers. Every one of them is deterministic: a
#: missing value sorts LAST, and ties fall back to symbol + long strike, so the
#: same chain always produces the same list (the chain order is not a rule).
SORT_MODES = ("rendite_desc", "be_asc", "net_ratio_desc", "net_profit_desc",
              "max_loss_asc", "none")
SORT_LABELS = {
    "rendite_desc": "Rendite brutto (max. Gewinn / max. Verlust)",
    "be_asc": "BE-Abstand (nächster zuerst)",
    "net_ratio_desc": "Netto-Auszahlungsverhältnis (nach Kosten)",
    "net_profit_desc": "Netto-Gewinnpotenzial (nach Kosten)",
    "max_loss_asc": "maximaler Verlust (kleinster zuerst)",
    "none": "Kettenreihenfolge",
}


def _sort_asc(value):
    """``(0, value)`` for a real number, ``(1, 0.0)`` for a missing one.

    The leading flag puts "kein Wert" after every real number in BOTH
    directions - a row without a denominator must not look like the best or the
    worst trade. The second slot is the value itself (ascending).
    """
    return (0, float(value)) if value is not None else (1, 0.0)


def _sort_desc(value):
    """Like :func:`_sort_asc`, but the second slot is negated (descending)."""
    return (0, -float(value)) if value is not None else (1, 0.0)


def _vertical_sort_key(mode, criteria=None):
    """Sortierschluessel der Spread-ANZEIGE (nicht der Schnittstelle).

    Unbekannte Werte (None) sortieren ans Ende, damit sie nichts verdraengen.
    Der dritte Schluessel ist das Symbol - ohne ihn waere die Reihenfolge bei
    Gleichstand von der Kettenreihenfolge abhaengig statt reproduzierbar.

    The three net orders (net_ratio_desc, net_profit_desc, max_loss_asc) read
    the pair's own after-cost numbers. None of them is a probability or an
    expected return: they rank a payoff, and the page says so.

    ``criteria`` is REQUIRED for those three: ``net_max_profit()`` without a
    criteria set returns None (no size, no costs), and a key that silently
    compared None for every row would quietly fall back to the symbol tie-break
    - measured 2026-11-11 on a live chain: three different orders produced the
    same three rows.
    """
    def key(v):
        if mode == "none":
            return (0,)
        rendite = _spread_return_pct(v)
        abstand = _spread_be_gap_pct(v)
        kette = (v.symbol, float(getattr(v.long.asset, "strike", 0.0)))
        if mode == "be_asc":
            return (abstand if abstand is not None else 9e9,
                    -(rendite or 0.0), *kette)
        if mode == "max_loss_asc":
            # smallest risk first; at equal risk the larger profit first
            return (*_sort_asc(v.net_max_loss(criteria)),
                    *_sort_desc(v.net_max_profit(criteria)), *kette)
        if mode == "net_profit_desc":
            return (*_sort_desc(v.net_max_profit(criteria)), *kette)
        if mode == "net_ratio_desc":
            return (*_sort_desc(v.net_return_pct(criteria)), *kette)
        return (-(rendite if rendite is not None else -1.0),
                abstand if abstand is not None else 9e9, *kette)
    return key














def _sort_links(args):
    """Eine Sortier-Adresse je Modus, MIT den uebrigen Parametern.

    Ohne sie wuerde ein Klick auf einen Spaltenkopf Ticker, Verfall und alle
    Filter verlieren - die Seite fiele auf die Auswahl zurueck.
    """
    links = {}
    for mode in SORT_MODES:
        paar = [(k, wert) for k, wert in args.items(multi=True) if k != "sort"]
        paar.append(("sort", mode))
        links[mode] = urlencode(paar)
    return links










