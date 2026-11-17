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
}

#: Sections of this book that are translated completely. The structure check
#: treats every listed section as a promise: one missing key turns it red.
#: A section that is not listed falls back to German as a whole, so a
#: half-translated section is never shipped.
SECTIONS: tuple[str, ...] = ("detail", "gap")
