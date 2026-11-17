"""Das Einstellungs-Panel im Fuss - (f), ohne Skript und ohne Gedaechtnis.

Ein ``<details>`` im Fuss, zugeklappt beim Laden. Darin die Sprachzeile aus der
Registry. Der Streifen aus (e) faellt weg: es gibt **eine** sichtbare
Wechsel-Flaeche je Zustand, nicht zwei.

Die Merk-Funktion (browser-lokale Speicherung, Weiterleitung, Aufflackern) ist
NICHT hier - sie ist zurueckgestellt bis zur Freigabe (f2).
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from paperbroker import texts
from paperbroker.tests.test_gap_spreads_detail_endpoint import _client


def _seite(lang: str | None = None) -> str:
    client = _client()
    return client.get("/" if lang is None else "/?lang=" + lang).get_data(as_text=True)


def _panel(seite: str) -> str:
    """Das Panel selbst - vom oeffnenden Tag bis zum schliessenden."""
    start = seite.index('<details class="settings"')
    ende = seite.index("</details>", start) + len("</details>")
    return seite[start:ende]


class ThePanelSitsInTheFooterTest(unittest.TestCase):
    def test_the_panel_is_a_closed_details_element_in_the_footer(self):
        seite = _seite()
        fuss = seite[seite.index("<footer"):seite.index("</footer>")]
        self.assertIn('id="settings"', fuss)
        panel = _panel(seite)
        self.assertRegex(panel, r"<details[^>]*id=\"settings\"[^>]*>")
        self.assertNotRegex(panel, r"<details[^>]*\sopen[\s=>]",
                            "zugeklappt beim Laden")

    def test_the_summary_comes_from_the_book(self):
        for code in texts.languages():
            with self.subTest(code=code):
                panel = _panel(_seite(code))
                self.assertIn(texts.t("page.settings.summary", language=code), panel)

    def test_no_script_lives_inside_the_panel(self):
        panel = _panel(_seite("en"))
        self.assertNotIn("<script", panel)
        self.assertNotRegex(panel, r"\son[a-z]+=", "kein Handler im Markup")


class TheLanguageLineComesFromTheRegistryTest(unittest.TestCase):
    def test_every_registered_language_appears_exactly_once(self):
        panel = _panel(_seite("en"))
        for code in texts.languages():
            with self.subTest(code=code):
                self.assertEqual(1, panel.count(f">{code.upper()}<"))

    def test_the_current_language_is_marked_and_not_a_link(self):
        panel = _panel(_seite("en"))
        self.assertIn('<span aria-current="true">EN</span>', panel)
        self.assertIn('class="lang-switch" href="/?lang=de"', panel)

    def test_a_link_keeps_the_query_and_swaps_only_the_language(self):
        seite = _seite("en")
        panel = _panel(seite)
        treffer = re.findall(r'class="lang-switch" href="([^"]*)"', panel)
        self.assertTrue(treffer)
        for href in treffer:
            with self.subTest(href=href):
                self.assertIn("lang=de", href)

    def test_the_strip_from_the_previous_step_is_gone(self):
        seite = _seite("en")
        self.assertNotIn("lang-bar", seite,
                         "eine sichtbare Wechsel-Flaeche je Zustand")
        # Gezaehlt wird die FLaeche, nicht die Zahl der Links: die Zahl der Links
        # ist len(languages()) - 1 und waechst mit jeder neuen Sprache. Eine
        # Konstante hier haette die dokumentierte Erweiterung um eine Sprache
        # (eine Zeile in der Registry) blockiert - Befund Kody, PR #127.
        self.assertEqual(1, len(re.findall(r'<details class="settings"', seite)))
        self.assertEqual(len(texts.languages()) - 1,
                         len(re.findall(r'class="lang-switch"', seite)))

    def test_a_third_language_adds_a_link_not_a_second_surface(self):
        """Die Erweiterung um eine Sprache bleibt eine Zeile in der Registry."""
        entry = {
            "name": "Testsprache",
            "book": "paperbroker.texts.de_texts",
            "plural": "one_other",
            "numbers": {"decimal": ",", "thousands": "."},
            "switch_to": "de",
        }
        with mock.patch.dict(texts.LANGUAGES, {"xx": entry}):
            seite = _seite()
            self.assertEqual(3, len(texts.languages()))
            self.assertEqual(1, len(re.findall(r'<details class="settings"', seite)))
            self.assertEqual(2, len(re.findall(r'class="lang-switch"', seite)))


class ThePanelChangesNothingElseTest(unittest.TestCase):
    def test_an_unknown_code_is_still_refused_with_400(self):
        client = _client()
        antwort = client.get("/?lang=xx")
        self.assertEqual(400, antwort.status_code)

    def test_the_page_still_speaks_the_language_it_was_asked_for(self):
        for code in texts.languages():
            with self.subTest(code=code):
                seite = _seite(code)
                self.assertIn(texts.t("page.head.title", language=code), seite)
