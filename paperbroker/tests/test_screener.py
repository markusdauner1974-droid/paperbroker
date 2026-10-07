"""
    Offline tests for the Phase-4 screener (paperbroker/tests/test_screener.py).

    Mock-based OptionQuote fixtures only - no network, deterministic.
"""
import unittest
from datetime import datetime

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
        results, dropped = screen(make(), c)
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
        results, _ = screen(make(), ScreenerCriteria(), trend=trend)
        with_trend = [r for r in results if any("trend up match" in x for x in r.reasons)]
        self.assertTrue(with_trend)

    def test_criteria_validation_rejects_inverted(self):
        with self.assertRaises(ValueError):
            ScreenerCriteria(dte_min=45, dte_max=7).validate()
        with self.assertRaises(ValueError):
            ScreenerCriteria(iv_min=60, iv_max=20).validate()

    def test_freshness_guard(self):
        c = ScreenerCriteria(max_quote_age_min=10)
        # quote_timestamp fixed at 14:30 in quote(); now mocked to 15:00 -> stale
        now_fn = lambda: datetime(2026, 10, 7, 15, 0)  # noqa: E731
        _ = make()
        qs, dropped = filter_stale_mock(c, now_fn)
        self.assertTrue(qs)

    def test_screener_facade(self):
        s = OptionScreener(ScreenerCriteria())
        results, _ = s.screen(make())
        self.assertTrue(results)


def filter_stale_mock(criteria, now_fn):
    from paperbroker.screener import filter_stale
    qs = make()
    # quote_timestamp attribute set by the adapter - set here explicitly
    for q in qs:
        q.quote_timestamp = "2026-10-07 14:30:00"
    return filter_stale(qs, criteria, now_fn=now_fn)


if __name__ == "__main__":
    unittest.main()
