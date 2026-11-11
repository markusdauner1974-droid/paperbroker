"""The exclusion overview: one number per unit, one reason per item.

The summary has to be honest about what it counted. These tests pin the three
promises the page makes:

* every item is counted ONCE, with the first filter that rejected it;
* contracts, pairs, expiries and stocks are never added up;
* a filter that was switched off is named as "not checked" instead of silently
  missing - and no category appears that the pipeline did not really produce.
"""

import unittest

import arrow

from paperbroker import exclusions
from paperbroker.assets import asset_factory
from paperbroker.quotes import OptionQuote
from paperbroker.screener import (
    BULL_CALL_DEBIT,
    ScreenerCriteria,
    screen,
    vertical_spreads,
)

CLOCK = lambda: arrow.get("2026-10-07T14:35:00")  # noqa: E731 - a fixture
TS = "2026-10-07 14:30:00"


def quote(strike, *, side="call", bid=2.40, ask=2.50, oi=1500, volume=300,
          iv=30.0, stamp=TS, exp="2026-10-30"):
    ymd = exp[2:4] + exp[5:7] + exp[8:10]
    symbol = f"AAPL{ymd}{side[0].upper()}{int(round(strike * 1000)):08d}"
    a = asset_factory(symbol)
    q = OptionQuote(quote_date=TS, asset=a, price=(bid + ask) / 2, bid=bid, ask=ask,
                    bid_size=100, ask_size=100, delta=50, iv=iv, gamma=0.5,
                    vega=5.0, theta=-3.0, rho=0.5, underlying_price=250.0,
                    open_interest=oi, volume=volume, greeks_source="adapter")
    q.quote_timestamp = stamp
    return q


class TestTheCategoriserSpeaksTheScreenerLanguage(unittest.TestCase):
    """Every reason the pipeline really produces has a category."""

    def test_the_contract_reasons_of_the_real_pipeline_are_all_mapped(self):
        kette = [
            quote(240.0, bid=5.00, ask=5.10, oi=8000),          # kept
            quote(245.0, bid=1.00, ask=1.10, oi=0, iv=0.0),     # illiquid data
            quote(250.0, bid=2.40, ask=2.50, oi=100),           # OI too small
            quote(255.0, bid=0.10, ask=3.00, oi=9000),          # spread too wide
            quote(260.0, bid=2.40, ask=2.50, oi=9000, iv=0.0),  # iv=0
            quote(265.0, bid=2.40, ask=2.50, oi=9000, iv=90.0), # IV band
            quote(270.0, bid=0.0, ask=0.0, oi=9000),            # no bid/ask
            quote(275.0, bid=2.40, ask=2.50, oi=9000, stamp="2026-09-01 10:00:00"),
            quote(280.0, bid=2.40, ask=2.50, oi=None, volume=None),   # missing OI
        ]
        criteria = ScreenerCriteria(max_quote_age_min=30, iv_max=60.0)
        _treffer, dropped = screen(kette, criteria, now_fn=CLOCK)
        blasen = exclusions.summarise(dropped, exclusions.LEVEL_CONTRACT, criteria)
        self.assertEqual(len(dropped), blasen["counted"])
        self.assertEqual(len(dropped), sum(k["count"] for k in blasen["categories"]))
        # nothing fell through the mapping - the categories cover the real words
        self.assertEqual([], blasen["unmapped"])
        keys = {k["key"] for k in blasen["categories"]}
        self.assertIn(exclusions.CAT_LIQUIDITY, keys)
        self.assertIn(exclusions.CAT_DATA, keys)
        self.assertIn(exclusions.CAT_AGE, keys)
        self.assertIn(exclusions.CAT_VOLATILITY, keys)

    def test_the_pair_reasons_carry_their_own_categories(self):
        faelle = {
            "Debit -0.50 außerhalb (0, 5.0) - kein Gewinn möglich": exclusions.CAT_PRICE,
            "Credit 6.0 außerhalb (0, 5.0) - kein gültiger Credit": exclusions.CAT_PRICE,
            "Credit nicht bestimmbar - kein gültiger Geld-/Briefkurs": exclusions.CAT_PRICE,
            "Nach Kosten nicht gewinnfähig: Restsaldo -2.00 USD": exclusions.CAT_COSTS,
            "Risikobudget 4200.00 USD > Budget 1000.00 USD": exclusions.CAT_BUDGET,
            "Position 1200.00 USD > Budget 1000.00 USD": exclusions.CAT_BUDGET,
            "Verfall: Laufzeit [2] Tage außerhalb [7, 45]": exclusions.CAT_DTE,
            # the penny-zone bound speaks a different first word - measured on a
            # live chain, where it was the only unmapped contract reason
            "absoluter Spread 0.15$ > 0.10$ (Mitte 0.58)": exclusions.CAT_LIQUIDITY,
            "strike 332.5 call: Spread 13.8% > 10.0%": exclusions.CAT_LIQUIDITY,
            "Spread: kein Geld-/Briefkurs": exclusions.CAT_LIQUIDITY,
            "strike 390.0 call: Spread: kein Geld-/Briefkurs": exclusions.CAT_LIQUIDITY,
        }
        for reason, erwartet in faelle.items():
            with self.subTest(reason=reason):
                self.assertEqual(erwartet, exclusions.categorise(reason))

    def test_a_leg_reason_inside_a_pair_reason_is_read_through(self):
        reason = "strike 245.0 call: OI 40 < 500"
        self.assertEqual(exclusions.CAT_LIQUIDITY, exclusions.categorise(reason))
        reason = "strike 245.0 put: veraltet: 275 Min."
        self.assertEqual(exclusions.CAT_AGE, exclusions.categorise(reason))

    def test_an_unknown_reason_is_named_as_unknown_not_filed_away(self):
        blasen = exclusions.summarise([("x", "irgendwas Neues")],
                                      exclusions.LEVEL_CONTRACT)
        self.assertEqual(1, blasen["categories"][0]["count"])
        self.assertEqual(exclusions.CAT_OTHER, blasen["categories"][0]["key"])
        self.assertEqual(["irgendwas Neues"], blasen["unmapped"])


class TestTheUnitsAreNeverSummed(unittest.TestCase):

    def test_every_summary_counts_exactly_one_unit(self):
        vertraege = exclusions.summarise([("q", "OI 40 < 500"), ("q", "Oi 40 < 500")]
                                         if False else [("q", "OI 40 < 500")] * 3,
                                         exclusions.LEVEL_CONTRACT)
        paare = exclusions.summarise([("p", "Debit -0.5 außerhalb (0, 5.0)")] * 2,
                                     exclusions.LEVEL_PAIR)
        self.assertEqual(("contract", "Kontrakte", 3),
                         (vertraege["level"], vertraege["unit"], vertraege["counted"]))
        self.assertEqual(("pair", "Spread-Kombinationen", 2),
                         (paare["level"], paare["unit"], paare["counted"]))
        # there is no grand total anywhere - and no key that looks like one
        for block in (vertraege, paare):
            self.assertNotIn("total", block)
            self.assertNotIn("sum", block)

    def test_combine_orders_the_blocks_from_finest_to_coarsest(self):
        bloecke = exclusions.combine([
            exclusions.summarise(["Verfall: Laufzeit [2] Tage außerhalb [7, 45]"],
                                 exclusions.LEVEL_EXPIRY),
            exclusions.summarise([("q", "OI 1 < 500")], exclusions.LEVEL_CONTRACT),
            exclusions.summarise([("p", "Debit -1 außerhalb (0, 5.0)")], exclusions.LEVEL_PAIR),
        ])
        self.assertEqual(["contract", "pair", "expiry"], [b["level"] for b in bloecke])

    def test_the_first_reason_only_promise_is_written_down(self):
        blasen = exclusions.summarise([("q", "OI 40 < 500")], exclusions.LEVEL_CONTRACT)
        self.assertTrue(blasen["first_reason_only"])
        self.assertIn("erste Filter", blasen["first_reason_note"])
        self.assertIn("nicht gezählt", blasen["first_reason_note"])
        self.assertIn("Kontrakt", blasen["first_reason_note"])


class TestTheSwitchedOffFiltersAreNamed(unittest.TestCase):

    def test_no_age_guard_is_reported_as_not_checked(self):
        criteria = ScreenerCriteria(max_quote_age_min=None)
        blasen = exclusions.summarise([("q", "OI 1 < 500")],
                                      exclusions.LEVEL_CONTRACT, criteria)
        self.assertIn("Datenalter", blasen["not_checked"])

    def test_no_budget_and_no_iv_band_are_reported_as_not_checked(self):
        blasen = exclusions.summarise([], exclusions.LEVEL_PAIR,
                                      ScreenerCriteria(budget=None))
        self.assertIn("Budget", blasen["not_checked"])
        self.assertIn("Volatilität", blasen["not_checked"])

    def test_only_filters_that_really_did_not_run_are_listed(self):
        # the default criteria leaves the IV band, the budget and the size fields
        # off - so exactly those may appear as "not checked", and the age guard
        # (on by default) may not
        blasen = exclusions.summarise([], exclusions.LEVEL_CONTRACT, ScreenerCriteria())
        self.assertEqual(["Volatilität", "Budget"], blasen["not_checked"])
        # a run with every switch set names nothing
        voll = ScreenerCriteria(iv_min=1.0, iv_max=200.0, budget=100000.0)
        self.assertEqual([], exclusions.summarise([], exclusions.LEVEL_CONTRACT,
                                                  voll)["not_checked"])


class TestTheOverviewOfARealScreen(unittest.TestCase):
    """Measured with the real pipeline, not with hand-written strings."""

    def test_contracts_and_pairs_get_their_own_blocks(self):
        kette = [quote(240.0, bid=5.00, ask=5.10, oi=8000),
                 quote(245.0, bid=2.40, ask=2.50, oi=40),
                 quote(250.0, bid=2.40, ask=2.50, oi=8000)]
        criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None)
        _treffer, dropped = screen(kette, criteria, now_fn=CLOCK)
        _kept, pair_dropped = vertical_spreads(kette, criteria,
                                               strategy=BULL_CALL_DEBIT, now_fn=CLOCK)
        self.assertTrue(dropped)
        self.assertTrue(pair_dropped)
        vertraege = exclusions.summarise(dropped, exclusions.LEVEL_CONTRACT, criteria)
        paare = exclusions.summarise([(p, r) for p, r in pair_dropped if p is not None],
                                     exclusions.LEVEL_PAIR, criteria)
        self.assertGreater(vertraege["counted"], 0)
        self.assertGreater(paare["counted"], 0)
        # the two numbers are of the same KIND only within their own block
        self.assertEqual("Kontrakte", vertraege["unit"])
        self.assertEqual("Spread-Kombinationen", paare["unit"])


class TestTheScanOverview(unittest.TestCase):

    def test_stocks_and_expiries_are_counted_separately(self):
        counts = {"hits": 2, "no_match": 1, "fetch_failed": 2,
                  "not_checked": 3, "window_blocked": 1}
        entries = [{"skipped_expirations": [{"reason": "section_gate"},
                                            {"reason": "expiration_not_checkable"}]},
                   {"skipped_expirations": []}]
        bloecke = exclusions.summarise_scan(counts, entries, ScreenerCriteria())
        self.assertEqual(["symbol", "expiry"], [b["level"] for b in bloecke])
        aktien, verfaelle = bloecke
        self.assertEqual(6, aktien["counted"])       # 2 + 1 + 3 (hits/no_match zaehlen nicht)
        self.assertEqual(2, verfaelle["counted"])
        kategorien = {k["key"]: k["count"] for k in aktien["categories"]}
        self.assertEqual(2, kategorien[exclusions.CAT_DATA])       # fetch_failed
        self.assertEqual(3, kategorien[exclusions.CAT_SCAN_LIMIT])  # not_checked
        self.assertEqual(1, kategorien[exclusions.CAT_DTE])         # window_blocked
        # the two units are not added up anywhere
        self.assertNotEqual(aktien["counted"], aktien["counted"] + verfaelle["counted"])

    def test_the_headline_names_the_unit_and_the_rule(self):
        blasen = exclusions.summarise([("q", "OI 40 < 500"), ("q", "OI 10 < 500")],
                                      exclusions.LEVEL_CONTRACT)
        satz = exclusions.headline(blasen)
        self.assertIn("Kontrakte: 2 ausgeschlossen", satz)
        self.assertIn("Liquidität 2", satz)
        self.assertIn("erste Grund", satz)
        self.assertIsNone(exclusions.headline(
            exclusions.summarise([], exclusions.LEVEL_CONTRACT)))


if __name__ == "__main__":
    unittest.main()
