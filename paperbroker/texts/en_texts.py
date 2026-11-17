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
}

#: Sections of this book that are translated completely. The structure check
#: treats every listed section as a promise: one missing key turns it red.
#: A section that is not listed falls back to German as a whole, so a
#: half-translated section is never shipped.
SECTIONS: tuple[str, ...] = ("detail", "funnel", "gap", "gap_spreads")
