"""Die ZUSAMMENGESETZTEN Meldungen - dort sass der Sprach-Mix.

Ein Katalog allein reicht nicht: diese Saetze entstehen erst beim Zusammenbauen.
Ein deutsches Bruchstueck darin faellt in keiner Einzelpruefung auf, weil jeder
Baustein fuer sich uebersetzt ist. Deshalb wird hier die fertige Meldung in en
GEBAUT und auf deutsche Stoppwoerter geprueft - an den sechs Stellen, die live
nachgewiesen sind.
"""

from __future__ import annotations

import unittest

from paperbroker import texts

GERMAN = ("außerhalb", "gültig", "aufgezählt", " von ", "kein", "Verfall",
          "Tage", "ausgeschlossen", "Laufzeit", "Grund", "je")


class TheComposedMessagesTest(unittest.TestCase):
    def assert_english(self, text: str):
        for wort in GERMAN:
            with self.subTest(wort=wort, text=text):
                self.assertNotIn(wort, text)

    def test_the_maturity_refusal(self):
        from paperbroker.tests.test_screener import TheSpreadSectionSharesTheClockBasis

        fenster = TheSpreadSectionSharesTheClockBasis._window(7, 45)
        weit = [TheSpreadSectionSharesTheClockBasis._leg("AAPL261213C00250000", 1.00, 1.10),
                TheSpreadSectionSharesTheClockBasis._leg("AAPL261213C00252500", 0.45, 0.55)]
        from paperbroker.screener import vertical_spreads
        from paperbroker.tests.test_screener import _DAY_CHANGE

        with texts.use_language("en"):
            _behalten, verworfen = vertical_spreads(weit, fenster, now_fn=_DAY_CHANGE)
        meldung = verworfen[0][1]
        self.assert_english(meldung)
        self.assertIn("outside", meldung)

    def test_the_debit_refusal(self):
        from paperbroker.screener import vertical_spreads
        from paperbroker.tests.test_screener import _LOOSE, _NOW, make

        with texts.use_language("en"):
            _behalten, verworfen = vertical_spreads(make(), _LOOSE, now_fn=_NOW)
        meldungen = [r for _p, r in verworfen if "Debit" in r]
        self.assertTrue(meldungen, "die Fixture traegt ein Debit-Paar ausserhalb")
        for meldung in meldungen:
            with self.subTest(meldung=meldung):
                self.assert_english(meldung)

    def test_the_credit_refusal(self):
        from paperbroker.screener import BULL_PUT_CREDIT, ScreenerCriteria, vertical_spreads
        from paperbroker.tests.test_en_composed_messages import credit_chain

        with texts.use_language("en"):
            _behalten, verworfen = vertical_spreads(
                credit_chain(),
                ScreenerCriteria(max_spread_pct=100.0, dte_min=0, dte_max=400),
                strategy=BULL_PUT_CREDIT,
            )
        meldungen = [r for _p, r in verworfen if "Credit" in r]
        self.assertTrue(meldungen, "die Fixture traegt ein Credit-Paar ausserhalb")
        for meldung in meldungen:
            with self.subTest(meldung=meldung):
                self.assert_english(meldung)

    def test_the_refusal_keeps_its_category_in_both_languages(self):
        """Ein Grund zaehlt nach seinem CODE, nicht nach seinem Satz.

        Kody, PR #128: die Meldung war ein nackter Text. Im Deutschen traf sie
        zufaellig die Textregel "Verfall: Laufzeit ...", im Englischen fiel sie
        in "sonstiges" - derselbe Grund, zwei Kategorien.
        """
        from paperbroker import exclusions
        from paperbroker.screener import vertical_spreads
        from paperbroker.tests.test_screener import TheSpreadSectionSharesTheClockBasis

        fenster = TheSpreadSectionSharesTheClockBasis._window(7, 45)
        weit = [TheSpreadSectionSharesTheClockBasis._leg("AAPL261213C00250000", 1.00, 1.10),
                TheSpreadSectionSharesTheClockBasis._leg("AAPL261213C00252500", 0.45, 0.55)]
        from paperbroker.tests.test_screener import _DAY_CHANGE

        for code in texts.languages():
            with self.subTest(code=code), texts.use_language(code):
                _behalten, verworfen = vertical_spreads(weit, fenster, now_fn=_DAY_CHANGE)
                grund = verworfen[0][1]
                self.assertEqual("dte_outside", grund.code)
                self.assertEqual(exclusions.CAT_DTE, exclusions.categorise(grund))

    def test_the_pair_join(self):
        from paperbroker.gap_funnel import _capped_counts

        with texts.use_language("en"):
            satz = _capped_counts("Search", 5, {"enumerated": 45}, {}, "bull_call_debit")
        self.assert_english(satz)
        self.assertIn(" of ", satz)

    def test_the_exclusion_headline(self):
        from paperbroker import exclusions

        with texts.use_language("en"):
            satz = exclusions.headline({
                "counted": 3,
                "unit": texts.t("exclusion.unit.contracts"),
                "unit_singular": texts.t("exclusion.unit.contract"),
                "categories": [{"label": texts.t("exclusion.category.dte"), "count": 3}],
            })
        self.assert_english(satz or "")

    def test_the_earnings_rule_and_the_notes(self):
        from paperbroker import earnings_calendar as ec

        with texts.use_language("en"):
            regel = texts.t("earnings.exit_rule")
            notizen = [texts.t("earnings.notes.estimated"),
                       texts.t("earnings.notes.necessary_not_sufficient")]
        self.assert_english(regel)
        for notiz in notizen:
            self.assert_english(notiz)
        # Der Modul-Konstante darf die Sprache nicht einfrieren.
        self.assertFalse(hasattr(ec, "EXIT_RULE"),
                         "EXIT_RULE wurde beim Import uebersetzt und kann nie umschalten")


def credit_chain():
    """Eine Kette, deren Put-Credit-Paar mehr zahlt als die Breite (5,0).

    Bull-Put-Credit: verkauft wird der HOHE Strike (105), gekauft der tiefe
    (100). Credit = Bid(105) - Ask(100) = 6,0 - 0,7 = 5,3 > 5,0.
    """
    from paperbroker.tests.test_screener import option, quote

    tief = option("AAPL261218P00100000", option_type="put", strike=100.0)
    hoch = option("AAPL261218P00105000", option_type="put", strike=105.0)
    return [quote(tief, bid=0.5, ask=0.7),
            quote(hoch, bid=6.0, ask=6.2)]
