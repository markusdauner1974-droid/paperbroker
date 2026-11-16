"""The ONE place that knows which languages exist.

Adding a language is one entry here plus one book module. Nothing else in the
code base may name a language code: lookups, the structure check and the URL
parameter all read this registry. If a later step needs a third language, it is
one entry below and one ``*_texts.py`` file - no helper, no test and no view may
have to know the new code.

The plural selection rule of a language lives here too, because it is a property
of the language and not of the text. German and English share the trivial rule:
exactly one value takes ``one``, everything else takes ``other``. A three-to-six
form rule (Russian, Polish, Czech) is deliberately NOT built - it will be
written when a language actually needs it, and then it is one entry in
``PLURAL_RULES`` plus one line in the language entry, nothing else.

``switch_to`` is the language the switcher offers as the alternative. The
fallback book has no ``switch_to``: a switcher needs a target that differs from
the current language, and the source book is what the page shows by default.
"""

from __future__ import annotations

LANGUAGES: dict[str, dict[str, object]] = {
    "de": {
        "name": "Deutsch",
        # the source book: every key has to exist here
        "book": "paperbroker.texts.de_texts",
        "plural": "one_other",
        "switch_to": None,
    },
    "en": {
        "name": "English",
        "book": "paperbroker.texts.en_texts",
        "plural": "one_other",
        "switch_to": "de",
    },
}

#: The language a request gets when it names none.
DEFAULT_LANGUAGE = "de"

#: The book every missing key falls back to. Never a crash, never an empty
#: string - a missing translation shows the source text instead.
FALLBACK_LANGUAGE = "de"

#: Rule name -> branch selector. A branch is selected by the VALUE of ``n``;
#: a text without ``n`` never takes a branch, it is a plain string.
PLURAL_RULES = {
    "one_other": lambda count: "one" if count == 1 else "other",
}
