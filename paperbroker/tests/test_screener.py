"""
    Offline tests for the Phase-4 screener (paperbroker/tests/test_screener.py).

    Mock-based OptionQuote fixtures only - no network, deterministic.
"""
import unittest
from datetime import datetime

import arrow

from paperbroker.assets import asset_factory
from paperbroker.quotes import OptionQuote
from paperbroker.screener import (
    OptionScreener,
    ScreenerCriteria,
    ScreenResult,
    TrendSignal,
    filter_data_complete,
    filter_dte,
    filter_iv,
    filter_oi,
    filter_spread,
    filter_stale,
    screen,
)


def option(symbol="AAPL260116C00245000", option_type="call", strike=245.0,
           underlying="AAPL"):
    """Build a parsed Option asset from an OCC symbol via asset_factory."""
    return asset_factory(symbol)


def quote(occ, bid=2.40, ask=2.50, iv=30.0, oi=1500, volume=300,
          underlying_price=250.0, dte=None, greeks_source='adapter', **kw):
    asset = asset_factory(occ)
    q = OptionQuote(
        quote_date="2026-10-07 14:30:00", asset=asset, price=(bid + ask) / 2,
        bid=bid, ask=ask, bid_size=kw.get("bid_size", 100),
        ask_size=kw.get("ask_size", 100),
        delta=kw.get("delta", 50), iv=iv, gamma=kw.get("gamma", 0.5),
        vega=kw.get("vega", 5.0), theta=kw.get("theta", -3.0),
        rho=kw.get("rho", 0.5), underlying_price=underlying_price,
        open_interest=oi, volume=volume, greeks_source=greeks_source)
    if dte is not None:
        # dte is derived from asset/quote_date in real flow; mock override
        q.days_to_expiration = dte
    # adapter-flow attribute: set fresh by default so the staleness guard
    # (enabled by default) does not fail-closed-drop the whole chain
    q.quote_timestamp = kw.get("quote_timestamp", FRESH)
    return q


FRESH = "2026-10-07 14:30:00"


def make(fresh_ts=FRESH):
    """Standard small mock chain (call side)."""
    return [
        quote("AAPL260116C00240000", bid=5.00, ask=5.10, iv=28.0, oi=8000, volume=1200,
              dte=10),                                              # winner
        quote("AAPL260116C00245000", bid=2.40, ask=2.50, iv=30.0, oi=1500, volume=300,
              dte=10),                                              # mid
        quote("AAPL260116C00261000", bid=0.05, ask=0.15, iv=45.0, oi=40, volume=10,
              dte=10),                                              # wide spread + low oi
        quote("AAPL260116C00263000", bid=1.00, ask=1.20, iv=0.0, oi=900, volume=100,
              dte=10),                                              # illiquid (iv=0)
        quote("AAPL260116C00265000", bid=0.80, ask=0.95, iv=33.0, oi=None, volume=100,
              dte=10),                                              # missing OI
        quote("AAPL260216C00270000", bid=1.50, ask=1.55, iv=26.0, oi=2200, volume=500,
              dte=132),                                             # beyond dte_max 45
        quote("AAPL260108C00240000", bid=0.30, ask=0.32, iv=52.0, oi=5000, volume=2000,
              dte=1),                                               # 0dte exclusion (dte < 7)
    ]


class TestPure(unittest.TestCase):
    def test_data_complete_drops_illiquid_only(self):
        kept, dropped = filter_data_complete(make())
        reasons = [r for _, r in dropped]
        # 7 contracts - 1 iv=0 (illiquid); oi=None stays (tri-state tolerant)
        self.assertEqual(len(kept), 6)
        self.assertIn("iv=0 illiquid", " ".join(reasons))

    def test_spread_filter_relative_and_absolute(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=6.0, max_spread_abs=None,
                             min_oi=None, dte_min=None, dte_max=None,
                             max_quote_age_min=None)
        kept, _ = filter_spread(qs, c)
        syms = [q.asset.symbol for q in kept]
        # penny option (0.05/0.15) is wide relative (200 %) -> dropped
        self.assertNotIn("AAPL260116C00261000", syms)
        self.assertIn("AAPL260116C00240000", syms)

    def test_spread_absolute_floor_stabilizes_pennies(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        # 0.05/0.15 penny: abs spread 0.10, rel spread 200 % - the abs floor
        # must catch what the (deliberately wide) pct bound lets through
        c = ScreenerCriteria(max_spread_pct=500.0, max_spread_abs=0.08,
                             min_oi=None, dte_min=None, dte_max=None,
                             max_quote_age_min=None)
        kept, _ = filter_spread(qs, c)
        self.assertNotIn("AAPL260116C00261000", [q.asset.symbol for q in kept])

    def test_oi_filter_with_missing(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=500, dte_min=None, dte_max=None,
                             max_quote_age_min=None, require_oi=True)
        kept, dropped = filter_oi(qs, c)
        self.assertNotIn("AAPL260116C00265000", [q.asset.symbol for q in kept])

    def test_oi_filter_tolerant_mode(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=500, dte_min=None, dte_max=None,
                             max_quote_age_min=None, require_oi=False)
        kept, _ = filter_oi(qs, c)
        self.assertIn("AAPL260116C00265000", [q.asset.symbol for q in kept])

    def test_iv_band(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=None, dte_min=None, dte_max=None,
                             max_quote_age_min=None,
                             iv_min=20, iv_max=40)
        kept, _ = filter_iv(qs, c)
        syms = [q.asset.symbol for q in kept]
        self.assertNotIn("AAPL260108C00240000", syms)   # IV 52 > 40
        self.assertIn("AAPL260116C00240000", syms)      # IV 28 in band

    def test_dte_window(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=None, dte_min=7, dte_max=45,
                             max_quote_age_min=None)
        kept, _ = filter_dte(qs, c)
        syms = [q.asset.symbol for q in kept]
        self.assertNotIn("AAPL260108C00240000", syms)   # DTE 1 < 7
        self.assertNotIn("AAPL260216C00270000", syms)   # DTE 132 > 45


class TestScreenOrchestrator(unittest.TestCase):
    def test_full_pipeline_sorted_by_score(self):
        c = ScreenerCriteria()
        # clock pinned - fixture ts are hardcoded '2026-10-07 14:30'
        now_fn = lambda: arrow.get("2026-10-07T14:35:00")  # noqa: E731
        results, dropped = screen(make(), c, now_fn=now_fn)
        self.assertTrue(results)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # best candidate should be the liquid near-the-money one
        best = results[0]
        self.assertIsInstance(best, ScreenResult)
        self.assertTrue(best.reasons)

    def test_disabled_filters_via_none(self):
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=None, min_volume=None,
                             iv_min=None, iv_max=None,
                             dte_min=None, dte_max=None,
                             max_quote_age_min=None,
                             require_oi=False, require_volume=False)
        results, dropped = screen(make(), c)
        # only tri-state drops (iv=0) remain
        self.assertEqual(len(dropped), 1)
        self.assertEqual(len(results), 6)

    def test_trend_annotation_call(self):
        trend = TrendSignal(symbol="AAPL", direction="up", as_of=FRESH)
        # clock pinned: fixture ts are hardcoded '2026-10-07 14:30' -
        # a real now() goes stale (>30 min) and wipes the results
        now_fn = lambda: arrow.get("2026-10-07T14:35:00")  # noqa: E731
        results, _ = screen(make(), ScreenerCriteria(), trend=trend, now_fn=now_fn)
        with_trend = [r for r in results if any("trend up match" in x for x in r.reasons)]
        self.assertTrue(with_trend)

    def test_criteria_validation_rejects_inverted(self):
        with self.assertRaises(ValueError):
            ScreenerCriteria(dte_min=45, dte_max=7).validate()
        with self.assertRaises(ValueError):
            ScreenerCriteria(iv_min=60, iv_max=20).validate()

    def test_freshness_guard_drops_when_unknowable(self):
        c = ScreenerCriteria(max_quote_age_min=10)
        # every fixture ts = 14:30; clock at 15:00 -> 30 min age > 10 limit
        # -> the WHOLE chain must be dropped (fail-closed, not passed)
        now_fn = lambda: datetime(2026, 10, 7, 15, 0)  # noqa: E731
        qs, dropped = filter_stale_mock(c, now_fn)
        self.assertEqual(len(qs), 0)
        self.assertEqual(len(dropped), 7)
        self.assertIn("stale", dropped[0][1])

    def test_freshness_guard_drops_on_missing_timestamp(self):
        c = ScreenerCriteria(max_quote_age_min=10)
        now_fn = lambda: datetime(2026, 10, 7, 15, 0)  # noqa: E731
        qs = make()
        for q in qs:
            q.quote_timestamp = None   # adapter could not determine it
        kept, dropped = filter_stale(qs, c, now_fn=now_fn)
        self.assertEqual(len(kept), 0)
        self.assertTrue(all("freshness unknown" in r for _, r in dropped))

    def test_freshness_non_utc_now_converted(self):
        # 14:35-04:00 == 18:35 UTC; ts 14:30 UTC -> age 245 min > 30 limit
        # -> the offset MUST be converted (naive-stripping would hide it
        # and keep every quote). Fail-closed chain drop expected.
        c = ScreenerCriteria(max_quote_age_min=30)
        now_fn = lambda: arrow.get("2026-10-07T14:35:00-04:00")  # noqa: E731
        qs = make()
        for q in qs:
            q.quote_timestamp = "2026-10-07 14:30:00"  # UTC wall time
        kept, dropped = filter_stale(qs, c, now_fn=now_fn)
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(dropped), 7)
        self.assertIn("stale 245 min", dropped[0][1])

    def test_iv_pref_validated(self):
        for bad in ("High", "sell", 1):
            with self.assertRaises(ValueError):
                ScreenerCriteria(iv_pref=bad).validate()
        for good in (None, "high", "low"):
            ScreenerCriteria(iv_pref=good).validate()

    def test_iv_pref_ranking_only(self):
        # ranking only - with this fixture 'high' must put IV 52 first
        # and 'low' must put IV 26 first (a neutral score with both
        # calls would rank by tie-break instead and pass this test)
        qs, _ = filter_spread(make(), ScreenerCriteria(
            max_spread_pct=None, max_spread_abs=None, min_oi=None,
            dte_min=None, dte_max=None, max_quote_age_min=None))
        base = dict(max_spread_pct=None, max_spread_abs=None,
                    min_oi=None, dte_min=None, dte_max=None,
                    max_quote_age_min=None,
                    iv_min=20, iv_max=60,
                    weight_spread=None, weight_oi=None,
                    weight_iv=1.0)
        high, _ = screen(qs, ScreenerCriteria(**{**base, 'iv_pref': 'high'}))
        low, _ = screen(qs, ScreenerCriteria(**{**base, 'iv_pref': 'low'}))
        self.assertEqual(high[0].quote.iv, 52.0)
        self.assertEqual(low[0].quote.iv, 26.0)

    def test_screener_facade(self):
        s = OptionScreener(ScreenerCriteria())
        # clock pinned - fixture ts are hardcoded '2026-10-07 14:30'
        now_fn = lambda: arrow.get("2026-10-07T14:35:00")  # noqa: E731
        results, _ = s.screen(make(), now_fn=now_fn)
        self.assertTrue(results)


def filter_stale_mock(criteria, now_fn):
    from paperbroker.screener import filter_stale
    qs = make()
    # quote_timestamp attribute set by the adapter - set here explicitly
    for q in qs:
        q.quote_timestamp = "2026-10-07 14:30:00"
    return filter_stale(qs, criteria, now_fn=now_fn)



class TestClockErrorLabel(unittest.TestCase):
    """B-17: fail-closed stays, but the REASON must be true.

    Measured 2026-10-10: a caller passing an arrow OBJECT instead of a
    function as now_fn received 'freshness unknown (unparseable timestamp)'
    for 67 contracts, although all 104 carried a valid timestamp. The reason
    text is what UX-1 puts on the page.
    """

    TS = "2026-10-07 14:30:00"          # valid, 30 min before the clock below
    LATER = "2026-10-07T15:01:00"       # 31 min -> stale (boundary is ">")

    def _chain(self):
        qs = make()
        for q in qs:
            q.quote_timestamp = self.TS
        return qs

    def _chain_with_ts(self, value):
        qs = make()
        for q in qs:
            q.quote_timestamp = value
        return qs

    def _run(self, chain, now_fn):
        return filter_stale(chain, ScreenerCriteria(max_quote_age_min=30),
                            now_fn=now_fn)

    def test_a_failing_clock_is_labelled_clock_error(self):
        # an arrow object instead of a callable: now_fn() raises TypeError.
        # The FULL label is pinned - a prefix assertion would also match the
        # old, wrong text.
        kept, dropped = self._run(self._chain(), arrow.get(self.LATER))
        self.assertEqual(kept, [])                    # fail-closed, as before
        self.assertEqual(len(dropped), 7)
        self.assertEqual({r for _q, r in dropped},
                         {"freshness unknown (clock error)"})

    def test_a_raising_clock_is_labelled_clock_error_too(self):
        # not TypeError-specific: any clock failure is an internal error
        def boom():
            raise RuntimeError("clock down")

        kept, dropped = self._run(self._chain(), boom)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 7)
        self.assertEqual({r for _q, r in dropped},
                         {"freshness unknown (clock error)"})

    def test_the_age_boundary_is_exclusive_and_keeps_minutes(self):
        # pin: measured 2026-10-10 - "if age_s > max_age_s" keeps a quote of
        # EXACTLY max_quote_age_min (30 min) and drops it at 31 min; the
        # normal path must keep reporting the age in minutes
        kept, dropped = self._run(self._chain(),
                                  lambda: arrow.get("2026-10-07T15:00:00"))
        self.assertEqual(len(kept), 7)          # exactly 30 min -> kept
        self.assertEqual(dropped, [])
        kept, dropped = self._run(self._chain(), lambda: arrow.get(self.LATER))
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 7)
        self.assertEqual({r for _q, r in dropped}, {"stale 31 min"})

    def test_a_missing_timestamp_keeps_its_own_label(self):
        # pin: already correct today - must not be folded into the other two
        kept, dropped = self._run(self._chain_with_ts(None),
                                  lambda: arrow.get(self.LATER))
        self.assertEqual(kept, [])
        self.assertEqual({r for _q, r in dropped},
                         {"freshness unknown (no quote_timestamp)"})

    def test_an_unreadable_timestamp_keeps_its_unparseable_label(self):
        # pin: only a really unreadable stamp may say "unparseable"
        kept, dropped = self._run(self._chain_with_ts("gestern abend"),
                                  lambda: arrow.get(self.LATER))
        self.assertEqual(kept, [])
        self.assertEqual({r for _q, r in dropped},
                         {"freshness unknown (unparseable timestamp)"})


if __name__ == "__main__":
    unittest.main()
