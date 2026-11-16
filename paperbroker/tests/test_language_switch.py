"""Der Umschalter: ``?lang=`` auf den zwei Endpunkten, die ihm folgen.

Heute ist das Englische Buch leer. Der Umschalter ist deshalb noch nicht an einer
Uebersetzung zu erkennen - aber an drei Dingen, die jetzt schon gelten muessen und
die dieser Test festhaelt:

* **Deutsch bleibt Deutsch.** Mit ``?lang=de``, ``?lang=en`` (leeres Buch) und ohne
  Parameter kommt BYTE-GLEICH dieselbe Antwort.
* **Ein unbekannter Code wird abgelehnt**, nicht stillschweigend als Deutsch
  ausgeliefert: 400 mit Code und Liste der vorhandenen Sprachen.
* **Nur die zwei erlaubten Endpunkte hoeren zu.** ``/api/screen`` bleibt
  byte-stabil, auch mit ``?lang=en`` - und eine Sprache darf nicht in die NAECHSTE
  Anfrage bluten.
"""

from __future__ import annotations

import unittest

from .. import texts
from ..server import create_app
from ..texts import registry
from .test_eva_quality import _clock, _MockAdapter, make
from .test_gap_spreads_detail_endpoint import _client as detail_client

LISTE = "/api/v1/gap-spreads"
DETAIL = "/api/v1/gap-spreads/detail"
SCREEN = "/api/screen"

#: Ein Tag, den die Liste ohne Berichtstermin-Quelle DETERMINISTISCH beantwortet
#: (503 ``calendar_unavailable``). Der Test vergleicht Antworten, nicht Noten - ein
#: stabiler Fehlerfall ist dafuer genauso beweiskraeftig wie ein 200.
TAG_OHNE_QUELLE = "2026-09-15"


class _ClientTestCase(unittest.TestCase):
    def setUp(self):
        app = create_app(quote_adapter=_MockAdapter(make()), screener=None,
                         now_fn=_clock())
        app.config["TESTING"] = True
        self.client = app.test_client()
        # Der Detail-Endpunkt braucht die Kalender-Fixtures seiner eigenen Testdatei.
        self.detail = detail_client()

    def rumpf(self, pfad, **parameter):
        # Der Detail-Pfad braucht die Kalender-Fixtures, also seinen eigenen Client.
        client = self.detail if pfad == DETAIL else self.client
        antwort = client.get(pfad, query_string=parameter)
        return antwort.status_code, antwort.get_data(as_text=True)


class TheTwoEndpointsFollowTheLanguageTest(_ClientTestCase):
    def test_the_default_list_reads_the_same_in_every_available_language(self):
        """Solange das Zielbuch leer ist, ist die Uebersetzung der Rueckfall.

        Der Test ist damit ein Vertrag fuer HEUTE: der Umschalter aendert an einer
        Sprache ohne Buch keinen einzigen Buchstaben. Sobald ``en`` Texte hat,
        kippt genau diese Zusage - und das ist gewollt sichtbar.
        """
        ohne = self.rumpf(LISTE, day=TAG_OHNE_QUELLE)
        for code in texts.languages():
            with self.subTest(language=code):
                self.assertEqual(
                    self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang=code), ohne)

    def test_the_detail_answer_reads_the_same_in_every_available_language(self):
        def hole(lang=None):
            parameter = {"symbol": "AAA", "day": "2026-11-12"}
            if lang is not None:
                parameter["lang"] = lang
            antwort = self.detail.get(DETAIL, query_string=parameter)
            return antwort.status_code, antwort.get_data(as_text=True)

        ohne = hole()
        self.assertEqual(ohne[0], 200)
        for code in texts.languages():
            with self.subTest(language=code):
                self.assertEqual(hole(code), ohne)

    def test_a_code_in_any_case_is_accepted(self):
        ohne = self.rumpf(LISTE, day=TAG_OHNE_QUELLE)
        self.assertEqual(self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang="DE"), ohne)
        self.assertEqual(self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang=" De "), ohne)


class AnUnknownLanguageIsRefusedTest(_ClientTestCase):
    def test_an_unknown_code_gets_400_and_a_list_of_what_exists(self):
        for pfad, parameter in (
            (LISTE, {"day": TAG_OHNE_QUELLE}),
            (DETAIL, {"symbol": "AAA", "day": "2026-11-12"}),
        ):
            for code in ("fr", "de-DE", "englisch", "xx"):
                with self.subTest(pfad=pfad, lang=code):
                    status, rumpf = self.rumpf(pfad, lang=code, **parameter)
                    self.assertEqual(status, 400)
                    self.assertIn('"unknown_language"', rumpf)
                    for vorhanden in texts.languages():
                        self.assertIn(f'"{vorhanden}"', rumpf)

    def test_an_empty_parameter_means_the_default_not_an_error(self):
        ohne = self.rumpf(LISTE, day=TAG_OHNE_QUELLE)
        self.assertEqual(self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang=""), ohne)
        self.assertEqual(self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang="   "), ohne)


class TheOtherEndpointsDoNotListenTest(_ClientTestCase):
    def test_the_legacy_screen_answer_ignores_the_parameter(self):
        ohne = self.rumpf(SCREEN, ticker="SPY", expiration="2026-10-16")
        with_lang = self.rumpf(SCREEN, ticker="SPY", expiration="2026-10-16", lang="en")
        self.assertEqual(with_lang, ohne)

    def test_a_language_does_not_bleed_into_the_next_request(self):
        """Ein durchgesickerter Zustand waere der teuerste Fehler dieses Umbaus."""
        erst = self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang="en")
        danach = self.rumpf(LISTE, day=TAG_OHNE_QUELLE)
        self.assertEqual(danach, erst)
        self.assertEqual(texts.active_language(), registry.DEFAULT_LANGUAGE)

    def test_an_error_request_leaves_no_language_behind(self):
        self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang="fr")
        self.assertEqual(texts.active_language(), registry.DEFAULT_LANGUAGE)


class TheLanguageCanBeSetInCodeTest(unittest.TestCase):
    def test_a_block_serves_its_language_and_restores_the_old_one(self):
        vorher = texts.active_language()
        with texts.use_language(registry.DEFAULT_LANGUAGE) as code:
            self.assertEqual(code, registry.DEFAULT_LANGUAGE)
            self.assertEqual(texts.active_language(), registry.DEFAULT_LANGUAGE)
        self.assertEqual(texts.active_language(), vorher)

    def test_an_unknown_code_is_refused_instead_of_guessed(self):
        with self.assertRaises(texts.UnknownLanguage):
            with texts.use_language("fr"):
                pass

    def test_the_block_is_left_even_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with texts.use_language(registry.DEFAULT_LANGUAGE):
                raise RuntimeError("kaputt")
        self.assertEqual(texts.active_language(), registry.DEFAULT_LANGUAGE)


if __name__ == "__main__":
    unittest.main()
