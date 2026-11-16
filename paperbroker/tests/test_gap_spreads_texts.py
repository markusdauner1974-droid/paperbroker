"""Die Saetze der Gap-Spread-Route, wortgetreu gepinnt.

Diese Meldungen entstehen INLINE in der Route, teils aus mehreren Fragmenten.
Fuenf von ihnen erreicht der Abdruck ueber HTTP (ungueltiges Symbol, leeres
Symbol, unvollstaendige Struktur, nicht im Kalender, Verfall nicht gelistet);
drei erreicht er NICHT, weil sie einen Lauf ohne Berichtstermin, einen Lauf mit
Zeitgrenze oder eine unlesbare Kette brauchen:

* ``gap_spreads.note.finnhub_missing``
* ``gap_spreads.note.deadline``
* ``gap_spreads.error.chain_unreadable``

Genau diese drei werden hier wortgetreu festgehalten - ein Tippfehler im Katalog
soll nicht davon abhaengen, ob ein Testaufbau zufaellig in den Zweig laeuft.
"""

from __future__ import annotations

import unittest

from ..texts import t


class GapSpreadsSentencesTest(unittest.TestCase):
    """Jeder Satz steht so im Buch, wie er vorher im Code stand."""

    def test_the_sentences_are_word_for_word_the_old_ones(self):
        erwartet = {
            "gap_spreads.note.finnhub_missing": (
                "Ohne FINNHUB_API_KEY lässt sich kein Berichtstermin lesen - "
                "jeder Kandidat endet dann bei no_next_earnings."
            ),
            "gap_spreads.note.deadline": (
                "{count} Kandidat(en) wurden innerhalb von {seconds} s nicht "
                "fertig geprüft (deadline_exceeded)."
            ),
            "gap_spreads.error.invalid_symbol": "{symbol} ist kein gültiges Symbol.",
            "gap_spreads.error.incomplete_structure": (
                "expiration, long und short benennen EINE Struktur - alle drei "
                "oder keine (gegeben: {given})."
            ),
            "gap_spreads.error.not_in_calendar": (
                "{symbol} steht am {day} nicht im Reaktionskalender - zu diesem "
                "Titel gibt es an diesem Tag keine Zeile."
            ),
            "gap_spreads.error.gap_not_measurable": (
                "Der Gap von {symbol} ließ sich am {day} nicht messen ({code})."
            ),
            "gap_spreads.error.no_candidate": (
                "{symbol} ist am {day} kein Kandidat des Trichters (Schwelle "
                "{threshold} %, Umsatz >= {turnover} USD, Kurs >= {price} USD)."
            ),
            "gap_spreads.error.chain_unreadable": (
                "Die Optionskette von {symbol} ist nicht lesbar."
            ),
            "gap_spreads.error.expiration_not_listed": (
                "Der Verfall {expiration} ist für {symbol} nicht gelistet "
                "(gelistet: {listed})."
            ),
            "gap_spreads.error.strike_missing": (
                "Der Strike {strike} fehlt in der Kette von {symbol} zum "
                "Verfall {expiration}."
            ),
        }
        for schluessel, satz in erwartet.items():
            with self.subTest(schluessel=schluessel):
                # Der Platzhalter selbst ist der Beweis: t() setzt nur ein,
                # was hereinkommt, also ist der rohe Satz die Vorlage.
                self.assertEqual(t(schluessel), satz)


class TheVariablesAreFilledTest(unittest.TestCase):
    """Die zwei Notizen setzen ihre Zahlen ein, ohne sie umzuformatieren."""

    def test_the_deadline_note_takes_count_and_seconds(self):
        self.assertEqual(
            t("gap_spreads.note.deadline", count="3", seconds="8"),
            "3 Kandidat(en) wurden innerhalb von 8 s nicht fertig "
            "geprüft (deadline_exceeded).",
        )

    def test_the_candidate_note_takes_the_three_limits(self):
        self.assertEqual(
            t("gap_spreads.error.no_candidate", symbol="AAA", day="2026-11-12",
              threshold="3.0", turnover="1000000", price="5.00"),
            "AAA ist am 2026-11-12 kein Kandidat des Trichters (Schwelle "
            "3.0 %, Umsatz >= 1000000 USD, Kurs >= 5.00 USD).",
        )


if __name__ == "__main__":
    unittest.main()
