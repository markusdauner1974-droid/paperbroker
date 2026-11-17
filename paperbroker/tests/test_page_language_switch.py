"""Die sichtbare Sprachumschaltung der Seite - das Verhalten, nicht das Aussehen.

Was hier gepinnt wird, ist die Kette, die einen Sprach-Mix verhindert: die Anfrage
waehlt die Sprache, das Formular traegt sie weiter, und das Skript fragt seine
Antworten in derselben Sprache nach. Faellt ein Glied aus, mischt die Seite zwei
Sprachen - und genau das sieht man erst im Betrieb.
"""

from __future__ import annotations

import html
import re
import unittest
import urllib.parse

from paperbroker import texts
from paperbroker.tests.test_gap_funnel import DAY  # noqa: F401  (Fixture-Tag)
from paperbroker.tests.test_gap_spreads_detail_endpoint import _client


def _seite(*, lang: str | None = None):
    client = _client()
    url = "/" if lang is None else "/?lang=" + lang
    return client, client.get(url)


class ThePageFollowsTheLanguageTest(unittest.TestCase):
    def test_the_page_answers_in_english_when_asked(self):
        _client_obj, antwort = _seite(lang="en")
        self.assertEqual(200, antwort.status_code)
        rumpf = antwort.get_data(as_text=True)
        self.assertIn(texts.t("page.head.title", language="en"), rumpf)
        self.assertNotIn(texts.t("page.head.title", language="de"), rumpf)

    def test_the_page_stays_german_without_a_wish(self):
        _client_obj, antwort = _seite()
        rumpf = antwort.get_data(as_text=True)
        self.assertIn(texts.t("page.head.title", language="de"), rumpf)
        self.assertNotIn(texts.t("page.head.title", language="en"), rumpf)

    def test_an_unknown_language_is_refused_with_400(self):
        _client_obj, antwort = _seite(lang="xx")
        self.assertEqual(400, antwort.status_code)
        rumpf = antwort.get_data(as_text=True)
        self.assertIn("xx", rumpf, "die Anfrage wird benannt")
        for code in texts.languages():
            with self.subTest(code=code):
                self.assertIn(code, rumpf, "die verfuegbaren Kuerzel stehen da")

    def test_the_refusal_names_the_code_once_and_lists_what_we_have(self):
        """Der 400-Satz nennt das Kuerzel - nicht die Ausnahme.

        Ein frueherer Entwurf setzte ``str(fehler)`` ein und schrieb damit
        "Unbekannte Sprache 'unknown language 'xx''" - die Ausnahme im Satz.
        """
        _client_obj, antwort = _seite(lang="xx")
        rumpf = antwort.get_data(as_text=True)
        self.assertIn("<p>Unbekannte Sprache &#39;xx&#39;. Verfügbar: de, en.</p>", rumpf)
        self.assertNotIn("unknown language", rumpf)

    def test_a_broken_code_is_refused_too(self):
        # " en" und "" sind ABSICHTLICH nicht dabei: ``resolve_language`` schneidet
        # Leerraum ab und nimmt ein leeres Feld als "kein Wunsch". Nur ein Kuerzel,
        # das die Registry nicht kennt, ist ein Fehler.
        for kaputt in ("EN!", "de-DE", "englisch"):
            with self.subTest(code=kaputt):
                _client_obj, antwort = _seite(lang=urllib.parse.quote(kaputt))
                self.assertEqual(400, antwort.status_code)


class TheFormCarriesTheLanguageTest(unittest.TestCase):
    def test_the_hidden_field_holds_the_resolved_language(self):
        for code in texts.languages():
            with self.subTest(code=code):
                _client_obj, antwort = _seite(lang=code)
                rumpf = antwort.get_data(as_text=True)
                self.assertRegex(
                    rumpf,
                    rf'<input[^>]*type="hidden"[^>]*name="lang"[^>]*value="{code}"',
                )

    def test_the_field_sits_inside_the_form(self):
        _client_obj, antwort = _seite(lang="en")
        rumpf = antwort.get_data(as_text=True)
        form = rumpf.index("<form")
        feld = rumpf.index('name="lang"')
        ende = rumpf.index("</form>", form)
        self.assertLess(form, feld)
        self.assertLess(feld, ende)


class TheScriptAsksInTheSameLanguageTest(unittest.TestCase):
    def test_both_fetches_carry_the_language(self):
        from paperbroker.tests.test_gap_funnel_ui import _render

        with texts.use_language("en"):
            panel = _render(actions=["click:0"])
        self.assertEqual(2, len(panel["urls"]))
        for url in panel["urls"]:
            with self.subTest(url=url):
                self.assertEqual(["en"], urllib.parse.parse_qs(
                    urllib.parse.urlsplit(url).query).get("lang"))

    def test_the_block_names_the_language_it_carries(self):
        self.assertEqual("en", texts.block("js", language="en")["lang"])
        self.assertEqual("de", texts.block("js", language="de")["lang"])


class TheNumbersFollowTheLanguageTest(unittest.TestCase):
    """Eine uebersetzte Zahl ist keine uebersetzte Zahl - sie wird geschrieben.

    Der Block nennt die Schreibweise, das Skript benutzt sie. Ohne das steht in
    der englischen Seite "135,00" - ein deutsches Komma mitten im englischen Satz.
    """

    def test_the_block_names_the_number_style(self):
        self.assertEqual({"decimal": ",", "thousands": "."},
                         texts.block("js", language="de")["numbers"])
        self.assertEqual({"decimal": ".", "thousands": ","},
                         texts.block("js", language="en")["numbers"])

    def test_the_detail_writes_the_numbers_of_its_language(self):
        from paperbroker.tests.test_gap_funnel_ui import _render

        with texts.use_language("de"):
            deutsch = _render(actions=["click:0"])["detail"]["text"]
        with texts.use_language("en"):
            englisch = _render(actions=["click:0"])["detail"]["text"]
        self.assertIn("1,35", deutsch)
        self.assertNotIn("1.35", deutsch)
        self.assertIn("1.35", englisch)
        self.assertNotIn("1,35", englisch)


class TheSwitcherTest(unittest.TestCase):
    def test_the_switcher_lists_every_registered_language(self):
        _client_obj, antwort = _seite(lang="en")
        rumpf = antwort.get_data(as_text=True)
        fuss = rumpf[rumpf.index("<footer"):]
        for code in texts.languages():
            with self.subTest(code=code):
                self.assertIn(f">{code.upper()}<", fuss,
                              "der Wechsler kommt aus der Registry")

    def test_the_link_keeps_the_query_and_swaps_only_the_language(self):
        client = _client()
        antwort = client.get("/?day=" + DAY + "&lang=en")
        rumpf = antwort.get_data(as_text=True)
        ziele = [html.unescape(z) for z in
                 re.findall(r'<a[^>]*class="lang-switch"[^>]*href="([^"]*)"', rumpf)]
        self.assertTrue(ziele, "ein Wechsler-Link fehlt")
        for href in ziele:
            fragen = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
            with self.subTest(href=href):
                self.assertEqual([DAY], fragen.get("day"), "die Suche ueberlebt")
                self.assertEqual(["de"], fragen.get("lang"), "nur lang wird getauscht")

    def test_whitespace_and_case_are_tolerated(self):
        _client_obj, antwort = _seite(lang="%20EN%20")
        self.assertEqual(200, antwort.status_code)
        self.assertIn(texts.t("page.head.title", language="en"),
                      antwort.get_data(as_text=True))

    def test_the_current_language_is_not_a_link(self):
        _client_obj, antwort = _seite(lang="en")
        rumpf = antwort.get_data(as_text=True)
        self.assertNotIn('class="lang-switch" href="?lang=en"', rumpf)
        self.assertIn('aria-current="true"', rumpf)
