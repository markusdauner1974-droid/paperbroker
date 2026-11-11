"""The two strike-search modes: adjacent strikes vs every pair inside a width.

The modes are the answer to one measured problem: with adjacent-only pairing a
single illiquid strike in the middle of three good strikes costs BOTH adjacent
pairs, although the outer pair is a perfectly valid spread. The regression test
below is that exact chain.

Every case is local and deterministic - no network, no real clock.
"""

import time
import unittest

import arrow

from paperbroker.assets import asset_factory
from paperbroker.quotes import OptionQuote
from paperbroker.screener import (
    BEAR_CALL_CREDIT,
    BEAR_PUT_DEBIT,
    BULL_CALL_DEBIT,
    BULL_PUT_CREDIT,
    MAX_PAIRS_PER_SECTION,
    PAIR_SEARCH_ADJACENT,
    PAIR_SEARCH_MODES,
    PAIR_SEARCH_WITHIN_WIDTH,
    ScreenerCriteria,
    form_vertical_pairs,
    pair_search_report,
    resolve_pair_mode,
    screen,
    vertical_spreads,
)

CLOCK = lambda: arrow.get("2026-10-07T14:35:00")  # noqa: E731 - a fixture
TS = "2026-10-07 14:30:00"                         # 5 min before the clock


def occ(symbol, expiration, strike, side="call"):
    """A real OCC symbol, built from the parts the fixture names."""
    ymd = expiration[2:4] + expiration[5:7] + expiration[8:10]
    return f"{symbol}{ymd}{side[0].upper()}{int(round(strike * 1000)):08d}"


def quote(symbol, expiration, strike, side="call", bid=2.40, ask=2.50,
          oi=1500, volume=300, iv=30.0, stamp=TS):
    """One call/put quote on a real OCC symbol."""
    a = asset_factory(occ(symbol, expiration, strike, side))
    q = OptionQuote(quote_date=TS, asset=a, price=(bid + ask) / 2, bid=bid, ask=ask,
                    bid_size=100, ask_size=100, delta=50, iv=iv, gamma=0.5,
                    vega=5.0, theta=-3.0, rho=0.5, underlying_price=250.0,
                    open_interest=oi, volume=volume, greeks_source="adapter")
    q.quote_timestamp = stamp
    return q


LOOSE = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None)


class TestTheRegressionTheSliceExistsFor(unittest.TestCase):
    """One illiquid strike in the middle must not hide the outer pair.

    Chain: 240 (good) - 245 (illiquid, oi=0) - 250 (good). Measured before this
    slice: the adjacent search pairs (240,245) and (245,250), both die on the
    illiquid leg, and the section is empty although 240/250 is a valid spread.
    """

    def _chain(self):
        exp = "2026-10-30"
        return [
            quote("AAPL", exp, 240.0, bid=5.00, ask=5.10, oi=8000),
            quote("AAPL", exp, 245.0, bid=1.00, ask=1.10, oi=0, iv=0.0),   # illiquid
            quote("AAPL", exp, 250.0, bid=2.40, ask=2.50, oi=4000),
        ]

    def test_adjacent_mode_finds_nothing_on_that_chain(self):
        kette = self._chain()
        kept, dropped = vertical_spreads(kette, LOOSE, strategy=BULL_CALL_DEBIT,
                                         now_fn=CLOCK)
        self.assertEqual([], kept)
        self.assertEqual(2, len(dropped))
        gruende = [reason for _p, reason in dropped]
        # the illiquid leg is named on its own pair; the other adjacent pair dies
        # on the arithmetic that leg produces (measured: "Debit -1.3 außerhalb")
        self.assertTrue(any("illiquide" in r for r in gruende), gruende)
        self.assertTrue(all("245.0" in r or "Debit" in r for r in gruende), gruende)

    def test_within_width_finds_the_outer_pair(self):
        kette = self._chain()
        criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                    pair_mode=PAIR_SEARCH_WITHIN_WIDTH,
                                    pair_max_width=10.0)
        kept, dropped = vertical_spreads(kette, criteria, strategy=BULL_CALL_DEBIT,
                                         now_fn=CLOCK)
        self.assertEqual(1, len(kept), dropped)
        paar = kept[0]
        self.assertEqual(240.0, float(paar.long.asset.strike))
        self.assertEqual(250.0, float(paar.short.asset.strike))
        self.assertEqual(10.0, paar.strike_width)
        # the two adjacent pairs are still rejected - with their own reason
        self.assertEqual(2, len(dropped))

    def test_the_page_wording_of_that_result_is_reproducible(self):
        """The same chain through the screening pipeline: 1 candidate, 2 drops."""
        kette = self._chain()
        criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                    pair_mode=PAIR_SEARCH_WITHIN_WIDTH,
                                    pair_max_width=10.0)
        treffer, _dropped = screen(kette, criteria, now_fn=CLOCK)
        self.assertEqual(2, len(treffer))       # the two liquid contracts
        kept, _ = vertical_spreads(kette, criteria, strategy=BULL_CALL_DEBIT,
                                   now_fn=CLOCK)
        self.assertEqual(1, len(kept))


class TestHowTheModeIsDecided(unittest.TestCase):

    def test_no_mode_and_no_width_is_the_adjacent_search(self):
        self.assertEqual((PAIR_SEARCH_ADJACENT, None),
                         resolve_pair_mode(ScreenerCriteria()))

    def test_no_mode_and_a_width_is_the_wide_search(self):
        """The historical derivation: a width always meant "all pairs inside"."""
        self.assertEqual((PAIR_SEARCH_WITHIN_WIDTH, None),
                         resolve_pair_mode(ScreenerCriteria(pair_max_width=25.0)))

    def test_an_explicit_mode_beats_the_derivation(self):
        mode, error = resolve_pair_mode(ScreenerCriteria(
            pair_mode=PAIR_SEARCH_ADJACENT, pair_max_width=25.0))
        self.assertEqual((PAIR_SEARCH_ADJACENT, None), (mode, error))

    def test_a_zero_width_without_a_mode_is_a_400_not_a_500(self):
        """Kody finding (PR #78): the DERIVED path had no positive-width guard.

        Measured before the fix: /api/screen?pair_max_width=0 raised
        "ValueError: pair search 'within_width' needs a positive max_width" out
        of vertical_spreads - a 500 for a value the form itself offers.
        """
        # zero: the derived path names it ("0 ist keine Suche")
        with self.assertRaises(ValueError) as ctx:
            ScreenerCriteria(pair_max_width=0).validate()
        self.assertIn("pair_max_width", str(ctx.exception))
        self.assertIn("positive Breite", str(ctx.exception))
        # negative: the existing rule already refuses it - ONE reason, not two
        with self.assertRaises(ValueError) as ctx:
            ScreenerCriteria(pair_max_width=-5.0).validate()
        self.assertIn("muss ≥ 0 sein", str(ctx.exception))
        self.assertNotIn("positive Breite", str(ctx.exception))

    def test_the_wide_mode_requires_a_positive_width(self):
        """A width of 0 used to pair nothing and say nothing - measured."""
        for breite in (None, 0, -5.0):
            with self.subTest(breite=breite):
                criteria = ScreenerCriteria(pair_mode=PAIR_SEARCH_WITHIN_WIDTH,
                                            pair_max_width=breite)
                with self.assertRaises(ValueError) as ctx:
                    criteria.validate()
                self.assertIn("pair_mode", str(ctx.exception))
                # ... and the direct call refuses as well, not only the validator
                with self.assertRaises(ValueError):
                    form_vertical_pairs([], strategy=BULL_CALL_DEBIT,
                                        max_width=breite,
                                        mode=PAIR_SEARCH_WITHIN_WIDTH)

    def test_a_leftover_width_in_adjacent_mode_is_named_not_rejected(self):
        """Switching back to adjacent must not hit a wall.

        The form keeps the width field; refusing the request would blame the
        operator for a number they cannot see. The width is ignored and the
        answer says so.
        """
        criteria = ScreenerCriteria(pair_mode=PAIR_SEARCH_ADJACENT,
                                    pair_max_width=25.0)
        criteria.validate()                     # no error
        report = pair_search_report(criteria)
        self.assertTrue(report["width_ignored"])
        self.assertIsNone(report["max_width"])
        self.assertIn("nicht verwendet", report["note"])

    def test_an_unknown_mode_is_refused_with_the_allowed_values(self):
        with self.assertRaises(ValueError) as ctx:
            ScreenerCriteria(pair_mode="irgendwas").validate()
        text = str(ctx.exception)
        self.assertIn("pair_mode", text)
        for mode in PAIR_SEARCH_MODES:
            self.assertIn(mode, text)


class TestBothModesOnAllFourStrategies(unittest.TestCase):
    """The search is a property of the pairing, not of one strategy."""

    def _chain(self, side):
        exp = "2026-10-30"
        strikes = [240.0, 245.0, 250.0, 255.0]
        return [quote("AAPL", exp, k, side,
                      oi=0 if k == 245.0 else 4000) for k in strikes]

    def test_adjacent_pairs_are_counted_per_strategy(self):
        for strategy, side in ((BULL_CALL_DEBIT, "call"), (BEAR_CALL_CREDIT, "call"),
                               (BEAR_PUT_DEBIT, "put"), (BULL_PUT_CREDIT, "put")):
            with self.subTest(strategy=strategy):
                pairs = form_vertical_pairs(self._chain(side), strategy=strategy,
                                             mode=PAIR_SEARCH_ADJACENT)
                self.assertEqual(3, len(pairs))       # four strikes = three neighbours
                # the trade direction is the strategy's, not the mode's
                for pair in pairs:
                    self.assertEqual(strategy, pair.strategy_id)
                    lang = pair.long.asset.strike
                    kurz = pair.short.asset.strike
                    if strategy in (BEAR_PUT_DEBIT, BEAR_CALL_CREDIT):
                        self.assertGreater(lang, kurz)     # buys the HIGHER strike
                    else:
                        self.assertLess(lang, kurz)

    def test_the_wide_mode_finds_the_same_pairs_plus_the_wider_ones(self):
        for strategy, side in ((BULL_CALL_DEBIT, "call"), (BEAR_CALL_CREDIT, "call"),
                               (BEAR_PUT_DEBIT, "put"), (BULL_PUT_CREDIT, "put")):
            with self.subTest(strategy=strategy):
                paare = form_vertical_pairs(self._chain(side), strategy=strategy,
                                            mode=PAIR_SEARCH_WITHIN_WIDTH,
                                            max_width=15.0)
                # 240-245, 240-250, 240-255, 245-250, 245-255, 250-255
                self.assertEqual(6, len(paare))
                breiten = {p.strike_width for p in paare}
                self.assertEqual({5.0, 10.0, 15.0}, breiten)


class TestTheBoundedSearch(unittest.TestCase):
    """A cap may cost pairs - it may never cost the truth about it."""

    def _wide_chain(self, strikes):
        exp = "2027-01-15"
        return [quote("BIG", exp, 100.0 + i, bid=2.40, ask=2.50)
                for i in range(strikes)]

    def test_a_small_chain_reports_a_complete_search(self):
        meldung = {}
        form_vertical_pairs(self._wide_chain(20), strategy=BULL_CALL_DEBIT,
                            mode=PAIR_SEARCH_WITHIN_WIDTH, max_width=5.0,
                            coverage=meldung)
        self.assertEqual(PAIR_SEARCH_WITHIN_WIDTH, meldung["mode"])
        self.assertFalse(meldung["capped"])
        self.assertEqual(meldung["possible"], meldung["enumerated"])
        self.assertTrue(meldung["possible_exact"])

    def test_the_cap_is_named_and_the_work_stays_bounded(self):
        """Kody finding (PR #78): the ceiling must bound WORK, not only output.

        Before the fix the whole n^2/2 list was allocated and then trimmed, so a
        user-chosen width of 100 000 $ on a long chain cost hundreds of megabytes
        before anything was reported. Measured here: the ceiling is enforced
        inside the loop, the answer says `capped`, and `possible` is honestly a
        LOWER bound from then on (possible_exact: False).
        """
        from unittest import mock

        from paperbroker import screener as screener_mod

        kette = self._wide_chain(320)          # 320 strikes -> 51 040 possible pairs
        grenze = 500                           # a small ceiling keeps the test fast
        meldung = {}
        start = time.perf_counter()
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", grenze):
            paare = form_vertical_pairs(kette, strategy=BULL_CALL_DEBIT,
                                        mode=PAIR_SEARCH_WITHIN_WIDTH,
                                        max_width=100_000.0,   # far beyond the chain
                                        coverage=meldung)
        dauer = time.perf_counter() - start
        self.assertTrue(meldung["capped"])
        self.assertEqual(grenze, len(paare))
        self.assertEqual(grenze, meldung["enumerated"])
        self.assertEqual(grenze, meldung["possible"])
        # the count is no longer exact - and it says so instead of "N von N"
        self.assertFalse(meldung["possible_exact"])
        # bounded time: without the in-loop ceiling this would enumerate 51 040
        # pairs; the point is the bound, not a benchmark number
        self.assertLess(dauer, 1.0, f"capped search took {dauer:.3f}s")
        # deterministic truncation: enumeration order (lower strike, then upper),
        # the first pair is the narrowest possible one
        self.assertEqual(1.0, paare[0].strike_width)
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", grenze):
            zweite = form_vertical_pairs(kette, strategy=BULL_CALL_DEBIT,
                                         mode=PAIR_SEARCH_WITHIN_WIDTH,
                                         max_width=100_000.0)
        self.assertEqual([p.symbol for p in paare], [p.symbol for p in zweite])


class TestTheCostOfTheSearch(unittest.TestCase):
    """Measured on representative local data (200 strikes, 4 expiries)."""

    def _chain(self, strikes=200, expiries=4):
        kette = []
        for exp in ("2026-11-20", "2026-12-18", "2027-01-15", "2027-03-19")[:expiries]:
            for i in range(strikes):
                kette.append(quote("BENCH", exp, 100.0 + i, bid=2.40, ask=2.50))
        return kette

    def test_both_modes_stay_well_under_a_second(self):
        kette = self._chain()
        start = time.perf_counter()
        eng = form_vertical_pairs(kette, strategy=BULL_CALL_DEBIT,
                                  mode=PAIR_SEARCH_ADJACENT)
        t_eng = time.perf_counter() - start
        start = time.perf_counter()
        weit = form_vertical_pairs(kette, strategy=BULL_CALL_DEBIT,
                                   mode=PAIR_SEARCH_WITHIN_WIDTH, max_width=25.0)
        t_weit = time.perf_counter() - start
        self.assertEqual(4 * 199, len(eng))
        self.assertGreater(len(weit), len(eng))
        # generous bounds: the point is "no explosion", not a benchmark run
        self.assertLess(t_eng, 1.0, f"adjacent took {t_eng:.3f}s")
        self.assertLess(t_weit, 1.0, f"within-width took {t_weit:.3f}s")


if __name__ == "__main__":
    unittest.main()
