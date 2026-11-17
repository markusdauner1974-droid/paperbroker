"""Der englische Entwurf der 193 Seitentexte - und die Vorpruefungen der Durchsicht.

Die Pruefungen hier sind keine Geschmacksfragen: jede von ihnen hatte in dieser
Geschichte schon einen echten Fehler gefunden. Sie laufen VOR der Durchsicht des
Eigentuemers, damit seine Tabelle nur noch die Wortwahl bewertet.
"""

from __future__ import annotations

import unittest
from string import Formatter

from paperbroker import texts
from paperbroker.texts import glossary, registry

SEITE = ("page", "page_route", "js", "support", "trend", "sort")

#: Die zwei Spalten, deren englisches Wort laenger ist als die vereinbarten 20 %.
#: Nicht "weggekuerzt", sondern benannt: die Tabelle ist 837 px breit, und ein
#: gekuerztes Wort waere eine andere Entscheidung als eine gemeldete Abweichung.
UEBER_LAENGE = frozenset({
    "page.table.col.close",
    "page.table.col.expiration",
    "page.table.col.next_report",
    "page.table.col.required_move",
})


def _seite(book):
    return {k: v for k, v in book.items() if k.split(".")[0] in SEITE}


def _felder(wert):
    if isinstance(wert, dict):
        return {f for zweig in wert.values() for _, f, _, _ in Formatter().parse(zweig) if f}
    return {f for _, f, _, _ in Formatter().parse(str(wert)) if f}


def _naehe(wert):
    text = str(wert)
    return (len(text) - len(text.lstrip(" ")), len(text) - len(text.rstrip(" ")),
            text.count("\n"))


class TheEnglishPageDraftTest(unittest.TestCase):
    def setUp(self):
        self.de = _seite(texts.book(registry.FALLBACK_LANGUAGE))
        self.en = _seite(texts.book("en"))

    def test_all_page_texts_are_translated(self):
        # 193 Seitentexte aus der Durchsicht, dazu was erst die Umschaltung und das
        # Panel gebraucht haben: der Satz des 400-Pfades und die zwei Woerter des
        # Einstellungs-Panels. Die elf Texte der Griechen-Tasten (fuenf Namen,
        # vier Achsentitel, der Legendeneintrag und der Satz fuer den
        # Screenreader) kommen mit dem Overlay hinzu: 196 + 11.
        self.assertEqual(len(self.de), 207)
        self.assertEqual(set(self.en), set(self.de), "kein Schluessel darf fehlen")

    def test_every_placeholder_is_unchanged(self):
        for key, wert in self.en.items():
            with self.subTest(key=key):
                self.assertEqual(_felder(wert), _felder(self.de[key]),
                                 f"{key}: Platzhalter muessen gleich heissen")
                self.assertEqual(isinstance(wert, dict), isinstance(self.de[key], dict),
                                 f"{key}: Zweig oder Satz, wie im Quellbuch")

    def test_the_terms_read_exactly_as_in_the_source_book(self):
        for key in glossary.IDENTICAL_KEYS:
            if key in self.en:
                with self.subTest(key=key):
                    self.assertEqual(self.en[key], self.de[key])

    def test_the_near_values_keep_their_spaces_and_their_line_break(self):
        """Die fuenf Stellen, an denen ein Leerzeichen die Anzeige macht.

        ``lede_before`` endet auf einem Leerzeichen, ``lede_between`` beginnt mit
        einem und traegt einen Umbruch mit vier Einrueckungs-Leerzeichen; ``lede_after``
        beginnt mit einem. Sie umschliessen Zahlen im Template - ein verlorenes
        Leerzeichen klebt "20" an "Allowed:".
        """
        for key in ("page.head.lede_before", "page.head.lede_between",
                    "page.head.lede_after", "page_route.option_note.stale_suffix"):
            with self.subTest(key=key):
                self.assertEqual(_naehe(self.en[key]), _naehe(self.de[key]),
                                 f"{key}: Leerzeichen/Umbruch weichen ab")
        self.assertTrue(self.en["page.head.lede_before"].endswith("≥ "))
        # Beide Nahtstellen beginnen mit dem Leerzeichen, das die Zahl davor trennt.
        self.assertTrue(self.en["page.head.lede_between"].startswith(" and "))
        self.assertTrue(self.en["page_route.option_note.stale_suffix"].startswith(" "))

    def test_no_column_label_grows_beyond_a_fifth(self):
        """Die th-Zellen sind nowrap und bestimmen die Spaltenbreite (837 px).

        Zwei Abweichungen sind benannt und nicht weggekuerzt; jede WEITERE ist rot.
        """
        zu_lang = set()
        for key, wert in self.en.items():
            if not key.startswith("page.table.col."):
                continue
            if len(str(wert)) > len(str(self.de[key])) * 1.2:
                zu_lang.add(key)
        self.assertEqual(zu_lang, set(UEBER_LAENGE),
                         f"unbenannte Ueberlaenge: {sorted(zu_lang - UEBER_LAENGE)}")

    def test_one_concept_has_one_english_word(self):
        for wort, keys in glossary.UNIFORM_TERMS.items():
            for key in keys:
                with self.subTest(term=wort, key=key):
                    self.assertEqual(self.en[key], wort)

    def test_the_word_expiry_is_refused(self):
        """Der Begriff heisst 'Expiration' - auch dort, wo 'Expiry' kuerzer waere.

        Die Entscheidung des Eigentuemers vom 17.11.2026 steht in ``TERMS``; hier
        wird sie durchgesetzt. Schluesselnamen sind davon unberuehrt
        (``js.detail.expiry_label`` heisst weiter so - er ist keine Anzeige).
        """
        self.assertIn("Expiration", glossary.TERMS)
        for key, wert in self.en.items():
            texte = wert.values() if isinstance(wert, dict) else [wert]
            for satz in texte:
                with self.subTest(key=key):
                    self.assertNotIn("Expiry", satz)
                    self.assertNotIn("expiry", satz)

    def test_no_german_word_survives_in_the_draft(self):
        """Ein deutscher Rest faellt in der Tabelle auf - hier faellt er frueher."""
        reste = ("Verfall", "Kurs", "Gewinn", "Schwankung", "Treffer", "Zeilen",
                 "nächster", "ausgeblendet", "gelistet")
        for key, wert in self.en.items():
            texte = wert.values() if isinstance(wert, dict) else [wert]
            for satz in texte:
                for rest in reste:
                    with self.subTest(key=key, rest=rest):
                        self.assertNotIn(rest, satz)


class TheInjectedBlockStaysGermanTest(unittest.TestCase):
    """Schritt 2 schaltet NICHTS um - die Seite liest weiter Deutsch.

    Der Injektionsblock ist die Stelle, an der die Sprache spaeter ankommt; solange
    er deutsch ist, ist die Seite deutsch, egal was im Buch steht.
    """

    def test_the_page_still_carries_the_german_block(self):
        from .test_gap_funnel import _client, _fixture

        kalender, historie, ketten, termine = _fixture()
        seite = _client(kalender, historie, ketten, termine).get("/").get_data(as_text=True)
        self.assertIn(texts.t("js.expand.open", language="de"), seite)
        self.assertNotIn(texts.t("js.expand.open", language="en"), seite)
