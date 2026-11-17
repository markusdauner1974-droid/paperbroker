"""Financial terms this project keeps identical in every language.

A term that is used untranslated in German - Strike, Spread, Debit, Break-even,
Bid-Ask, Open Interest, IV, delta, R/R - must read the same in the English book.
That is a decision about words, so it is written down here and checked, instead
of being an accident of copy-paste.

``IDENTICAL_KEYS`` lists the catalogue keys that have to carry one and the same
value in every book. ``TERMS`` is the reference used when a text is drafted or
reviewed: it says which word is left alone and why.
"""

from __future__ import annotations

#: Catalogue keys whose value must be byte-identical in every book.
#: Filled while the sections are extracted - an entry without a reason in
#: ``TERMS`` is a mistake, not a shortcut.
#:
#: NOT in here, on purpose: ``page.table.col.strategy`` ("Strategie"). The HEADER
#: is a word and becomes "Strategy"; what stays identical are the strategy NAMES
#: below (``js.strategy.*``). Listing the header would have shipped German in an
#: English table.
IDENTICAL_KEYS: tuple[str, ...] = (
    # table headers that ARE the term
    "page.table.col.rr",
    "page.table.col.strikes",
    # the leg table of the JavaScript figure
    "js.legs.col.strike",
    "js.legs.col.bid",
    "js.legs.col.ask",
    "js.legs.col.iv",
    "js.legs.col.oi",
    "js.legs.col.delta",
    "js.legs.col.mid",
    "js.legs.side.long",
    "js.legs.side.short",
    # the figure labels that name a term
    "js.figures.debit",
    "js.figures.breakeven",
    "js.figures.rr",
    # the product names
    "js.strategy.bull_call_debit",
    "js.strategy.bear_put_debit",
    # a ratio, not a sentence
    "js.rr_hurdle.fallback",
)

#: One concept, one English word - the owner's decision of 17.11.2026.
#: German keeps its two spellings ("maximaler Verlust" in the table, "max. Verlust"
#: in the figure); the English book may not do that, because a reader who sees two
#: words assumes two things. Keyed by the word the English book must use.
UNIFORM_TERMS: dict[str, tuple[str, ...]] = {
    "Max loss": ("page.table.col.max_loss", "js.figures.max_loss"),
    "Max profit": ("page.table.col.max_profit", "js.figures.max_profit"),
    "Volatility": ("page.table.col.volatility", "js.figures.volatility"),
}

#: The untranslated vocabulary, with the reason it stays as it is.
TERMS: dict[str, str] = {
    "Strike": "the exchange's own word for the price a contract fixes",
    "Spread": "names the product, not a range - 'Spanne' would read like a quote",
    "Debit": "the paid premium of a long vertical; 'Belastung' is ambiguous",
    "Credit": "the received premium of a short vertical",
    "Break-even": "standard on every broker ticket in both languages",
    "Bid-Ask": "the two sides of the quote; both short forms are English",
    "Open Interest": "exchange field, no German equivalent in use",
    "IV": "implied volatility, used as an abbreviation in both languages",
    "Expiration": (
        "one word for the concept, decided by the owner on 17.11.2026: 'Expiry' is "
        "refused even where a column would be narrower - a reader who meets two "
        "words assumes two things"
    ),
    "R/R": "risk to reward, a ratio label and not a sentence",
    "Delta": "greek name, never translated",
    "Bid": "the buy side of the quote - the short form is English in both books",
    "Ask": "the sell side of the quote, same reason as Bid",
    "Mid": "the midpoint of Bid and Ask, a derived field name",
    "OI": "the exchange's own abbreviation for open interest",
    "Long": "leg side; 'Kauf' would name the action instead of the side",
    "Short": "leg side; 'Verkauf' would name the action instead of the side",
    "Bull Call": "the product name a broker's ticket shows; 'Hausse-Call' is nobody's word",
    "Bear Put": "the product name, same reason as Bull Call",
    "Strikes (Long / Short)": "two terms in one header, both untranslated",
    "1 : 2": "a ratio, not a sentence - digits and a colon read the same everywhere",
}
