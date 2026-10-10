"""Offline tests for the Phase-5 web display (paperbroker/server.py).

Flask test client + mock quote chain: no network, deterministic.
Covers the debate-judge requirements: input validation (no path
injection into the CBOE URL), trend failure != flat, freshness lamp,
healthz liveness without external calls.
"""
import html
import re
import unittest
from datetime import datetime

import arrow
import pytest
from flask import Flask

from paperbroker.assets import asset_factory
from paperbroker.screener import OptionScreener, ScreenerCriteria
from paperbroker.server import _freshness_lamp, create_app
from paperbroker.tests.test_screener import make, quote


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

    def test_bookmarked_date_is_listed_first(self):
        # keeping the bookmarked date is not enough - it has to come FIRST.
        # Appending it would still satisfy the "is it there" test while
        # burying the very date the user asked for at the bottom of a list
        # of dates they did not ask about.
        html = self._app().test_client().get(
            "/", query_string={"ticker": "SPY", "expiration": "2026-12-18"}
        ).get_data(as_text=True)
        options = re.findall(r'<option value="([^"]*)"', html)
        self.assertEqual(options[0], "2026-12-18")
        self.assertEqual(options, ["2026-12-18"] + self.IN_WINDOW)

    def test_non_bookmarked_ticker_keeps_the_window_order(self):
        # without a bookmark the offer is exactly the filtered list, so the
        # "bookmark first" rule must not reorder anything on its own
        html = self._app().test_client().get(
            "/", query_string={"ticker": "SPY"}).get_data(as_text=True)
        options = re.findall(r'<option value="([^"]*)"', html)
        self.assertEqual(options, self.IN_WINDOW)

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



class TestDropReasons(unittest.TestCase):
    """UX-1 (Ausbau-Slice 1): the page names WHY a contract was dropped."""

    STALE_TS = "2026-10-07 10:00:00"     # 275 min before the pinned clock

    def _app(self, quotes, criteria=None):
        app = create_app(
            quote_adapter=_MockAdapter(quotes),
            screener=None if criteria is None else OptionScreener(criteria),
            now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _stale_chain(self):
        qs = make()
        for q in qs:
            q.quote_timestamp = self.STALE_TS
        return qs

    def _mixed_chain(self):
        # six contracts, one clean candidate. The drops cover a raw-text
        # group (two identical "iv=0 illiquid"), a PATTERN group (two
        # different OI bounds) and a single raw group (one wide spread).
        return [
            quote("AAPL260116C00240000", bid=5.00, ask=5.10, iv=28.0, oi=8000,
                  dte=10),                                   # candidate
            quote("AAPL260116C00245000", bid=2.40, ask=2.50, iv=0.0, oi=1500,
                  dte=10),                                   # iv=0 illiquid
            quote("AAPL260116C00260000", bid=2.40, ask=2.50, iv=0.0, oi=1500,
                  dte=10),                                   # iv=0 illiquid
            quote("AAPL260116C00270000", bid=1.20, ask=1.30, iv=30.0, oi=5,
                  dte=10),                                   # OI 5 < 500
            quote("AAPL260116C00275000", bid=1.20, ask=1.30, iv=30.0, oi=200,
                  dte=10),                                   # OI 200 < 500
            quote("AAPL260116C00280000", bid=1.50, ask=2.00, iv=45.0, oi=9000,
                  dte=10),                                   # spread 28.6 %
        ]

    @staticmethod
    def _pairs(html_page):
        """Read the rendered groups: (reason, count) in page order.

        The template is autoescaped, so '<' and '>' arrive as entities -
        unescape BEFORE comparing, because the browser shows the text.
        """
        return [(html.unescape(m.group(1)), int(m.group(2))) for m in re.finditer(
            r'<li class="drop-reason">(.*?) <span class="drop-count">(\d+)</span></li>',
            html_page)]

    def _page(self, quotes, criteria=None):
        return self._app(quotes, criteria).test_client().get(
            "/", query_string={"ticker": "SPY", "expiration": "2026-10-16"}
        ).get_data(as_text=True)

    def _json(self, quotes, criteria=None):
        return self._app(quotes, criteria).test_client().get(
            "/api/screen",
            query_string={"ticker": "SPY", "expiration": "2026-10-16"}
        ).get_json()

    # --- the empty case: the whole chain fails the age guard ---

    def test_the_page_names_the_reasons_when_nothing_hits(self):
        html = self._page(self._stale_chain())
        self.assertIn("Keine Kontrakte haben die Kriterien bestanden", html)
        # drop order is the pipeline order: iv=0 first, the rest is stale
        self.assertEqual(self._pairs(html),
                         [("stale 275 min", 6), ("iv=0 illiquid", 1)])

    def test_the_page_shows_the_age_and_the_limit(self):
        # the lamp already said "alt"; UX-1 adds the measured value
        html_page = self._page(self._stale_chain())
        self.assertIn("lamp red", html_page)
        self.assertIn("(275 min, Grenze 30 min)", html_page)

    def test_the_header_age_agrees_with_the_reason_text(self):
        # 14:35:00 - 10:00:24 = 274.6 min: the header must round the same way
        # as the "stale N min" text (274.6 -> 275), not floor it to 274
        chain = make()
        for q in chain:
            q.quote_timestamp = "2026-10-07 10:00:24"
        html_page = self._page(chain)
        self.assertIn("(275 min, Grenze 30 min)", html_page)
        self.assertEqual(self._pairs(html_page)[0], ("stale 275 min", 6))

    # --- the mixed case: candidates exist, drops are still explained ---

    def test_the_block_is_rendered_with_candidates_too(self):
        html = self._page(self._mixed_chain())
        self.assertIn("Kandidaten: <b>1</b>", html)
        # ties (both count 2) keep the order the filters produced them in:
        # data-complete (iv=0) runs before oi; the spread group is last
        self.assertEqual(self._pairs(html),
                         [("iv=0 illiquid", 2), ("OI # < #", 2),
                          ("spread 28.6% > 10.0%", 1)])

    def test_two_numbers_of_one_family_collapse_into_one_group(self):
        pairs = dict((r, c) for r, c in self._pairs(self._page(self._mixed_chain())))
        # 5 and 200 are different bounds - one family, one line
        self.assertEqual(pairs.get("OI # < #"), 2)
        self.assertNotIn("OI 5 < 500", pairs)
        self.assertNotIn("OI 200 < 500", pairs)

    def test_a_single_raw_text_keeps_its_measured_value(self):
        # a group with exactly one text shows it (with its number)
        pairs = dict(self._pairs(self._page(self._stale_chain())))
        self.assertIn("stale 275 min", pairs)
        self.assertNotIn("stale # min", pairs)

    # --- the JSON contract (additive) ---

    def test_json_carries_the_groups_additively(self):
        d = self._json(self._mixed_chain())
        self.assertIsInstance(d["dropped"], int)          # unchanged
        self.assertEqual(d["dropped"], 5)
        self.assertEqual(d["dropped_reasons"],
                         [{"reason": "iv=0 illiquid", "count": 2},
                          {"reason": "OI # < #", "count": 2},
                          {"reason": "spread 28.6% > 10.0%", "count": 1}])
        counts = [g["count"] for g in d["dropped_reasons"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_json_and_html_report_the_same_groups(self):
        chain = self._mixed_chain()
        self.assertEqual(self._pairs(self._page(chain)),
                         [(g["reason"], g["count"])
                          for g in self._json(chain)["dropped_reasons"]])

    def test_the_empty_list_is_carried_in_both_stages(self):
        # picker stage: no scan happened yet - and a scan that dropped
        # nothing; the key exists in both, the list is empty (contract:
        # a consumer never needs a key check)
        picker = self._app(self._stale_chain()).test_client().get(
            "/api/screen", query_string={"ticker": "SPY"}).get_json()
        self.assertEqual(picker["dropped_reasons"], [])
        clean = self._json([quote("AAPL260116C00240000", bid=5.00, ask=5.10,
                                  iv=28.0, oi=8000, dte=10)])
        self.assertEqual(clean["dropped_reasons"], [])
        self.assertEqual(clean["dropped"], 0)

    # --- edge case: nothing dropped at all ---

    def test_no_drops_render_no_block(self):
        chain = [quote("AAPL260116C00240000", bid=5.00, ask=5.10, iv=28.0,
                       oi=8000, dte=10)]
        html_page = self._page(chain)
        self.assertIn("Kandidaten: <b>1</b>", html_page)
        self.assertIn("Datenalter:", html_page)          # the header stays
        # match the ELEMENT, not the bare class: the same name also lives
        # in the <style> block, so a substring check would always pass
        self.assertNotIn('<div class="dropped-summary">', html_page)
        self.assertNotIn('<li class="drop-reason">', html_page)

    @staticmethod
    def _one(ts):
        return [quote("AAPL260116C00240000", bid=5.00, ask=5.10, iv=28.0,
                      oi=8000, dte=10, quote_timestamp=ts)]

    def test_the_lamp_bands_and_the_missing_value(self):
        # pin: measured before this slice - green < 30, yellow < 120, red
        # from 120 min; unreadable or absent timestamp -> no lamp at all
        lamp = _freshness_lamp
        self.assertEqual(lamp(self._one("2026-10-07 14:06:00"), now_fn=_clock()),
                         "green")                       # 29 min
        self.assertEqual(lamp(self._one("2026-10-07 14:05:00"), now_fn=_clock()),
                         "yellow")                      # 30 min exactly
        self.assertEqual(lamp(self._one("2026-10-07 12:36:00"), now_fn=_clock()),
                         "yellow")                      # 119 min
        self.assertEqual(lamp(self._one("2026-10-07 12:35:00"), now_fn=_clock()),
                         "red")                         # 120 min exactly
        self.assertIsNone(lamp(self._one("gestern abend"), now_fn=_clock()))
        self.assertIsNone(lamp([], now_fn=_clock()))

    def test_the_lamp_reads_the_fetched_chain_and_the_header_shows_it(self):
        # newest governs; the lamp reads the FETCHED chain (CodeRabbit), not
        # the results - here the fresh contract is the DROPPED one
        mixed = [
            quote("AAPL260116C00240000", bid=5.00, ask=5.10, iv=0.0, oi=8000,
                  dte=10, quote_timestamp="2026-10-07 14:30:00"),   # fresh, dropped
            quote("AAPL260116C00245000", bid=2.40, ask=2.50, iv=28.0, oi=1500,
                  dte=10, quote_timestamp="2026-10-07 10:00:00"),   # old, stale
        ]
        html_page = self._page(mixed)
        self.assertIn("lamp green", html_page)
        self.assertIn("(5 min, Grenze 30 min)", html_page)

    def test_a_reason_text_cannot_inject_markup(self):
        # the reasons are free text from the screener; they are rendered
        # escaped, so a '<' stays text (measured: '&lt;' in the HTML)
        html_page = self._page(self._mixed_chain())
        self.assertIn("OI # &lt; #", html_page)
        self.assertIn('<span class="drop-count">2</span>', html_page)



class TestCriteriaPanel(unittest.TestCase):
    """Ausbau-Slice 2 (A): the filter criteria become operator-settable.

    Six fields plus the switch `ignore_quote_age`. The criteria of the app the
    request runs against stay the BASE: only values the request really sets
    override them (the picker tests inject their own criteria).
    0 is a valid, sharp age bound - the switch is the only way to turn the
    guard off, and it is announced on the page.
    """

    def _app(self, quotes=None, criteria=None):
        app = create_app(
            quote_adapter=_MockAdapter(make() if quotes is None else quotes),
            screener=None if criteria is None else OptionScreener(criteria),
            now_fn=_clock())
        app.config["TESTING"] = True
        return app

    @staticmethod
    def _stale_chain():
        # every fixture is 275 min old: with the default 30-min guard the
        # chain is wiped; a settable guard lets two of them through
        qs = make()
        for q in qs:
            q.quote_timestamp = "2026-10-07 10:00:00"
        return qs

    @staticmethod
    def _stats(html_page):
        m = re.search(r'<div class="stats">(.*?)</div>', html_page, re.S)
        return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())

    def _request(self, params, quotes=None, criteria=None, path="/"):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params)
        return self._app(quotes, criteria).test_client().get(path, query_string=query)

    def _page(self, params, quotes=None, criteria=None):
        return self._request(params, quotes, criteria).get_data(as_text=True)

    def _json(self, params, quotes=None, criteria=None):
        return self._request(params, criteria=criteria, quotes=quotes,
                             path="/api/screen").get_json()

    # --- the age guard becomes settable (the Saturday case) ---

    def test_the_age_guard_becomes_settable(self):
        stale = self._stale_chain()
        self.assertIn("Kandidaten: 0", self._stats(self._page({}, stale)))
        html = self._page({"max_quote_age_min": "999"}, stale)
        self.assertIn("Kandidaten: 2", self._stats(html))
        self.assertIn("(275 min, Grenze 999 min)", html)
        # the form shows what is in effect and offers the switch
        self.assertIn('name="max_quote_age_min"', html)
        self.assertIn('value="999"', html)
        self.assertIn('name="ignore_quote_age"', html)

    def test_the_switch_disables_the_age_check_visibly(self):
        html = self._page({"ignore_quote_age": "1"}, self._stale_chain())
        self.assertIn("Kandidaten: 2", self._stats(html))
        self.assertIn("(275 min, Grenze aus)", html)
        self.assertIn('class="warn"', html)            # the banner element
        labels = re.findall(r'<li class="drop-reason">(.*?) <span', html)
        self.assertTrue(labels)
        self.assertTrue(all("stale" not in label for label in labels), labels)

    def test_the_switch_is_stateless(self):
        cli = self._app().test_client()
        base_q = {"ticker": "SPY", "expiration": "2026-10-16"}
        a = cli.get("/api/screen", query_string=base_q).get_json()
        b = cli.get("/api/screen",
                    query_string={**base_q, "ignore_quote_age": "1"}).get_json()
        c = cli.get("/api/screen", query_string=base_q).get_json()
        self.assertEqual(a["criteria"]["max_quote_age_min"], 30)
        self.assertIsNone(b["criteria"]["max_quote_age_min"])
        self.assertEqual(c["criteria"]["max_quote_age_min"], 30)

    # --- the injected criteria stay the base ---

    def test_an_injected_criteria_stays_the_base(self):
        picker = self._app(criteria=ScreenerCriteria(dte_min=40)).test_client().get(
            "/api/screen", query_string={"ticker": "SPY"}).get_json()
        self.assertEqual(picker["listed_dates"], ["2026-11-21"])

    def test_a_request_value_overrides_the_injected_criteria(self):
        # injected dte_min=20 against a request that sets dte_max=10 -> the
        # merged set is contradictory: reported, not silently accepted
        # error texts are rendered escaped ("&lt;=") - compare the visible text
        page = html.unescape(self._page({"dte_max": "10"},
                                        criteria=ScreenerCriteria(dte_min=20)))
        self.assertIn("dte_min <= dte_max", page)
        self.assertNotIn("Kandidaten:", page)

    # --- contradictions and bad values are errors, not silence ---

    def test_contradicting_criteria_are_reported(self):
        page = html.unescape(self._page({"dte_min": "45", "dte_max": "7"}))
        self.assertIn("dte_min <= dte_max", page)
        self.assertNotIn("Kandidaten:", page)
        res = self._request({"dte_min": "45", "dte_max": "7"}, path="/api/screen")
        self.assertEqual(res.status_code, 400)
        self.assertIn("dte_min <= dte_max", res.get_json()["error"])

    def test_bad_values_are_reported_readably(self):
        for params, needle in (({"min_oi": "abc"}, "keine ganze Zahl"),
                               ({"min_volume": "-5"}, "min_volume >= 0"),
                               ({"max_spread_pct": "nan"}, "endliche Zahl")):
            page = html.unescape(self._page(params))
            self.assertIn(needle, page)
            self.assertNotIn("Kandidaten:", page)
            self.assertEqual(self._request(params, path="/api/screen").status_code, 400)

    def test_a_comma_decimal_gets_a_hint(self):
        html = self._page({"max_spread_pct": "7,5"})
        self.assertIn("Punkt als Dezimaltrenner", html)
        self.assertNotIn("Kandidaten:", html)

    def test_an_unknown_parameter_is_named(self):
        html = self._page({"min_o": "500"})
        self.assertIn("Unbekannte Parameter", html)
        self.assertIn("min_o", html)
        self.assertIn("Kandidaten: 2", self._stats(html))   # criteria unchanged

    # --- the fields really move the result ---

    def test_a_min_oi_moves_the_candidate_count(self):
        self.assertIn("Kandidaten: 2", self._stats(self._page({})))
        self.assertIn("Kandidaten: 1", self._stats(self._page({"min_oi": "5000"})))

    def test_the_echo_is_the_object_that_computed(self):
        d = self._json({"dte_min": "045"})
        self.assertEqual(d["criteria"]["dte_min"], 45)          # int, not "045"
        self.assertEqual(d["criteria"]["max_quote_age_min"], 30)
        self.assertIsNone(d["criteria"]["iv_min"])
        self.assertEqual(len(d["criteria"]), 17)
        picker = self._app().test_client().get(
            "/api/screen", query_string={"ticker": "SPY"}).get_json()
        self.assertNotIn("criteria", picker)                     # no picker echo
        # the request criteria must reach the PICKER too (B-14: one source):
        # dte_min=45 leaves only the 45-dte date of the fixture chain
        narrow = self._app().test_client().get(
            "/api/screen", query_string={"ticker": "SPY", "dte_min": "45"}).get_json()
        self.assertEqual(narrow["listed_dates"], ["2026-11-21"])
        # ... and on the PAGE too: the picker there is built by index(), not by
        # the API helper - a mutant that passes the base criteria to only one
        # of the two routes has to fail here (B-14: one source for both)
        page = self._app().test_client().get(
            "/", query_string={"ticker": "SPY", "dte_min": "45"}).get_data(as_text=True)
        options = re.findall(r'<option value="(\d{4}-\d{2}-\d{2})"', page)
        self.assertEqual(options, ["2026-11-21"])


if __name__ == "__main__":
    unittest.main()
