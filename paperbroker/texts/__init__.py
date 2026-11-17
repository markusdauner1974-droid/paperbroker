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

import contextlib
import contextvars
import importlib
from collections.abc import Iterator
from typing import Any

from . import glossary
from .registry import DEFAULT_LANGUAGE, FALLBACK_LANGUAGE, LANGUAGES, PLURAL_RULES

__all__ = [
    "UnknownLanguage",
    "active_language",
    "book",
    "declared_sections",
    "in_language",
    "is_language",
    "languages",
    "lookup",
    "block",
    "render",
    "resolve_language",
    "section",
    "sections",
    "structure_problems",
    "t",
    "use_language",
]

#: Language code -> imported book module. Filled on first use. Typed loosely so
#: a test can install a stand-in with the same two attributes (TEXTS, SECTIONS).
_BOOKS: dict[str, Any] = {}

#: The language of the request being served. A route sets it once
#: (``with use_language(code):``), and every ``t()`` call underneath reads it -
#: so a text deep inside a helper needs no parameter of its own. The default is
#: the source language, which is what every request got before the switch
#: existed; a request without a setting is therefore byte-identical to before.
_ACTIVE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "paperbroker_texts_language", default=DEFAULT_LANGUAGE)


def active_language() -> str:
    """The language set for the current request (source language by default)."""
    return _ACTIVE.get()


def in_language(code: str | None, fn: Any) -> Any:
    """``fn``, but running with ``code`` active - for threads that inherit nothing.

    A ``ThreadPoolExecutor`` starts its workers in a fresh context, so a worker
    would read the default language and put German sentences into an English
    answer. The language is read HERE, in the submitting thread, and set inside the
    worker: one decision, made before the work is handed out.
    """

    def lauf(*args: Any, **kwargs: Any) -> Any:
        with use_language(code):
            return fn(*args, **kwargs)

    return lauf


@contextlib.contextmanager
def use_language(code: str | None) -> Iterator[str]:
    """Serve everything inside this block in ``code``.

    ``None`` means the default. A code the registry does not list is refused, not
    guessed - the same rule as ``resolve_language``.
    """
    resolved = DEFAULT_LANGUAGE if code is None else resolve_language(code)
    token = _ACTIVE.set(resolved)
    try:
        yield resolved
    finally:
        _ACTIVE.reset(token)


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


def language_name(code: str) -> str:
    """Der Name einer Sprache, wie ihn die Registry fuehrt - fuer den Wechsler."""
    return str(LANGUAGES[code]["name"])


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
    """The text book of ``language`` - the module's own mapping, not a copy.

    Read-only for callers: ``lookup``, ``sections`` and ``structure_problems``
    only read it, and a copy per call would cost O(book size) on EVERY displayed
    text (``lookup`` asks for the book twice). Measured on a 400-key book:
    a page with 323 texts pays 1,7 ms of copying for nothing.
    """
    return _module(language).TEXTS


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
    code = language or active_language()
    if code not in LANGUAGES:
        raise UnknownLanguage(code)
    return render(lookup(key, code), code, variables)


def section(prefix: str, language: str | None = None) -> dict[str, Any]:
    """Only the keys of one catalogue family (``js`` -> every ``js.*`` key)."""
    code = language or active_language()
    return {
        key: value
        for key, value in book(code).items()
        if key == prefix or key.startswith(prefix + ".")
    }


def block(prefix: str, language: str | None = None) -> dict[str, Any]:
    """One family plus the language's plural-rule name, for the page's JSON block.

    The script needs both: the texts, and the name of the rule that picks a
    ``{one, other}`` branch - so the decision stays with the language and is not
    re-invented as a comparison inside the script.
    """
    code = language or active_language()
    return {
        "lang": code,
        "plural": LANGUAGES[code]["plural"],
        # A language that names no number style falls back to the dot: the
        # catalogue must not crash on an entry that predates this field.
        "numbers": dict(LANGUAGES[code].get("numbers") or {"decimal": ".", "thousands": ","}),
        "texts": section(prefix, code),
    }


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
    #: ``reference`` maps family -> keys; the term check needs the flat set AND
    #: the values, so it reads the source book itself.
    reference_keys = {key for keys in reference.values() for key in keys}
    source_book = book(FALLBACK_LANGUAGE)
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
        # A term this project never translates has to read the SAME in every book.
        # Reported here, not only in a test, so the structural gate sees it too.
        other_book = book(code)
        for key in glossary.IDENTICAL_KEYS:
            if key in reference_keys and key in other_book:
                if other_book[key] != source_book[key]:
                    problems.append(
                        f"{code}: {key} must read as in {FALLBACK_LANGUAGE} "
                        f"(a term, not a sentence)")
    return problems
