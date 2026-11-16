"""The text catalogue: two books, one registry, no build step.

These tests pin the promises the catalogue makes before a single text is
extracted - so the extraction cannot quietly weaken them:

* the registry is the only place that knows a language code, and adding a
  language is one entry plus one book,
* a value is a string or a ``{one/other}`` branch, and which branch is taken is
  the language's rule (never a comparison at the call site),
* a missing key falls back to the source book instead of crashing or showing an
  empty line,
* the books carry the same keys per section; a section a book declares must be
  complete,
* a term the glossary calls untranslatable reads the same in every book.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from paperbroker import texts
from paperbroker.texts import glossary, registry


class CatalogueTestCase(unittest.TestCase):
    """Helpers that prime the book cache with a synthetic pair of books."""

    def prime(self, books):
        """Install ``{code: (texts, sections)}`` as the loaded books."""
        for code, (book, sections) in books.items():
            texts._BOOKS[code] = SimpleNamespace(TEXTS=dict(book), SECTIONS=tuple(sections))
        self.addCleanup(texts._BOOKS.clear)


class RegistryTest(CatalogueTestCase):
    def test_the_registry_lists_the_default_and_the_fallback(self):
        self.assertIn(registry.DEFAULT_LANGUAGE, registry.LANGUAGES)
        self.assertIn(registry.FALLBACK_LANGUAGE, registry.LANGUAGES)
        self.assertEqual(texts.languages(), tuple(registry.LANGUAGES))

    def test_every_language_names_a_book_and_a_plural_rule(self):
        for code, entry in registry.LANGUAGES.items():
            with self.subTest(language=code):
                self.assertTrue(str(entry["book"]).startswith("paperbroker.texts."))
                self.assertIn(entry["plural"], registry.PLURAL_RULES)

    def test_the_switch_target_is_a_language_and_never_the_language_itself(self):
        """A switcher needs a target that differs; the fallback book has none."""
        for code, entry in registry.LANGUAGES.items():
            with self.subTest(language=code):
                target = entry["switch_to"]
                if target is None:
                    self.assertEqual(code, registry.FALLBACK_LANGUAGE)
                else:
                    self.assertIn(target, registry.LANGUAGES)
                    self.assertNotEqual(target, code)


class ResolveLanguageTest(unittest.TestCase):
    def test_a_missing_parameter_means_the_default(self):
        self.assertEqual(texts.resolve_language(None), registry.DEFAULT_LANGUAGE)
        self.assertEqual(texts.resolve_language(""), registry.DEFAULT_LANGUAGE)
        self.assertEqual(texts.resolve_language("   "), registry.DEFAULT_LANGUAGE)

    def test_a_known_code_is_returned_in_lower_case(self):
        for code in texts.languages():
            with self.subTest(language=code):
                self.assertEqual(texts.resolve_language(code.upper()), code)

    def test_an_unknown_code_is_refused_instead_of_falling_back(self):
        """400, not German-with-an-English-label."""
        for code in ("fr", "de-DE", "englisch", "xx"):
            with self.subTest(code=code):
                with self.assertRaises(texts.UnknownLanguage):
                    texts.resolve_language(code)


class FallbackTest(CatalogueTestCase):
    def test_a_missing_key_falls_back_to_the_source_book(self):
        self.prime({
            "de": ({"page.head.hurdle": "Zugelassen: R/R >= {ratio}"}, ()),
            "en": ({}, ()),
        })
        self.assertEqual(texts.t("page.head.hurdle", language="en", ratio="1 : 2"),
                         "Zugelassen: R/R >= 1 : 2")

    def test_a_key_missing_everywhere_is_returned_instead_of_crashing(self):
        self.prime({"de": ({}, ()), "en": ({}, ())})
        self.assertEqual(texts.t("page.head.hurdle", language="en"), "page.head.hurdle")

    def test_the_target_book_wins_over_the_source_book(self):
        self.prime({
            "de": ({"js.state.running": "Lauf für {day} …"}, ()),
            "en": ({"js.state.running": "Run for {day} …"}, ()),
        })
        self.assertEqual(texts.t("js.state.running", language="en", day="2026-11-13"),
                         "Run for 2026-11-13 …")
        self.assertEqual(texts.t("js.state.running", day="2026-11-13"),
                         "Lauf für 2026-11-13 …")


class PluralTest(CatalogueTestCase):
    """One matrix per language, and one entry per plural site once they exist."""

    def test_every_language_selects_one_and_other(self):
        self.prime({
            "de": ({"js.count.candidates": {"one": "{n} Kandidat", "other": "{n} Kandidaten"}}, ()),
            "en": ({"js.count.candidates": {"one": "{n} candidate", "other": "{n} candidates"}}, ()),
        })
        for code in texts.languages():
            with self.subTest(language=code):
                for count in (0, 2, 7):
                    self.assertIn(str(count), texts.t("js.count.candidates", language=code, n=count))
                self.assertIn("1 ", texts.t("js.count.candidates", language=code, n=1))

    def test_a_branch_value_without_n_takes_the_other_form(self):
        """A text without a count cannot be singular - 'other' is the safe side."""
        self.prime({
            "de": ({"js.count.rows": {"one": "eine Zeile", "other": "Zeilen"}}, ()),
            "en": ({"js.count.rows": {"one": "one row", "other": "rows"}}, ()),
        })
        expected = {"de": "Zeilen", "en": "rows"}
        for code in texts.languages():
            with self.subTest(language=code):
                self.assertEqual(texts.t("js.count.rows", language=code), expected[code])

    def test_each_real_plural_site_is_proven_in_every_language(self):
        """Mehrfach-Ausweis je Stelle: both branches, per book, per key.

        Runs over whatever the books actually carry, so every extracted plural
        text is proven without editing this test.
        """
        source = texts.book(registry.FALLBACK_LANGUAGE)
        plural_keys = sorted(key for key, value in source.items() if isinstance(value, dict))
        for code in texts.languages():
            book = texts.book(code)
            for key in plural_keys:
                if key not in book:
                    continue
                with self.subTest(language=code, key=key):
                    value = book[key]
                    self.assertIsInstance(value, dict)
                    self.assertIn("one", value)
                    self.assertIn("other", value)
                    self.assertTrue(str(value["one"]))
                    self.assertTrue(str(value["other"]))


class StructureTest(CatalogueTestCase):
    def test_the_shipped_books_agree_section_by_section(self):
        self.assertEqual(texts.structure_problems(), [])

    def test_an_invented_key_turns_the_check_red(self):
        self.prime({
            "de": ({"page.head.hurdle": "x"}, ()),
            "en": ({"page.head.invented": "y"}, ()),
        })
        problems = texts.structure_problems()
        self.assertTrue(any("unknown key page.head.invented" in line for line in problems), problems)

    def test_a_declared_section_must_be_complete(self):
        self.prime({
            "de": ({"page.a": "x", "page.b": "y"}, ()),
            "en": ({"page.a": "x"}, ("page",)),
        })
        problems = texts.structure_problems()
        self.assertTrue(any("missing key page.b" in line for line in problems), problems)

    def test_an_undeclared_section_may_still_be_empty(self):
        """Falling back to German is allowed while a section is not translated."""
        self.prime({
            "de": ({"page.a": "x", "js.b": "y"}, ()),
            "en": ({"js.b": "y"}, ("js",)),
        })
        self.assertEqual(texts.structure_problems(), [])

    def test_a_declared_section_may_not_be_half_done(self):
        self.prime({
            "de": ({"js.b": "y", "page.a": "x"}, ()),
            "en": ({"js.b": "y", "page.a": "x"}, ("js", "page")),
        })
        self.assertEqual(texts.structure_problems(), [])


class ThirdLanguageTest(CatalogueTestCase):
    def test_a_new_language_needs_one_entry_and_one_book(self):
        """No helper, no view and no test may name a language code."""
        entry = {
            "name": "Testsprache",
            "book": "paperbroker.texts.de_texts",
            "plural": "one_other",
            "switch_to": "de",
        }
        with mock.patch.dict(texts.LANGUAGES, {"xx": entry}):
            self.prime({
                "de": ({"page.head.hurdle": "Zugelassen: R/R >= 1 : 2"}, ()),
                "xx": ({"page.head.hurdle": "R/R >= 1 : 2 erlaubt"}, ("page",)),
            })
            self.assertEqual(texts.t("page.head.hurdle", language="xx"),
                             "R/R >= 1 : 2 erlaubt")
            self.assertEqual(texts.resolve_language("xx"), "xx")
            self.assertEqual(texts.structure_problems(), [])
            self.assertIn("xx", texts.languages())


class ValueShapeTest(CatalogueTestCase):
    def test_placeholders_are_filled_with_str_format(self):
        self.prime({"de": ({"page.head.cap": "bis {cap} % und {rest}"}, ())})
        self.assertEqual(texts.t("page.head.cap", cap="30", rest="mehr"), "bis 30 % und mehr")

    def test_every_shipped_value_has_balanced_braces(self):
        """A stray brace makes str.format raise at request time - catch it here."""
        for code in texts.languages():
            for key, value in texts.book(code).items():
                if isinstance(value, dict):
                    strings = [str(v) for v in value.values()]
                else:
                    strings = [str(value)]
                for text in strings:
                    with self.subTest(language=code, key=key):
                        self.assertEqual(text.count("{"), text.count("}"), text)


class GlossaryTest(unittest.TestCase):
    def test_a_term_that_is_never_translated_has_a_reason(self):
        for term in glossary.TERMS:
            with self.subTest(term=term):
                self.assertTrue(glossary.TERMS[term])

    def test_identical_keys_read_the_same_in_every_book(self):
        source = texts.book(registry.FALLBACK_LANGUAGE)
        for key in glossary.IDENTICAL_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, source)
                expected = source[key]
                for code in texts.languages():
                    self.assertEqual(texts.book(code).get(key, expected), expected)

class PluralMatrixTest(CatalogueTestCase):
    """Die Mehrfach-Matrix, festgenagelt - nicht nur "geht nicht kaputt".

    Zwei Zusagen, beide noetig:

    * **Form:** ein Wert mit Zweigen hat GENAU die Zweige, die seine Regel
      braucht - nicht mehr, nicht weniger. Ein Buch mit einem dritten Zweig
      waere in de/en nie erreichbar, und ein fehlender Zweig wuerde erst beim
      Rendern auffallen.
    * **Inhalt:** welche Zahl welchen Text ergibt, steht hier wortgetreu. Wer
      eine Form aendert, aendert diesen Test - und sieht dabei, dass er eine
      Anzeige aendert.
    """

    #: Die Zweige, die eine Regel bei diesen Zahlen verlangt. Bewusst aus der
    #: Regel ABGELEITET, nicht hartkodiert: eine neue Sprache mit anderen Formen
    #: bringt ihre Zweige selbst mit.
    PROBEN = (0, 1, 2, 11, 1000)

    def test_every_branched_value_has_exactly_the_branches_its_rule_needs(self):
        for code in texts.languages():
            rule = registry.PLURAL_RULES[str(registry.LANGUAGES[code]["plural"])]
            erwartet = {rule(n) for n in self.PROBEN}
            for key, value in texts.book(code).items():
                if not isinstance(value, dict):
                    continue
                with self.subTest(sprache=code, key=key):
                    self.assertEqual(erwartet, set(value), f"{code}/{key}")

    def test_the_german_plural_matrix_is_pinned(self):
        faelle = {
            "funnel.pairs.valid": {
                0: "0 gültige Paare",
                1: "1 gültiges Paar",
                2: "2 gültige Paare",
                1000: "1.000 gültige Paare",
            },
            "funnel.pairs.enumerated": {
                1: "1 aufgezählten Paar",
                2: "2 aufgezählten Paaren",
            },
            "funnel.pairs.possible": {
                1: "1 mögliches Paar",
                45: "45 möglich",
            },
            "funnel.pairs.capped_title": {
                1: "1 Titel",
                3: "3 Titeln",
            },
        }
        for key, faelle_je_zahl in faelle.items():
            for zahl, erwartet in faelle_je_zahl.items():
                with self.subTest(key=key, n=zahl):
                    self.assertEqual(erwartet, texts.t(key, n=zahl, count=_tausender(zahl)))

    def test_the_three_funnel_counters_read_as_the_page_says(self):
        from paperbroker.gap_funnel import (
            _enumerated_pairs,
            _possible_pairs_word,
            _valid_pairs,
        )

        self.assertEqual("1 gültiges Paar", _valid_pairs(1))
        self.assertEqual("45 gültige Paare", _valid_pairs(45))
        self.assertEqual("1 aufgezählten Paar", _enumerated_pairs(1))
        self.assertEqual("45 aufgezählten Paaren", _enumerated_pairs(45))
        self.assertEqual("1 mögliches Paar", _possible_pairs_word(1))
        self.assertEqual("45 möglich", _possible_pairs_word(45))
        self.assertEqual("? möglich", _possible_pairs_word(None))


def _tausender(number):
    """Dieselbe Zifferngruppierung, die das Modul fuer Anzeigen benutzt."""
    return f"{number:,}".replace(",", ".")


if __name__ == "__main__":
    unittest.main()
