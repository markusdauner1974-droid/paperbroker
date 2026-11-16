"""Display texts by key, in the active language, with a safe fallback.

The catalogue has no build step and no dependency: ``de_texts.py`` and
``en_texts.py`` are plain dictionaries imported once and read at request time.
Nothing is compiled into the Docker image, nothing has to be kept in sync by a
tool - the structure check in ``paperbroker/tests/test_texts.py`` compares the
key sets of all books of the registry.

Four rules the code enforces:

* **Keys are neutral English IDs** in dotted notation, one commented section per
  family (``page.head.hurdle``, ``js.table.col.chance``). The key names the
  place, never the wording.
* **A value is either a string or a branch dict** (``{"one": ..., "other": ...}``).
  Which branch is taken is decided by the language's rule in the registry, not by
  a comparison in the view - so a language with more than two forms later needs
  no change here.
* **A missing key falls back to the source book**, and if even that lacks it the
  key itself is returned. The page then shows ``page.head.hurdle`` - visible and
  obviously wrong, which is what a broken catalogue should look like; silently
  dropping the text would look like a design decision.
* **A section a book declares must be complete.** A book lists the sections it
  has translated (``SECTIONS``); for each of them the key set has to equal the
  source book's key set exactly, otherwise the structure check is red. Sections
  that are not declared fall back to German as a whole - a half-translated
  section is never shipped.
"""

from __future__ import annotations

import importlib
from typing import Any

from .registry import DEFAULT_LANGUAGE, FALLBACK_LANGUAGE, LANGUAGES, PLURAL_RULES

__all__ = [
    "UnknownLanguage",
    "book",
    "declared_sections",
    "is_language",
    "languages",
    "lookup",
    "render",
    "resolve_language",
    "sections",
    "structure_problems",
    "t",
]

#: Language code -> imported book module. Filled on first use. Typed loosely so
#: a test can install a stand-in with the same two attributes (TEXTS, SECTIONS).
_BOOKS: dict[str, Any] = {}


class UnknownLanguage(ValueError):
    """A language code the registry does not list.

    Raised instead of silently falling back: a URL parameter that asks for a
    language this deployment does not have is a bad request (400), not a reason
    to show German and pretend it was English.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"unknown language {code!r}")
        self.code = code


def languages() -> tuple[str, ...]:
    """Every language the registry knows, in registry order."""
    return tuple(LANGUAGES)


def is_language(code: object) -> bool:
    return isinstance(code, str) and code in LANGUAGES


def _module(language: str) -> Any:
    if language not in LANGUAGES:
        raise UnknownLanguage(language)
    if language not in _BOOKS:
        name = str(LANGUAGES[language]["book"])
        _BOOKS[language] = importlib.import_module(name)
    return _BOOKS[language]


def book(language: str) -> dict[str, Any]:
    """The text book of ``language``."""
    return dict(_module(language).TEXTS)


def declared_sections(language: str) -> tuple[str, ...]:
    """The sections this book claims to have translated."""
    declared = getattr(_module(language), "SECTIONS", None)
    if declared is None:
        return tuple(sorted(sections(language)))
    return tuple(declared)


def resolve_language(raw: object) -> str:
    """Turn a URL parameter into a language code.

    Empty or missing means the default. Anything the registry does not list
    raises ``UnknownLanguage`` - no guessing, no partial match, no
    ``Accept-Language`` sniffing.
    """
    if raw is None:
        return DEFAULT_LANGUAGE
    code = str(raw).strip().lower()
    if not code:
        return DEFAULT_LANGUAGE
    if code not in LANGUAGES:
        raise UnknownLanguage(code)
    return code


def lookup(key: str, language: str = DEFAULT_LANGUAGE) -> Any:
    """The raw catalogue value: string or branch dict."""
    value = book(language).get(key)
    if value is not None:
        return value
    return book(FALLBACK_LANGUAGE).get(key, key)


def render(value: Any, language: str, variables: dict[str, Any] | None = None) -> str:
    """Select the branch (if any) and insert the variables."""
    variables = variables or {}
    if isinstance(value, dict):
        rule = PLURAL_RULES[str(LANGUAGES[language]["plural"])]
        branch = rule(variables.get("n"))
        text = value.get(branch, value.get("other"))
        if text is None:
            raise KeyError(f"plural branch {branch!r} missing in {sorted(value)}")
        value = text
    text = str(value)
    return text.format(**variables) if variables else text


def t(key: str, *, language: str | None = None, **variables: Any) -> str:
    """The display text for ``key``.

    ``n`` selects a plural branch; every other keyword fills a ``{placeholder}``.
    """
    code = language or DEFAULT_LANGUAGE
    if code not in LANGUAGES:
        raise UnknownLanguage(code)
    return render(lookup(key, code), code, variables)


def sections(language: str) -> dict[str, set[str]]:
    """Keys grouped by their first dotted component - the catalogue families."""
    grouped: dict[str, set[str]] = {}
    for key in book(language):
        family = key.split(".", 1)[0]
        grouped.setdefault(family, set()).add(key)
    return grouped


def structure_problems() -> list[str]:
    """Every book has to agree with the source book, section by section.

    Two rules, both reported as readable lines:

    * a key in a translated book that the source book does not have (invented),
    * a key of a *declared* section that the translated book is missing.
    """
    problems: list[str] = []
    reference = sections(FALLBACK_LANGUAGE)
    for code in languages():
        if code == FALLBACK_LANGUAGE:
            continue
        declared = set(declared_sections(code))
        other = sections(code)
        for family in sorted(set(reference) | set(other)):
            expected = reference.get(family, set())
            actual = other.get(family, set())
            for key in sorted(actual - expected):
                problems.append(f"{code}: unknown key {key} (section {family})")
            if family in declared:
                for key in sorted(expected - actual):
                    problems.append(f"{code}: missing key {key} (section {family})")
    return problems
