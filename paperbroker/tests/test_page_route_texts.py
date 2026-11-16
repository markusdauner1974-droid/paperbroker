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
            "page_route.hint.unknown_params": "Unbekannte Parameter ignoriert: {names}",
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


if __name__ == "__main__":
    unittest.main()
