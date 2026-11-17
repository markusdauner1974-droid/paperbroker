"""The English book.

Same keys as the German book - the structure check refuses a partial book, so a
missing translation is a red test and not a German sentence on an English page.
Until a section is extracted, its keys do not exist here either: the fallback
rule then shows the German text, which is honest about what is translated.

Terms that this project does not translate (Strike, Spread, Debit, Break-even,
Bid-Ask, Open Interest, IV, delta, ...) carry the German book's text verbatim;
``glossary.py`` lists them so the equality is a decision on record and not an
accident of copy-paste.
"""

from __future__ import annotations

TEXTS: dict[str, object] = {
    # --- detail.* ----------------------------------------------------------- #
    # spread_detail.py: the six reasons a detail cannot be computed, and the four
    # notes under the chart. Numbers and dates arrive as TEXT variables; the
    # wording around them is all this section decides.
    "detail.error.no_strike": "A leg has no strike.",
    "detail.error.leg_value": "The {side} leg has no readable value for {name}.",
    "detail.error.free_spread": (
        "The spread costs nothing - the long leg's ask is not above the short "
        "leg's bid."
    ),
    "detail.error.same_strike": "Both legs share the same strike.",
    "detail.error.no_price_axis": "No quote for the price axis.",
    "detail.error.missing_day_or_expiry": "Day or expiration missing.",
    "detail.notes.pl": "P&L per 1 spread (100 shares), excluding fees.",
    "detail.notes.today_curve": (
        "Today curve: Black-Scholes with each leg's own IV (r = {rate} %, "
        "q = 0 %); expiration exact."
    ),
    "detail.notes.price_axis": (
        "Price axis {from_pct} % to {to_pct} % of spot ({source})."
    ),
    "detail.notes.no_iv": "Without a leg's IV the today curve stays empty.",
    # --- gap.notes.* -------------------------------------------------------- #
    # gap_support.gap_spreads_notes(): the method notes under the list answer.
    # Machine names (data_mode, net_return_pct, window_sessions) and the fee
    # figure travel as they are; only the sentences around them are English.
    "gap.notes.data_mode": (
        "The calculation runs on the last available market state "
        "(data_mode=last_available) - there is no age filter."
    ),
    "gap.notes.rr": (
        "R/R = net_return_pct / 100, fees included (fees=0.0 USD per position, "
        "because this repo knows no fee schedule)."
    ),
    "gap.notes.expiry": (
        "Expiration: the first listed chain STRICTLY after the next earnings "
        "date; without an earnings date or without a later chain the candidate "
        "is dropped."
    ),
    "gap.notes.strike_pair": (
        "Strike pair: every combination of the expiration, highest R/R - but only "
        "with |delta| of the long leg >= 20 and a net debit >= 0.50 USD."
    ),
    "gap.notes.dte": (
        "Time to expiration (dte) in calendar days from the scanned day, not from "
        "today."
    ),
    "gap.notes.required_move": (
        "Required move = distance from the close to the spread's break-even in "
        "percent (sign = direction); an amount up to {max_move} % is allowed. "
        "R/R still has a lower bound only, no upper bound."
    ),
    "gap.notes.hit_rate": (
        "Hit rate = share of past windows of as many trading days as the time to "
        "expiration covers (dte in calendar days, converted per row into "
        "window_sessions), in which the price reached the required move at least; "
        "measured on at most {bars} daily bars. A frequency, not a probability."
    ),
    # --- funnel.* ----------------------------------------------------------- #
    # gap_funnel.py: the variants of a row, and the reasons the funnel rejects a
    # symbol - stage by stage. {direction} carries an already-rendered word for
    # 'rising'/'falling'; the four plural entries keep the German structure
    # (one/other) because they are composed into sentences above.
    "funnel.variant.near_money": "near the money",
    "funnel.variant.at_resistance": "at resistance",
    "funnel.variant.tightest_spread": "tightest spread",
    "funnel.variant.row_pick": "Leverage (row)",
    "funnel.move.up": "rise",
    "funnel.move.down": "fall",
    "funnel.move.too_far": (
        "The price would have to {direction} {move} %; {limit} % is allowed."
    ),
    "funnel.pairs.valid": {
        "one": "{count} valid pair",
        "other": "{count} valid pairs",
    },
    "funnel.pairs.enumerated": {
        "one": "{count} enumerated pair",
        "other": "{count} enumerated pairs",
    },
    "funnel.pairs.possible": {
        "one": "{count} possible pair",
        "other": "{count} possible",
    },
    "funnel.pairs.possible_unknown": "? possible",
    "funnel.pairs.capped_title": {
        "one": "{count} symbol",
        "other": "{count} symbols",
    },
    "funnel.pairs.capped": (
        "{text} ({valid} of {enumerated}, search at the cap of {limit} - {possible})"
    ),
    "funnel.pairs.capped_note": (
        "At {title} the pair search hit the cap{ceiling} - the R/R shown there is "
        "the best pair FOUND, and \"no pair\" means \"none in the part that was "
        "checked\"; a better pair further down the enumeration was not checked."
    ),
    "funnel.chain.unreadable": "The option chain could not be read{detail}.",
    "funnel.chain.not_listed": (
        "The source lists no tradable option chain for this symbol."
    ),
    "funnel.chain.empty": "The chain of this symbol contains no contract.",
    "funnel.expiry.empty": "no contracts in this expiration",
    "funnel.expiry.none_after_earnings": (
        "No listed expiration lies after the earnings date {date}."
    ),
    "funnel.expiry.line": "Expiration {date}: {note}",
    "funnel.strike.single": "only one strike listed - no strike pair possible",
    "funnel.strike.no_pair": "no valid strike pair in the screening",
    "funnel.strike.sample": " (e.g. {sample})",
    "funnel.oi.missing": "open interest of the {leg} leg is missing",
    "funnel.oi.below": "open interest of the {leg} leg {value} < {minimum}",
    "funnel.rr.none": "no pair with R/R >= {limit}",
    "funnel.rr.delta": (
        "R/R met ({ratios} of {checked}), but |delta| of the long leg < {limit}"
    ),
    "funnel.rr.debit": (
        "R/R and delta met ({ratios} of {checked}), but net debit < {limit} USD"
    ),
    "funnel.gap.no_direction": "The gap has no direction - no debit strategy.",
    "funnel.earnings.no_symbol": "A candidate without a symbol has no earnings date.",
    "funnel.earnings.none_in_window": (
        "The source names no earnings date in the window for this symbol."
    ),
    "funnel.earnings.unreadable": "The earnings date could not be read ({error}).",
    "funnel.budget.timeout": "The check did not finish within the time budget.",
    "funnel.budget.failed": "The check failed ({error}).",
    # --- gap_spreads.* ------------------------------------------------------ #
    # routes/gap_spreads.py: the notes and the refusal reasons of the two
    # endpoints that follow the language. Codes and thresholds arrive as text
    # variables; the numbers inside them keep their own writing (see _zahl).
    "gap_spreads.note.finnhub_missing": (
        "Without FINNHUB_API_KEY no earnings date can be read - every candidate "
        "then ends at no_next_earnings."
    ),
    "gap_spreads.note.deadline": (
        "{count} candidate(s) were not finished within {seconds} s "
        "(deadline_exceeded)."
    ),
    "gap_spreads.error.invalid_symbol": "{symbol} is not a valid symbol.",
    "gap_spreads.error.incomplete_structure": (
        "expiration, long and short name ONE structure - all three or none "
        "(given: {given})."
    ),
    "gap_spreads.error.not_in_calendar": (
        "{symbol} is not in the reaction calendar on {day} - there is no row for "
        "this symbol on that day."
    ),
    "gap_spreads.error.gap_not_measurable": (
        "The gap of {symbol} could not be measured on {day} ({code})."
    ),
    "gap_spreads.error.no_candidate": (
        "{symbol} is not a candidate of the funnel on {day} (threshold "
        "{threshold} %, turnover >= {turnover} USD, price >= {price} USD)."
    ),
    "gap_spreads.error.chain_unreadable": (
        "The option chain of {symbol} cannot be read."
    ),
    "gap_spreads.error.expiration_not_listed": (
        "The expiration {expiration} is not listed for {symbol} (listed: {listed})."
    ),
    "gap_spreads.error.strike_missing": (
        "The strike {strike} is missing in the chain of {symbol} for the expiration "
        "{expiration}."
    ),
    # --- page.* ------------------------------------------------------------- #
    # templates/screen.html: the page itself. lede_before/between/after keep the
    # line break and the spaces of the German template - they wrap around numbers.
    "page.settings.summary": "Settings",
    "page.settings.language": "Language",
    "page.error.unknown_language": "Unknown language '{requested}'. Available: {available}.",
    "page.detail.summary": "Spread details",
    "page.excluded.summary": "Exclusions",
    "page.failures.summary": "Failures",
    "page.foot.feed_note": "CBOE quotes delayed – about 15 minutes behind real time.",
    "page.form.day_label": "Reaction day",
    "page.form.expand_all": "expand all",
    "page.form.heading": "Check a reaction day",
    "page.form.run": "Scan",
    "page.head.lede_after": " %.",
    "page.head.lede_before": "Earnings gap, options, expiration after the next report.\n    Allowed: R/R ≥ ",
    "page.head.lede_between": " and a required move up to\n    ",
    "page.head.title": "Gap spread funnel",
    "page.state.no_run": "No scan has run yet",
    "page.table.col.chance": "Score",
    "page.table.col.close": "Close",
    "page.table.col.dte": "DTE",
    "page.table.col.expiration": "Expiration",
    "page.table.col.gap": "Gap percent",
    "page.table.col.hit_rate": "Hit rate",
    "page.table.col.max_loss": "Max loss",
    "page.table.col.max_profit": "Max profit",
    "page.table.col.next_report": "Next earnings report",
    "page.table.col.number": "No.",
    "page.table.col.options": "Options",
    "page.table.col.required_move": "Required move %",
    "page.table.col.rr": "R/R",
    "page.table.col.strategy": "Strategy",
    "page.table.col.strikes": "Strikes (Long / Short)",
    "page.table.col.symbol": "Symbol",
    "page.table.col.volatility": "Volatility",
    # --- page_route.* ------------------------------------------------------ #
    # routes/page.py: hints, badges and refusal reasons of the legacy page answer.
    # Alt payloads stay German by contract; these values are for the switch.
    "page_route.confirmation.confirmed": "confirmed",
    "page_route.confirmation.estimated": "estimated",
    "page_route.earnings.note.days_failed": "{count} days could not be fetched",
    "page_route.earnings.note.last_state": "last known state, source is not responding",
    "page_route.earnings.note.no_source": "no earnings source configured",
    "page_route.earnings.note.one_day_failed": "one day could not be fetched",
    "page_route.earnings.note.source_silent": "source is not responding",
    "page_route.error.expiration_not_listed": "Expiration is not listed for this ticker.",
    "page_route.error.invalid_expiration": "Invalid expiration - YYYY-MM-DD.",
    "page_route.error.invalid_ticker": "Invalid ticker - 1-6 letters A-Z.",
    "page_route.error.market_data": "Market data error - details in the server log.",
    "page_route.error.source_unreadable": "Source unreadable - details in the server log.",
    "page_route.failure.invalid_json": "source returns no JSON",
    "page_route.failure.invalid_payload": "source returns unusable data",
    "page_route.failure.recent_failure": "last attempt just failed",
    "page_route.failure.source_blocked": "source blocks the request",
    "page_route.failure.source_reports": "source reports {code}",
    "page_route.failure.source_throttled": "source throttles the requests",
    "page_route.failure.source_unreachable": "source unreachable",
    "page_route.feed_note.cboe": "CBOE quotes delayed – about 15 minutes behind real time",
    "page_route.hint.unknown_params": "Unknown parameters ignored: {names} (available: {fields})",
    "page_route.option_note.hidden": "{count} symbols without listed options hidden",
    "page_route.option_note.hidden_none": "no symbol without listed options hidden",
    "page_route.option_note.stale_suffix": " (option list from the last known state)",
    "page_route.option_note.unavailable": "option list not available - unfiltered",
    "page_route.session.after": "after hours",
    "page_route.session.before": "pre-market",
    "page_route.status.unknown_time": "time unknown",
    # --- js.* -------------------------------------------------------------- #
    # static/gap_funnel.js: the injected block. {placeholders} are filled by the
    # script itself, so their names may not change.
    "js.cell.chance.title": (
        "Score = hit rate × R/R; an upper bound of the expected value, not a promise - "
        "sorted by hit rate"
    ),
    "js.cell.close.title": "closing price of the reaction day",
    "js.cell.dte.title": "calendar days to expiration",
    "js.cell.expiration.title": "expiration after the next report",
    "js.cell.gap.title": "gap: close of the reaction day against the close before it",
    "js.cell.hit.title": (
        "Percentage of historical {sessions}-trading-day windows in which the price moved "
        "by at least the required amount (window length in trading days; adjacent DTE "
        "value in calendar days)"
    ),
    "js.cell.max_loss.title": "max loss per unit",
    "js.cell.max_profit.title": "max profit per unit",
    "js.cell.move.title": "move to the break-even of the spread, in percent of the reaction day's close",
    "js.cell.options.bid_ask_max": "Bid-Ask max {spread} %",
    "js.cell.options.bid_ask_pair": "Bid-Ask {long} % / {short} %",
    "js.cell.options.oi": "OI {long} / {short}",
    "js.cell.options.title_one_leg": (
        "open interest of both legs (long / short) · bid-ask mark-up of the worse leg in "
        "percent of its mid price"
    ),
    "js.cell.options.title_per_leg": (
        "open interest of both legs (long / short) · bid-ask mark-up in percent of the "
        "mid price, per leg"
    ),
    "js.cell.rank.many": "expand the {count} alternatives of this row",
    "js.cell.rank.one": "expand the single alternative of this row",
    "js.cell.report.title_earnings_day": "report on the reaction day: {date}",
    "js.cell.report.title_next": "next known earnings report",
    "js.cell.rr.title": "profit to loss at expiration",
    "js.cell.strikes.sub": "Long / Short",
    "js.cell.strikes.title": "Long {long} · Short {short}",
    "js.cell.volatility.day": "day {value} %",
    "js.cell.volatility.title": "volatility: annualized dispersion of daily returns; below it the mean daily move",
    "js.cell.windows": "n={count}",
    "js.cell.windows_sessions": "{count} TD",
    "js.chart.aria": "P&L of {symbol}: solid today, dashed at expiration",
    "js.chart.aria.greek": "{greek} as an additional dashed line",
    "js.chart.axis.greek.delta": "Delta",
    "js.chart.axis.greek.gamma": "Gamma",
    "js.chart.axis.greek.theta": "Theta",
    "js.chart.axis.greek.vega": "Vega",
    "js.chart.axis.pl": "P&L (USD per spread)",
    "js.chart.axis.price": "Price",
    "js.chart.desc": (
        "Solid line: today's model-based P&L. Dashed line: P&L at expiration. Vertical "
        "line: price {spot}. Horizontal axis: price. Vertical axis: P&L in US dollars per "
        "spread."
    ),
    "js.chart.legend.expiry": "at expiration",
    "js.chart.legend.greek": "{greek} (model)",
    "js.chart.legend.today": "today (model)",
    "js.chart.spot": "Spot {value}",
    "js.chart.title": "P&L per spread against the price — today and at expiration",
    "js.counts.candidates": {
        "one": "{n} candidate",
        "other": "{n} candidates",
    },
    "js.counts.expiry": "{n} with a matching expiration",
    "js.counts.gaps": {
        "one": "{n} gap measured",
        "other": "{n} gaps measured",
    },
    "js.counts.move": "{n} with a required move up to {cap} %",
    "js.counts.next_earnings": "{n} with a next earnings date",
    "js.counts.rr": "{n} with R/R at least {rr}",
    "js.counts.shown": "{n} shown",
    "js.counts.turnover": "{n} above the turnover hurdle",
    "js.detail.chain_unavailable": "The option chain of this symbol cannot be read.",
    "js.detail.chart_heading": "P&L against the price — today and at expiration",
    "js.detail.expiry_label": "Expiration {expiration}",
    "js.detail.failed": "Detail for {symbol} failed: {message}",
    "js.detail.leg_not_listed": "One leg of this spread is not in the chain.",
    "js.detail.legs_heading": "Both legs",
    "js.detail.loading": "Detail for {symbol} — loading …",
    "js.detail.method": "Method: {sentence}",
    "js.detail.no_candidate": "This symbol is not a candidate of the funnel on this day.",
    "js.detail.no_expiry": "No listed expiration lies after the next report.",
    "js.detail.no_next_earnings": "No next earnings date is known for this symbol.",
    "js.detail.scenarios_heading": "Scenarios",
    "js.detail.subtitle": "{strategy} · Expiration {expiration}",
    "js.detail.summary": "Spread details",
    "js.detail.title": "Detail for {symbol}",
    "js.detail.unknown_symbol": "There is no row for this symbol on this day.",
    "js.direction.down": "down",
    "js.direction.unknown": "unknown",
    "js.direction.up": "up",
    "js.empty.no_candidate": "No candidate — {parts}.",
    "js.error.invalid_day": "No valid date — please give a reaction day in the format YYYY-MM-DD.",
    "js.error.not_json": "The answer was not JSON.",
    "js.error.run_failed": "Run for {day} failed: {message}",
    "js.expand.close": "collapse all",
    "js.expand.open": "expand all",
    "js.figures.breakeven": "Break-even",
    "js.figures.debit": "Debit",
    "js.figures.hit_rate": "Hit rate",
    "js.figures.max_loss": "Max loss",
    "js.figures.max_profit": "Max profit",
    "js.figures.required_move": "Required move",
    "js.figures.rr": "R/R",
    "js.figures.volatility": "Volatility",
    "js.foot.chance": (
        "Score = hit rate × R/R; because the hit rate measures reaching the break-even, "
        "this is an upper bound of the expected value, not a promise. Sorted by hit rate."
    ),
    "js.foot.chance_method": (
        "Score = hit rate × R/R; because the hit rate measures reaching the break-even, "
        "this is an upper bound of the expected value, not a promise. Still sorted by hit "
        "rate."
    ),
    "js.foot.frequency": "A frequency, not a probability.",
    "js.foot.hit_sentence": (
        "Hit rate = share of past windows of {sessions} trading days (≈ {calendar} "
        "calendar days to expiration) in which the price moved by at least the required "
        "amount; measured on {windows} windows from at most 520 daily bars."
    ),
    "js.greeks.delta": "Delta",
    "js.greeks.gamma": "Gamma",
    "js.greeks.label": "Greeks over the price",
    "js.greeks.theta": "Theta",
    "js.greeks.vega": "Vega",
    "js.history.source_failed": "source unreadable",
    "js.history.too_short": "too little history",
    "js.legs.col.ask": "Ask",
    "js.legs.col.bid": "Bid",
    "js.legs.col.delta": "Delta",
    "js.legs.col.iv": "IV",
    "js.legs.col.mid": "Mid",
    "js.legs.col.oi": "OI",
    "js.legs.col.side": "Side",
    "js.legs.col.strike": "Strike",
    "js.legs.side.long": "Long",
    "js.legs.side.short": "Short",
    "js.liquidity.full": "fully liquid",
    "js.liquidity.thin": "thin",
    "js.list.excluded": "Exclusions",
    "js.list.failures": "Failures",
    "js.list.summary": "{label} ({count})",
    "js.move.fall": "fall",
    "js.move.rise": "rise",
    "js.plural.excluded": {
        "one": "{n} exclusion",
        "other": "{n} exclusions",
    },
    "js.plural.failure": {
        "one": "{n} failure",
        "other": "{n} failures",
    },
    "js.plural.rows": {
        "one": "{n} row",
        "other": "{n} rows",
    },
    "js.reason.chain_unavailable": "option chain unreadable",
    "js.reason.no_expiry_after_earnings": "no expiration after the next report",
    "js.reason.no_next_earnings": "no next earnings date known",
    "js.reason.no_spread_ge_rr": "no spread with R/R at least {rr}",
    "js.reason.not_optionable": "no listed options",
    "js.reason.unknown": "reason unknown",
    "js.rr_hurdle.fallback": "1 : 2",
    "js.scenarios.col.change": "Change (%)",
    "js.scenarios.col.delta": "Delta",
    "js.scenarios.col.gamma": "Gamma",
    "js.scenarios.col.pl": "P&L",
    "js.scenarios.col.theta": "Theta",
    "js.scenarios.col.underlying": "Underlying",
    "js.scenarios.col.vega": "Vega",
    "js.stale": (
        "Calendar data is outdated — these rows are based on an older version of the "
        "calendar and may still change."
    ),
    "js.state.failed": "Run failed",
    "js.state.not_started": "No run started",
    "js.state.ready": "As of {clock} · {rows}",
    "js.state.running": "Run for {day} … {seconds} s",
    "js.strategy.bear_put_debit": "Bear Put",
    "js.strategy.bull_call_debit": "Bull Call",
    # --- support.* ---------------------------------------------------------- #
    # view_support.py: parser refusals - the field name arrives as {field}.
    "support.parse.choice": "{field}: one of {allowed} expected (got: '{text}').",
    "support.parse.comma": "{field}: please use the dot as decimal separator (e.g. 7.5 instead of '{text}').",
    "support.parse.finite": "{field}: '{text}' is not a finite number.",
    "support.parse.flag": "{field}: '0' or '1' expected (got: '{text}').",
    "support.parse.integer": "{field}: '{text}' is not an integer.",
    "support.parse.number": "{field}: '{text}' is not a number.",
    # --- trend.* ------------------------------------------------------------ #
    "trend.unavailable.label": "Trend not available",
    "trend.unavailable.note": "Trend source unreachable - the list is displayed without a trend check.",
    # --- sort.* ------------------------------------------------------------- #
    "sort.be_asc": "BE distance (nearest first)",
    "sort.max_loss_asc": "Max loss (smallest first)",
    "sort.net_profit_desc": "Net profit potential (after costs)",
    "sort.net_ratio_desc": "Net payout ratio (after costs)",
    "sort.none": "Chain order",
    "sort.rendite_desc": "Gross return (max. profit / max. loss)",
    "earnings.reason.date_confirmation_unknown": "The date is neither confirmed nor documented as an estimate.",
    "earnings.reason.earnings_data_invalid": "The earnings answer is unusable or contradicts itself.",
    "earnings.reason.earnings_data_stale": "The earnings evidence is too old - it may not clear a trade.",
    "earnings.reason.earnings_date_in_past": "The earnings date lies in the past.",
    "earnings.reason.earnings_date_unknown": "No next earnings date known.",
    "earnings.reason.earnings_source_not_configured": "No earnings source configured - nothing is estimated.",
    "earnings.reason.earnings_source_unavailable": "The earnings source is unreachable and no cache entry exists.",
    "earnings.reason.earnings_status_unknown": (
        "The review status of the earnings entry is unknown - no clearance without "
        "documented status."
    ),
    "earnings.reason.earnings_status_unrecognised": "The review status is an unknown value - no clearance.",
    "earnings.reason.earnings_today": "The report is today — an entry before the date is no longer possible.",
    "earnings.reason.exit_day_in_past": "The planned exit day lies in the past.",
    "earnings.reason.exit_day_not_resolvable": "No US trading day can be determined before the earnings date.",
    "earnings.reason.expiration_not_after_earnings": "The expiration does not lie after the earnings date.",
    "earnings.reason.expirations_unavailable": "The listed expiration dates could not be read.",
    "earnings.reason.no_expirations_listed": "The option chain names no expiration date.",
    "earnings.reason.no_listed_expiration_after_earnings": "No listed expiration lies after the earnings date.",
    # --- exclusion.* -------------------------------------------------------- #
    # exclusions.py: the composed refusals. The three sentences that used to live
    # in the screener as f-strings carry the SAME words in German, so the German
    # answer stays byte-identical.
    "exclusion.category.age": "Data age",
    "exclusion.category.budget": "Budget",
    "exclusion.category.costs": "Costs",
    "exclusion.category.data": "missing data",
    "exclusion.category.dte": "Maturity",
    "exclusion.category.liquidity": "Liquidity",
    "exclusion.category.other": "other",
    "exclusion.category.price": "invalid debit/credit",
    "exclusion.category.scan_limit": "scan limit (budget/time)",
    "exclusion.category.volatility": "Volatility",
    "exclusion.headline": "{unit}: {counted} excluded - {parts} (one reason per {unit_one}: the first)",
    "exclusion.note.first_reason": (
        "Exactly one reason counts per {unit}: the first filter that rejected it. Further "
        "reasons that also apply are not counted and therefore not claimed."
    ),
    "exclusion.note.worst_state": (
        "Exactly one state counts per {unit} - the most severe that applies. Further "
        "reasons are not counted."
    ),
    "exclusion.not_checked.oi_off": "{label} (OI limit off)",
    "exclusion.not_checked.spread_off": "{label} (spread limit off)",
    "exclusion.unit.contract": "contract",
    "exclusion.unit.contracts": "contracts",
    "exclusion.unit.expiry": "Expiration",
    "exclusion.unit.expiries": "Expirations",
    "exclusion.unit.pair": "spread combination",
    "exclusion.unit.pairs": "spread combinations",
    "exclusion.unit.symbol": "stock",
    "exclusion.unit.symbols": "stocks",
    "exclusion.dte_outside": "Expiration: maturity {days} days outside [{low}, {high}]",
    "exclusion.credit_out_of_range": "Credit {credit} outside (0, {width}) - not a valid credit",
    "exclusion.debit_out_of_range": "Debit {debit} outside (0, {width}) - no profit possible",
    # --- scan.* ------------------------------------------------------------- #
    # earnings_scan.py: why a chain could not be read.
    "scan.chain.blocked": "The source blocks the request (HTTP 403).",
    "scan.chain.malformed": "The source's answer was unreadable.",
    "scan.chain.not_listed": "The source lists no option chain for this symbol.",
    "scan.chain.throttled": "The source throttles the request (HTTP 429) - try again later.",
    "scan.chain.unreachable": "The option chain was unreachable.",
    "scan.chain.unusable": "The chain was read, but no contract is usable.",
    "scan.chain_note.window_blocked": "not loaded (window prevented)",
    # --- earnings.* --------------------------------------------------------- #
    # earnings_calendar.py: the rule and the notes of the earnings window. The
    # verdicts (earnings.reason.*) stay German - they belong to the legacy payload.
    "earnings.exit_rule": "last US trading day strictly before the earnings date",
    "earnings.notes.estimated": "The date is the source's estimate, not a confirmed announcement.",
    "earnings.notes.first_expiry": "First listed expiration after the earnings date: {date}.",
    "earnings.notes.necessary_not_sufficient": "The expiration after earnings is a necessary condition, not a sufficient one.",
    "earnings.notes.session_unknown": (
        "Publication time unknown - the exit day is therefore the last trading day before "
        "it."
    ),
    "earnings.parse.no_date": "no MM/DD/YYYY date in the source text",
    # --- funnel.pairs.of ---------------------------------------------------- #
    "funnel.pairs.of": "{valid} of {enumerated}",
}

#: Sections of this book that are translated completely. The structure check
#: treats every listed section as a promise: one missing key turns it red.
#: A section that is not listed falls back to German as a whole, so a
#: half-translated section is never shipped.
SECTIONS: tuple[str, ...] = (
    "detail",
    "earnings",
    "exclusion",
    "funnel",
    "gap",
    "gap_spreads",
    "js",
    "page",
    "page_route",
    "scan",
    "sort",
    "support",
    "trend",
)
