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

from .adapters.earnings.NasdaqEarningsAdapter import NasdaqEarningsAdapter, NullEarningsAdapter
from .adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter
from .assets import calendar_days_between
from .earnings_calendar import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_ESTIMATED,
    SESSION_AFTER,
    SESSION_BEFORE,
    combine_earnings_and_expirations,
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
from .PaperBroker import PaperBroker
from .screener import (
    DEFAULT_STRATEGY,
    FUTURE_TOLERANCE_S,
    LEGACY_OPTION_TYPES,
    STRATEGIES,
    STRATEGY_IDS,
    OptionScreener,
    ScreenerCriteria,
    TrendSignal,
    _trend_label,
    resolve_strategy,
    screen,
    strategy_option_type,
    vertical_spreads,
)

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z]{1,6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# fetch-age thresholds for the freshness lamp (design decision, not
# measurement): the CBOE delayed feed is ~15 min behind by nature, so the
# lamp measures data age; the feed delay itself is communicated as a
# static banner, not as a red state.
_FRESH_GREEN_MIN = 30
_FRESH_YELLOW_MIN = 120

# UX-1 (Ausbau-Slice 1): the screener hands back one free-text reason per
# dropped contract ("veraltet: 257 Min.", "OI 7 < 500"). Counting the raw texts
# would give roughly one line per contract (measured: 48 distinct texts for
# 97 drops with the age guard off), so numbers are folded to a placeholder.
# It is a NUMBER TOKEN, not a single digit: folding digit by digit would
# split "7.41" and "10.2" into different groups although both are "spread
# too wide". A group whose raw texts are all identical keeps the raw text,
# so the measured value stays on the page ("veraltet: 257 Min.", not "veraltet: # Min.").
_REASON_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


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
_CRITERIA_INT_FIELDS = ("dte_min", "dte_max", "min_oi", "min_volume", "max_quote_age_min",
                        "min_bid_size", "min_ask_size", "multiplier", "quantity")
_CRITERIA_FLOAT_FIELDS = ("max_spread_pct", "max_spread_abs", "pair_max_width", "iv_min", "iv_max",
                          "weight_spread", "weight_oi", "weight_iv", "fees", "budget")
_CRITERIA_BOOL_FIELDS = ("require_oi", "require_volume")
_CRITERIA_CHOICE_FIELDS = {"iv_pref": ("high", "low")}
_CRITERIA_FIELDS = (_CRITERIA_INT_FIELDS + _CRITERIA_FLOAT_FIELDS
                    + _CRITERIA_BOOL_FIELDS + tuple(_CRITERIA_CHOICE_FIELDS))
_CRITERIA_ECHO_FIELDS = tuple(ScreenerCriteria.__dataclass_fields__)
_IGNORED_PARAMS = ("ticker", "expiration", "ignore_quote_age", "sort", "option_type",
                   "strategy", "no_calendar")


def _parse_criteria_value(field, raw):
    """Parse one request value into the criteria type.

    Raises ValueError with a readable hint. A comma is rejected on purpose:
    the value has to round-trip through the URL and the reasons printed by the
    screener use a dot ("spread 28.6% > 10.0%").
    """
    text = str(raw).strip()
    if "," in text:
        raise ValueError(f"{field}: bitte den Punkt als Dezimaltrenner verwenden "
                         f"(z. B. 7.5 statt '{text}').")
    if field in _CRITERIA_INT_FIELDS:
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError(f"{field}: '{text}' ist keine ganze Zahl.") from exc
    if field in _CRITERIA_BOOL_FIELDS:
        # The empty field (handled above) means "keep the default"; a switch
        # therefore only has to name the two explicit states.
        if text not in ("0", "1"):
            raise ValueError(f"{field}: '0' oder '1' erwartet (ist: '{text}').")
        return text == "1"
    if field in _CRITERIA_CHOICE_FIELDS:
        erlaubt = _CRITERIA_CHOICE_FIELDS[field]
        if text not in erlaubt:
            raise ValueError(f"{field}: einer von {', '.join(erlaubt)} erwartet "
                             f"(ist: '{text}').")
        return text
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{field}: '{text}' ist keine Zahl.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{field}: '{text}' ist keine endliche Zahl.")
    return value


def _criteria_from_request(base_criteria, args):
    """Merge the settable criteria from the request over the base criteria.

    Returns (criteria, error, unknown):
      error   - readable text when a value is unusable or the merged set is
                contradictory (validate()), else None
      unknown - request parameters that were not used (they are named on the
                page instead of being ignored silently)
    """
    values = {name: getattr(base_criteria, name) for name in _CRITERIA_ECHO_FIELDS}
    for field in _CRITERIA_FIELDS:
        raw = args.get(field, "").strip()
        if not raw:
            continue
        try:
            values[field] = _parse_criteria_value(field, raw)
        except ValueError as exc:
            return base_criteria, str(exc), []
    if args.get("ignore_quote_age", "").strip() == "1":
        values["max_quote_age_min"] = None
    criteria = ScreenerCriteria(**values)
    try:
        criteria.validate()
    except ValueError as exc:
        return base_criteria, str(exc), []
    unknown = [k for k in args.keys()
               if k not in _IGNORED_PARAMS and k not in _CRITERIA_FIELDS]
    return criteria, None, sorted(unknown)


def _criteria_echo(criteria):
    """All effective criteria as a JSON-able dict (None -> null).

    One serialisation for every field - a whitelist would drift.
    """
    return {name: getattr(criteria, name) for name in _CRITERIA_ECHO_FIELDS}


_SESSION_TEXT = {SESSION_BEFORE: "vorbörslich", SESSION_AFTER: "nachbörslich"}
_SESSION_UNKNOWN_TEXT = "Zeitpunkt unbekannt"
_CONFIRMATION_TEXT = {CONFIRMATION_CONFIRMED: "bestätigt", CONFIRMATION_ESTIMATED: "geschätzt"}
_CALENDAR_DAY_LABEL = {DAY_PREV: "gestern", DAY_TODAY: "heute", DAY_NEXT: "morgen"}


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


def _option_symbols(adapter):
    """``(symbols|None, state, fetched_at_utc)`` from the earnings adapter.

    Every failure mode collapses into "no list": an adapter without the
    method, an exception, a non-dict answer or a malformed symbol list all
    mean the page must not filter. A callable that raises may not cost the
    calendar - the list is a filter, not content.
    """
    getter = getattr(adapter, "get_optionable_symbols", None)
    if not callable(getter):
        return None, "unavailable", None
    try:
        antwort = getter()
    except Exception:                                    # noqa: BLE001 - the page must render
        logger.exception("optionable symbol list failed")
        return None, "unavailable", None
    if not isinstance(antwort, dict):
        return None, "unavailable", None

    stempel = antwort.get("fetched_at_utc")
    wert = antwort.get("symbols")
    if not isinstance(wert, list) or not wert:
        return None, "unavailable", stempel
    symbole = set()
    for eintrag in wert:
        if not isinstance(eintrag, str) or not eintrag.strip():
            return None, "unavailable", stempel
        symbole.add(eintrag.strip())
    return frozenset(symbole), str(antwort.get("state") or ""), stempel


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


def _earnings_source_from_env():
    """Which earnings source create_app wires when none is injected.

    Default is OFF (fail-closed): deploying the code must not silently start
    calling a new external service. The compose stack sets
    PAPERBROKER_EARNINGS_SOURCE=nasdaq; tests and a bare `flask run` keep the
    Null adapter and say so on the page.
    """
    return (os.environ.get("PAPERBROKER_EARNINGS_SOURCE") or "").strip().lower()


def _us_today(now_fn):
    """The trading date in New York - a UTC date is a day ahead after 19:00 ET."""
    return arrow.get(now_fn()).to("America/New_York").date()


def create_app(quote_adapter=None, screener=None, now_fn=None, earnings_adapter=None):
    """now_fn: injectable clock for tests (default: real arrow.get).

    The screener's 30-min-stale guard compares quote_timestamp against
    now() - a hardcoded fixture timestamp goes stale in real time, so
    tests MUST pin the clock instead of freezing the fixtures.
    """
    app = Flask(__name__, template_folder="templates")
    if now_fn is None:
        now_fn = arrow.get

    if quote_adapter is None:
        quote_adapter = CBOEQuoteAdapter()
    if screener is None:
        screener = OptionScreener(ScreenerCriteria())
    if earnings_adapter is None:
        # Fail-closed default: no source configured -> UNKNOWN, never a guess.
        if _earnings_source_from_env() == "nasdaq":
            earnings_adapter = NasdaqEarningsAdapter(now_fn=now_fn)
        else:
            earnings_adapter = NullEarningsAdapter(now_fn=now_fn)
    broker = PaperBroker(quote_adapter=quote_adapter)

    # JSON error contract for the API (CodeRabbit finding): abort() would
    # send Flask's HTML error page, which a JSON client cannot parse
    @app.errorhandler(400)
    def _bad_request(err):
        if request.path.startswith("/api/"):
            return jsonify(error=getattr(err, "description", "bad request")), 400
        return err

    @app.get("/healthz")
    def healthz():
        # liveness only: no CBOE, no xang - must stay green in LAN tests
        # even when the internet is down
        return jsonify(status="ok")

    def _in_dte_window(dte, criteria):
        # Same semantics as the screener's per-quote DTE filter
        # (screener.py:242-247): a None bound disables that side.
        if criteria.dte_min is not None and dte < criteria.dte_min:
            return False
        if criteria.dte_max is not None and dte > criteria.dte_max:
            return False
        return True

    def _drop_reason_groups(dropped):
        """Aggregate the screener's per-quote drop reasons for page + JSON.

        Output: [{"reason": str, "count": int}, ...], count descending.
        Ties keep the order in which the filters produced them (= pipeline
        order), because that is the order a reader expects: the filter that
        acts first is named first. HTML and JSON consume the same list -
        parity by construction.
        """
        groups = {}
        for _quote, reason in dropped or []:
            raw = str(reason)
            pattern = _REASON_NUM_RE.sub("#", raw)
            entry = groups.setdefault(pattern, {"count": 0, "raw": set()})
            entry["count"] += 1
            entry["raw"].add(raw)
        # E7: instead of blanking the numbers out (the pattern "Spread #% > #%" was
        # what the page showed) the summary names one real example WITH its bounds;
        # the pattern stays only the grouping key. sorted() keeps the example stable.
        out = [{"reason": sorted(e["raw"])[0], "count": e["count"]}
               for _pattern, e in groups.items()]
        out.sort(key=lambda g: -g["count"])   # stable: ties keep pipeline order
        return out

    def _selectable_dates(dates, criteria):
        # B-11: the picker must only OFFER dates that can still hit. A
        # chain runs years out, but the screener window is ~6 weeks, so
        # most listed dates were dead entries in the dropdown. Validation
        # keeps using the FULL list, so a bookmarked date that fell out of
        # the window still answers 200 with zero hits instead of 400.
        as_of = now_fn()
        return [d for d in dates
                if _in_dte_window(calendar_days_between(d, as_of), criteria)]

    def _parse_args():
        # one criteria object per request: base = injected criteria, the URL
        # overrides only what it really sets (Ausbau-Slice 2, A)
        criteria, criteria_error, unknown = _criteria_from_request(
            screener.criteria, request.args)
        ticker = request.args.get("ticker", "").upper().strip()
        # form-first flow (CodeRabbit finding): load the expiration list
        # as soon as the ticker is valid so the selector appears without
        # requiring a pre-known date; empty expiration = picker only
        if ticker and not _TICKER_RE.match(ticker):
            abort(400, description="invalid ticker (1-6 letters A-Z expected)")
        expiration = request.args.get("expiration", "").strip()
        dates = None
        selectable = None
        if ticker:
            dates = broker.get_expiration_dates(ticker)
            selectable = _selectable_dates(dates, criteria)
        if expiration:
            if not _DATE_RE.match(expiration):
                abort(400, description="invalid expiration (YYYY-MM-DD expected)")
            # expiration must be one of the chain's real dates - prevents
            # probing arbitrary CBOE URLs with made-up dates
            if not dates or expiration not in dates:
                abort(400, description="expiration is not a listed date for this ticker")
        elif not ticker:
            abort(400, description="ticker and expiration required")
        if criteria_error:
            # a contradictory or unparsable criteria set is a client error -
            # no silent correction (panel decision)
            abort(400, description=criteria_error)
        return ticker, expiration, dates, selectable, criteria, unknown

    @app.get("/api/screen")
    def api_screen():
        ticker, expiration, dates, selectable, criteria, unknown = _parse_args()
        strategy, spread_type, strategy_error, _text = _strategy_selection(request.args)
        if strategy_error:
            return jsonify(_strategy_error_body(strategy_error)), 400
        if not expiration:
            # picker stage: offer only the dates that can still hit. No
            # screening happened, so the drop list is empty - but the key
            # is always present, so a consumer never needs a key check.
            return jsonify(ticker=ticker, expiration=None,
                           strategy=strategy, spread_type=spread_type,
                           listed_dates=selectable, results=[], freshness=None,
                           dropped_reasons=[], verticals=[],
                           vertical_drops=[], trend=None,
                           # no snapshot was screened, so there is nothing to
                           # certify - the key is present but empty
                           eva_quality=None)
        # D9b: the JSON endpoint must answer JSON. An unknown symbol is the caller
        # mistake (404), a feed outage is nobodys (503) - the HTML error page the
        # framework produced broke every consumer that parses the body.
        try:
            quotes = broker.get_options(ticker, expiration)
        except Exception as exc:                        # noqa: BLE001 - classified below
            from paperbroker.adapters.quotes.CBOEQuoteAdapter import CboeNotFoundError
            if isinstance(exc, CboeNotFoundError):
                return jsonify(error="unknown symbol", ticker=ticker), 404
            app.logger.exception("api_screen: market data failed for %s %s", ticker, expiration)
            return jsonify(error="market data unavailable"), 503
        trend_signal = _trend_for_screener(ticker)
        results, dropped = screen(quotes, criteria, trend=trend_signal,
                                  now_fn=now_fn)
        vertical_kept, vertical_dropped = vertical_spreads(quotes, criteria,
                                                            strategy=strategy,
                                                            now_fn=now_fn)
        # One identity for the fetch. Both the gate and every vertical entry
        # carry it, so a consumer can prove they refer to the same snapshot.
        snapshot_id = snapshot_identity(ticker, expiration, quotes, _feed_stamp(quotes))
        eva = _eva_quality(quotes, criteria, now_fn,
                           ticker=ticker, expiration=expiration, snapshot_id=snapshot_id)
        return jsonify(
            ticker=ticker,
            expiration=expiration,
            chain_size=len(quotes),
            # L: the strategy that was priced, and - unchanged - the chain side it
            # trades on. `spread_type` stays for existing consumers; `strategy` is
            # the name a caller should read.
            strategy=strategy,
            strategy_label=_strategy_label(strategy),
            spread_type=spread_type,
            # B-16: rows the adapter had to skip (list subclass carries
            # the count); the key is always present so a consumer never
            # needs a key check
            chain_skipped=getattr(quotes, "skipped", 0),
            candidates=len(results),
            dropped=len(dropped),
            dropped_reasons=_drop_reason_groups(dropped),
            criteria=_criteria_echo(criteria),
            freshness=_freshness_lamp(quotes, now_fn=now_fn),
            # E3: the operator own limit gets its own named line.
            freshness_age=_newest_quote_age_min(quotes, now_fn=now_fn),
            freshness_limit=criteria.max_quote_age_min,
            age_within_limit=_age_within_limit(quotes, criteria, now_fn=now_fn),
            feed_stamp=_feed_stamp(quotes),
            # B (Ausbau-Slice B): vertical CALL spreads from the RAW chain.
            # They are deliberately not built from `results` - the eight
            # single-option filters do not preserve strike adjacency
            # (measured: 0 candidates -> 0 pairs on a live chain that
            # carries 102 adjacent call pairs).
            # EVA: a separate, machine-readable verdict. A row in `verticals`
            # is a calculated candidate - never an approval. `eva_eligible`
            # repeats the gate's single word per row so it cannot be inferred
            # from presence.
            eva_quality=eva,
            verticals=[{**v.as_dict(criteria),
                        "snapshot_id": snapshot_id,
                        "eva_eligible": eva["eligible"]} for v in vertical_kept],
            vertical_drops=_drop_reason_groups(vertical_dropped),
            # the trend signal that really reached the screener: null when the
            # source is down or no provider is configured - then the list is
            # built without the trend guard instead of being withheld
            trend=None if trend_signal is None else {
                "direction": trend_signal.direction,
                "symbol": trend_signal.symbol,
            },
            results=[
                {
                    "symbol": r.quote.asset.symbol,
                    "bid": r.quote.bid,
                    "ask": r.quote.ask,
                    "iv": r.quote.iv,
                    "open_interest": r.quote.open_interest,
                    "volume": r.quote.volume,
                    "delta": r.quote.delta,
                    "score": round(r.score, 4),
                    "spread_pct": round(r.spread_pct, 2),
                    "reasons": r.reasons,
                }
                for r in results
            ],
        )

    # Unfiltered, read-only option-chain view for Hermes. Deliberately NOT a
    # screener: no criteria object, no trend provider, no spread engine. The
    # route validates its arguments, fetches the raw snapshot through the
    # facade and hands it to a pure serializer.
    @app.get("/api/v1/option-chain")
    def api_option_chain():
        from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
            CboeNotFoundError,
            CboeRequestError,
        )
        from paperbroker.option_chain_api import (
            SCHEMA_VERSION,
            listed_expirations,
            serialise_snapshot,
        )

        def _chain_error(status, code, message):
            # Own envelope: schema_version plus a stable error.code, so a JSON
            # client never has to parse an HTML page or a free-text string.
            return (
                jsonify(
                    schema_version=SCHEMA_VERSION,
                    error={"code": code, "message": message},
                ),
                status,
            )

        erlaubt = ("ticker", "expiration")
        unbekannt = sorted(set(request.args) - set(erlaubt))
        if unbekannt:
            return _chain_error(
                400,
                "unknown_parameter",
                f"unknown parameter(s): {', '.join(unbekannt)}. Allowed: ticker, expiration",
            )
        doppelt = sorted(name for name in set(request.args) if len(request.args.getlist(name)) > 1)
        if doppelt:
            return _chain_error(
                400,
                "duplicate_parameter",
                f"duplicate parameter(s): {', '.join(doppelt)}",
            )

        ticker = (request.args.get("ticker") or "").strip().upper()
        if not ticker:
            return _chain_error(400, "ticker_required", "ticker is required")
        if not _TICKER_RE.match(ticker):
            return _chain_error(
                400, "invalid_ticker", "invalid ticker (1-6 letters A-Z expected)"
            )
        expiration = (request.args.get("expiration") or "").strip()
        if expiration:
            if not _DATE_RE.match(expiration):
                return _chain_error(
                    400, "invalid_expiration", "invalid expiration (YYYY-MM-DD expected)"
                )
            # The pattern only checks the shape: 2026-13-99 passes it. An
            # impossible date is a client error, not "not listed".
            from datetime import date as _date

            try:
                _date.fromisoformat(expiration)
            except ValueError:
                return _chain_error(
                    400, "invalid_expiration", "invalid expiration (not a real calendar date)"
                )

        # One fetch. The snapshot comes from the existing cache; this route
        # adds no refresh mechanism and never truncates silently.
        try:
            snapshot = broker.get_option_chain_snapshot(ticker)
        except CboeNotFoundError:
            return _chain_error(404, "unknown_symbol", "the source does not know this symbol")
        except CboeRequestError:
            app.logger.warning("api_option_chain: market data failed for %s", ticker)
            return _chain_error(
                503, "market_data_unavailable", "the market data source is unreachable right now"
            )

        # A selected expiration must be one the snapshot itself lists - this
        # keeps the answer honest instead of returning an empty chain for a
        # date nobody offered.
        verfaelle = set(listed_expirations(snapshot))
        if expiration and expiration not in verfaelle:
            return _chain_error(
                400,
                "expiration_not_listed",
                f"expiration {expiration} is not a listed expiration for this ticker",
            )

        try:
            koerper = serialise_snapshot(
                snapshot,
                ticker=ticker,
                requested_expiration=expiration or None,
                now_fn=lambda: now_fn().to("utc").datetime,
            )
        except Exception:                                    # noqa: BLE001 - classified below
            app.logger.exception("api_option_chain: serialisation failed for %s", ticker)
            return _chain_error(
                503, "market_data_unavailable", "the snapshot could not be processed"
            )
        return jsonify(koerper)

    # ------------------------------------------------------------------ #
    # Earnings calendar (read-only, fail-closed).                        #
    #                                                                    #
    # Two routes, one contract: /api/v1/earnings answers "what does the   #
    # source say about this ticker", /api/v1/earnings-window adds the     #
    # expirations this chain really lists and the deterministic decision. #
    # Neither route places an order, and neither invents a date.          #
    # ------------------------------------------------------------------ #
    def _earnings_error(status, code, message):
        from paperbroker.earnings_calendar import SCHEMA_VERSION as _EARNINGS_SCHEMA

        return (
            jsonify(schema_version=_EARNINGS_SCHEMA, error={"code": code, "message": message}),
            status,
        )

    def _earnings_args():
        """ticker-only argument validation, same envelope as the chain route."""
        erlaubt = ("ticker",)
        unbekannt = sorted(set(request.args) - set(erlaubt))
        if unbekannt:
            return None, _earnings_error(
                400, "unknown_parameter",
                f"unknown parameter(s): {', '.join(unbekannt)}. Allowed: ticker",
            )
        doppelt = sorted(name for name in set(request.args) if len(request.args.getlist(name)) > 1)
        if doppelt:
            return None, _earnings_error(
                400, "duplicate_parameter", f"duplicate parameter(s): {', '.join(doppelt)}"
            )
        ticker = (request.args.get("ticker") or "").strip().upper()
        if not ticker:
            return None, _earnings_error(400, "ticker_required", "ticker is required")
        if not _TICKER_RE.match(ticker):
            return None, _earnings_error(
                400, "invalid_ticker", "invalid ticker (1-6 letters A-Z expected)"
            )
        return ticker, None

    def _earnings_snapshot(ticker):
        """Adapter call with one place that turns a crash into a JSON error."""
        try:
            return earnings_adapter.get_earnings(ticker), None
        except Exception:                                    # noqa: BLE001 - classified below
            app.logger.exception("earnings lookup failed for %s", ticker)
            return None, _earnings_error(
                503, "earnings_source_failed", "the earnings source could not be read"
            )

    @app.get("/api/v1/earnings")
    def api_earnings():
        ticker, fehler = _earnings_args()
        if fehler is not None:
            return fehler
        koerper, fehler = _earnings_snapshot(ticker)
        if fehler is not None:
            return fehler
        return jsonify(koerper)

    @app.get("/api/v1/earnings-window")
    def api_earnings_window():
        from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
            CboeNotFoundError,
            CboeRequestError,
        )
        from paperbroker.option_chain_api import listed_expirations

        ticker, fehler = _earnings_args()
        if fehler is not None:
            return fehler
        koerper, fehler = _earnings_snapshot(ticker)
        if fehler is not None or koerper is None:
            return fehler

        # The expirations come from the SAME snapshot the option-chain route
        # serves. A chain that cannot be read is reported as such: expirations
        # stay unknown and the decision blocks - it never falls back to a
        # guessed date.
        verfaelle, ketten_status = None, "ok"
        try:
            snapshot = broker.get_option_chain_snapshot(ticker)
            verfaelle = listed_expirations(snapshot)
        except CboeNotFoundError:
            ketten_status = "unknown_symbol"
        except CboeRequestError:
            ketten_status = "market_data_unavailable"
        except Exception:                                    # noqa: BLE001 - classified here
            app.logger.exception("earnings window: chain failed for %s", ticker)
            ketten_status = "market_data_unavailable"

        fenster = combine_earnings_and_expirations(
            koerper["earnings"], verfaelle, today=_us_today(now_fn)
        )
        antwort = dict(fenster)
        antwort["ticker"] = ticker
        antwort["chain"] = {
            "status": ketten_status,
            "expirations_read": verfaelle is not None,
        }
        return jsonify(antwort)

    # ------------------------------------------------------------------ #
    # One scan across a whole earnings day (GET /api/v1/earnings-scan).   #
    #                                                                     #
    # Deliberately NOT wired into GET /: one chain request per title is    #
    # far too expensive for a page view, and the page's Top-25 cut is      #
    # exactly the limit this scan exists to lift. It therefore runs on an  #
    # explicit request only, behind a hard budget, and answers with its    #
    # own coverage report instead of pretending to be complete.           #
    # ------------------------------------------------------------------ #
    _SCAN_LIMIT_PARAMS = ("max_symbols", "max_expirations", "max_seconds", "concurrency")

    def _scan_limit(name, raw, default, ceiling, *, integer, minimum):
        """One budget number, or a readable reason why it is unusable.

        Bounds are enforced here, not in the client: a scan of 5000 titles is
        not a slower answer, it is an outage waiting to happen.
        """
        text = (raw or "").strip()
        if not text:
            return default, None
        try:
            wert = int(text) if integer else float(text)
        except ValueError:
            return None, f"{name}: '{text}' is not a number"
        if not integer and not math.isfinite(wert):
            return None, f"{name}: '{text}' is not a finite number"
        if wert < minimum:
            return None, f"{name}: must be >= {minimum} (is: {wert})"
        if wert > ceiling:
            return None, f"{name}: must be <= {ceiling} (is: {wert})"
        return wert, None

    def _scan_args():
        """Validated arguments of one scan, or a ready-made JSON error."""
        erlaubt = (set(_SCAN_LIMIT_PARAMS) | {"day", "strategy", "option_type",
                                             "ignore_quote_age"}
                   | set(_CRITERIA_FIELDS))
        unbekannt = sorted(set(request.args) - erlaubt)
        if unbekannt:
            return None, _earnings_error(
                400, "unknown_parameter",
                f"unknown parameter(s): {', '.join(unbekannt)}. Allowed: day, strategy, "
                f"{', '.join(_SCAN_LIMIT_PARAMS)} and the screener criteria",
            )
        doppelt = sorted(name for name in ("day", "option_type") + _SCAN_LIMIT_PARAMS
                         if len(request.args.getlist(name)) > 1)
        if doppelt:
            return None, _earnings_error(
                400, "duplicate_parameter",
                f"duplicate parameter(s): {', '.join(doppelt)} (only strategy may repeat)",
            )

        day = (request.args.get("day") or "").strip()
        if not day:
            return None, _earnings_error(400, "day_required", "day is required (YYYY-MM-DD)")
        if not _DATE_RE.match(day):
            return None, _earnings_error(400, "invalid_day", "invalid day (YYYY-MM-DD expected)")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return None, _earnings_error(
                400, "invalid_day", "invalid day (not a real calendar date)"
            )

        # One or more strategies; `strategy` may repeat and may carry a comma
        # list. An empty list means "all four" - the scan answers what the day
        # offers, and the caller narrows it down.
        gewaehlt = []
        for wert in request.args.getlist("strategy"):
            for teil in str(wert).split(","):
                teil = teil.strip().lower()
                if teil and teil not in gewaehlt:
                    gewaehlt.append(teil)
        for sid in gewaehlt:
            if sid not in STRATEGIES:
                return None, (jsonify(_strategy_error_body("unknown_strategy")), 400)

        option_type = (request.args.get("option_type") or "").strip().lower()
        if option_type:
            # The legacy alias names ONE strategy. With several strategies it
            # cannot say which one is meant, so the request is refused instead
            # of resolved in silence - the same rule the single view follows.
            if len(gewaehlt) > 1:
                return None, (jsonify(_strategy_error_body("conflict")), 400)
            aufgeloest, code, _text = resolve_strategy(
                gewaehlt[0] if gewaehlt else None, option_type
            )
            if code:
                return None, (jsonify(_strategy_error_body(code)), 400)
            gewaehlt = [aufgeloest]

        limits = {}
        for name, default, ceiling, integer, minimum in (
            ("max_symbols", DEFAULT_MAX_SYMBOLS, MAX_SYMBOLS_CEILING, True, 1),
            ("max_expirations", DEFAULT_MAX_EXPIRATIONS, MAX_EXPIRATIONS_CEILING, True, 1),
            ("max_seconds", DEFAULT_MAX_SECONDS, MAX_SECONDS_CEILING, False, 0.1),
            ("concurrency", DEFAULT_CONCURRENCY, CONCURRENCY_CEILING, True, 1),
        ):
            wert, fehler = _scan_limit(name, request.args.get(name), default, ceiling,
                                       integer=integer, minimum=minimum)
            if fehler is not None:
                return None, _earnings_error(400, "invalid_limit", fehler)
            limits[name] = wert

        criteria, criteria_error, _unused = _criteria_from_request(
            screener.criteria, request.args
        )
        if criteria_error:
            return None, _earnings_error(400, "invalid_criteria", criteria_error)

        return {
            "day": day,
            "strategies": tuple(gewaehlt) or None,
            "criteria": criteria,
            "limits": ScanLimits(
                max_symbols=int(limits["max_symbols"]),
                max_expirations_per_symbol=int(limits["max_expirations"]),
                max_seconds=float(limits["max_seconds"]),
                concurrency=int(limits["concurrency"]),
            ),
        }, None

    @app.get("/api/v1/earnings-scan")
    def api_earnings_scan():
        """Scan one earnings day: every optionable title, every listed expiry.

        Every external call is counted and bounded (``coverage.limits``); the
        answer says how many titles the day listed, how many carried options,
        how many the scan took on and how many it never reached. A cut scan is
        ``complete: false`` with a named reason - never a silently short list.
        """
        args, fehler = _scan_args()
        if fehler is not None:
            return fehler
        day = args["day"]

        symbole, option_state, option_fetched = _option_symbols(earnings_adapter)
        try:
            antwort = earnings_adapter.get_calendar_days({DAY_TODAY: day}) or {}
        except Exception:                                    # noqa: BLE001 - classified here
            app.logger.exception("earnings scan: calendar failed for %s", day)
            return _earnings_error(
                503, "calendar_source_failed", "the earnings calendar could not be read"
            )
        tage = antwort.get("days") if isinstance(antwort, dict) else None
        tag = (tage or {}).get(day) if isinstance(tage, dict) else None
        gesamt = str((antwort.get("state") if isinstance(antwort, dict) else None) or "")
        if gesamt == "not_configured":
            # Setup fact, not an outage: the null adapter answers no days at
            # all, and "not configured" is the honest wording for that.
            return _earnings_error(
                503, "earnings_source_not_configured",
                "no earnings source is configured - there is nothing to scan",
            )
        if not isinstance(tag, dict):
            # No entry for the requested day is no answer - not an empty day.
            return _earnings_error(
                503, "calendar_unavailable",
                f"the earnings calendar did not answer for {day}",
            )
        zustand = str(tag.get("state") or gesamt or "unavailable")
        zeilen = tag.get("rows") or []
        if zustand == "not_configured":
            return _earnings_error(
                503, "earnings_source_not_configured",
                "no earnings source is configured - there is nothing to scan",
            )
        if zustand == "unavailable" or not isinstance(zeilen, list):
            return _earnings_error(
                503, "calendar_unavailable",
                f"the earnings calendar did not answer for {day}",
            )

        try:
            koerper = scan_day(
                zeilen,
                fetch_earnings=earnings_adapter.get_earnings,
                # ONE request per ticker: the adapter caches the whole chain per
                # underlying, and every expiry of it is sliced locally.
                fetch_chain=lambda symbol: broker.get_options(symbol, None),
                criteria=args["criteria"],
                strategies=args["strategies"],
                day=day,
                today=_us_today(now_fn),
                now_fn=now_fn,
                limits=args["limits"],
                optionable=symbole,
                # Diagnostics stay in the log: the answer itself carries a
                # stable code and says nothing about adapter internals.
                on_ticker_error=lambda symbol, exc: app.logger.warning(
                    "earnings scan: %s not scanned (%s: %s)",
                    symbol, type(exc).__name__, exc,
                ),
            )
        except Exception:                                    # noqa: BLE001 - classified here
            app.logger.exception("earnings scan failed for %s", day)
            return _earnings_error(503, "scan_failed", "the scan could not be completed")

        koerper["day_state"] = zustand
        koerper["day_fetched_at_utc"] = tag.get("fetched_at_utc")
        koerper["day_rows_skipped"] = int(tag.get("skipped") or 0)
        koerper["option_filter"]["state"] = option_state
        koerper["option_filter"]["fetched_at_utc"] = option_fetched
        return jsonify(koerper)

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
            tage = {
                DAY_PREV: (heute - timedelta(days=1)).isoformat(),
                DAY_TODAY: heute.isoformat(),
                DAY_NEXT: (heute + timedelta(days=1)).isoformat(),
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
                gruppen.append(dict(
                    gruppe,
                    rows=zeilen,
                    label=_CALENDAR_DAY_LABEL.get(gruppe["key"], ""),
                    date_text=_german_date(gruppe["date"]),
                    short_day=_short_day(gruppe["date"]),
                    counter=f"{len(zeilen)} von {gruppe['total']}",
                    # Tab counter: how many titles the day lists (after the
                    # option filter). A failed day has no number - "0" would
                    # read like "nothing reported".
                    tab_counter=None if zustand == "unavailable" else gruppe["total"],
                    unavailable=zustand == "unavailable",
                    stale=zustand == "stale",
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
        # The calendar is decoration: its adapter call must never break the
        # screener. `?no_calendar` is the escape hatch for tests and bookmarks
        # that only need the form. It acts by its PRESENCE - any value,
        # including the empty one a bare `?no_calendar` produces, means "off".
        # A truthiness test would read that empty spelling as "on" and spend
        # the three queries the caller just declined.
        calendar = None if "no_calendar" in request.args else _calendar_view()
        error = None
        hint = None
        results = dropped = dates = selectable = quotes = None
        vertical_kept = vertical_dropped = None
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
                    selectable = _selectable_dates(dates, criteria)
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
                                quotes, criteria, strategy=strategy, now_fn=now_fn)
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
            for v in sorted(vertical_kept, key=_vertical_sort_key(sort_mode))
        ]
        age_min = _newest_quote_age_min(lamp_source, now_fn=now_fn)
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
        # Earnings-Termin und Verfallsfenster. Only when the chain was read, so
        # the panel never claims "no expiration after earnings" for a ticker
        # whose chain was not loaded. A failure here degrades the panel, never
        # the page.
        earnings_view = None
        if ticker and dates is not None:
            earnings_view = _earnings_view(ticker, dates, offered)
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
            vertical_drops=_drop_reason_groups(vertical_dropped),
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
            earnings=earnings_view,
            calendar=calendar,
            feed_note="CBOE-Kursdaten zeitversetzt – ca. 15 Minuten hinter Echtzeit")

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


def _newest_quote_age_min(quotes, now_fn=None):
    """Age in minutes of the YOUNGEST quote in the FETCHED chain.

    (CodeRabbit finding: use the raw chain, not the screened results -
    the stale-quote guard can remove every candidate and hide the very
    staleness the lamp is supposed to report.)

    Accepts OptionQuote objects or ScreenResults (duck-typed). None/no
    rows / no readable timestamp -> None: the template shows 'keine
    Daten' instead of inventing a number without input.

    UX-1: this is the single place where the age is computed - the lamp
    colour and the minutes shown in the header come from the same value.
    """
    if not quotes:
        return None
    now = now_fn() if now_fn is not None else arrow.get()
    newest = None
    for item in quotes:
        q = getattr(item, "quote", item)  # ScreenResult or OptionQuote
        ts = getattr(q, "quote_timestamp", None)
        if ts is None:
            continue
        try:
            t = arrow.get(str(ts).replace(" ", "T"))
        except Exception:
            continue
        if newest is None or t > newest:
            newest = t
    if newest is None:
        return None
    return (now - newest).total_seconds() / 60.0


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


def _vertical_sort_key(mode):
    """Sortierschluessel der Spread-ANZEIGE (nicht der Schnittstelle).

    Unbekannte Werte (None) sortieren ans Ende, damit sie nichts verdraengen.
    Der dritte Schluessel ist das Symbol - ohne ihn waere die Reihenfolge bei
    Gleichstand von der Kettenreihenfolge abhaengig statt reproduzierbar.
    """
    def key(v):
        if mode == "none":
            return (0,)
        rendite = _spread_return_pct(v)
        abstand = _spread_be_gap_pct(v)
        if mode == "be_asc":
            return (abstand if abstand is not None else 9e9,
                    -(rendite or 0.0), v.symbol)
        return (-(rendite if rendite is not None else -1.0),
                abstand if abstand is not None else 9e9, v.symbol)
    return key


_SPREAD_TYPES = ("call", "put")


def _strategy_selection(args):
    """Slice L: which strategy a request asks for.

    `strategy` is the primary parameter; `option_type` stays the legacy alias and
    means exactly what it always meant (`call` = bull call debit, `put` = bear put
    debit). Returns (strategy, spread_type, error_code, error_text). A request that
    names both with contradicting values is an ERROR on both routes - see
    `screener.resolve_strategy` for the documented rule; nothing is guessed.
    """
    strategy, code, text = resolve_strategy(args.get("strategy"), args.get("option_type"))
    if code:
        return None, None, code, text
    return strategy, strategy_option_type(strategy), None, None


def _strategy_label(strategy):
    """The human label of a resolved strategy id; None when no strategy is known."""
    return None if strategy is None else STRATEGIES[strategy]["label"]


def _strategy_error_body(code):
    """The JSON body for a rejected strategy request.

    The two legacy refusals keep their old shape word for word so an existing
    consumer does not have to change; the new codes name themselves.
    """
    if code == "unknown_option_type":
        return {"error": "unknown spread type", "allowed": list(_SPREAD_TYPES)}
    if code == "conflict":
        return {"error": "conflicting strategy and option_type",
                "allowed": list(STRATEGY_IDS),
                "option_type_to_strategy": dict(LEGACY_OPTION_TYPES)}
    return {"error": "unknown strategy", "allowed": list(STRATEGY_IDS)}


def _sort_links(args):
    """Die drei Sortier-Adressen MIT den uebrigen Parametern.

    Ohne sie wuerde ein Klick auf einen Spaltenkopf Ticker, Verfall und alle
    Filter verlieren - die Seite fiele auf die Auswahl zurueck.
    """
    links = {}
    for mode in ("rendite_desc", "be_asc", "none"):
        paar = [(k, wert) for k, wert in args.items(multi=True) if k != "sort"]
        paar.append(("sort", mode))
        links[mode] = urlencode(paar)
    return links


def _age_within_limit(quotes, criteria, now_fn=None):
    """E3: is the newest quote inside the operator OWN age limit?

    The traffic light beside it answers a different question (general feed freshness,
    30/120 minutes by design - the CBOE feed is ~15 minutes behind by nature). With the
    limit switched off there is nothing to be inside of, hence None.
    """
    if criteria is None or criteria.max_quote_age_min is None:
        return None
    age = _newest_quote_age_min(quotes, now_fn=now_fn)
    if age is None:
        return None
    return age <= criteria.max_quote_age_min


def _feed_stamp(quotes):
    """E3: the exact feed timestamp of the newest quote (what the data says).

    Compared as text: the adapter stores ISO-ish stamps, which sort correctly.
    """
    stamps = [str(q.quote_timestamp) for q in quotes or []
              if getattr(q, "quote_timestamp", None) is not None]
    return max(stamps) if stamps else None


def _eva_quality(quotes, criteria, now_fn=None, *, ticker=None, expiration=None, snapshot_id=None):
    """EVA gate for the snapshot this request actually screened.

    The CBOE payload names no timezone and no multiplier, so both stay unproven
    and the answer is ``unknown`` with explicit reasons - never ``ok``. The
    values are read from the same ``quotes`` list the spread engine was given;
    ``snapshot_id`` is derived from that list as well, which is what makes
    "quality and maths saw one snapshot" checkable instead of promised.
    """
    jetzt = now_fn() if now_fn is not None else arrow.get()
    jetzt_dt = getattr(jetzt, "datetime", jetzt)
    return assess_source_quality(
        source_timestamp=_feed_stamp(quotes),
        # CBOE delivers naive stamps; option_chain_api reports the same fact as
        # SOURCE_TIMEZONE_UNKNOWN. Nothing here may upgrade that to UTC.
        source_timezone=None,
        fetched_at=None,
        now=jetzt_dt,
        max_age_seconds=None if criteria.max_quote_age_min is None
        else criteria.max_quote_age_min * 60,
        # No silent 100: the payload does not carry it.
        multiplier=None,
        multiplier_source="unavailable",
        snapshot_id=snapshot_id,
    )


def _lamp_state(age_min):
    """green/yellow/red from a quote age; None age -> None (no lamp)."""
    if age_min is None:
        return None
    if age_min < -FUTURE_TOLERANCE_S / 60:
        # Slice J: the youngest timestamp of the RAW chain is in the future.
        # Measured before the fix: the page said "frisch (-1440 min)" and the
        # JSON said green. Same tolerance as the stale guard - one rule.
        return "red"
    if age_min < _FRESH_GREEN_MIN:
        return "green"
    if age_min < _FRESH_YELLOW_MIN:
        return "yellow"
    return "red"


def _freshness_lamp(quotes, now_fn=None):
    """Traffic light for the fetched chain (same value as the age above)."""
    return _lamp_state(_newest_quote_age_min(quotes, now_fn=now_fn))
