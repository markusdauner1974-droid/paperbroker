"""The page is ONE gap-spread funnel - and nothing else.

This file used to guard the earnings-overview panel (Slice O). That panel is
gone, together with the earnings calendar, the single-instrument form and the
credit strategies: the page starts at the reaction day and ends at the debit
spread that survived the funnel. What is left to guard is the DEMOLITION - the
parts a later commit could quietly bring back:

* no earnings calendar and no day tabs (``cal-tab``, ``data-scan-day``),
* no earnings overview (``earnings-scan``, ``escan-table``, ``escan-config``),
* no single-instrument form and none of its filters (ticker, expiration,
  liquidity, DTE, IV preference, weights, budget, multiplier, quantity, fees,
  pair search, data mode),
* no exclusion block per unit and no credit strategy (Bull Put, Bear Call),
* no second table: the funnel's one table is the page's only table,
* no inline JavaScript - the behaviour is one static file,
* the old panel scripts are gone from the package and unreferenced,
* the page renders whether or not a calendar adapter answers: it reads none of
  the route's other template variables, so a missing context cannot break it.

Every assertion is measured on the rendered page of the real ``GET /`` with the
fake adapters of ``test_gap_scan_endpoint`` - no socket, no browser.
"""

import re
import unittest
from pathlib import Path

import paperbroker
from paperbroker.tests.test_gap_scan_endpoint import _client

PACKAGE = Path(paperbroker.__file__).parent
STATIC = PACKAGE / "static"

#: Words and ids the order removed from the page. Each one is a piece of the old
#: screen, not a synonym of anything the funnel needs.
FORBIDDEN = [
    # Earnings-Kalender mit den drei Tages-Reitern
    "Earnings-Kalender",
    "Earnings-Aktien scannen",
    "cal-tab",
    "cal-body",
    "data-scan-day",
    # Earnings-Uebersicht
    "Earnings-Übersicht",
    "earnings-scan",
    "escan-table",
    "escan-config",
    "escan-stats",
    # Einzel-Instrument-Formular mit allen Filtern
    "screener-form",
    'id="ticker"',
    'name="expiration"',
    "max_spread_pct",
    "max_spread_abs",
    "min_oi",
    "min_volume",
    "pair_max_width",
    "pair_mode",
    "min_bid_size",
    "min_ask_size",
    "require_oi",
    "require_volume",
    "dte_min",
    "dte_max",
    "max_quote_age_min",
    "ignore_quote_age",
    "data_mode",
    "iv_min",
    "iv_max",
    "iv_pref",
    "IV-Vorliebe",
    "weight_spread",
    "weight_oi",
    "weight_iv",
    "Multiplikator",
    "Stückzahl",
    "quantity",
    "Gebühren gesamt",
    "budget",
    # Spread-Tabelle des Einzelscans und Ausschluss-Block
    "Ausschlüsse nach Einheit",
    "dropped-reasons",
    "vertical-drops",
    "Sortierung",
    # Credit-Strategien
    "Bull Put",
    "Bear Call",
    "bull_put_credit",
    "bear_call_credit",
]


def _page(query=""):
    return _client().get("/" + query).get_data(as_text=True)


class TestTheOldScreenIsGone(unittest.TestCase):
    def test_none_of_the_removed_parts_reach_the_page(self):
        seite = _page()
        for wort in FORBIDDEN:
            with self.subTest(wort=wort):
                self.assertNotIn(wort, seite)

    def test_the_page_has_one_input_and_it_is_the_reaction_day(self):
        seite = _page()
        self.assertEqual(1, seite.count("<input"))
        self.assertIn('<input type="date" id="funnel-day"', seite)
        # keine Zahlenfelder, keine Schalter, keine Auswahlfelder
        for feld in ('type="number"', 'type="checkbox"', 'type="radio"',
                     'type="submit"', "<select", "<fieldset", "<textarea"):
            with self.subTest(feld=feld):
                self.assertNotIn(feld, seite)

    def test_there_is_exactly_one_table_and_it_is_the_funnel(self):
        seite = _page()
        self.assertEqual(1, seite.count("<table"))
        self.assertIn('id="funnel-table"', seite)
        # die siebzehn Spalten des Trichters (drei davon: nötige Bewegung,
        # Schwankung, Treffer), nicht die zwoelf des Einzelscans
        self.assertEqual(17, seite.count("<th>"))

    def test_the_page_carries_no_inline_javascript(self):
        seite = _page()
        self.assertEqual(1, seite.count("<script"))
        self.assertIn('<script src=', seite)
        self.assertIn("/static/gap_funnel.js", seite)
        # kein zweites Inline-Skript, kein Handler im Markup
        self.assertNotIn("onclick=", seite)
        self.assertNotIn("addEventListener", seite)
        self.assertNotIn("fetch(", seite)

    def test_the_old_panel_scripts_are_gone_and_unreferenced(self):
        seite = _page()
        for name in ("gap_scan.js", "earnings_scan.js"):
            with self.subTest(datei=name):
                self.assertFalse((STATIC / name).exists(), f"{name} liegt noch im Paket")
                self.assertNotIn(name, seite)

    def test_the_credit_strategies_are_gone_from_the_panel_too(self):
        quelle = (STATIC / "gap_funnel.js").read_text(encoding="utf-8")
        for wort in ("Bull Put", "Bear Call", "bull_put_credit", "bear_call_credit",
                     "Credit", "Stückzahl", "IV-Vorliebe"):
            with self.subTest(wort=wort):
                self.assertNotIn(wort, quelle)


class TestThePageNeedsNoOtherContext(unittest.TestCase):
    def test_the_page_renders_with_and_without_a_calendar(self):
        mit = _client().get("/")
        ohne = _client().get("/?no_calendar")
        self.assertEqual(200, mit.status_code)
        self.assertEqual(200, ohne.status_code)
        for antwort in (mit, ohne):
            seite = antwort.get_data(as_text=True)
            self.assertIn("Gap-Spread-Trichter", seite)
            self.assertIn('id="gap-funnel"', seite)
            self.assertIn("/static/gap_funnel.js", seite)

    def test_a_full_calendar_changes_nothing_on_the_page(self):
        # Der Adapter liefert Zeilen fuer drei Tage - die Seite liest sie nicht.
        # Waere noch ein Kalender im Markup, stuenden hier Ticker.
        seite = _page()
        for ticker in ("AAA", "BBB", "CCC", "DDD"):
            with self.subTest(ticker=ticker):
                self.assertNotIn(ticker, seite)
        self.assertNotIn("Nasdaq", seite)

    def test_the_order_of_the_page_is_the_order_of_the_funnel(self):
        seite = _page()
        kopf = seite.index("Gap-Spread-Trichter")
        feld = seite.index('id="funnel-day"')
        knopf = seite.index('id="funnel-run"')
        zaehler = seite.index('id="funnel-counts"')
        tabelle = seite.index('id="funnel-table"')
        ausschluesse = seite.index('id="funnel-excluded-block"')
        fehlschlaege = seite.index('id="funnel-failures-block"')
        fuss = seite.index("CBOE-Kursdaten zeitversetzt")
        self.assertEqual([kopf, feld, knopf, zaehler, tabelle, ausschluesse,
                          fehlschlaege, fuss],
                         sorted([kopf, feld, knopf, zaehler, tabelle, ausschluesse,
                                 fehlschlaege, fuss]))

    def test_the_subtitle_names_the_three_filters_of_the_funnel(self):
        seite = _page()
        treffer = re.search(r'<p class="lede">(.*?)</p>', seite, re.S)
        if treffer is None:
            raise AssertionError("kein Untertitel neben dem Kopf")
        untertitel = treffer.group(1)
        # Vier Bedingungen: Gap, Optionen, Verfall nach dem Bericht und die
        # Regel des Trichters - R/R-Floor UND die Grenze der nötigen Bewegung.
        for teil in ("Earnings-Gap", "Optionen", "Verfall nach dem nächsten Bericht",
                     "R/R ≥ 1:2", "nötige Bewegung bis"):
            with self.subTest(teil=teil):
                self.assertIn(teil, untertitel)


if __name__ == "__main__":
    unittest.main()
