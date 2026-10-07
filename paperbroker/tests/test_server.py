"""Offline tests for the Phase-5 web display (paperbroker/server.py).

Flask test client + mock quote chain: no network, deterministic.
Covers the debate-judge requirements: input validation (no path
injection into the CBOE URL), trend failure != flat, freshness lamp,
healthz liveness without external calls.
"""
import unittest
from datetime import datetime

import arrow
from flask import Flask

from paperbroker.server import create_app
from paperbroker.tests.test_screener import make


class _MockAdapter:
    """Returns the mock chain for every get_options call."""

    def __init__(self, quotes):
        self.quotes = quotes
        self.expirations = ["2026-10-09", "2026-10-16"]

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
        self.assertEqual(data["listed_dates"], ["2026-10-09", "2026-10-16"])
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
        self.assertEqual(d["listed_dates"], ["2026-10-09", "2026-10-16"])
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


if __name__ == "__main__":
    unittest.main()
