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
from flask import Flask, g, jsonify, render_template, request, url_for
from markupsafe import escape

from . import company_profile, exclusions, gap_funnel, quote_age, spread_detail, texts
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
from .earnings_feed import EarningsFeedAdapter
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
from .hand_calendar import HandCalendar
from .next_earnings import NextEarningsAdapter
from .PaperBroker import PaperBroker
from .quote_age import AGE_MODES, MODE_FRESHNESS, MODE_LAST_AVAILABLE
from .reachability import HISTORY_BARS
from .routes import chain as chain_routes
from .routes import earnings as earnings_routes
from .routes import gap_scan as gap_scan_routes
from .routes import gap_spreads as gap_spreads_routes
from .routes import hand_earnings as hand_earnings_routes
from .routes import health as health_routes
from .routes import page as page_routes
from .routes import quality as quality_routes
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
from .texts import t
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
    SORT_LABELS,
    SORT_MODES,
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
    _sort_asc,
    _sort_desc,
    _spread_be_gap_pct,
    _spread_return_pct,
    _strategy_error_body,
    _strategy_label,
    _strategy_selection,
    _us_today,
    _vertical_sort_key,
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


















# German weekday abbreviations for the day tabs. ``%a`` would be locale
# dependent - the same reason parse_as_of spells its tables out.















def _earnings_source_from_env():
    """Which earnings source create_app wires when none is injected.

    Default is OFF (fail-closed): deploying the code must not silently start
    calling a new external service. The compose stack sets
    PAPERBROKER_EARNINGS_SOURCE=nasdaq; tests and a bare `flask run` keep the
    Null adapter and say so on the page.
    """
    return (os.environ.get("PAPERBROKER_EARNINGS_SOURCE") or "").strip().lower()






def create_app(quote_adapter=None, screener=None, now_fn=None, earnings_adapter=None,
               gap_adapter=None, next_earnings_adapter=None,
               company_profile_adapter=None):
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

    ``company_profile_adapter`` is the website lookup beside it (same key, same
    vendor, one call per detail block). ``None`` uses ``company_profile.py``
    itself. It is injectable for the same reason as the others: a test that
    opens a detail block must not depend on the network or on a key.
    """
    app = Flask(__name__, template_folder="templates")
    # The templates look their text up by key. The catalogue answers in whatever
    # language the request set (see _serve_in_request_language) - the default is
    # the source language, so a request without a setting reads exactly as before.
    app.jinja_env.globals["t"] = texts.t

    @app.before_request
    def _never_inherit_an_outbound_timeout():
        """Drop a socket default that the analytics module sets for its own outbound calls.

        ``analytics/app.py`` calls ``socket.setdefaulttimeout(8)`` so a stalled Yahoo request
        cannot hang for good. A socket accepted after that inherits the default, so the client
        connection would carry an 8 s send/recv limit. Measured: gunicorn's gthread worker clears
        it (the request socket reports ``None``), but werkzeug's development server does not - and
        a response cut off mid-send is exactly the kind of bug nobody would look for in a dev run.
        Freed here once per request, whichever server accepted the connection.
        """
        connection = request.environ.get("gunicorn.socket") or request.environ.get("werkzeug.socket")
        if connection is not None:
            connection.settimeout(None)

    #: The two endpoints that follow the language. Every other endpoint stays in
    #: the source language on purpose: /api/screen, the option chain, the earnings
    #: routes and the scan answers are stable legacy payloads, and a language
    #: switch must not move a byte in them.
    language_endpoints = {"api_gap_spreads", "api_gap_spreads_detail", "index"}

    @app.before_request
    def _serve_in_request_language():
        """Read ``?lang=`` for the allowed endpoints, ignore it everywhere else.

        An unknown code is refused with 400 instead of falling back: a caller that
        asks for a language we do not have gets told, not silently served German
        under an English name.
        """
        if request.endpoint not in language_endpoints:
            g.language = texts.DEFAULT_LANGUAGE
            return None
        try:
            g.language = texts.resolve_language(request.args.get("lang"))
        except texts.UnknownLanguage as fehler:
            if request.endpoint == "index":
                # Eine Seite, die um eine Sprache gebeten wird, die es nicht gibt,
                # antwortet als Seite - und in der Quellsprache, weil die gewuenschte
                # gerade nicht lesbar ist.
                satz = texts.t(
                    "page.error.unknown_language",
                    requested=fehler.code,
                    available=", ".join(texts.languages()),
                )
                return (
                    "<!doctype html><html lang=\"de\"><meta charset=\"utf-8\">"
                    f"<title>400</title><p>{escape(satz)}</p>"
                ), 400, {"Content-Type": "text/html; charset=utf-8"}
            return jsonify({
                "error": "unknown_language",
                "requested": str(fehler),
                "available": list(texts.languages()),
            }), 400
        return None

    @app.before_request
    def _enter_request_language():
        """Everything below this hook reads the language of the request."""
        texts._ACTIVE.set(getattr(g, "language", texts.DEFAULT_LANGUAGE))
        return None

    @app.teardown_request
    def _leave_request_language(_ausnahme=None):
        """Undo the setting - a leaked language would colour the NEXT request."""
        texts._ACTIVE.set(texts.DEFAULT_LANGUAGE)
        return None
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

    # The hand calendar: one file (mounted cache directory) and one feed adapter
    # for the lifetime of the app. Both are optional by design - if either is
    # missing, the funnel simply keeps its old answer.
    hand_calendar = HandCalendar(now_fn=now_fn)
    earnings_feed = EarningsFeedAdapter(now_fn=now_fn)

    # One context for the route modules: the state that used to live in the
    # closures of this factory. A route reads it instead of closing over it.
    app.extensions[CONTEXT_KEY] = FactoryContext(
        broker=broker,
        screener=screener,
        earnings_adapter=earnings_adapter,
        gap_adapter=gap_adapter,
        next_earnings_adapter=next_earnings_adapter,
        # Default ist das Modul selbst (es hat ``web_url``): ein injizierter
        # Adapter gewinnt, damit ein Test ohne Netz und ohne Schluessel laeuft.
        company_profile=company_profile_adapter or company_profile,
        now_fn=now_fn,
        trend_for_screener=_trend_for_screener,
        trend_display=_trend_display,
        hand_calendar=hand_calendar,
        earnings_feed=earnings_feed,
    )

    health_routes.register(app)
    chain_routes.register(app)
    earnings_routes.register(app)
    gap_scan_routes.register(app)
    gap_spreads_routes.register(app)
    hand_earnings_routes.register(app)
    page_routes.register(app)
    # Der eigene Baustein hinter demselben Host: /qualitaet/* geht unveraendert
    # an analytics (own process, own --base-path).
    quality_routes.register(app)


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
        return {"direction": "unavailable",
                "label": t("trend.unavailable.label"),
                "note": t("trend.unavailable.note")}
    return {"direction": sig.direction, "label": _trend_label(sig.direction)}










#: The sort orders the page offers. Every one of them is deterministic: a
#: missing value sorts LAST, and ties fall back to symbol + long strike, so the
#: same chain always produces the same list (the chain order is not a rule).






























