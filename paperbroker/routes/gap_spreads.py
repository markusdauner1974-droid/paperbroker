"""Gap spreads list, one spread's detail and their helpers.

Moved 1:1 out of ``server.create_app``. The gap helpers they use come from
``paperbroker.gap_support`` (shared with the scan route); the factory state
(gap adapter, next-earnings adapter, broker, clock) comes from the context.
"""

from __future__ import annotations

import math
from datetime import datetime

from flask import current_app, jsonify, request

from .. import gap_funnel, spread_detail
from ..earnings_day_calendar import DAY_PREV, DAY_TODAY
from ..gap_scan import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MIN_PRICE,
    DEFAULT_MIN_TURNOVER_USD,
    DEFAULT_THRESHOLD_PCT,
    fetch_gaps,
    reaction_day_rows,
    select_candidates,
)
from ..gap_scan import _counts as gap_run_counts
from ..gap_support import (
    _GAP_DEADLINE_SECONDS,
    _GAP_SPREADS_PARAMETERS,
    _GAP_SPREADS_SCHEMA_VERSION,
    _SPREAD_DETAIL_CODES,
    _SPREAD_DETAIL_SYMBOL_RE,
    _STRUCTURE_PARAMS,
    _gap_calendar,
    _gap_day,
    _gap_scan_args,
    _gap_scan_error,
    _required_move_cap,
    gap_spreads_notes,
)
from ..texts import t
from ..view_support import _same_strike
from .context import context


def api_gap_spreads():
    ctx = context()
    """The gap candidates of one reaction day, run through the option funnel.

    ``day`` is the same reaction session ``/api/v1/gap-scan`` measures, and
    the candidates are the same ones - the three gap calls are repeated here
    with the same defaults (threshold 2 %, turnover 5 M USD, price 5 USD), so
    a row of this answer is a row of that answer plus a priced spread.

    Four questions per candidate, in the order of the funnel:
    ``no_next_earnings`` (Finnhub could not name a date - including the case
    of a missing key, which is a setup fact and never a guess),
    ``no_expiry_after_earnings`` (no listed series after the report),
    ``not_optionable`` (the source lists no usable chain) and
    ``no_spread_ge_rr`` (no pair with R/R >= 2.0, |delta| of the bought leg
    >= 20 and a net debit >= 0.50 USD). A source that FAILS - a block, a
    throttle, a timeout - is not an exclusion but a ``failures`` entry with
    its class, exactly like the earnings scan: the difference between "this
    title has nothing" and "we could not ask" is the whole point of the two
    lists.

    One chain request per symbol, six in parallel, and a whole-run deadline:
    a title that has not finished by then is named ``deadline_exceeded``
    rather than holding the request. The answer is always a 200 with counts
    that describe the whole day - only a calendar that cannot be read is a
    503, because then there is no candidate set at all.

    ``max_required_move_pct`` (default 30, allowed > 0 and <= 100) bounds the
    DISTANCE from the reaction day's close to the spread's break-even, for
    calls and puts alike (``abs``). A row that needs more is excluded as
    ``move_too_far`` with both numbers on the exclusion. The reward/risk
    ratio keeps its floor and has NO ceiling - the distance is the filter,
    because a ratio says nothing about whether the move was ever seen. The
    four hurdles this run applied are named together in ``limits``
    (``rr_min``, ``min_long_delta``, ``min_net_debit``,
    ``max_required_move_pct``) - an additive object under the same
    ``schema_version`` 1.0, so the page can write its sentences from the
    numbers that actually ran instead of from a hardcoded 2:1. That
    frequency is measured per row from the daily history
    (``volatility_pct``, ``daily_move_pct``, ``hit_rate_pct``,
    ``windows_measured``, ``history_state``), after the funnel and only for
    the rows that have a spread - one extra request each.
    """
    # "lang" is accepted here and only here (plus the detail route): this answer
    # follows the language of the request, every other endpoint stays in German.
    args, fehler = _gap_scan_args(ctx, extra=(*_GAP_SPREADS_PARAMETERS, "lang"))
    if fehler is not None:
        return fehler
    max_move, fehler = _required_move_cap(request.args.get("max_required_move_pct"))
    if fehler is not None:
        return _gap_scan_error(400, "invalid_parameter", fehler)
    day = args["day"]
    reaction = args["reaction"]
    gelesen, previous, fehler = _gap_calendar(ctx, day, reaction)
    if fehler is not None:
        return fehler

    kandidatenzeilen = reaction_day_rows(
        gelesen[DAY_PREV]["rows"],
        gelesen[DAY_TODAY]["rows"],
        previous_date=previous,
        day_date=reaction,
    )
    try:
        gemessen = fetch_gaps(
            [zeile["symbol"] for zeile in kandidatenzeilen], reaction,
            adapter=ctx.gap_adapter, deadline_seconds=_GAP_DEADLINE_SECONDS,
        )
    except Exception:                                    # noqa: BLE001 - one 503, not HTML
        current_app.logger.exception("gap spreads: measurement failed for %s", day)
        return _gap_scan_error(
            503, "gap_source_failed", "the gap source could not be read"
        )

    ausgewaehlt = select_candidates(
        gemessen.rows,
        threshold_pct=args["gap_threshold_pct"],
        min_turnover_usd=args["min_turnover_usd"],
        max_candidates=args["max_candidates"],
        min_price=args["min_price"],
    )
    # The numbers of the gap stage have ONE definition (``gap_scan._counts``),
    # the same one the gap scan answers with - this route only renames them
    # into the agreed contract of THIS answer.
    basis = gap_run_counts(
        kandidatenzeilen,
        gemessen.rows,
        ausgewaehlt,
        gemessen.failures,
        args["gap_threshold_pct"],
        args["min_turnover_usd"],
        args["min_price"],
    )

    nach_symbol = {zeile["symbol"]: zeile for zeile in kandidatenzeilen}
    kandidaten = []
    for kandidat in ausgewaehlt:
        zeile = nach_symbol.get(kandidat.symbol) or {}
        kandidaten.append({
            "symbol": kandidat.symbol,
            "name": zeile.get("name"),
            "gap_pct": kandidat.gap_pct,
            "direction": kandidat.direction,
            # The strategy follows the direction of the gap; the funnel
            # restates the same table rather than trusting this field.
            "strategy": kandidat.strategy,
            "close": kandidat.close,
            "turnover_usd": kandidat.turnover_usd,
            "earnings_date": zeile.get("earnings_date"),
        })

    try:
        lauf = gap_funnel.run_funnel(
            kandidaten,
            # The adapter owns the key, the window and the 12 h cache; this
            # route only asks per symbol.
            fetch_next_earnings=ctx.next_earnings_adapter.get_next_earnings,
            # ONE request per ticker: every expiry of the chain is sliced
            # locally, exactly as the earnings scan does it.
            fetch_chain=lambda symbol: ctx.broker.get_options(symbol, None),
            day=reaction,
            now_fn=ctx.now_fn,
            max_workers=gap_funnel.DEFAULT_MAX_WORKERS,
            deadline_seconds=gap_funnel.DEFAULT_DEADLINE_SECONDS,
            rr_min=gap_funnel.DEFAULT_RR_MIN,
            min_oi=gap_funnel.DEFAULT_MIN_OI,
            # Named explicitly so the answer's ``limits`` and the run cannot
            # drift apart: what the page writes as a sentence is what ran.
            min_long_delta=gap_funnel.DEFAULT_MIN_LONG_DELTA,
            min_net_debit=gap_funnel.DEFAULT_MIN_NET_DEBIT,
            max_required_move_pct=max_move,
            # The SAME adapter the gap stage used (one bounded cache): the
            # reachability numbers need about two years of sessions, so the
            # window is opened wide and closed by the count.
            fetch_daily=ctx.gap_adapter.fetch_daily,
        )
    except Exception:                                    # noqa: BLE001 - classified here
        current_app.logger.exception("gap spreads funnel failed for %s", day)
        return _gap_scan_error(
            503, "scan_failed", "the spread scan could not be completed"
        )

    # The chain of the funnel, stage by stage: calendar rows -> gaps ->
    # threshold/turnover/price -> candidates -> report date -> chain ->
    # expiry -> qualifying spread -> shown. A title can never be counted as
    # having passed a stage it never reached.
    zaehler = {
        "calendar_rows": basis["candidate_rows"],
        "gaps_computed": basis["gaps_computed"],
        "over_threshold": basis["over_threshold"],
        "turnover_passed": basis["turnover_passed"],
        "price_passed": basis["price_passed"],
        "candidates": basis["selected"],
        "with_next_earnings": lauf.counts["with_next_earnings"],
        "with_expiry_after_earnings": lauf.counts["with_expiry_after_earnings"],
        "with_chain": lauf.counts["with_chain"],
        "with_spread_ge_rr": lauf.counts["with_spread_ge_rr"],
        "with_reachable_move": lauf.counts["with_reachable_move"],
        "shown": lauf.counts["shown"],
    }
    notes = gap_spreads_notes(max_move_pct=max_move)
    if kandidaten and not lauf.counts["with_next_earnings"] and any(
            "FINNHUB_API_KEY" in str(e.message) for e in lauf.excluded):
        notes.append(t("gap_spreads.note.finnhub_missing"))
    verspaetet = sum(1 for failure in lauf.failures
                     if failure.code == gap_funnel.CODE_DEADLINE)
    if verspaetet:
        notes.append(t("gap_spreads.note.deadline", count=str(verspaetet),
                       seconds=str(int(gap_funnel.DEFAULT_DEADLINE_SECONDS))))
    # The run's OWN notes are part of this answer, not a module secret: a
    # capped pair search makes every shown R/R "the best pair FOUND", and a
    # limitation that never reaches the JSON is a silent one. Appended last
    # (general notes, then the run's), each sentence once - the general
    # block and the run may name the same thing.
    gesehen = set(notes)
    for hinweis in lauf.notes:
        if hinweis and hinweis not in gesehen:
            gesehen.add(hinweis)
            notes.append(hinweis)

    return jsonify(
        schema_version=_GAP_SPREADS_SCHEMA_VERSION,
        day=day,
        # The reaction day's own state, passed through - never upgraded.
        day_state=gelesen[DAY_TODAY]["state"],
        threshold_pct=args["gap_threshold_pct"],
        min_turnover_usd=args["min_turnover_usd"],
        rr_min=gap_funnel.DEFAULT_RR_MIN,
        min_oi=gap_funnel.DEFAULT_MIN_OI,
        # The filter this answer actually applied - a run with a different
        # bound must be readable as such, not as the default.
        max_required_move_pct=max_move,
        # The hurdles of this run in ONE object, additive to the fields above
        # (schema_version stays 1.0): the page writes "R/R mindestens 1 : 2"
        # from ``limits.rr_min`` instead of printing a hardcoded 2 that a
        # request with another bound would contradict. ``rr_min``,
        # ``min_long_delta`` and ``min_net_debit`` are code constants today,
        # ``max_required_move_pct`` is per request - the object names all
        # four so a reader never has to know which is which.
        limits={
            "rr_min": gap_funnel.DEFAULT_RR_MIN,
            "min_long_delta": gap_funnel.DEFAULT_MIN_LONG_DELTA,
            "min_net_debit": gap_funnel.DEFAULT_MIN_NET_DEBIT,
            "max_required_move_pct": max_move,
        },
        counts=zaehler,
        results=lauf.results,
        excluded=[eintrag.as_dict() for eintrag in lauf.excluded],
        failures=[eintrag.as_dict() for eintrag in lauf.failures],
        notes=notes,
    )



def api_gap_spreads_detail():
    ctx = context()
    """One spread of the funnel's list: legs, numbers, series, scenarios.

    ``symbol`` and ``day`` are the whole request. The spread itself is
    re-derived with the SAME stages and the SAME defaults as
    ``/api/v1/gap-spreads`` (gap threshold, turnover, price, report date,
    expiry strictly after it, best pair with R/R >= 2, |delta| of the bought
    leg >= 20, net debit >= 0.50 USD), so the answer describes the row the
    page clicked and not a second opinion about the same chain.

    A missing chain, a chain that lost one of the two legs, a day that is
    no finished session and a symbol that has no row that day are all named:
    404 ``chain_unavailable`` / ``leg_not_listed`` / ``unknown_symbol`` and
    400 ``invalid_day``. There is no partial answer - half a spread would
    price as if the other half did not exist.
    """
    args, fehler = _spread_detail_args(ctx)
    if fehler is not None:
        return fehler
    symbol, day, reaction = args["symbol"], args["day"], args["reaction"]
    struktur = args.get("structure")

    gelesen, previous, fehler = _gap_calendar(ctx, day, reaction)
    if fehler is not None:
        return fehler
    # The candidate rows of that day, exactly as the list builds them: a
    # title may have reported after the PREVIOUS session, so looking only at
    # the reaction day's rows would call half the list unknown.
    kandidatenzeilen = reaction_day_rows(
        gelesen[DAY_PREV]["rows"],
        gelesen[DAY_TODAY]["rows"],
        previous_date=previous,
        day_date=reaction,
    )
    zeile = next((eintrag for eintrag in kandidatenzeilen
                  if eintrag.get("symbol") == symbol), None)
    if zeile is None:
        return _gap_scan_error(
            404, "unknown_symbol",
            t("gap_spreads.error.not_in_calendar", symbol=symbol, day=day),
        )

    try:
        gemessen = fetch_gaps([symbol], reaction, adapter=ctx.gap_adapter,
                              deadline_seconds=_GAP_DEADLINE_SECONDS)
    except Exception:                                    # noqa: BLE001 - one 503, not HTML
        current_app.logger.exception("spread detail: measurement failed for %s", symbol)
        return _gap_scan_error(
            503, "gap_source_failed", "the gap source could not be read"
        )
    code = gemessen.failures.get(symbol)
    if code is not None:
        return _gap_scan_error(
            404, "no_gap",
            t("gap_spreads.error.gap_not_measurable", symbol=symbol, day=day,
              code=code),
        )

    ausgewaehlt = select_candidates(
        gemessen.rows,
        threshold_pct=DEFAULT_THRESHOLD_PCT,
        min_turnover_usd=DEFAULT_MIN_TURNOVER_USD,
        max_candidates=DEFAULT_MAX_CANDIDATES,
        min_price=DEFAULT_MIN_PRICE,
    )
    kandidat = next((eintrag for eintrag in ausgewaehlt if eintrag.symbol == symbol), None)
    if kandidat is None:
        return _gap_scan_error(
            404, "no_candidate",
            t("gap_spreads.error.no_candidate", symbol=symbol, day=day,
              threshold=f"{DEFAULT_THRESHOLD_PCT:.1f}",
              turnover=f"{DEFAULT_MIN_TURNOVER_USD:.0f}",
              price=f"{DEFAULT_MIN_PRICE:.2f}"),
        )

    # One candidate, one chain request - memoised, because the legs are
    # looked up in the SAME chain the funnel priced.
    ketten = {}

    def _chain(ticker):
        if ticker not in ketten:
            ketten[ticker] = ctx.broker.get_options(ticker, None)
        return ketten[ticker]

    try:
        lauf = gap_funnel.run_funnel(
            [{
                "symbol": kandidat.symbol,
                "name": zeile.get("name"),
                "gap_pct": kandidat.gap_pct,
                "direction": kandidat.direction,
                "strategy": kandidat.strategy,
                "close": kandidat.close,
                "turnover_usd": kandidat.turnover_usd,
                "earnings_date": zeile.get("earnings_date"),
            }],
            fetch_next_earnings=ctx.next_earnings_adapter.get_next_earnings,
            fetch_chain=_chain,
            day=reaction,
            now_fn=ctx.now_fn,
            max_workers=1,
            deadline_seconds=gap_funnel.DEFAULT_DEADLINE_SECONDS,
            rr_min=gap_funnel.DEFAULT_RR_MIN,
            min_oi=gap_funnel.DEFAULT_MIN_OI,
            # The daily bars the list measures a row's reachability on: the
            # detail needs them for the same numbers AND to name the
            # structure's character (the resistance level and the hit rates
            # come from this one history). A failing source is a state, not
            # an error - ``measure_reachability`` reports it per row.
            fetch_daily=ctx.gap_adapter.fetch_daily,
        )
    except Exception:                                    # noqa: BLE001 - classified here
        current_app.logger.exception("spread detail: funnel failed for %s", symbol)
        return _gap_scan_error(
            503, "scan_failed", "the spread scan could not be completed"
        )

    if lauf.results:
        row = lauf.results[0]
    elif lauf.excluded:
        grund = lauf.excluded[0]
        return _gap_scan_error(
            404, _SPREAD_DETAIL_CODES.get(grund.reason, "no_spread_ge_rr"),
            grund.message,
        )
    else:
        fehlschlag = lauf.failures[0] if lauf.failures else None
        return _gap_scan_error(
            404, "chain_unavailable",
            fehlschlag.message if fehlschlag
            else t("gap_spreads.error.chain_unreadable", symbol=symbol),
        )

    chain = ketten.get(symbol) or []
    option_type = gap_funnel.strategy_option_type(row["strategy"])
    row_expiration = datetime.strptime(row["expiration"], "%Y-%m-%d").date()
    if struktur is None:
        # The pick: the expiry the funnel chose and the strikes of its row.
        expiration = row_expiration
        long_strike, short_strike = row["long_strike"], row["short_strike"]
    else:
        # A named structure: its own expiry (which has to be LISTED) and its
        # own strikes. Nothing is derived from the row here - that is the
        # whole point of naming one - but it still has to be a contract pair
        # of this chain, so half a structure cannot be priced.
        expiration = struktur["expiration"]
        listed = gap_funnel.expirations_in(chain)
        if expiration is None or expiration not in listed:
            genannt = expiration.isoformat() if expiration else "?"
            gelistet = ", ".join(day.isoformat() for day in listed) or "keiner"
            return _gap_scan_error(
                404, "expiration_not_listed",
                t("gap_spreads.error.expiration_not_listed", expiration=genannt,
                  symbol=symbol, listed=gelistet),
            )
        long_strike, short_strike = struktur["long"], struktur["short"]
    section = gap_funnel.slice_chain(chain, expiration)
    lang = _detail_leg(section, long_strike, option_type)
    kurz = _detail_leg(section, short_strike, option_type)
    if lang is None or kurz is None:
        fehlend = long_strike if lang is None else short_strike
        return _gap_scan_error(
            404, "leg_not_listed",
            t("gap_spreads.error.strike_missing", strike=str(fehlend),
              symbol=symbol, expiration=expiration.isoformat()),
        )

    # The character of THIS structure: the pick is the Hebel, a named
    # alternative keeps its name, anything else has none.
    is_row_pick = (expiration == row_expiration
                   and _same_strike(long_strike, row["long_strike"])
                   and _same_strike(short_strike, row["short_strike"]))
    structure_label = (gap_funnel.variant_label(gap_funnel.ROW_PICK_ID) if is_row_pick
                       else _structure_label(row, expiration, row_expiration,
                                             long_strike, short_strike))

    # The centre of the chart: the chain's own underlying price when it
    # carries one, else the close of the reaction day. Named in the answer,
    # because a price axis drawn around the wrong number is a lie with a
    # correct-looking shape.
    spot = None
    for quote in section:
        wert = getattr(quote, "underlying_price", None)
        if isinstance(wert, (int, float)) and math.isfinite(float(wert)) and float(wert) > 0:
            spot = float(wert)
            break
    spot_source = "chain_underlying"
    if spot is None:
        spot = row.get("close")
        spot_source = "row_close"

    try:
        detail = spread_detail.build_detail(
            symbol=symbol,
            name=row.get("name"),
            day=reaction,
            strategy=row.get("strategy"),
            strategy_label=row.get("strategy_label"),
            expiration=expiration,
            option_type=option_type,
            long_quote=lang,
            short_quote=kurz,
            spot=spot,
            spot_source=spot_source,
        )
    except spread_detail.SpreadDetailError as exc:
        return _gap_scan_error(404, exc.code, exc.message)
    # WHICH structure this answer describes, in one word: the row's pick is
    # the Hebel, a named alternative keeps its character, and a pair that is
    # listed but is neither says ``null``.
    detail["structure_label"] = structure_label
    detail["is_row_pick"] = is_row_pick
    return jsonify(detail)



def _detail_date(value):
    """``value`` as a ``date``, or ``None`` - an unparsable date is no date."""
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None



def _detail_strike(value):
    """``value`` as a finite float strike, or ``None``."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None



def _structure_label(row, expiration, row_expiration, long_strike, short_strike):
    """The character of a listed structure, or ``None`` if it has none.

    The characters are computed for the ROW's expiry (they are alternatives
    of that one chain section), so a structure of a different expiry has no
    character here and says so with ``None`` instead of a borrowed name. A
    pair that is listed but is neither the pick nor one of the three
    characters is named by ``None`` too - the answer does not invent a
    fourth character for a strike pair nobody's rule chose.
    """
    if expiration != row_expiration:
        return None
    for variant in row.get("variants") or ():
        if (_same_strike(variant.get("long_strike"), long_strike)
                and _same_strike(variant.get("short_strike"), short_strike)):
            return variant.get("label")
    return None



def _spread_detail_args(ctx):
    """``symbol``/``day``/``reaction`` (+ optional structure) - or an error.

    The route knows two parameters for the row and three that name ONE
    listed structure of the same chain: ``expiration``, ``long``, ``short``.
    Everything the list allows to move its thresholds
    (``gap_threshold_pct``, ``min_turnover_usd``, ``max_candidates``,
    ``min_price``) is NOT re-accepted here: the detail re-derives the row
    with the route's defaults, and accepting a different threshold would let
    one request describe a spread the list never showed.
    """
    erlaubt = {"day", "symbol", "lang"} | set(_STRUCTURE_PARAMS)
    unbekannt = sorted(set(request.args) - erlaubt)
    if unbekannt:
        return None, _gap_scan_error(
            400, "unknown_parameter",
            f"unknown parameter(s): {', '.join(unbekannt)}. "
            f"Allowed: day, symbol, expiration, long, short, lang",
        )
    doppelt = sorted(name for name in erlaubt if len(request.args.getlist(name)) > 1)
    if doppelt:
        return None, _gap_scan_error(
            400, "duplicate_parameter", f"duplicate parameter(s): {', '.join(doppelt)}"
        )
    day, reaction, fehler = _gap_day(ctx)
    if fehler is not None:
        return None, fehler
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not _SPREAD_DETAIL_SYMBOL_RE.match(symbol):
        # No ticker shape, no ticker: "unknown" is the honest answer, and it
        # costs neither the calendar nor the chain a request.
        return None, _gap_scan_error(
            404, "unknown_symbol",
            t("gap_spreads.error.invalid_symbol", symbol=symbol or "?"),
        )
    gegeben = [name for name in _STRUCTURE_PARAMS if name in request.args]
    if gegeben and len(gegeben) != len(_STRUCTURE_PARAMS):
        return None, _gap_scan_error(
            400, "incomplete_structure",
            t("gap_spreads.error.incomplete_structure", given=", ".join(gegeben)),
        )
    struktur = None
    if gegeben:
        struktur = {
            "expiration": _detail_date(request.args.get("expiration")),
            "long": _detail_strike(request.args.get("long")),
            "short": _detail_strike(request.args.get("short")),
        }
    return {"symbol": symbol, "day": day, "reaction": reaction,
            "structure": struktur}, None



def _detail_leg(section, strike, option_type):
    """The contract of ``section`` at ``strike`` and ``option_type``, or ``None``.

    The strike is compared at the precision the row STORES it (two
    decimals): the row carries a listed 96.70 as 96.70 and an adjusted
    15.335 as 15.34, and both must find their contract. A leg that is not in
    the chain is not invented - the caller answers 404.
    """
    if not isinstance(strike, (int, float)):
        return None
    for quote in section or ():
        asset = getattr(quote, "asset", None)
        if option_type is not None and getattr(asset, "option_type", None) != option_type:
            continue
        wert = getattr(asset, "strike", None)
        if isinstance(wert, (int, float)) and math.isfinite(float(wert)):
            if _same_strike(wert, strike):
                return quote
    return None



def register(app) -> None:
    app.add_url_rule("/api/v1/gap-spreads", endpoint="api_gap_spreads",
                     view_func=api_gap_spreads, methods=["GET"])
    app.add_url_rule("/api/v1/gap-spreads/detail", endpoint="api_gap_spreads_detail",
                     view_func=api_gap_spreads_detail, methods=["GET"])
