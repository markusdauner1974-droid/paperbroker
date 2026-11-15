"""Screener page JSON and the unfiltered option-chain view.

Moved 1:1 out of ``server.create_app``. The only edits inside the bodies are the
three names that used to come from the closure:

- ``broker``, ``screener`` and ``now_fn`` are read from the factory context;
- ``app.logger`` is ``current_app.logger`` (same logger, via the app);
- ``_trend_for_screener`` is reached through the context, because that helper
  stays in ``server`` until its two test patch targets move with it.

Rules, endpoints, status codes and payloads are unchanged.
"""

from __future__ import annotations

from flask import current_app, jsonify, request

from ..eva_quality import snapshot_identity
from ..screener import screen, vertical_spreads
from ..view_support import (
    _DATE_RE,
    _TICKER_RE,
    _age_within_limit,
    _criteria_echo,
    _data_state,
    _drop_reason_groups,
    _eva_quality,
    _exclusion_blocks,
    _feed_stamp,
    _freshness_lamp,
    _newest_quote_age_min,
    _pair_search_block,
    _parse_args,
    _strategy_error_body,
    _strategy_label,
    _strategy_selection,
)
from .context import context


def api_screen():
    ctx = context()
    ticker, expiration, dates, selectable, criteria, unknown = _parse_args(
        ctx.broker, ctx.screener, ctx.now_fn)
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
                       # no chain was read: the block is present and empty,
                       # so a consumer never needs a key check
                       data_state=_data_state(None, criteria, ctx.now_fn),
                       # the chosen search is a property of the request, not
                       # of a chain - so it is known even at the picker stage
                       pair_search=_pair_search_block(criteria),
                       exclusions=[],
                       # no snapshot was screened, so there is nothing to
                       # certify - the key is present but empty
                       eva_quality=None)
    # D9b: the JSON endpoint must answer JSON. An unknown symbol is the caller
    # mistake (404), a feed outage is nobodys (503) - the HTML error page the
    # framework produced broke every consumer that parses the body.
    try:
        quotes = ctx.broker.get_options(ticker, expiration)
    except Exception as exc:                        # noqa: BLE001 - classified below
        from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
            CboeNotFoundError,
            CboeUnusableChainError,
        )
        if isinstance(exc, CboeNotFoundError):
            return jsonify(error="unknown symbol", ticker=ticker), 404
        current_app.logger.exception("api_screen: market data failed for %s %s", ticker, expiration)
        # A chain the feed delivered but no contract of it could be built is
        # NOT an unknown symbol: the source knows the ticker and answered.
        # It is a source-side failure (503) with its own wording.
        if isinstance(exc, CboeUnusableChainError):
            return jsonify(error="chain unreadable", ticker=ticker), 503
        return jsonify(error="market data unavailable"), 503
    trend_signal = ctx.trend_for_screener(ticker)
    results, dropped = screen(quotes, criteria, trend=trend_signal,
                              now_fn=ctx.now_fn)
    # The search report is filled by the pairing itself: what the chosen
    # mode really enumerated, and whether the ceiling cut it.
    pair_coverage = {}
    vertical_kept, vertical_dropped = vertical_spreads(quotes, criteria,
                                                       strategy=strategy,
                                                       now_fn=ctx.now_fn,
                                                       coverage=pair_coverage)
    # One identity for the fetch. Both the gate and every vertical entry
    # carry it, so a consumer can prove they refer to the same snapshot.
    snapshot_id = snapshot_identity(ticker, expiration, quotes, _feed_stamp(quotes))
    eva = _eva_quality(quotes, criteria, ctx.now_fn,
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
        freshness=_freshness_lamp(quotes, now_fn=ctx.now_fn),
        # E3: the operator own limit gets its own named line.
        freshness_age=_newest_quote_age_min(quotes, now_fn=ctx.now_fn),
        freshness_limit=criteria.max_quote_age_min,
        age_within_limit=_age_within_limit(quotes, criteria, now_fn=ctx.now_fn),
        feed_stamp=_feed_stamp(quotes),
        # The mode, the data state and the provisional marker. Same object
        # the page renders - one rule, two surfaces.
        data_state=_data_state(quotes, criteria, ctx.now_fn),
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
        # How the pairs were searched, and why something was excluded -
        # counted per unit, never summed across units.
        pair_search=_pair_search_block(criteria, pair_coverage),
        exclusions=_exclusion_blocks(dropped, vertical_dropped, criteria),
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


def api_option_chain():
    ctx = context()
    from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
        CboeNotFoundError,
        CboeRequestError,
        CboeUnusableChainError,
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
        snapshot = ctx.broker.get_option_chain_snapshot(ticker)
    except CboeNotFoundError:
        return _chain_error(404, "unknown_symbol", "the source does not know this symbol")
    # Order matters: CboeUnusableChainError IS a CboeRequestError, so it must
    # be caught first. The source delivered the chain - "unknown symbol"
    # would be a false statement about the source.
    except CboeUnusableChainError:
        current_app.logger.warning("api_option_chain: chain unusable for %s", ticker)
        return _chain_error(
            503,
            "chain_unreadable",
            "the source delivered the chain, but no contract of it could be read",
        )
    except CboeRequestError:
        current_app.logger.warning("api_option_chain: market data failed for %s", ticker)
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
            now_fn=lambda: ctx.now_fn().to("utc").datetime,
        )
    except Exception:                                    # noqa: BLE001 - classified below
        current_app.logger.exception("api_option_chain: serialisation failed for %s", ticker)
        return _chain_error(
            503, "market_data_unavailable", "the snapshot could not be processed"
        )
    return jsonify(koerper)

def register(app) -> None:
    app.add_url_rule("/api/screen", endpoint="api_screen", view_func=api_screen,
                     methods=["GET"])
    app.add_url_rule("/api/v1/option-chain", endpoint="api_option_chain",
                     view_func=api_option_chain, methods=["GET"])
