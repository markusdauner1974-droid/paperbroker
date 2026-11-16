"""Die Texte der Alt-Antwort /api/screen, wortgetreu gepinnt.

Ein Teil dieser Saetze haengt an Zustaenden, die ein Testaufbau nicht ohne
weiteres herstellt: eine nicht gelistete Frist, ein Marktdaten-Fehler, eine
unerwartete Parameterliste, ausgefallene Kalendertage, sechs Quellen-Fehlerklassen.
Der Abdruck (``docs/slice_c_abdruck.py``) erreicht den Normalfall und die
Parameter-400er; alles andere waere geraten.

Deshalb steht hier jeder Satz noch einmal - wortgleich zu dem, was vorher im Code
stand. Der Test haelt die SCHREIBWEISE fest, nicht die Formatierung: die Zahlen
kommen als Text herein, also ist der rohe Satz mit Platzhaltern die Vorlage.
"""

from __future__ import annotations

import unittest

from ..texts import t


class PageRouteSentencesTest(unittest.TestCase):
    def test_the_sentences_are_word_for_word_the_old_ones(self):
        erwartet = {
            "page_route.hint.unknown_params":
                "Unbekannte Parameter ignoriert: {names} (bedienbar sind: {fields})",
            "page_route.error.invalid_ticker": "Ungültiger Ticker - 1-6 Buchstaben A-Z.",
            "page_route.error.invalid_expiration": "Ungültiges Verfallsdatum - JJJJ-MM-TT.",
            "page_route.error.expiration_not_listed":
                "Verfallsdatum ist für diesen Ticker nicht gelistet.",
            "page_route.error.market_data": "Marktdaten-Fehler - Details im Server-Log.",
            "page_route.error.source_unreadable":
                "Quelle nicht lesbar - Details im Server-Log.",
            "page_route.feed_note.cboe":
                "CBOE-Kursdaten zeitversetzt – ca. 15 Minuten hinter Echtzeit",
            "page_route.option_note.unavailable":
                "Options-Liste nicht abrufbar - ungefiltert",
            "page_route.option_note.hidden":
                "{count} Titel ohne gelistete Optionen ausgeblendet",
            "page_route.option_note.hidden_none":
                "kein Titel ohne gelistete Optionen ausgeblendet",
            "page_route.option_note.stale_suffix": " (Options-Liste aus letztem Stand)",
            "page_route.earnings.note.no_source": "keine Earnings-Quelle konfiguriert",
            "page_route.earnings.note.source_silent": "Quelle antwortet nicht",
            "page_route.earnings.note.last_state":
                "letzter Stand, Quelle antwortet nicht",
            "page_route.earnings.note.one_day_failed":
                "ein Tag konnte nicht abgerufen werden",
            "page_route.earnings.note.days_failed":
                "{count} Tage konnten nicht abgerufen werden",
            "page_route.confirmation.confirmed": "bestätigt",
            "page_route.confirmation.estimated": "geschätzt",
            "page_route.session.before": "vorbörslich",
            "page_route.session.after": "nachbörslich",
            "page_route.status.unknown_time": "Zeitpunkt unbekannt",
            "page_route.failure.source_blocked": "Quelle blockt die Anfrage",
            "page_route.failure.source_throttled": "Quelle drosselt die Anfragen",
            "page_route.failure.source_unreachable": "Quelle nicht erreichbar",
            "page_route.failure.invalid_payload": "Quelle liefert unbrauchbare Daten",
            "page_route.failure.invalid_json": "Quelle liefert kein JSON",
            "page_route.failure.recent_failure": "letzter Versuch gerade gescheitert",
            "page_route.failure.source_reports": "Quelle meldet {code}",
        }
        for schluessel, satz in erwartet.items():
            with self.subTest(schluessel=schluessel):
                self.assertEqual(t(schluessel), satz)


class TheDerivedTablesStayInStepTest(unittest.TestCase):
    """Die drei abgeleiteten Mappen nennen dieselben Woerter wie das Buch."""

    def test_the_badge_tables_match_the_book(self):
        from ..routes import page

        self.assertEqual(page._CONFIRMATION_TEXT["confirmed"], "bestätigt")
        self.assertEqual(page._CONFIRMATION_TEXT["estimated"], "geschätzt")
        self.assertEqual(page._SESSION_TEXT["before_market"], "vorbörslich")
        self.assertEqual(page._SESSION_TEXT["after_market"], "nachbörslich")
        self.assertEqual(page._SESSION_UNKNOWN_TEXT, "Zeitpunkt unbekannt")

    def test_every_failure_class_has_a_sentence(self):
        from ..routes import page

        for code, satz in page._FAILURE_REASONS.items():
            with self.subTest(code=code):
                self.assertTrue(satz, f"{code} hat keinen Satz")
                self.assertEqual(satz, t(f"page_route.failure.{code}"))


class TheUnknownParameterHintIsBuiltTest(unittest.TestCase):
    """Kody hat den Schluessel zu Recht als tot bezeichnet - jetzt ist er es nicht mehr.

    Der Code baute den Satz weiter selbst, waehrend der Katalogschluessel daneben
    stand; ein Pin-Test haette dann eine Fassung festgehalten, die niemand sieht.

    ZWEI Befunde dazu, beide hier festgehalten statt still entschieden:

    1. Der Satz hat zwei Platzhalter - die Liste der bedienbaren Felder gehoert in
       den Satz, nicht in den Aufrufer. So steht es jetzt.
    2. Der Hinweis wird von KEINEM Template gelesen und steht in KEINEM JSON. Er
       reist im Template-Kontext und endet dort. Das war vorher schon so und ist
       nicht Teil dieses PR - der Satz selbst bleibt darum wortgleich.

    Geprueft wird der Kontext, den die Route an ``render_template`` uebergibt: das
    ist die Stelle, an der der Satz tatsaechlich entsteht.
    """

    def test_an_unknown_parameter_produces_the_catalogued_hint(self):
        from unittest import mock

        from ..routes import page as page_route
        from ..server import create_app
        from .test_eva_quality import _clock, _MockAdapter, make

        app = create_app(quote_adapter=_MockAdapter(make()), screener=None,
                         now_fn=_clock())
        app.config["TESTING"] = True
        echt = page_route.render_template
        gesehen = {}

        def merke(name, **kontext):
            gesehen.update(kontext)
            return echt(name, **kontext)

        with mock.patch.object(page_route, "render_template", merke):
            antwort = app.test_client().get(
                "/", query_string={"ticker": "SPY",
                                    "expiration": "2026-10-16",
                                    "quatsch": "1"})
        self.assertEqual(antwort.status_code, 200)
        felder = (
            "dte_min, dte_max, min_oi, min_volume, max_quote_age_min, "
            "min_bid_size, min_ask_size, multiplier, quantity, max_spread_pct, "
            "max_spread_abs, pair_max_width, iv_min, iv_max, weight_spread, "
            "weight_oi, weight_iv, fees, budget, require_oi, require_volume, "
            "iv_pref, pair_mode, data_mode, ignore_quote_age, sort"
        )
        self.assertEqual(
            gesehen["hint"],
            f"Unbekannte Parameter ignoriert: quatsch (bedienbar sind: {felder})",
        )


if __name__ == "__main__":
    unittest.main()
