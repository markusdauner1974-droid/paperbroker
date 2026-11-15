"""Closing-gap scan route, moved 1:1 out of ``server.create_app``."""

from __future__ import annotations

from datetime import timedelta

from flask import current_app, jsonify, request

from ..earnings_day_calendar import DAY_PREV, DAY_TODAY
from ..gap_scan import _counts as gap_run_counts
from ..gap_scan import fetch_gaps, reaction_day_rows, select_candidates
from ..gap_support import (
    _GAP_DEADLINE_SECONDS,
    _GAP_PUBLICATION_GRACE_DAYS,
    GAP_SCAN_MODE,
    _gap_calendar,
    _gap_day,
    _gap_scan_args,
    _gap_scan_error,
)
from ..view_support import _us_today
from .context import context


def api_gap_scan():
    """The closing gaps of one finished reaction day, as tradable candidates.

    ``day`` is the reaction session whose close is measured - not "today":
    a gap needs a finished session on both sides, so a day that has not
    closed (in New York) is a 400, not an empty answer.

    WHO is asked comes from the calendar: every title of the previous
    TRADING day (its numbers came out after that close) plus the
    PRE-MARKET titles of ``day`` itself. The previous day is resolved
    through the trading calendar, never as "one calendar day back" - that
    would be Sunday after a Monday, or a holiday, and Nasdaq answers both
    with an empty rows list (i.e. Friday's reports would vanish). The two
    halves come from two calendar days and are not merged -
    ``reaction_day_rows`` owns that rule, including the ``session_uncertain``
    doubt for a row whose session the source has already deleted.

    HOW FAR is a second source, one daily-history request per title, run in
    parallel. The adapter and its bounded cache are shared across requests
    (built once in ``create_app``), so a second scan of the same day reads
    no history again, and the run has a whole-run deadline: a title that has
    not finished by then is named ``deadline_exceeded`` in ``failures``
    rather than holding the request. Its failures are per title: a symbol
    that cannot be measured ends in ``failures`` with the code that stopped
    it, and a day where every symbol fails is still an ANSWER (200,
    ``selected: 0``) - the run happened, it just produced nothing. Only a
    calendar that cannot be read is a 503, because then there is no
    candidate set at all.

    ``max_candidates`` caps the returned list, never the measurement: all
    candidates are fetched and checked, so ``counts`` describes the whole
    day while ``candidates`` shows the biggest moves.

    ``min_price`` is the third hurdle beside the threshold and the turnover.
    The turnover is ``close * volume``, so VOLUME alone carries a penny stock
    over 5 M USD - measured on 2026-11-12, four of the 30 candidates traded
    between 0.18 and 2.22 USD, where no tradable option spread exists. The
    floor is a request parameter (``min_price=0`` switches it off) and it is
    repeated in the answer next to the other numbers of the run.
    """
    ctx = context()
    args, fehler = _gap_scan_args(ctx)
    if fehler is not None:
        return fehler
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

    quelle = ctx.gap_adapter
    try:
        gemessen = fetch_gaps(
            [zeile["symbol"] for zeile in kandidatenzeilen], reaction, adapter=quelle,
            deadline_seconds=_GAP_DEADLINE_SECONDS,
        )
    except Exception:  # noqa: BLE001 - a broken pool must not become an HTML 500
        # Per-symbol failures never reach this line: ``fetch_gaps`` turns
        # each of them into a code. Only the run itself can break here.
        current_app.logger.exception("gap scan: measurement failed for %s", day)
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
    # One definition of the numbers (``gap_scan._counts``), so this answer
    # and ``scan_gaps`` can never disagree about what "selected" counts.
    zaehler = gap_run_counts(
        kandidatenzeilen,
        gemessen.rows,
        ausgewaehlt,
        gemessen.failures,
        args["gap_threshold_pct"],
        args["min_turnover_usd"],
        args["min_price"],
    )

    # A day whose closes are not published yet is NOT a quiet day. Measured
    # on 2026-11-12 17:00 ET: the calendar listed 214 titles for the running
    # day, the daily table carried no bar for it yet, and the answer was a
    # silent ``selected: 0``. The request is valid and the run happened, so
    # the status stays 200 - but the reason is a fact about the SOURCE, and
    # it is named here so nobody reads it as "nothing moved".
    #
    # Only a day that was ASKED and answered without the reaction bar counts:
    # a run whose titles all failed with a source error (``http_error``,
    # ``deadline_exceeded``, ...) is an outage, not a late publication, and
    # the per-symbol codes in ``failures`` already name it.
    #
    # "Not published yet" is only a believable claim while the publication
    # can still run: the daily table fills in late on the session's own day,
    # so a day inside the last few days is plausibly still late. An older
    # day is something else - this run asked for it and the source answered
    # no row for it - and the answer says exactly that, without inventing a
    # publication that may never come. The window is measured from the
    # CLOCK, not from the asked window, because it is a claim about time.
    result_state = "ok"
    if zaehler["candidate_rows"] > 0 and zaehler["gaps_computed"] == 0:
        codes = set(gemessen.failures.values())
        if not codes or codes == {"no_reaction_bar"}:
            frisch_bis = _us_today(ctx.now_fn) - timedelta(days=_GAP_PUBLICATION_GRACE_DAYS)
            result_state = (
                "history_not_published" if reaction >= frisch_bis
                else "day_without_bars"
            )

    nach_symbol = {zeile["symbol"]: zeile for zeile in kandidatenzeilen}
    kandidaten = []
    for kandidat in ausgewaehlt:
        zeile = nach_symbol.get(kandidat.symbol) or {}
        kandidaten.append({
            "symbol": kandidat.symbol,
            "name": zeile.get("name"),
            "session": zeile.get("session"),
            "session_uncertain": bool(zeile.get("session_uncertain")),
            "earnings_date": zeile.get("earnings_date"),
            "gap_pct": kandidat.gap_pct,
            "direction": kandidat.direction,
            "strategy": kandidat.strategy,
            "close": kandidat.close,
            "volume": kandidat.volume,
            "turnover_usd": kandidat.turnover_usd,
        })

    return jsonify(
        mode=GAP_SCAN_MODE,
        day=day,
        # The reaction day's OWN state, passed through - never upgraded to
        # "live": a stale calendar answers 200 with ``day_state: stale``,
        # because the rows are old, not because they are missing.
        day_state=gelesen[DAY_TODAY]["state"],
        # ``ok``, ``history_not_published`` or ``day_without_bars``: the
        # counts can be zero for three very different reasons, and the
        # answer must not leave the reader to guess which one it is.
        result_state=result_state,
        threshold_pct=args["gap_threshold_pct"],
        min_turnover_usd=args["min_turnover_usd"],
        max_candidates=args["max_candidates"],
        min_price=args["min_price"],
        counts=zaehler,
        candidates=kandidaten,
        failures=dict(gemessen.failures),
    )



def register(app) -> None:
    app.add_url_rule("/api/v1/gap-scan", endpoint="api_gap_scan",
                     view_func=api_gap_scan, methods=["GET"])
