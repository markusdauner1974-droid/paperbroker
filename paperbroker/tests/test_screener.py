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
    filter_volume,
    screen,
)


def option(symbol="AAPL261016C00245000", option_type="call", strike=245.0,
           underlying="AAPL"):
    """Build a parsed Option asset from an OCC symbol via asset_factory."""
    return asset_factory(symbol)


_LOOSE = ScreenerCriteria(max_spread_pct=100.0)
# The pinned scan clock. It is deliberately BEFORE every fixture expiry: D8
# measures the maturity window on THIS clock (expiration minus now), so a
# clock after the expiries would make every contract honestly expired and the
# whole chain would fall with "DTE -264 < 7". The fixture comments name the
# DTE this clock yields; the timestamp matches the hardcoded quote_date below.
_NOW = lambda: arrow.get('2026-10-07T14:35:00')  # noqa: E731


def quote(occ, bid=2.40, ask=2.50, iv=30.0, oi=1500, volume=300,
          underlying_price=250.0, greeks_source='adapter', **kw):
    # days_to_expiration is NOT settable here: it is derived from the real
    # OCC expiry and the quote_date, exactly as the live flow derives it. The
    # old `dte=` override was the blind spot - it crossed the filter's clock
    # with a stored number the filter never sees.
    asset = asset_factory(occ)
    q = OptionQuote(
        quote_date="2026-10-07 14:30:00", asset=asset, price=(bid + ask) / 2,
        bid=bid, ask=ask, bid_size=kw.get("bid_size", 100),
        ask_size=kw.get("ask_size", 100),
        delta=kw.get("delta", 50), iv=iv, gamma=kw.get("gamma", 0.5),
        vega=kw.get("vega", 5.0), theta=kw.get("theta", -3.0),
        rho=kw.get("rho", 0.5), underlying_price=underlying_price,
        open_interest=oi, volume=volume, greeks_source=greeks_source)
    # adapter-flow attribute: set fresh by default so the staleness guard
    # (enabled by default) does not fail-closed-drop the whole chain
    q.quote_timestamp = kw.get("quote_timestamp", FRESH)
    return q


FRESH = "2026-10-07 14:30:00"


def make(fresh_ts=FRESH):
    """Standard small mock chain (call side).

    Every expiry is a real OCC date; the DTE a test sees is that expiry minus
    the pinned scan clock (_NOW = 2026-10-07), never a stored number.
    """
    return [
        # DTE 9 (2026-10-16 - 2026-10-07), inside the default window [7, 45]
        quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0,
              oi=8000, volume=1200),                              # winner
        quote("AAPL261016C00245000", bid=2.40, ask=2.50, iv=30.0,
              oi=1500, volume=300),                               # mid
        quote("AAPL261016C00261000", bid=0.05, ask=0.15, iv=45.0,
              oi=40, volume=10),                   # wide spread + low oi
        quote("AAPL261016C00263000", bid=1.00, ask=1.20, iv=0.0,
              oi=900, volume=100),                        # illiquide (iv=0)
        quote("AAPL261016C00265000", bid=0.80, ask=0.95, iv=33.0,
              oi=None, volume=100),                              # OI fehlt
        # DTE 132 (2027-02-16 - 2026-10-07) -> beyond dte_max 45
        quote("AAPL270216C00270000", bid=1.50, ask=1.55, iv=26.0,
              oi=2200, volume=500),                      # beyond dte_max 45
        # DTE 2 (2026-10-09 - 2026-10-07) -> below dte_min 7
        quote("AAPL261009C00240000", bid=0.30, ask=0.32, iv=52.0,
              oi=5000, volume=2000),               # below dte_min (dte < 7)
    ]


class TestPure(unittest.TestCase):
    def test_data_complete_drops_illiquid_only(self):
        kept, dropped = filter_data_complete(make())
        reasons = [r for _, r in dropped]
        # 7 contracts - 1 iv=0 (illiquide); oi=None stays (tri-state tolerant)
        self.assertEqual(len(kept), 6)
        self.assertIn("iv=0 illiquide", " ".join(reasons))

    def test_spread_filter_relative_and_absolute(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=6.0, max_spread_abs=None,
                             min_oi=None, dte_min=None, dte_max=None,
                             max_quote_age_min=None)
        kept, _ = filter_spread(qs, c)
        syms = [q.asset.symbol for q in kept]
        # penny option (0.05/0.15) is wide relative (200 %) -> dropped
        self.assertNotIn("AAPL261016C00261000", syms)
        self.assertIn("AAPL261016C00240000", syms)

    def test_spread_absolute_floor_stabilizes_pennies(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        # 0.05/0.15 penny: abs spread 0.10, rel spread 200 % - the abs floor
        # must catch what the (deliberately wide) pct bound lets through
        c = ScreenerCriteria(max_spread_pct=500.0, max_spread_abs=0.08,
                             min_oi=None, dte_min=None, dte_max=None,
                             max_quote_age_min=None)
        kept, _ = filter_spread(qs, c)
        self.assertNotIn("AAPL261016C00261000", [q.asset.symbol for q in kept])

    def test_oi_filter_with_missing(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=500, dte_min=None, dte_max=None,
                             max_quote_age_min=None, require_oi=True)
        kept, dropped = filter_oi(qs, c)
        self.assertNotIn("AAPL261016C00265000", [q.asset.symbol for q in kept])

    def test_oi_filter_tolerant_mode(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=500, dte_min=None, dte_max=None,
                             max_quote_age_min=None, require_oi=False)
        kept, _ = filter_oi(qs, c)
        self.assertIn("AAPL261016C00265000", [q.asset.symbol for q in kept])

    def test_iv_band(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=None, dte_min=None, dte_max=None,
                             max_quote_age_min=None,
                             iv_min=20, iv_max=40)
        kept, _ = filter_iv(qs, c)
        syms = [q.asset.symbol for q in kept]
        self.assertNotIn("AAPL261009C00240000", syms)   # IV 52 > 40
        self.assertIn("AAPL261016C00240000", syms)      # IV 28 in band

    def test_dte_window(self):
        qs = make()
        qs, _ = filter_data_complete(qs)
        c = ScreenerCriteria(max_spread_pct=None, max_spread_abs=None,
                             min_oi=None, dte_min=7, dte_max=45,
                             max_quote_age_min=None)
        # D8: the window is measured against the SCAN clock, so the clock is
        # injected. A bare filter_dte() call would read the wall clock and the
        # expectation would drift with the day the suite runs.
        kept, _ = filter_dte(qs, c, now_fn=_NOW)
        syms = [q.asset.symbol for q in kept]
        # honestly computed from the OCC expiry minus _NOW - not the stored
        # days_to_expiration field
        self.assertEqual(
            asset_factory("AAPL261009C00240000").get_days_to_expiration(_NOW()), 2)
        self.assertEqual(
            asset_factory("AAPL270216C00270000").get_days_to_expiration(_NOW()), 132)
        self.assertNotIn("AAPL261009C00240000", syms)   # DTE 2 < 7
        self.assertNotIn("AAPL270216C00270000", syms)   # DTE 132 > 45


class TestTheWindowIsMeasuredOnTheScanClock(unittest.TestCase):
    """D8 (E1 = c): the SAME contract flips with the clock of the scan.

    The stored days_to_expiration keeps the DATA's own maturity (it is the
    basis of the Greeks); the window is expiry minus the clock of THIS scan.
    Both numbers below are read from the asset via get_days_to_expiration(),
    never from the stored field, so the assertion cannot drift with a mocked
    value - that mock is exactly what hid the bug this slice fixes.
    """

    def test_a_past_expiry_drops_and_a_future_one_keeps(self):
        q = quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0,
                  oi=8000, volume=1200)
        # a deliberately WRONG stored value: a filter that read the field
        # would call this contract 999 days out and keep it either way
        q.days_to_expiration = 999
        fenster = ScreenerCriteria(dte_min=7, dte_max=45)
        # clock AFTER the expiry -> honestly expired by four days
        nach = lambda: arrow.get("2026-10-20T14:35:00")  # noqa: E731
        self.assertEqual(q.asset.get_days_to_expiration(nach()), -4)
        kept, dropped = filter_dte([q], fenster, now_fn=nach)
        self.assertEqual(kept, [])
        self.assertEqual([r for _q, r in dropped], ["DTE -4 < 7"])
        # the SAME contract, clock BEFORE the expiry -> inside the window
        self.assertEqual(q.asset.get_days_to_expiration(_NOW()), 9)
        kept2, dropped2 = filter_dte([q], fenster, now_fn=_NOW)
        self.assertEqual(dropped2, [])
        self.assertEqual([x.asset.symbol for x in kept2], ["AAPL261016C00240000"])


class TestNonFinitePricesAreNotCandidates(unittest.TestCase):
    """D8 (E2 = a): NaN and +-inf are rejected at both boundaries.

    A non-finite quote is not a cheap option, it is no data. Measured before
    D8: an ``ask=inf`` contract divided to a NaN spread, NaN compares False
    against every bound, so the contract was KEPT and its bid/ask reached the
    JSON body as the bare tokens Infinity/NaN - which no strict parser reads.
    """

    def test_a_non_finite_ask_is_rejected_not_ranked(self):
        q = quote("AAPL261016C00240000", bid=2.40, ask=float("inf"), iv=30.0,
                  oi=1500, volume=300)
        kept, dropped = filter_spread([q], ScreenerCriteria(max_spread_abs=None))
        self.assertEqual(kept, [])
        self.assertEqual([r for _q, r in dropped],
                         ["Spread: kein Geld-/Briefkurs"])
        # and it never becomes a candidate of the whole pipeline either
        results, dropped_all = screen([q], ScreenerCriteria(), now_fn=_NOW)
        self.assertEqual(results, [])
        self.assertEqual([r for _q, r in dropped_all],
                         ["Spread: kein Geld-/Briefkurs"])

    def test_a_non_finite_bid_is_rejected_too(self):
        for bid in (float("nan"), float("inf")):
            with self.subTest(bid=bid):
                q = quote("AAPL261016C00240000", bid=bid, ask=3.00, iv=30.0,
                          oi=1500, volume=300)
                kept, dropped = filter_spread(
                    [q], ScreenerCriteria(max_spread_abs=None))
                self.assertEqual(kept, [])
                self.assertEqual([r for _q, r in dropped],
                                 ["Spread: kein Geld-/Briefkurs"])

    def test_the_adapter_turns_a_non_finite_feed_value_into_missing(self):
        from paperbroker.adapters.quotes.CBOEQuoteAdapter import _num
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(_num(value))
        # the guard is not a wipe: missing stays missing, zero and a real
        # number keep their value
        self.assertIsNone(_num(None))
        self.assertEqual(_num(0.0), 0.0)
        self.assertEqual(_num(2.5), 2.5)
        self.assertEqual(_num("3.5", scale=100.0), 350.0)


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
        with_trend = [r for r in results if any("Trend steigend passt (Call)" in x for x in r.reasons)]
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
        self.assertIn("veraltet", dropped[0][1])

    def test_freshness_guard_drops_on_missing_timestamp(self):
        c = ScreenerCriteria(max_quote_age_min=10)
        now_fn = lambda: datetime(2026, 10, 7, 15, 0)  # noqa: E731
        qs = make()
        for q in qs:
            q.quote_timestamp = None   # adapter could not determine it
        kept, dropped = filter_stale(qs, c, now_fn=now_fn)
        self.assertEqual(len(kept), 0)
        self.assertTrue(all("Aktualität unbekannt" in r for _, r in dropped))

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
        self.assertIn("veraltet: 245 Min.", dropped[0][1])

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
    function as now_fn received 'Aktualität unbekannt (Zeitstempel unlesbar)'
    for 67 contracts, although all 104 carried a valid timestamp. The reason
    text is what UX-1 puts on the page.
    """

    TS = "2026-10-07 14:30:00"          # valid, 30 min before the clock below
    LATER = "2026-10-07T15:01:00"       # 31 min -> veraltet (boundary is ">")

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
                         {"Aktualität unbekannt (Uhrfehler)"})

    def test_a_raising_clock_is_labelled_clock_error_too(self):
        # not TypeError-specific: any clock failure is an internal error
        def boom():
            raise RuntimeError("clock down")

        kept, dropped = self._run(self._chain(), boom)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 7)
        self.assertEqual({r for _q, r in dropped},
                         {"Aktualität unbekannt (Uhrfehler)"})

    def test_a_clock_returning_junk_is_labelled_clock_error(self):
        # CodeRabbit (PR #19): a clock function returning None - or anything
        # that is not a datetime - must not be reported as an unreadable
        # timestamp. Fail-closed either way, but the label has to be true.
        for bad in (lambda: None, lambda: "2026-10-07T15:00:00"):
            kept, dropped = self._run(self._chain(), bad)
            self.assertEqual(kept, [])
            self.assertEqual(len(dropped), 7)
            self.assertEqual({r for _q, r in dropped},
                             {"Aktualität unbekannt (Uhrfehler)"})

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
        self.assertEqual({r for _q, r in dropped}, {"veraltet: 31 Min."})

    def test_a_missing_timestamp_keeps_its_own_label(self):
        # pin: already correct today - must not be folded into the other two
        kept, dropped = self._run(self._chain_with_ts(None),
                                  lambda: arrow.get(self.LATER))
        self.assertEqual(kept, [])
        self.assertEqual({r for _q, r in dropped},
                         {"Aktualität unbekannt (kein Zeitstempel)"})

    def test_an_unreadable_timestamp_keeps_its_unparseable_label(self):
        # pin: only a really unreadable stamp may say "unparseable"
        kept, dropped = self._run(self._chain_with_ts("gestern abend"),
                                  lambda: arrow.get(self.LATER))
        self.assertEqual(kept, [])
        self.assertEqual({r for _q, r in dropped},
                         {"Aktualität unbekannt (Zeitstempel unlesbar)"})



def make_verticals():
    """Vertical-spread fixture (Ausbau-Slice B).

    One single expiry for every leg - 2026-10-16, DTE 9 against the pinned scan
    clock (2026-10-07) - so the section's maturity gate keeps the whole chain
    (a second expiry would split the groups and change the pair count).

    Deliberately separate from make(): that chain is pinned by the existing
    drop counts and by the display tests, so it must not change. One
    adjacent CALL pair per underlying, each carrying exactly ONE edge case,
    plus a PUT pair that must never show up:

      AAPL 250/252.5      debit == width exactly (2.50) -> dropped by the PAIR
                          rule (D7: only 0 < debit < width is still a result)
      AMZN 260/262.5      long leg open_interest == 500 exactly
      MSFT 270/272.5      long leg has no bid (0.0) -> dropped: Spread: kein Geld-/Briefkurs
      NVDA 280/282.5      short leg open_interest is None
      TSLA 290/292.5      ordinary tradeable pair
      AAPL PUT 250/252.5  must not appear anywhere (calls only)

    D7 note: the short legs of AMZN/NVDA/TSLA quote 0.50/0.70 - 0.20 $ wide at a
    mid of 0.60 - so under the wide criteria of the tests below the default
    ABSOLUTE spread bound (0.10 $ in the penny zone) rejects those legs before the
    OI rules can speak. That is wanted: the leg chain here is the same chain the
    list above uses. Tests that need the pairs to survive switch that one bound
    off (see _WIDE in test_server.py).

    Every quote is FRESH against the pinned clock, so the age guard never hides
    the chain, and every strike pair is adjacent, so the pair count is exact.
    """
    return [
        quote("AAPL261016C00250000", bid=2.60, ask=3.00, iv=30.0, oi=1200, volume=100),
        quote("AAPL261016C00252500", bid=0.50, ask=0.60, iv=30.0, oi=1200, volume=100),
        quote("AAPL261016P00250000", bid=2.60, ask=3.00, iv=30.0, oi=1200, volume=100),
        quote("AAPL261016P00252500", bid=0.50, ask=0.60, iv=30.0, oi=1200, volume=100),
        quote("AMZN261016C00260000", bid=1.00, ask=1.20, iv=30.0, oi=500, volume=100),
        quote("AMZN261016C00262500", bid=0.50, ask=0.70, iv=30.0, oi=1500, volume=100),
        quote("MSFT261016C00270000", bid=0.0, ask=1.00, iv=30.0, oi=1500, volume=100),
        quote("MSFT261016C00272500", bid=0.50, ask=0.70, iv=30.0, oi=1500, volume=100),
        quote("NVDA261016C00280000", bid=1.00, ask=1.20, iv=30.0, oi=1500, volume=100),
        quote("NVDA261016C00282500", bid=0.50, ask=0.70, iv=30.0, oi=None, volume=100),
        quote("TSLA261016C00290000", bid=1.00, ask=1.20, iv=30.0, oi=1500, volume=100),
        quote("TSLA261016C00292500", bid=0.50, ask=0.70, iv=30.0, oi=1500, volume=100),
    ]


class TestFutureTimestamps(unittest.TestCase):
    """Slice J: a timestamp AFTER the clock must not pass the age guard.

    Measured before the fix: every future timestamp was kept with no reason
    (+1 s, +1 day, 9999-12-31), while the lamp reported the chain as fresh.
    Fail-closed is the rule everywhere else in this pipeline, so this was the
    one open door.
    """

    JETZT = "2026-10-07T14:35:00"

    def _q(self, ts):
        return quote("AAPL261016C00240000", quote_timestamp=ts)

    def _lauf(self, ts, max_age=30):
        c = ScreenerCriteria(max_quote_age_min=max_age)
        return filter_stale([self._q(ts)], c, now_fn=lambda: arrow.get(self.JETZT))

    def test_a_clock_skew_of_seconds_still_passes(self):
        # the tolerance is the boundary: -60 s is not suspicious, -61 s is
        for ts in ("2026-10-07 14:35:00", "2026-10-07 14:35:01", "2026-10-07 14:35:30",
                   "2026-10-07 14:36:00"):
            kept, dropped = self._lauf(ts)
            self.assertEqual(len(kept), 1, ts)
            self.assertEqual(dropped, [], ts)

    def test_a_timestamp_just_beyond_the_tolerance_is_dropped_and_named(self):
        kept, dropped = self._lauf("2026-10-07 14:36:01")
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0][1], "Zeitstempel in der Zukunft (+1 Min.)")

    def test_a_day_ahead_is_dropped_with_the_real_distance(self):
        kept, dropped = self._lauf("2026-10-08 14:35:00")
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0][1], "Zeitstempel in der Zukunft (+1440 Min.)")

    def test_the_far_future_is_dropped_too(self):
        kept, dropped = self._lauf("9999-12-31 23:59:59")
        self.assertEqual(kept, [])
        self.assertTrue(dropped[0][1].startswith("Zeitstempel in der Zukunft (+"), dropped[0][1])

    def test_the_existing_stale_edge_did_not_move(self):
        # exactly 30 min stays; 30 min + 1 s keeps the old wording (rounded)
        kept, dropped = self._lauf("2026-10-07 14:05:00")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])
        kept, dropped = self._lauf("2026-10-07 14:04:59")
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0][1], "veraltet: 30 Min.")

    def test_the_guard_switched_off_does_not_check_at_all(self):
        kept, dropped = self._lauf("9999-12-31 23:59:59", max_age=None)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])


class TestVerticalCallSpreads(unittest.TestCase):
    """Ausbau-Slice B: adjacent-strike CALL spreads from the raw chain.

    The four numbers of a pair are pure arithmetic on measured quote
    fields, so every expected value below is hand-derivable:
    debit = ask(long) - bid(short), max loss = debit,
    max profit = width - debit, breakeven = long strike + debit.
    """

    @staticmethod
    def _api():
        # MODULE import, attribute access at call time: the first commit
        # carries the tests only, so the module must still import (a
        # `from ... import name` at module level would break the whole
        # collection). This way the red proof is a failing TEST.
        from paperbroker import screener as module
        return module.form_vertical_pairs, module.leg_drop_reason, module.vertical_spreads

    def test_the_fixture_yields_four_adjacent_call_pairs(self):
        form_vertical_pairs, _leg, _vs = self._api()
        pairs = form_vertical_pairs(make())
        self.assertEqual([p.symbol for p in pairs],
                         ["AAPL261016C00240000/AAPL261016C00245000",
                          "AAPL261016C00245000/AAPL261016C00261000",
                          "AAPL261016C00261000/AAPL261016C00263000",
                          "AAPL261016C00263000/AAPL261016C00265000"])

    def test_the_strikes_are_sorted_before_pairing(self):
        # the input order is deliberately REVERSED - without the strike sort
        # the pair would be built upside down (negative width)
        form_vertical_pairs, _leg, _vs = self._api()
        descending = [make()[1], make()[0]]
        pair = form_vertical_pairs(descending)[0]
        self.assertEqual((pair.long.asset.strike, pair.short.asset.strike), (240.0, 245.0))
        self.assertEqual(pair.strike_width, 5.0)

    def test_two_expirations_never_pair_with_each_other(self):
        # the group key carries the expiration: 240 (Oct 2026) and 270
        # (Feb 2027) are not adjacent legs of one spread
        form_vertical_pairs, _leg, _vs = self._api()
        pairs = form_vertical_pairs(make())
        self.assertNotIn("AAPL261016C00265000/AAPL270216C00270000",
                         [p.symbol for p in pairs])
        self.assertEqual([p.expiration for p in pairs], ["2026-10-16"] * 4)

    def test_the_lower_strike_is_the_long_leg(self):
        form_vertical_pairs, _leg, _vs = self._api()
        pair = form_vertical_pairs(make())[0]
        self.assertEqual((pair.long.asset.strike, pair.short.asset.strike), (240.0, 245.0))

    def test_the_debit_is_ask_long_minus_bid_short(self):
        form_vertical_pairs, _leg, _vs = self._api()
        pair = form_vertical_pairs(make())[0]
        # 5.10 - 2.40 is 2.6999999999999997 in binary floating point - the
        # pair rounds once at the source, so 2.7 is the pinned value
        self.assertEqual(pair.net_debit, 2.7)
        self.assertEqual(pair.net_debit, pair.max_loss)

    def test_width_profit_breakeven_and_ratio(self):
        form_vertical_pairs, _leg, _vs = self._api()
        pair = form_vertical_pairs(make())[0]
        self.assertEqual(pair.strike_width, 5.0)
        self.assertEqual(pair.max_profit, 2.3)          # 5.0 - 2.7
        self.assertEqual(pair.breakeven, 242.7)         # 240.0 + 2.7
        self.assertEqual(pair.debit_width_ratio, 0.54)  # 2.7 / 5.0

    def test_the_derived_numbers_are_rounded_at_the_source(self):
        # Break points, measured on the two-decimal float grid:
        #   width 5.00 - debit 4.02 = 0.9800000000000004  -> 0.98
        #   strike 50.0 + debit 4.02 = 54.019999999999996 -> 54.02
        # The literals are taken FROM those break points, not from the next
        # plausible example: a pair one step off would stay green.
        form_vertical_pairs, _leg, _vs = self._api()
        chain = [quote("XYZ261016C00050000", bid=4.00, ask=4.12, iv=30.0,
                       oi=1500, volume=100),
                 quote("XYZ261016C00055000", bid=0.10, ask=0.30, iv=30.0,
                       oi=1500, volume=100)]
        pair = form_vertical_pairs(chain)[0]
        self.assertEqual(pair.strike_width, 5.0)
        self.assertEqual(pair.net_debit, 4.02)
        self.assertEqual(pair.max_profit, 0.98)
        self.assertEqual(pair.breakeven, 54.02)

    def test_max_loss_is_the_debit_not_the_remainder(self):
        # the panel draft had max_loss = width - debit; that is the maximum
        # PROFIT, and pinning it would have inverted the worst case
        form_vertical_pairs, _leg, _vs = self._api()
        pair = form_vertical_pairs(make())[0]
        self.assertEqual(pair.max_loss, 2.7)
        self.assertNotEqual(pair.max_loss, 2.3)

    def test_the_pairs_are_formed_before_the_leg_rules_run(self):
        _fp, _leg, vertical_spreads = self._api()
        form_vertical_pairs, _leg, vertical_spreads = self._api()
        kept, dropped = vertical_spreads(make(), _LOOSE, now_fn=_NOW)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 3)
        self.assertEqual(kept[0].symbol,
                         "AAPL261016C00240000/AAPL261016C00245000")
        # conservation: no pair may vanish silently - every raw pair is either
        # listed or carries a reason
        self.assertEqual(len(kept) + len(dropped), len(form_vertical_pairs(make())))

    def test_one_reason_per_dropped_pair_naming_strike_and_value(self):
        # D7: one reason per dropped pair, in the order of the single-option
        # chain. The middle pair is rejected by the PAIR rule (debit <= 0), so it
        # carries the pair text instead of a leg name; the last pair is named by
        # the data-completeness rule (iv=0 illiquide, not "iv 0.0").
        _fp, _leg, vertical_spreads = self._api()
        _kept, dropped = vertical_spreads(make(), _LOOSE, now_fn=_NOW)
        self.assertEqual([reason for _pair, reason in dropped],
                         ["strike 261.0 call: OI 40 < 500",
                          "Debit -0.85 außerhalb (0, 2.0) - kein Gewinn möglich",
                          "strike 263.0 call: iv=0 illiquide"])

    def test_a_leg_without_two_sides_drops_the_pair(self):
        _fp, leg_drop_reason, vertical_spreads = self._api()
        chain = make_verticals()
        kept, dropped = vertical_spreads(chain, _LOOSE, now_fn=_NOW)
        # D7: the pairs are formed first and then run through the WHOLE leg chain,
        # so with these criteria every raw pair is rejected - the one-sided MSFT
        # leg still falls with its own reason, the others with the abs spread
        # bound of the penny zone and the degenerated debit.
        self.assertEqual([p.symbol for p in kept], [])
        self.assertEqual([reason for _p, reason in dropped],
                         ["Debit 2.5 außerhalb (0, 2.5) - kein Gewinn möglich",
                          "strike 262.5 call: absoluter Spread 0.20$ > 0.10$ (Mitte 0.60)",
                          "strike 270.0 call: Spread: kein Geld-/Briefkurs",
                          "strike 282.5 call: absoluter Spread 0.20$ > 0.10$ (Mitte 0.60)",
                          "strike 292.5 call: absoluter Spread 0.20$ > 0.10$ (Mitte 0.60)"])
        msft = [q for q in chain if q.asset.strike == 270.0][0]
        self.assertEqual(leg_drop_reason(msft, _LOOSE, _NOW), "strike 270.0 call: Spread: kein Geld-/Briefkurs")

    def test_the_open_interest_floor_is_inclusive_on_both_sides(self):
        # BOTH sides of the boundary: exactly 500 survives, one below drops.
        # Only pinning the boundary itself would let a shift by one pass.
        _fp, leg_drop_reason, _vs = self._api()
        chain = make_verticals()
        amzn = [q for q in chain if q.asset.strike == 260.0][0]
        self.assertEqual(amzn.open_interest, 500)
        self.assertIsNone(leg_drop_reason(amzn, _LOOSE, _NOW))
        below = quote("AMZN261016C00257500", bid=0.60, ask=0.80, iv=30.0,
                      oi=499, volume=100)
        # the leg still falls - but the chain names the FIRST rule it violates,
        # and that is the absolute spread bound of the penny zone, not the floor
        self.assertEqual(leg_drop_reason(below, _LOOSE, _NOW),
                         "strike 257.5 call: absoluter Spread 0.20$ > 0.10$ (Mitte 0.70)")
        # the floor itself stays pinned on a leg the spread rules let through
        # ("OI 40 < 500" in test_one_reason_per_dropped_pair_...)
        eng = quote("AMZN261016C00257500", bid=0.68, ask=0.72, iv=30.0,
                    oi=499, volume=100)
        self.assertEqual(leg_drop_reason(eng, _LOOSE, _NOW), "strike 257.5 call: OI 499 < 500")

    def test_missing_open_interest_is_not_treated_as_liquid(self):
        _fp, leg_drop_reason, _vs = self._api()
        chain = make_verticals()
        nvda = [q for q in chain if q.asset.strike == 282.5][0]
        self.assertIsNone(nvda.open_interest)
        # D7: the leg is dropped, never silently kept - the reason comes from the
        # first rule of the shared chain (the abs spread bound, as above)
        self.assertEqual(leg_drop_reason(nvda, _LOOSE, _NOW),
                         "strike 282.5 call: absoluter Spread 0.20$ > 0.10$ (Mitte 0.60)")

    def test_the_five_trend_hints_speak_german(self):
        """Alle fuenf Vorlagen: drei Richtungen, Symbol-Wache, Fehlpassung.

        Die Kopfzeile sagt schon "Trend: steigend" (_trend_label) - die
        Hinweise muessen dieselben Worte benutzen, sonst widerspricht sich
        eine Seite selbst. Jeder Fall prueft zusaetzlich, dass KEIN
        englisches Wort stehengeblieben ist.
        """
        now_fn = lambda: arrow.get("2026-10-07T14:35:00")  # noqa: E731
        faelle = [
            (TrendSignal(symbol="AAPL", direction="up"),
             "Trend steigend passt (Call)"),
            (TrendSignal(symbol="AAPL", direction="down"),
             "Trend fallend passt nicht zu call"),
            (TrendSignal(symbol="AAPL", direction="flat"),
             "Trend neutral"),
            (TrendSignal(symbol="MSFT", direction="up"),
             "Trend-Symbol (MSFT) passt nicht"),
        ]
        for signal, erwartet in faelle:
            with self.subTest(erwartet=erwartet):
                results, _ = screen(make(), ScreenerCriteria(), trend=signal,
                                    now_fn=now_fn)
                self.assertTrue(results, "Fixture liefert keine Ergebnisse mehr")
                hinweise = [x for r in results for x in r.reasons]
                self.assertIn(erwartet, hinweise)
                englisch = [x for x in hinweise
                            if any(w in x for w in ("trend", "match", "mismatch"))]
                self.assertEqual(englisch, [], "englischer Hinweis stehengeblieben")

    def test_the_put_side_of_a_down_trend_says_put(self):
        """Die Richtungsregel selbst: fallender Trend + Put = Passung.

        make() liefert nur Calls (gemessen), der Put entsteht hier von Hand.
        Spread bewusst eng (1.00/1.05), sonst faellt er durch max_spread_pct.
        """
        now_fn = lambda: arrow.get("2026-10-07T14:35:00")  # noqa: E731
        puts = [quote("AAPL261016P00260000", bid=1.00, ask=1.05, iv=28.0,
                      oi=8000, volume=100)]
        results, _ = screen(puts, ScreenerCriteria(),
                            trend=TrendSignal(symbol="AAPL", direction="down"),
                            now_fn=now_fn)
        hinweise = [x for r in results for x in r.reasons]
        self.assertIn("Trend fallend passt (Put)", hinweise)

    def test_a_leg_without_iv_is_reported_as_missing(self):
        """D7: the leg carries the FIRST rule of the chain, not a private rule.

        A 0.20 $ wide leg under a mid of 1 $ is named with the absolute spread
        bound of the penny zone (the same rule the list above applies); a leg with
        iv == 0 is named with the data-completeness rule. Both stay distinct, so
        the reader can tell the two cases apart.
        """
        _fp, leg_drop_reason, _vs = self._api()
        ohne = quote("AAPL261016C00270000", bid=0.60, ask=0.80, iv=None,
                     oi=900, volume=100)
        self.assertIsNone(ohne.iv)
        self.assertEqual(leg_drop_reason(ohne, _LOOSE, _NOW),
                         "strike 270.0 call: absoluter Spread 0.20$ > 0.10$ (Mitte 0.70)")
        null = quote("AAPL261016C00270000", bid=0.60, ask=0.80, iv=0.0,
                     oi=900, volume=100)
        self.assertEqual(leg_drop_reason(null, _LOOSE, _NOW), "strike 270.0 call: iv=0 illiquide")
        # with a narrow penny leg the iv rule is reached and keeps its own text
        eng = quote("AAPL261016C00270000", bid=0.68, ask=0.72, iv=None,
                    oi=900, volume=100)
        self.assertEqual(leg_drop_reason(eng, _LOOSE, _NOW), "strike 270.0 call: IV fehlt")

    def test_a_put_pair_is_never_built(self):
        form_vertical_pairs, _leg, _vs = self._api()
        pairs = form_vertical_pairs(make_verticals())
        self.assertEqual(len(pairs), 5)
        self.assertNotIn("P00", "".join(p.symbol for p in pairs))
        self.assertTrue(all(p.long.asset.option_type == "call" for p in pairs))

    def test_debit_equal_to_the_width_is_dropped_with_a_reason(self):
        # D7: the degenerated pair (debit == width, max profit 0) is no longer
        # listed - the pair rule 0 < debit < width drops it and says why. Only a
        # pair at or above the width may carry the flag; the ordinary neighbours
        # must not.
        form_vertical_pairs, _leg, vertical_spreads = self._api()
        chain = make_verticals()
        kept, dropped = vertical_spreads(chain, _LOOSE, now_fn=_NOW)
        degenerate = [p for p, _r in dropped
                      if p.symbol == "AAPL261016C00250000/AAPL261016C00252500"]
        self.assertEqual(len(degenerate), 1)
        pair = degenerate[0]
        self.assertEqual(pair.strike_width, 2.5)
        self.assertEqual(pair.net_debit, 2.5)
        self.assertEqual(pair.max_profit, 0.0)
        self.assertTrue(pair.debit_ge_width)
        self.assertEqual([r for p, r in dropped if p.symbol == pair.symbol],
                         ["Debit 2.5 außerhalb (0, 2.5) - kein Gewinn möglich"])
        self.assertEqual([p.symbol for p in kept], [])
        # the flag is not blanket-applied: of all raw pairs only the degenerated
        # one reaches the width
        self.assertEqual([p.symbol for p in form_vertical_pairs(chain) if p.debit_ge_width],
                         [pair.symbol])

    def test_a_duplicated_strike_neither_crashes_nor_divides_by_zero(self):
        # no live chain measured carries a duplicated strike in one group
        # (0 of 688 pairs), but the case must stay defined: width 0 gives no
        # ratio (None) and flags, and nothing vanishes silently.
        form_vertical_pairs, _leg, vertical_spreads = self._api()
        chain = [quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=30.0,
                       oi=1500, volume=100),
                 quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=30.0,
                       oi=1500, volume=100),
                 quote("AAPL261016C00245000", bid=2.40, ask=2.50, iv=30.0,
                       oi=1500, volume=100)]
        pairs = form_vertical_pairs(chain)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0].strike_width, 0.0)
        self.assertIsNone(pairs[0].debit_width_ratio)
        self.assertTrue(pairs[0].debit_ge_width)
        self.assertEqual(pairs[1].strike_width, 5.0)
        kept, dropped = vertical_spreads(chain, _LOOSE, now_fn=_NOW)
        self.assertEqual(len(kept) + len(dropped), 2)

    def test_different_underlyings_never_pair_with_each_other(self):
        # the group key carries the underlying: same expiration and same
        # strikes on two tickers are still two spreads, never one
        form_vertical_pairs, _leg, _vs = self._api()
        chain = [quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=30.0,
                       oi=1500, volume=100),
                 quote("AAPL261016C00245000", bid=2.40, ask=2.50, iv=30.0,
                       oi=1500, volume=100),
                 quote("MSFT261016C00240000", bid=5.00, ask=5.10, iv=30.0,
                       oi=1500, volume=100),
                 quote("MSFT261016C00245000", bid=2.40, ask=2.50, iv=30.0,
                       oi=1500, volume=100)]
        self.assertEqual([p.symbol for p in form_vertical_pairs(chain)],
                         ["AAPL261016C00240000/AAPL261016C00245000",
                          "MSFT261016C00240000/MSFT261016C00245000"])

    def test_a_non_positive_debit_is_dropped(self):
        # pair rule: with BOTH legs quoted, the higher strike's bid reaching
        # the lower strike's ask (debit <= 0) is incoherent data - it would
        # print a profit above the width. Measured: 0 of 560 live pairs.
        form_vertical_pairs, _leg, vertical_spreads = self._api()
        chain = [quote("AAPL261016C00240000", bid=1.90, ask=2.00, iv=30.0,
                       oi=1500, volume=100),
                 quote("AAPL261016C00245000", bid=2.50, ask=2.70, iv=30.0,
                       oi=1500, volume=100)]
        kept, dropped = vertical_spreads(chain, _LOOSE, now_fn=_NOW)
        self.assertEqual(kept, [])
        self.assertEqual([reason for _pair, reason in dropped],
                         ["Debit -0.5 außerhalb (0, 5.0) - kein Gewinn möglich"])
        self.assertEqual(form_vertical_pairs(chain)[0].max_profit, 5.5)
        # BOTH sides of the boundary: exactly zero is outside (0, width) too,
        # otherwise a shift to `debit < 0` stays green
        boundary = [quote("AAPL261016C00240000", bid=2.40, ask=2.50, iv=30.0,
                          oi=1500, volume=100),
                    quote("AAPL261016C00245000", bid=2.50, ask=2.70, iv=30.0,
                          oi=1500, volume=100)]
        kept_zero, dropped_zero = vertical_spreads(boundary, _LOOSE, now_fn=_NOW)
        self.assertEqual(form_vertical_pairs(boundary)[0].net_debit, 0.0)
        self.assertEqual(kept_zero, [])
        self.assertEqual([reason for _pair, reason in dropped_zero],
                         ["Debit 0.0 außerhalb (0, 5.0) - kein Gewinn möglich"])

    def test_an_empty_chain_yields_no_pairs(self):
        form_vertical_pairs, _leg, vertical_spreads = self._api()
        self.assertEqual(form_vertical_pairs([]), [])
        self.assertEqual(vertical_spreads([], _LOOSE, now_fn=_NOW), ([], []))

    # --- D11: der Standard bleibt die Nachbarschaft ------------------------

    @staticmethod
    def _nachbarpaare(chain):
        """Die Nachbarschafts-Paare unabhaengig nachgebaut.

        Gruppen getrennt, Strikes sortiert, OCC-Symbole als Namen. Die Erwartung
        kommt NICHT aus der Funktion, die sie prueft.
        """
        gruppen = {}
        for q in chain:
            if q.asset.option_type != "call":
                continue
            gruppen.setdefault((q.asset.underlying.symbol, q.asset.expiration_date),
                               []).append(q)
        paare = []
        for key in sorted(gruppen):
            legs = sorted(gruppen[key], key=lambda q: q.asset.strike)
            paare += [f"{a.asset.symbol}/{b.asset.symbol}"
                      for a, b in zip(legs, legs[1:], strict=False)]
        return paare

    def test_the_default_width_leaves_the_adjacency_unchanged(self):
        """D11-Aequivalenz: ohne Breite genau die Paare von vorher.

        `max_width=None` ist der Standard, und der Standard ist die Nachbarschaft
        (ScreenerCriteria.pair_max_width ist None - das ist der Wert, den ein
        leeres Formularfeld hinterlaesst). Geprueft an beiden Fixtures und an
        ihrer Vereinigung; die Erwartung ist aus den Rohnummern nachgebaut.
        """
        form_vertical_pairs, _leg, _vs = self._api()
        self.assertIsNone(ScreenerCriteria().pair_max_width)      # leer = Nachbarschaft
        self.assertIsNone(_LOOSE.pair_max_width)
        for kette in (make(), make_verticals(), make() + make_verticals()):
            erwartet = self._nachbarpaare(kette)
            self.assertEqual([p.symbol for p in form_vertical_pairs(kette)], erwartet)
            # die Breite ausdruecklich auf None: dieselbe Liste, kein zweiter Weg
            self.assertEqual([p.symbol for p in form_vertical_pairs(kette, max_width=None)],
                             erwartet)

    def test_the_fast_mode_forms_adjacent_pairs_only(self):
        """D11-Gestalt: die Zahl der Paare ist Legs minus Gruppen, und kein Paar
        ueberspringt einen Strike.

        Eine Gestaltaussage, keine Laufzeitmessung: festgehalten wird, WAS der
        schnelle Modus liefert (Nachbarn), nicht wie lange er braucht.
        """
        form_vertical_pairs, _leg, _vs = self._api()
        for kette in (make(), make_verticals(), make() + make_verticals()):
            paare = form_vertical_pairs(kette, max_width=None)
            legs = [q for q in kette if q.asset.option_type == "call"]
            gruppen = {(q.asset.underlying.symbol, q.asset.expiration_date)
                       for q in legs}
            self.assertEqual(len(paare), len(legs) - len(gruppen))
            # Nachbarn: zwischen den beiden Strikes eines Paares liegt kein
            # weiterer Kontrakt derselben Gruppe
            for paar in paare:
                dazwischen = [
                    q.asset.symbol for q in legs
                    if q.asset.underlying.symbol == paar.long.asset.underlying.symbol
                    and q.asset.expiration_date == paar.long.asset.expiration_date
                    and paar.long.asset.strike < q.asset.strike < paar.short.asset.strike]
                self.assertEqual(dazwischen, [], paar.symbol)


class PutPairsCarryTheirOwnDirection(unittest.TestCase):
    """D12: the pair order IS the trade direction, and every derived number
    follows it.

    Measured on the live SPY chain (20.11.2026, first put pair):
      long SPY261120P00305000 (strike 305.0) / short SPY261120P00300000 (300.0)
      debit 0.01 | width 5.00 | break-even 304.99 | max profit 4.99
    Before the fix the code bought the LOWER put for every option type and
    printed the CALL break-even: the same pair came out with debit 0.01 at a
    break-even of -1.00 instead of 304.99.

    The fixtures below reuse the two measured strikes on the put side and the
    same two strikes on the call side, so both directions are read off ONE
    chain with real OCC symbols.
    """

    @staticmethod
    def _api():
        from paperbroker import screener as module
        return module.form_vertical_pairs, module.vertical_spreads

    @staticmethod
    def _kette():
        # the put pair of the measurement, both sides quoted, plus the same two
        # strikes as calls: ask(P305) 0.21 - bid(P300) 0.20 = 0.01 debit
        return [
            quote("SPY261120P00300000", bid=0.20, ask=0.30, iv=30.0,
                  oi=1500, volume=100),
            quote("SPY261120P00305000", bid=0.19, ask=0.21, iv=30.0,
                  oi=1500, volume=100),
            quote("SPY261120C00300000", bid=1.00, ask=1.10, iv=30.0,
                  oi=1500, volume=100),
            quote("SPY261120C00305000", bid=0.90, ask=0.95, iv=30.0,
                  oi=1500, volume=100),
        ]

    @staticmethod
    def _fernes_paar():
        # a second put pair on a later expiry, 0.34 $ per unit - the same shape,
        # a position 34 times as large as the near one (0.01 debit x 100). Both
        # legs quote 0.09 $ wide: the default ABSOLUTE bound (0.10 $) must let
        # them through, otherwise the pair would fall for the leg rule and not
        # for the budget.
        return [
            quote("SPY261218P00300000", bid=0.10, ask=0.19, iv=30.0,
                  oi=1500, volume=100),
            quote("SPY261218P00305000", bid=0.35, ask=0.44, iv=30.0,
                  oi=1500, volume=100),
        ]

    # --- die Richtung ------------------------------------------------------

    def test_a_put_pair_buys_the_higher_strike_and_a_call_pair_the_lower(self):
        form_vertical_pairs, _vs = self._api()
        put = form_vertical_pairs(self._kette(), option_type="put")[0]
        call = form_vertical_pairs(self._kette(), option_type="call")[0]
        # a put debit spread is long the HIGHER strike (the measurement)
        self.assertEqual(put.long.asset.symbol, "SPY261120P00305000")
        self.assertEqual(put.short.asset.symbol, "SPY261120P00300000")
        self.assertEqual((put.long.asset.strike, put.short.asset.strike),
                         (305.0, 300.0))
        # ... a call debit spread keeps the direction it always had
        self.assertEqual(call.long.asset.symbol, "SPY261120C00300000")
        self.assertEqual(call.short.asset.symbol, "SPY261120C00305000")
        self.assertEqual((call.long.asset.strike, call.short.asset.strike),
                         (300.0, 305.0))
        # the direction comes from the OPTION TYPE, not from the strike order:
        # both pairs carry the same two strikes in the opposite order
        self.assertEqual(put.long.asset.option_type, "put")
        self.assertEqual(call.long.asset.option_type, "call")

    # --- der Break-even ----------------------------------------------------

    def test_the_break_even_of_a_put_pair_subtracts_the_debit(self):
        form_vertical_pairs, _vs = self._api()
        put = form_vertical_pairs(self._kette(), option_type="put")[0]
        call = form_vertical_pairs(self._kette(), option_type="call")[0]
        self.assertEqual(put.net_debit, 0.01)      # ask(P305) .21 - bid(P300) .20
        self.assertEqual(put.breakeven, 304.99)    # 305.0 - 0.01
        self.assertEqual(put.breakeven, put.long.asset.strike - put.net_debit)
        self.assertEqual(call.net_debit, 0.2)      # ask(C300) 1.10 - bid(C305) .90
        self.assertEqual(call.breakeven, 300.2)    # 300.0 + 0.2
        self.assertEqual(call.breakeven, call.long.asset.strike + call.net_debit)

    # --- die Breite ist eine Distanz ---------------------------------------

    def test_the_width_is_a_distance_for_both_directions(self):
        """Vor der Korrektur las strike_width short - long und kam fuer JEDES
        Put-Paar auf -5.00 - die Paar-Regel 0 < Debit < Breite haette alle Puts
        verworfen.
        """
        form_vertical_pairs, vertical_spreads = self._api()
        put = form_vertical_pairs(self._kette(), option_type="put")[0]
        call = form_vertical_pairs(self._kette(), option_type="call")[0]
        self.assertEqual(put.strike_width, 5.0)
        self.assertEqual(call.strike_width, 5.0)
        self.assertGreater(put.strike_width, 0)
        self.assertGreater(call.strike_width, 0)
        # und die Paar-Regel behaelt das Put-Paar: 0.01 liegt echt in (0, 5.0)
        kept, dropped = vertical_spreads(self._kette(), _LOOSE, option_type="put",
                                         now_fn=_NOW)
        self.assertEqual([p.symbol for p in kept], [put.symbol])
        self.assertEqual(dropped, [])
        self.assertEqual(kept[0].max_profit, 4.99)

    # --- die Positionsgroesse ---------------------------------------------

    def test_the_position_debit_is_the_debit_times_the_position_size(self):
        """D12: multiplier x quantity x debit + fees - abgelesen an
        as_dict(criteria), nicht nachgebaut.
        """
        form_vertical_pairs, _vs = self._api()
        put = form_vertical_pairs(self._kette(), option_type="put")[0]
        criteria = ScreenerCriteria(max_spread_pct=100.0, multiplier=100,
                                    quantity=3, fees=1.3)
        d = put.as_dict(criteria)
        self.assertEqual(d["position_debit"],
                         criteria.multiplier * criteria.quantity * d["net_debit"]
                         + criteria.fees)
        self.assertEqual(d["position_debit"], 4.3)     # 100 * 3 * 0.01 + 1.30
        self.assertEqual(d["currency"], "USD")         # benannt, nicht umgerechnet
        # die Zahlen des PAARES bleiben je Einheit - die Position ist eine
        # eigene Zahl daneben, keine Skalierung
        self.assertEqual(d["net_debit"], 0.01)
        self.assertEqual(d["max_profit"], 4.99)
        self.assertEqual(d["breakeven"], 304.99)

    def test_a_missing_position_size_has_no_position_debit(self):
        # eine fehlende Groesse darf nicht still als Null gelesen werden
        form_vertical_pairs, _vs = self._api()
        put = form_vertical_pairs(self._kette(), option_type="put")[0]
        self.assertIsNone(put.position_debit())
        self.assertIsNone(put.as_dict()["position_debit"])

    # --- die Budgetgrenze --------------------------------------------------

    def test_the_budget_bound_drops_exactly_the_pair_above_it(self):
        """D12: budget ist die harte Obergrenze der Position. Ohne Budget bleiben
        beide Put-Paare stehen; mit einem Budget ZWISCHEN ihren Positionen faellt
        genau das teurere heraus - mit dem Grund, der beide Zahlen nennt.
        """
        form_vertical_pairs, vertical_spreads = self._api()
        kette = self._kette() + self._fernes_paar()
        nah, fern = form_vertical_pairs(kette, option_type="put")
        self.assertEqual((nah.symbol, fern.symbol),
                         ("SPY261120P00305000/SPY261120P00300000",
                          "SPY261218P00305000/SPY261218P00300000"))
        # the near pair costs 1.00 USD, the far one 34.00 USD (standard size)
        ohne = ScreenerCriteria(max_spread_pct=100.0, dte_max=None)
        kept, dropped = vertical_spreads(kette, ohne, option_type="put", now_fn=_NOW)
        self.assertEqual([p.symbol for p in kept], [nah.symbol, fern.symbol])
        self.assertEqual(dropped, [])
        mit = ScreenerCriteria(max_spread_pct=100.0, dte_max=None, budget=5.0)
        kept, dropped = vertical_spreads(kette, mit, option_type="put", now_fn=_NOW)
        self.assertEqual([p.symbol for p in kept], [nah.symbol])
        self.assertEqual([(p.symbol, r) for p, r in dropped],
                         [(fern.symbol, "Position 34.00 USD > Budget 5.00 USD")])
        self.assertEqual(nah.position_debit(mit), 1.0)
        self.assertEqual(fern.position_debit(mit), 34.0)


class TheReasonsAreGerman(unittest.TestCase):
    """Slice D4: die Ausschuss-Gruende sind deutsch.

    Uebersetzt ist nur die Beschreibung; die Namen (strike, DTE, OI, IV) und alle
    Zahlen bleiben. Eine Zusicherung fuer jede der sieben Vorlagen, die bis dahin
    von keiner Zusicherung gedeckt war (die zwei DTE-Vorlagen bleiben wortgleich,
    sie bestehen nur aus Namen und Zahlen).
    """

    def test_the_illiquid_reason_names_the_zero_open_interest(self):
        kept, dropped = filter_data_complete([quote("AAPL261016C00265000", oi=0)])
        self.assertEqual(kept, [])
        self.assertEqual([r for _q, r in dropped], ["oi=0 illiquide"])

    def test_the_spread_reason_without_a_quote(self):
        ohne = quote("AAPL261016C00245000")
        # kein Geld- und kein Briefkurs: der Grund entsteht VOR jeder Rechnung
        ohne.bid, ohne.ask = None, None
        kept, dropped = filter_spread([ohne], ScreenerCriteria(max_spread_abs=None))
        self.assertEqual(kept, [])
        self.assertEqual([r for _q, r in dropped], ["Spread: kein Geld-/Briefkurs"])

    def test_the_volume_reason(self):
        kept, dropped = filter_volume([quote("AAPL261016C00245000", volume=50)],
                                     ScreenerCriteria(min_volume=300))
        self.assertEqual(kept, [])
        self.assertEqual([r for _q, r in dropped], ["Volumen 50 <= 300"])

    def test_the_missing_data_reasons(self):
        _k, iv = filter_iv([quote("AAPL261016C00245000", iv=None)], ScreenerCriteria())
        self.assertEqual([r for _q, r in iv], ["IV fehlt"])
        # D8 REPLACEMENT: the old assertion cleared the STORED days_to_expiration
        # field and expected "DTE fehlt". The filter no longer reads that field -
        # it asks the asset for the honest maturity - so the old setup would now
        # keep the contract and the assertion would be silently meaningless.
        # The two statements below keep the full meaning, split in two:
        ohne = quote("AAPL261016C00245000")
        ohne.days_to_expiration = None
        kept, dte = filter_dte([ohne], ScreenerCriteria(dte_min=7, dte_max=45),
                               now_fn=_NOW)
        # (1) a missing STORED field is not a missing maturity: the contract
        #     stays, judged on its real expiry (2026-10-16 -> 9 days)
        self.assertEqual([q.asset.symbol for q in kept], ["AAPL261016C00245000"])
        self.assertEqual(dte, [])
        # (2) "DTE fehlt" is still the wording when no asset can be asked at all
        class _NoAsset:
            days_to_expiration = None

        _k, leer = filter_dte([_NoAsset()], ScreenerCriteria(), now_fn=_NOW)
        self.assertEqual([r for _q, r in leer], ["DTE fehlt"])

    def test_the_dte_reasons_keep_names_and_numbers(self):
        fenster = ScreenerCriteria(dte_min=7, dte_max=45)
        _k, zu_kurz = filter_dte([quote("AAPL261009C00240000")], fenster,
                                 now_fn=_NOW)
        self.assertEqual([r for _q, r in zu_kurz], ["DTE 2 < 7"])
        _k, zu_lang = filter_dte([quote("AAPL270216C00270000")], fenster,
                                 now_fn=_NOW)
        self.assertEqual([r for _q, r in zu_lang], ["DTE 132 > 45"])


class TestCriteriaValidationSpeaksGerman(unittest.TestCase):
    """D6: the criteria check reports in German - every violation, with values.

    The same text reaches the page and /api/screen, so it is pinned here on the
    exception itself; the two surfaces are covered in test_server.py.
    """

    def _text(self, **werte):
        with self.assertRaises(ValueError) as ctx:
            ScreenerCriteria(**werte).validate()
        return str(ctx.exception)

    def test_each_single_violation(self):
        faelle = (
            (dict(iv_pref="mittel"),
             "iv_pref: einer von high, low erwartet (ist: 'mittel')."),
            (dict(max_spread_pct=-1.0), "max_spread_pct: muss ≥ 0 sein (ist: -1.0)."),
            (dict(max_spread_abs=-0.5), "max_spread_abs: muss ≥ 0 sein (ist: -0.5)."),
            (dict(min_oi=-5), "min_oi: muss ≥ 0 sein (ist: -5)."),
            (dict(min_volume=-1), "min_volume: muss ≥ 0 sein (ist: -1)."),
            (dict(min_bid_size=-3.0), "min_bid_size: muss ≥ 0 sein (ist: -3.0)."),
            (dict(min_ask_size=-1.5), "min_ask_size: muss ≥ 0 sein (ist: -1.5)."),
            (dict(max_quote_age_min=-1),
             "max_quote_age_min: muss ≥ 0 sein (ist: -1)."),
            (dict(weight_spread=-0.5),
             "weight_spread: muss ≥ 0 sein (ist: -0.5)."),
            (dict(weight_oi=-0.1), "weight_oi: muss ≥ 0 sein (ist: -0.1)."),
            (dict(weight_iv=-0.2), "weight_iv: muss ≥ 0 sein (ist: -0.2)."),
        )
        for werte, erwartet in faelle:
            with self.subTest(werte=werte):
                self.assertEqual(erwartet, self._text(**werte))

    def test_the_two_contradictions(self):
        self.assertEqual("dte_min: muss ≤ dte_max sein (ist: 45 > 7).",
                         self._text(dte_min=45, dte_max=7))
        self.assertEqual("iv_min: muss ≤ iv_max sein (ist: 60.0 > 20.0).",
                         self._text(iv_min=60.0, iv_max=20.0))

    def test_all_violations_are_reported_not_only_the_first(self):
        text = self._text(min_oi=-5, min_volume=-1, dte_min=45, dte_max=7)
        for feld in ("min_oi:", "min_volume:", "dte_min:"):
            self.assertIn(feld, text)
        self.assertNotIn("ScreenerCriteria invalid", text,
                         "der englische Sammeltext ist noch da")
        self.assertEqual(2, text.count("; "), "die Verstoesse werden nicht verbunden")

    def test_the_valid_extremes_stay_valid(self):
        for werte in (dict(max_spread_pct=0.0), dict(min_oi=0),
                      dict(min_bid_size=0.0), dict(min_ask_size=0.0),
                      dict(weight_iv=0.0)):
            with self.subTest(werte=werte):
                ScreenerCriteria(**werte).validate()

if __name__ == "__main__":
    unittest.main()
