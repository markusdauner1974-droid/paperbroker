"""Offline tests for the Phase-5 web display (paperbroker/server.py).

Flask test client + mock quote chain: no network, deterministic.
Covers the debate-judge requirements: input validation (no path
injection into the CBOE URL), trend failure != flat, freshness lamp,
healthz liveness without external calls.
"""
import unittest
from datetime import datetime

import arrow
import pytest
from flask import Flask

from paperbroker.assets import asset_factory
from paperbroker.screener import OptionScreener, ScreenerCriteria
from paperbroker.server import create_app
from paperbroker.tests.test_screener import make


class _MockAdapter:
    """Returns the mock chain for every get_options call."""

    def __init__(self, quotes):
        self.quotes = quotes
        # The five dates span the default DTE window [7, 45] from the
        # pinned clock 2026-10-07: 2 (below), 7 (lower bound, inclusive),
        # 9 (inside), 45 (upper bound, inclusive), 72 (above). A fixture
        # with only in-window dates cannot tell a dte_min-only filter
        # from a full-window filter.
        self.expirations = ["2026-10-09", "2026-10-14", "2026-10-16",
                            "2026-11-21", "2026-12-18"]

    def get_expiration_dates(self, underlying_asset=None):
        return list(self.expirations)

    def get_options(self, underlying_asset=None, expiration_date=None):
        return list(self.quotes)

    def get_quote(self, asset):
        return None


def _clock():
    """Pinned test clock: fixture ts are hardcoded '2026-10-07 14:30' -
    a real now() goes stale (>30 min) and wipes every result."""
    return lambda: arrow.get("2026-10-07T14:35:00")


def _later_clock():
    """A second as-of day, to prove the filter reads the clock rather than
    a frozen date (see test_window_follows_the_injected_clock)."""
    return lambda: arrow.get("2026-11-15T14:35:00")


class TestServerDisplay(unittest.TestCase):
    def setUp(self):
        self.app = create_app(quote_adapter=_MockAdapter(make()),
                              screener=None,
                              now_fn=_clock())
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_healthz_no_external_calls(self):
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")

    def test_api_screen_valid_params(self):
        res = self.client.get("/api/screen",
                              query_string={"ticker": "SPY",
                                            "expiration": "2026-10-16"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["ticker"], "SPY")
        self.assertIn("results", data)
        self.assertIn("freshness", data)

    def test_api_screen_rejects_bad_ticker(self):
        # path-injection attempts must be rejected before the CBOE URL
        for bad in ("../etc", "SP Y", "SP;Y", "a" * 20, ""):
            res = self.client.get("/api/screen",
                                  query_string={"ticker": bad,
                                                "expiration": "2026-10-16"})
            self.assertEqual(res.status_code, 400, bad)

    def test_api_screen_rejects_bad_expiration(self):
        # '' is now the legal picker stage (form-first flow) - real
        # malformed dates still 400
        for bad in ("2026-10-99", "not-a-date", "2026/10/16"):
            res = self.client.get("/api/screen",
                                  query_string={"ticker": "SPY",
                                                "expiration": bad})
            self.assertEqual(res.status_code, 400, bad)

    def test_api_screen_rejects_unlisted_expiration(self):
        # a well-formed but non-listed date would still probe the CBOE
        # chain endpoint - must be caught against the real date list
        res = self.client.get("/api/screen",
                              query_string={"ticker": "SPY",
                                            "expiration": "2019-01-01"})
        self.assertEqual(res.status_code, 400)

    def test_api_error_is_json_not_html(self):
        # CodeRabbit finding: JSON clients must get JSON errors
        res = self.client.get("/api/screen",
                              query_string={"ticker": "../etc",
                                            "expiration": "2026-10-16"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.content_type, "application/json")
        self.assertIn("error", res.get_json())

    def test_api_picker_lists_dates_without_expiration(self):
        # form-first flow: valid ticker, no date yet -> list real dates
        res = self.client.get("/api/screen", query_string={"ticker": "SPY"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["listed_dates"],
                         ["2026-10-14", "2026-10-16", "2026-11-21"])
        self.assertEqual(data["results"], [])

    def test_lamp_reports_stale_chain_despite_empty_results(self):
        # CodeRabbit finding: the stale-quote guard can wipe all
        # candidates - the lamp must still report the chain staleness
        from paperbroker.screener import OptionScreener, ScreenerCriteria
        qs = make()
        for q in qs:
            q.quote_timestamp = "2026-10-07 10:00:00"  # hours old
        app2 = create_app(
            quote_adapter=_MockAdapter(qs),
            screener=OptionScreener(ScreenerCriteria()),
            now_fn=_clock())
        app2.config["TESTING"] = True
        c2 = app2.test_client()
        res = c2.get("/api/screen",
                     query_string={"ticker": "SPY",
                                   "expiration": "2026-10-16"})
        data = res.get_json()
        # default criteria drops the whole stale chain...
        self.assertEqual(data["candidates"], 0)
        # ...but the lamp still tells the truth (red = >= 2 h old)
        self.assertEqual(data["freshness"], "red")

    def test_delta_none_renders_placeholder(self):
        qs = make()
        qs[0].delta = None
        app2 = create_app(quote_adapter=_MockAdapter(qs), screener=None,
                          now_fn=_clock())
        app2.config["TESTING"] = True
        c2 = app2.test_client()
        res = c2.get("/", query_string={"ticker": "SPY",
                                        "expiration": "2026-10-16"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("–", res.get_data(as_text=True))

    def test_index_renders_form(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Options-Screener", html)
        self.assertIn("Scan", html)

    def test_index_arms_auto_loader(self):
        # the empty page must ship the auto-loader JS that fetches
        # /api/screen (picker stage) on ticker input
        res = self.client.get("/")
        html = res.get_data(as_text=True)
        self.assertIn("loadDates", html)
        self.assertIn("/api/screen?ticker=", html)
        # spinner placeholder in the untouched select
        self.assertIn("Verfaelle werden geladen", html)

    def test_picker_api_contract_for_auto_loader(self):
        # the exact response shape the JS loader consumes: listed_dates
        # as 'YYYY-MM-DD' strings, results empty, freshness null
        res = self.client.get("/api/screen", query_string={"ticker": "SPY"})
        self.assertEqual(res.status_code, 200)
        d = res.get_json()
        self.assertEqual(d["listed_dates"],
                         ["2026-10-14", "2026-10-16", "2026-11-21"])
        self.assertEqual(d["results"], [])
        self.assertIsNone(d["freshness"])
        self.assertIsNone(d["expiration"])

    def test_picker_api_rejects_bad_ticker(self):
        # the loader only fires on the client for ^[A-Z]{1,6}$ - but an
        # attacker can hit the endpoint directly; server must guard too
        res = self.client.get("/api/screen", query_string={"ticker": "../etc"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"], "invalid ticker (1-6 letters A-Z expected)")

    def test_lamp_uses_injected_clock(self):
        # CodeRabbit finding: the lamp must take create_app's clock, not
        # real now() - with the pinned clock (fixture ts 14:30) the lamp
        # must be GREEN here, not red-by-real-time
        res = self.client.get("/",
                              query_string={"ticker": "SPY",
                                            "expiration": "2026-10-16"})
        html = res.get_data(as_text=True)
        self.assertIn('lamp green', html)

    def test_last_init_is_rendered_json(self):
        # regression guard: the Jinja default must render a VALID JS
        # literal in both branches (was broken twice during round 1)
        res = self.client.get("/")
        self.assertIn('var last = "";', res.get_data(as_text=True))
        res = self.client.get("/", query_string={"ticker": "SPY"})
        self.assertIn('var last = "SPY";', res.get_data(as_text=True))

    def test_index_renders_result_table(self):
        res = self.client.get("/", query_string={"ticker": "SPY",
                                                 "expiration": "2026-10-16"})
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Kette", html)
        self.assertIn("Kandidaten", html)

    def test_index_error_rendered_not_crash(self):
        # SSR pages render the error as a readable box (200) - only the
        # JSON API answers with 400; the page must never turn into a
        # naked stacktrace
        res = self.client.get("/", query_string={"ticker": "SPY",
                                                 "expiration": "2019-01-01"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("nicht gelistet", res.get_data(as_text=True))

    def test_index_rejects_injection_attempt_readably(self):
        res = self.client.get("/", query_string={"ticker": "../etc",
                                                 "expiration": "2026-10-16"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Ungueltiger Ticker", res.get_data(as_text=True))


class TestDteWindow(unittest.TestCase):
    """B-11: the expiration picker must only offer dates that can hit.

    17 of 25 AAPL dates fall outside [dte_min, dte_max] - 3 below and 14
    above - so the picker listed dates that can never produce a hit. The
    window filter narrows the OFFERED list; validation still runs against
    the full list, so a bookmarked (filtered) date returns 200 with zero
    hits instead of 400.
    """

    # the five fixture dates against the pinned clock 2026-10-07T14:35
    ALL_DATES = list(_MockAdapter(make()).expirations)
    IN_WINDOW = ["2026-10-14", "2026-10-16", "2026-11-21"]

    def _app(self, criteria=None, now_fn=None):
        app = create_app(
            quote_adapter=_MockAdapter(make()),
            screener=None if criteria is None else OptionScreener(criteria),
            now_fn=now_fn or _clock())
        app.config["TESTING"] = True
        return app

    def _picker_dates(self, criteria=None, path="/api/screen", ticker="SPY",
                      now_fn=None):
        res = self._app(criteria, now_fn).test_client().get(
            path, query_string={"ticker": ticker})
        return res

    # --- characterisation of the calendar-day rule (green on master) ---

    def test_days_to_expiration_space_and_t_separator(self):
        # the T- and space-separated forms of the same instant must not
        # drift apart: raw .days would return -1 on expiration day
        opt = asset_factory("AAPL261009C00250000")
        self.assertEqual(opt.get_days_to_expiration("2026-10-07T14:35:00"), 2)
        self.assertEqual(opt.get_days_to_expiration("2026-10-07 14:35:00"), 2)

    def test_days_to_expiration_accepts_date_and_arrow(self):
        opt = asset_factory("AAPL261009C00250000")
        self.assertEqual(opt.get_days_to_expiration("2026-10-07"), 2)
        self.assertEqual(
            opt.get_days_to_expiration(arrow.get("2026-10-07")), 2)
        self.assertEqual(
            opt.get_days_to_expiration(datetime(2026, 10, 7).date()), 2)

    def test_days_to_expiration_negative_on_past_expiry(self):
        # an expired date must stay negative, not be clamped to 0
        opt = asset_factory("AAPL261009C00250000")
        self.assertEqual(opt.get_days_to_expiration("2026-10-10 00:00:00"), -1)

    # --- the window filter (red on master) ---

    def test_picker_lists_only_dates_inside_the_dte_window(self):
        d = self._picker_dates().get_json()
        self.assertEqual(d["listed_dates"],
                         ["2026-10-14", "2026-10-16", "2026-11-21"])

    def test_picker_keeps_the_inclusive_lower_boundary(self):
        # dte == 7 must remain selectable (>= not >)
        d = self._picker_dates().get_json()
        self.assertIn("2026-10-14", d["listed_dates"])

    def test_picker_keeps_the_inclusive_upper_boundary(self):
        # dte == 45 must remain selectable (<= not <)
        d = self._picker_dates().get_json()
        self.assertIn("2026-11-21", d["listed_dates"])

    def test_index_html_does_not_list_out_of_window_dates(self):
        res = self._picker_dates(path="/")
        html = res.get_data(as_text=True)
        self.assertNotIn("2026-12-18", html)   # 72 dte, above the window
        self.assertIn("2026-11-21", html)       # 45 dte, still offered

    def test_filtered_but_real_date_still_returns_200(self):
        # the regression guard on the DANGEROUS direction: narrowing the
        # OFFER must not narrow VALIDATION - a saved link to a real but
        # filtered date keeps working (200, zero hits), not 400
        for date in ("2026-12-18", "2026-10-09"):
            res = self._app().test_client().get(
                "/api/screen",
                query_string={"ticker": "SPY", "expiration": date})
            self.assertEqual(res.status_code, 200, date)

    # --- B-14: the <select> and validation must read the SAME list ---

    def test_index_keeps_an_out_of_window_bookmark(self):
        # 200 alone is not enough: test_filtered_but_real_date_still_returns_200
        # was green while the <select> rendered WITHOUT the bookmarked date,
        # leaving the form displaying a different date than it screened for.
        html = self._app().test_client().get(
            "/", query_string={"ticker": "SPY", "expiration": "2026-12-18"}
        ).get_data(as_text=True)
        self.assertIn('<option value="2026-12-18" selected>', html)

    def test_in_window_bookmark_is_still_marked(self):
        # control for the test above: the normal case must not regress
        html = self._app().test_client().get(
            "/", query_string={"ticker": "SPY", "expiration": "2026-10-16"}
        ).get_data(as_text=True)
        self.assertIn('<option value="2026-10-16" selected>', html)

    def test_empty_window_says_unavailable_not_loading(self):
        html = self._app(
            ScreenerCriteria(dte_min=900, dte_max=1000)
        ).test_client().get("/", query_string={"ticker": "SPY"}
                            ).get_data(as_text=True)
        # match the OPTION, not the bare phrase: the same text also lives in
        # the JS loader (invalidateSelector), so a bare assertNotIn is useless
        self.assertNotIn('<option value="">Verfaelle werden geladen', html)
        self.assertIn('<option value="" disabled>Nicht verfuegbar', html)

    def test_no_ticker_still_says_loading(self):
        # the real loading state (no dates fetched yet) keeps its placeholder
        html = self._app().test_client().get("/").get_data(as_text=True)
        self.assertIn('<option value="">Verfaelle werden geladen', html)

    def test_unlisted_date_never_reaches_the_picker(self):
        # the union guard: only dates that are really in the chain may be
        # added back to the offer list. Note the index route reports this
        # softly (200 + message) where /api/screen aborts with 400.
        res = self._app().test_client().get(
            "/", query_string={"ticker": "SPY", "expiration": "2030-01-01"})
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("nicht gelistet", html)
        self.assertNotIn('<option value="2030-01-01"', html)

    def test_picker_uses_the_injected_screener_criteria(self):
        d = self._picker_dates(ScreenerCriteria(dte_min=20)).get_json()
        self.assertEqual(d["listed_dates"], ["2026-11-21"])
        d = self._picker_dates(ScreenerCriteria(dte_max=10)).get_json()
        self.assertEqual(d["listed_dates"], ["2026-10-14", "2026-10-16"])

    # --- the optional bounds: None disables that side ---

    def test_picker_without_a_lower_bound(self):
        # dte_min=None disables the floor - a naive chained comparison
        # would raise TypeError instead of widening the list
        d = self._picker_dates(ScreenerCriteria(dte_min=None)).get_json()
        self.assertEqual(d["listed_dates"], self.ALL_DATES[:-1])

    def test_picker_without_an_upper_bound(self):
        d = self._picker_dates(ScreenerCriteria(dte_max=None)).get_json()
        self.assertEqual(d["listed_dates"], self.ALL_DATES[1:])

    def test_empty_window_yields_an_empty_picker(self):
        # a window that matches nothing is a legal state: empty picker,
        # HTTP 200, no crash and no fallback to the unfiltered list.
        # [10, 40] excludes every fixture date: 2/7/9 below, 45/72 above.
        res = self._picker_dates(ScreenerCriteria(dte_min=10, dte_max=40))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["listed_dates"], [])

    # --- the window must follow the CLOCK, not a frozen date ---

    def test_window_follows_the_injected_clock(self):
        # Every other test pins the clock to 2026-10-07, so a hardcoded
        # as_of inside the filter would pass all of them. Measured: with
        # as_of frozen to 2026-10-07 the suite stays green. This case moves
        # the as-of day and demands the offered set move with it.
        later = _later_clock()
        d = self._picker_dates(now_fn=later).get_json()
        # against 2026-11-15 the fixture dtes are [-37,-32,-30, 6, 33]:
        # only 2026-12-18 falls inside [7, 45]
        self.assertEqual(d["listed_dates"], ["2026-12-18"])
        # and the SAME dates judged at the original clock give the other set
        d = self._picker_dates().get_json()
        self.assertEqual(d["listed_dates"], self.IN_WINDOW)


if __name__ == "__main__":
    unittest.main()
