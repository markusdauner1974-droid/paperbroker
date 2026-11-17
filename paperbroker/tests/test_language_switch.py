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
from .test_gap_funnel import DAY as FUNNEL_DAY
from .test_gap_funnel import _client as funnel_client
from .test_gap_funnel import _fixture as funnel_fixture
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
    def test_an_answer_without_notes_does_not_change_with_the_language(self):
        """Ohne Notizen im Rumpf gibt es nichts zu uebersetzen - die Bytes bleiben.

        Der Fehlerfall der Liste (keine Berichtstermin-Quelle) traegt keine Notiz;
        ``?lang=en`` darf daran nichts aendern. Der Fall mit Notizen steht in
        ``test_the_list_notes_speak_english_because_they_are_translated``.
        """
        ohne = self.rumpf(LISTE, day=TAG_OHNE_QUELLE)
        self.assertEqual(ohne[0], 503)
        for code in texts.languages():
            with self.subTest(language=code):
                self.assertEqual(
                    self.rumpf(LISTE, day=TAG_OHNE_QUELLE, lang=code), ohne)

    def test_the_list_notes_speak_english_because_they_are_translated(self):
        """Die Notizen der Liste - die erste Familie, die im LISTEN-Rumpf ankommt.

        Ein 200 braucht den Aufbau der Trichter-Tests (Kalender, Historie, Ketten);
        deshalb wird deren Fixture importiert statt eine zweite erfunden.
        """
        def hole(lang=None):
            kalender, historie, ketten, termine = funnel_fixture()
            client = funnel_client(kalender, historie, ketten, termine)
            parameter = {"day": FUNNEL_DAY}
            if lang is not None:
                parameter["lang"] = lang
            antwort = client.get(LISTE, query_string=parameter)
            self.assertEqual(antwort.status_code, 200)
            return antwort.get_json()["notes"]

        deutsch, englisch = hole("de"), hole("en")
        self.assertNotEqual(deutsch, englisch)
        # Ohne Parameter und mit de dieselbe Antwort - der Umschalter ist opt-in.
        self.assertEqual(deutsch, hole())
        self.assertIn(texts.t("gap.notes.rr", language="en"), englisch)
        self.assertNotIn(texts.t("gap.notes.rr", language="de"), englisch)
        self.assertEqual(len(deutsch), len(englisch))

    def test_a_reason_from_a_worker_thread_is_english_too(self):
        """Der Beleg fuer Kodys ThreadPool-Befund, end-to-end.

        Der Ausschlussgrund dieser Antwort entsteht im Arbeiter des Trichters, nicht
        im Anfrage-Thread; ohne ``texts.in_language`` stuende hier Deutsch in einer
        englischen Antwort.
        """
        kalender, historie, ketten, termine = funnel_fixture()
        client = funnel_client(kalender, historie, ketten, termine)

        def ausschluss(lang):
            antwort = client.get(LISTE, query_string={"day": FUNNEL_DAY, "lang": lang})
            self.assertEqual(antwort.status_code, 200)
            gruende = antwort.get_json()["excluded"]
            self.assertTrue(gruende, "der Aufbau muss mindestens einen Ausschluss tragen")
            return gruende[0]["message"]

        deutsch, englisch = ausschluss("de"), ausschluss("en")
        self.assertEqual(deutsch, texts.t("funnel.earnings.none_in_window", language="de"))
        self.assertEqual(englisch, texts.t("funnel.earnings.none_in_window", language="en"))
        self.assertNotEqual(deutsch, englisch)

    def test_the_switch_never_touches_the_default_answer(self):
        """Ohne ``lang`` und mit ``lang=de`` muss dieselbe Antwort kommen.

        Diese Zusage gilt in JEDER Sprache des Buchs: der Umschalter aendert nur,
        was ausdruecklich verlangt wird.
        """
        for pfad, parameter in (
            (LISTE, {"day": TAG_OHNE_QUELLE}),
            (DETAIL, {"symbol": "AAA", "day": "2026-11-12"}),
        ):
            with self.subTest(pfad=pfad):
                ohne = self.rumpf(pfad, **parameter)
                self.assertEqual(self.rumpf(pfad, **parameter, lang="de"), ohne)

    def test_the_detail_speaks_english_because_its_section_is_translated(self):
        """Der erste uebersetzte Abschnitt - hier wird der Umschalter sichtbar.

        ``detail`` ist im Zielbuch deklariert, also liest ``?lang=en`` die
        englischen Saetze und die deutschen sind aus dem Rumpf verschwunden.
        """
        def hole(lang):
            antwort = self.detail.get(DETAIL, query_string={
                "symbol": "AAA", "day": "2026-11-12", "lang": lang})
            self.assertEqual(antwort.status_code, 200)
            return antwort.get_json()

        # Der Rumpf maskiert Umlaute als \u00fc - verglichen wird der gelesene
        # Wert, nicht die Rohbytes.
        deutsch, englisch = hole("de")["notes"], hole("en")["notes"]
        self.assertIn(texts.t("detail.notes.pl", language="de"), deutsch)
        self.assertIn(texts.t("detail.notes.pl", language="en"), englisch)
        self.assertNotIn(texts.t("detail.notes.pl", language="de"), englisch)
        # Preisachse und Heute-Kurve tragen Zahlen: sie muessen in beiden Sprachen
        # dieselben Zahlen tragen, nur der Satz drumherum wechselt.
        self.assertEqual(len(deutsch), len(englisch))
        for satz in englisch:
            self.assertNotIn("\u00fc", satz)

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


class AWorkerThreadDoesNotInheritTheLanguageTest(unittest.TestCase):
    """Kody's Befund: ein ThreadPoolExecutor erbt keine ContextVar.

    Der Fehler waere still: die Antwort bliebe formal englisch, aber Ausschluss- und
    Fehlertexte kaemen deutsch heraus - aus dem Arbeiter, der die Quellsprache sieht.
    Der Test haelt BEIDE Haelften fest: den Bruch (ohne Umhuellung) und die Umhuellung.
    """

    def test_a_bare_worker_reads_the_source_language(self):
        from concurrent.futures import ThreadPoolExecutor

        with texts.use_language("en"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                gesehen = pool.submit(texts.active_language).result()
        self.assertEqual(gesehen, registry.DEFAULT_LANGUAGE)

    def test_a_wrapped_worker_reads_the_language_of_the_request(self):
        from concurrent.futures import ThreadPoolExecutor

        with texts.use_language("en"):
            umhuellt = texts.in_language("en", texts.active_language)
            with ThreadPoolExecutor(max_workers=1) as pool:
                gesehen = pool.submit(umhuellt).result()
        self.assertEqual(gesehen, "en")

    def test_the_wrapper_passes_arguments_and_keeps_them(self):
        from concurrent.futures import ThreadPoolExecutor

        with texts.use_language("en"):
            umhuellt = texts.in_language("en", lambda a, b=0: (texts.active_language(), a, b))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(umhuellt, "x", b=7).result(), ("en", "x", 7))

    def test_the_language_is_restored_after_the_worker(self):
        from concurrent.futures import ThreadPoolExecutor

        with texts.use_language("en"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(texts.in_language("en", texts.active_language)).result()
            self.assertEqual(texts.active_language(), "en")
        self.assertEqual(texts.active_language(), registry.DEFAULT_LANGUAGE)


class TheNumberInASentenceFollowsTheSentenceTest(unittest.TestCase):
    """Kody #121: '12,5 %' in einem englischen Satz ist ein Fehler.

    Die Zahl selbst bleibt Datum - der Rumpf traegt weiter seine Floats, und das
    CSV-Format ist unberuehrt. Nur ihre SCHREIBWEISE im Satz gehoert zur Sprache.
    """

    def test_the_decimal_separator_follows_the_sentence(self):
        from .. import gap_funnel as gf

        with texts.use_language("de"):
            self.assertIn("12,5 %", gf.move_too_far_message(12.5, 10.0))
        with texts.use_language("en"):
            self.assertIn("12.5 %", gf.move_too_far_message(12.5, 10.0))
        self.assertEqual(texts.active_language(), texts.registry.DEFAULT_LANGUAGE)

    def test_the_thousands_grouping_follows_the_sentence(self):
        from .. import gap_funnel as gf

        with texts.use_language("de"):
            self.assertEqual(gf._tausender(45000), "45 000")
        with texts.use_language("en"):
            self.assertEqual(gf._tausender(45000), "45,000")

    def test_a_whole_number_keeps_no_dead_zeros_in_either_language(self):
        from .. import gap_funnel as gf

        for code in texts.languages():
            with self.subTest(language=code), texts.use_language(code):
                self.assertEqual(gf._zahl(20.0), "20")

    def test_the_payload_still_carries_the_plain_float(self):
        """Die Zahl im Rumpf ist Datum - der Satz ist der einzige Ort mit Schreibweise."""
        from .. import gap_funnel as gf

        with texts.use_language("en"):
            satz = gf.move_too_far_message(12.5, 10.0)
        self.assertIn("12.5", satz)
        self.assertNotIn("12,5", satz)


if __name__ == "__main__":
    unittest.main()
