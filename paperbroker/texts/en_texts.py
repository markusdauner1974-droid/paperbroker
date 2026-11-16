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
}

#: Sections of this book that are translated completely. The structure check
#: treats every listed section as a promise: one missing key turns it red.
#: A section that is not listed falls back to German as a whole, so a
#: half-translated section is never shipped.
SECTIONS: tuple[str, ...] = ("detail",)
