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
IDENTICAL_KEYS: tuple[str, ...] = ()

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
    "R/R": "risk to reward, a ratio label and not a sentence",
    "Delta": "greek name, never translated",
    "Long": "leg side; 'Kauf' would name the action instead of the side",
    "Short": "leg side; 'Verkauf' would name the action instead of the side",
}
