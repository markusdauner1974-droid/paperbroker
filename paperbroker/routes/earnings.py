"""Earnings routes, moved 1:1 out of ``server.create_app``.

Same three source reads as before (``earnings_adapter``, the chain snapshot for
the window route, the clock). The helpers that closed over the factory take the
context explicitly now (``_earnings_snapshot(ticker, ctx)``, ``_scan_args(ctx)``),
and ``app.logger`` became ``current_app.logger`` - the same logger.

Rules, endpoints, status codes and payloads are unchanged.
"""

from __future__ import annotations

import math
from datetime import datetime

from flask import current_app, jsonify, request

from ..earnings_calendar import combine_earnings_and_expirations
from ..earnings_day_calendar import DAY_TODAY
from ..earnings_scan import (
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
from ..screener import STRATEGIES, resolve_strategy
from ..view_support import (
    _CRITERIA_FIELDS,
    _DATE_RE,
    _SCAN_LIMIT_PARAMS,
    _TICKER_RE,
    _calendar_failure,
    _criteria_from_request,
    _option_symbols,
    _scan_limit,
    _strategy_error_body,
    _us_today,
)
from .context import context


def _earnings_error(status, code, message, *, source_code=None, source_status=None):
    from paperbroker.earnings_calendar import SCHEMA_VERSION as _EARNINGS_SCHEMA

    fehler = {"code": code, "message": message}
    # The upstream code and status stay separate fields: the route's own
    # code says WHAT this endpoint could not do, these two say what the
    # source did. Absent when there is nothing to report.
    if source_code:
        fehler["source_code"] = source_code
    if source_status is not None:
        fehler["http_status"] = source_status
    return (
        jsonify(schema_version=_EARNINGS_SCHEMA, error=fehler),
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



def _earnings_snapshot(ticker, ctx):
    """Adapter call with one place that turns a crash into a JSON error."""
    try:
        return ctx.earnings_adapter.get_earnings(ticker), None
    except Exception:                                    # noqa: BLE001 - classified below
        current_app.logger.exception("earnings lookup failed for %s", ticker)
        return None, _earnings_error(
            503, "earnings_source_failed", "the earnings source could not be read"
        )




def _scan_args(ctx):
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
        ctx.screener.criteria, request.args
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



def api_earnings():
    ctx = context()
    ticker, fehler = _earnings_args()
    if fehler is not None:
        return fehler
    koerper, fehler = _earnings_snapshot(ticker, ctx)
    if fehler is not None:
        return fehler
    return jsonify(koerper)



def api_earnings_window():
    ctx = context()
    from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
        CboeNotFoundError,
        CboeRequestError,
        CboeUnusableChainError,
    )
    from paperbroker.option_chain_api import listed_expirations

    ticker, fehler = _earnings_args()
    if fehler is not None:
        return fehler
    koerper, fehler = _earnings_snapshot(ticker, ctx)
    if fehler is not None or koerper is None:
        return fehler

    # The expirations come from the SAME snapshot the option-chain route
    # serves. A chain that cannot be read is reported as such: expirations
    # stay unknown and the decision blocks - it never falls back to a
    # guessed date.
    verfaelle, ketten_status = None, "ok"
    try:
        snapshot = ctx.broker.get_option_chain_snapshot(ticker)
        verfaelle = listed_expirations(snapshot)
    except CboeNotFoundError:
        ketten_status = "unknown_symbol"
    # The source knew the ticker and answered - only the mapping failed.
    # Caught before CboeRequestError, of which it is a subclass.
    except CboeUnusableChainError:
        ketten_status = "chain_unreadable"
    except CboeRequestError:
        ketten_status = "market_data_unavailable"
    except Exception:                                    # noqa: BLE001 - classified here
        current_app.logger.exception("earnings window: chain failed for %s", ticker)
        ketten_status = "market_data_unavailable"

    fenster = combine_earnings_and_expirations(
        koerper["earnings"], verfaelle, today=_us_today(ctx.now_fn)
    )
    antwort = dict(fenster)
    antwort["ticker"] = ticker
    antwort["chain"] = {
        "status": ketten_status,
        "expirations_read": verfaelle is not None,
    }
    return jsonify(antwort)



def api_earnings_scan():
    ctx = context()
    """Scan one earnings day: every optionable title, every listed expiry.

    Every external call is counted and bounded (``coverage.limits``); the
    answer says how many titles the day listed, how many carried options,
    how many the scan took on and how many it never reached. A cut scan is
    ``complete: false`` with a named reason - never a silently short list.
    """
    args, fehler = _scan_args(ctx)
    if fehler is not None:
        return fehler
    day = args["day"]

    symbole, option_state, option_fetched = _option_symbols(ctx.earnings_adapter)
    try:
        antwort = ctx.earnings_adapter.get_calendar_days({DAY_TODAY: day}) or {}
    except Exception:                                    # noqa: BLE001 - classified here
        current_app.logger.exception("earnings scan: calendar failed for %s", day)
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
        quell_code, quell_status, message = _calendar_failure(
            antwort.get("error") if isinstance(antwort, dict) else None, day
        )
        return _earnings_error(
            503, "calendar_unavailable", message,
            source_code=quell_code, source_status=quell_status,
        )
    zustand = str(tag.get("state") or gesamt or "unavailable")
    zeilen = tag.get("rows") or []
    if zustand == "not_configured":
        return _earnings_error(
            503, "earnings_source_not_configured",
            "no earnings source is configured - there is nothing to scan",
        )
    if zustand == "unavailable" or not isinstance(zeilen, list):
        # The day failed. The message must name what the source DID: a 403
        # or 429 is an answer, and "did not answer" would hide the reason.
        quell_code, quell_status, message = _calendar_failure(tag.get("error"), day)
        return _earnings_error(
            503, "calendar_unavailable", message,
            source_code=quell_code, source_status=quell_status,
        )

    try:
        koerper = scan_day(
            zeilen,
            fetch_earnings=ctx.earnings_adapter.get_earnings,
            # ONE request per ticker: the adapter caches the whole chain per
            # underlying, and every expiry of it is sliced locally.
            fetch_chain=lambda symbol: ctx.broker.get_options(symbol, None),
            criteria=args["criteria"],
            strategies=args["strategies"],
            day=day,
            # The state the cache reported for THIS day travels with the
            # rows: only a live day may turn a row into a fresh proof.
            day_state=zustand,
            today=_us_today(ctx.now_fn),
            now_fn=ctx.now_fn,
            limits=args["limits"],
            optionable=symbole,
            # Diagnostics stay in the log: the answer itself carries a
            # stable code and says nothing about adapter internals.
            on_ticker_error=lambda symbol, exc: current_app.logger.warning(
                "earnings scan: %s not scanned (%s: %s)",
                symbol, type(exc).__name__, exc,
            ),
        )
    except Exception:                                    # noqa: BLE001 - classified here
        current_app.logger.exception("earnings scan failed for %s", day)
        return _earnings_error(503, "scan_failed", "the scan could not be completed")

    koerper["day_state"] = zustand
    koerper["day_fetched_at_utc"] = tag.get("fetched_at_utc")
    koerper["day_rows_skipped"] = int(tag.get("skipped") or 0)
    koerper["option_filter"]["state"] = option_state
    koerper["option_filter"]["fetched_at_utc"] = option_fetched
    return jsonify(koerper)



def register(app) -> None:
    app.add_url_rule("/api/v1/earnings", endpoint="api_earnings",
                     view_func=api_earnings, methods=["GET"])
    app.add_url_rule("/api/v1/earnings-window", endpoint="api_earnings_window",
                     view_func=api_earnings_window, methods=["GET"])
    app.add_url_rule("/api/v1/earnings-scan", endpoint="api_earnings_scan",
                     view_func=api_earnings_scan, methods=["GET"])
