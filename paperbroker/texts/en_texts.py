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

TEXTS: dict[str, object] = {}

#: Sections of this book that are translated completely. The structure check
#: treats every listed section as a promise: one missing key turns it red.
#: A section that is not listed falls back to German as a whole, so a
#: half-translated section is never shipped.
SECTIONS: tuple[str, ...] = ()
