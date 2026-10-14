"""Offline tests for the Phase-5 web display (paperbroker/server.py).

Flask test client + mock quote chain: no network, deterministic.
Covers the debate-judge requirements: input validation (no path
injection into the CBOE URL), trend failure != flat, freshness lamp,
healthz liveness without external calls.
"""
import html
import json
import os
import re
import unittest
from datetime import datetime

import arrow
import pytest
from flask import Flask

from paperbroker import server as server_module
from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
    CboeNotFoundError,
    CboeRequestError,
)
from paperbroker.assets import asset_factory
from paperbroker.screener import OptionScreener, ScreenerCriteria, TrendSignal
from paperbroker.server import _freshness_lamp, create_app
from paperbroker.tests.test_screener import make, make_verticals, quote


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


_LOOSE = ScreenerCriteria(max_spread_pct=100.0)

# D7: the leg chain of the spread section is the chain of the list above, so the
# legs now also run through the default ABSOLUTE spread bound (0.10 $ in the penny
# zone). The vertical fixture quotes the short legs of AMZN/NVDA/TSLA at
# 0.50/0.70 - 0.20 $ wide at a mid of 0.60 - so EVERY pair would be rejected by
# that one bound and the section would be empty: the sort, shape and column tests
# below would then have no row left to check (and `_spread_rows` no table at all).
# Tests whose subject is the surviving PAIR LIST run with `_LOOSE` and that single
# bound switched off - the equivalence probe the panel asked for ("min_oi 500,
# OI-Pflicht, kein Spread-Limit"). The bound itself stays pinned, on legs, in
# test_screener.py (TestVerticalCallSpreads).
_WIDE = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None)


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
        self.assertIn("Verfälle werden geladen", html)

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
        self.assertIn("Ungültiger Ticker", res.get_data(as_text=True))


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
        self.assertNotIn('<option value="">Verfälle werden geladen', html)
        self.assertIn('<option value="" disabled>Nicht verfügbar', html)

    def test_no_ticker_still_says_loading(self):
        # the real loading state (no dates fetched yet) keeps its placeholder
        html = self._app().test_client().get("/").get_data(as_text=True)
        self.assertIn('<option value="">Verfälle werden geladen', html)

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
        # group (two identical "iv=0 illiquide"), a PATTERN group (two
        # different OI bounds) and a single raw group (one wide spread).
        return [
            quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0, oi=8000),                                   # candidate
            quote("AAPL261016C00245000", bid=2.40, ask=2.50, iv=0.0, oi=1500),                                   # iv=0 illiquide
            quote("AAPL261016C00260000", bid=2.40, ask=2.50, iv=0.0, oi=1500),                                   # iv=0 illiquide
            quote("AAPL261016C00270000", bid=1.20, ask=1.30, iv=30.0, oi=5),                                   # OI 5 < 500
            quote("AAPL261016C00275000", bid=1.20, ask=1.30, iv=30.0, oi=200),                                   # OI 200 < 500
            quote("AAPL261016C00280000", bid=1.50, ask=2.00, iv=45.0, oi=9000),                                   # spread 28.6 %
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

    def _page_with(self, quotes, **params):
        """The page for a request that carries the criteria fields itself.

        `_page` injects a criteria OBJECT; the switch `ignore_quote_age` only
        exists as a request parameter, so the new tests need this path.
        """
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params)
        return self._app(quotes).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    # --- the empty case: the whole chain fails the age guard ---

    def test_the_page_names_the_reasons_when_nothing_hits(self):
        html = self._page(self._stale_chain())
        self.assertIn("Keine Kontrakte haben die Kriterien bestanden", html)
        # drop order is the pipeline order: iv=0 first, the rest is stale
        self.assertEqual(self._pairs(html),
                         [("veraltet: 275 Min.", 6), ("iv=0 illiquide", 1)])

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
        self.assertEqual(self._pairs(html_page)[0], ("veraltet: 275 Min.", 6))

    # --- the mixed case: candidates exist, drops are still explained ---

    def test_the_block_is_rendered_with_candidates_too(self):
        html = self._page(self._mixed_chain())
        self.assertIn("Kandidaten: <b>1</b>", html)
        # ties (both count 2) keep the order the filters produced them in:
        # data-complete (iv=0) runs before oi; the spread group is last.
        # D9a/E7: the group names ONE real example out of its family of
        # bounds (the numbers-masked pattern is gone) - sorted() picks the
        # stable member, so the label is reproducible.
        self.assertEqual(self._pairs(html),
                         [("iv=0 illiquide", 2), ("OI 200 < 500", 2),
                          ("Spread 28.6% > 10.0%", 1)])

    def test_two_numbers_of_one_family_collapse_into_one_group(self):
        pairs = dict((r, c) for r, c in self._pairs(self._page(self._mixed_chain())))
        # 5 and 200 are different bounds - one family, one line. The family is
        # identified by ONE real member text now (D9a/E7), not by the pattern.
        self.assertEqual(pairs.get("OI 200 < 500"), 2)
        self.assertNotIn("OI 5 < 500", pairs)      # the other member is no line of its own
        self.assertNotIn("OI # < #", pairs)       # and the masked pattern is gone

    def test_two_bounds_of_one_family_share_one_real_example(self):
        """D9a/E7 (a): the grouping still collapses a family - now it names a
        real member as the example instead of the numbers-masked pattern."""
        chain = [
            quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0, oi=8000),  # candidate
            quote("AAPL261016C00270000", bid=1.20, ask=1.30, iv=30.0, oi=200),  # OI 200 < 500
            quote("AAPL261016C00271000", bid=1.20, ask=1.30, iv=30.0, oi=40),   # OI 40 < 500
        ]
        labels = [r for r, _ in self._pairs(self._page(chain))]
        self.assertEqual(labels, ["OI 200 < 500"])       # ONE line for the family
        self.assertIn(labels[0], {"OI 200 < 500", "OI 40 < 500"})   # a real member ...
        self.assertNotIn("#", labels[0])                 # ... not the pattern
        self.assertEqual(self._json(chain)["dropped_reasons"],
                         [{"reason": "OI 200 < 500", "count": 2}])

    def test_the_summary_shows_a_real_bound_not_the_masked_pattern(self):
        """D9a/E7 (a): the page names the concrete bounds of the group."""
        html_page = self._page(self._mixed_chain())
        # the relative-spread group carries the real limit; autoescaping turns
        # '>' into an entity, which is what the browser shows as a bound
        self.assertIn("&gt; 10.0%", html_page)
        self.assertNotIn("Spread #%", html_page)

    def test_a_single_raw_text_keeps_its_measured_value(self):
        # a group with exactly one text shows it (with its number)
        pairs = dict(self._pairs(self._page(self._stale_chain())))
        self.assertIn("veraltet: 275 Min.", pairs)
        self.assertNotIn("veraltet: # Min.", pairs)

    # --- the JSON contract (additive) ---

    def test_json_carries_the_groups_additively(self):
        d = self._json(self._mixed_chain())
        self.assertIsInstance(d["dropped"], int)          # unchanged
        self.assertEqual(d["dropped"], 5)
        self.assertEqual(d["dropped_reasons"],
                         [{"reason": "iv=0 illiquide", "count": 2},
                          {"reason": "OI 200 < 500", "count": 2},
                          {"reason": "Spread 28.6% > 10.0%", "count": 1}])
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
        clean = self._json([quote("AAPL261016C00240000", bid=5.00, ask=5.10,
                                  iv=28.0, oi=8000)])
        self.assertEqual(clean["dropped_reasons"], [])
        self.assertEqual(clean["dropped"], 0)

    # --- edge case: nothing dropped at all ---

    def test_no_drops_render_no_block(self):
        chain = [quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0,
                       oi=8000)]
        html_page = self._page(chain)
        self.assertIn("Kandidaten: <b>1</b>", html_page)
        self.assertIn("Datenalter:", html_page)          # the header stays
        # match the ELEMENT, not the bare class: the same name also lives
        # in the <style> block, so a substring check would always pass
        # D2: der Ausschuss-Kasten ist ein <details> geworden - das ELEMENT
        # pruefen, nicht den blanken Klassennamen (der steht auch im <style>)
        self.assertNotIn('<details class="dropped-summary"', html_page)
        self.assertNotIn('<li class="drop-reason">', html_page)

    @staticmethod
    def _one(ts):
        return [quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0,
                      oi=8000, quote_timestamp=ts)]

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
            quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=0.0, oi=8000, quote_timestamp="2026-10-07 14:30:00"),   # fresh, dropped
            quote("AAPL261016C00245000", bid=2.40, ask=2.50, iv=28.0, oi=1500, quote_timestamp="2026-10-07 10:00:00"),   # old, stale
        ]
        html_page = self._page(mixed)
        self.assertIn("lamp green", html_page)
        self.assertIn("(5 min, Grenze 30 min)", html_page)

    # --- D9a/E3 (b): the operator's own limit next to the general lamp ---

    def test_the_header_names_the_limit_question_separately_from_the_lamp(self):
        """D9a/E3 (b): the traffic light answers the general freshness class
        (30/120 min, unchanged); the operator's OWN limit gets its own named
        line, so 'frisch' can never again stand next to a violated limit."""
        inside = self._page(self._mixed_chain())          # 5 min vs limit 30
        self.assertIn("lamp green", inside)               # the general statement ...
        self.assertIn("(5 min, Grenze 30 min)", inside)
        self.assertRegex(inside, r"innerhalb deiner Altersgrenze:\s*ja")
        # the exact feed stamp the chain carries (D8 already names it)
        self.assertIn("Kursdatum 2026-10-07 14:30:00", inside)

        # a limit the present quotes violate: the limit line answers 'nein',
        # the general lamp may stay green - the two statements are separate
        violated = self._page_with(self._mixed_chain(), max_quote_age_min="1")
        self.assertIn("lamp green", violated)
        self.assertRegex(violated, r"innerhalb deiner Altersgrenze:\s*nein")
        self.assertIn("(Grenze 1 min)", violated)

    def test_the_header_announces_a_switched_off_age_limit(self):
        """D9a/E3 (b): with ignore_quote_age=1 the header says so instead of
        naming a limit that is not enforced."""
        html_page = self._page_with(self._mixed_chain(), ignore_quote_age="1")
        self.assertIn("Altersgrenze aus", html_page)
        self.assertIn("(5 min, Grenze aus)", html_page)   # the lamp line, unchanged

    def test_a_reason_text_cannot_inject_markup(self):
        # the reasons are free text from the screener; they are rendered
        # escaped, so a '<' stays text (measured: '&lt;' in the HTML). D9a/D7
        # made the shown text a REAL example, so the escaped bound is the one
        # the group picked - and it still must not become markup.
        html_page = self._page(self._mixed_chain())
        self.assertIn("OI 200 &lt; 500", html_page)
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
        self.assertIn("dte_min: muss ≤ dte_max sein (ist: 20 > 10).", page)
        self.assertNotIn("Kandidaten:", page)

    # --- contradictions and bad values are errors, not silence ---

    def test_contradicting_criteria_are_reported(self):
        page = html.unescape(self._page({"dte_min": "45", "dte_max": "7"}))
        self.assertIn("dte_min: muss ≤ dte_max sein (ist: 45 > 7).", page)
        self.assertNotIn("Kandidaten:", page)
        res = self._request({"dte_min": "45", "dte_max": "7"}, path="/api/screen")
        self.assertEqual(res.status_code, 400)
        self.assertIn("dte_min: muss ≤ dte_max sein (ist: 45 > 7).",
                      res.get_json()["error"])

    def test_bad_values_are_reported_readably(self):
        for params, needle in (({"min_oi": "abc"}, "keine ganze Zahl"),
                               ({"min_volume": "-5"},
                                "min_volume: muss ≥ 0 sein (ist: -5)."),
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
        # D11: pair_max_width is the 18th criterion. Unset it stays None - the
        # adjacency of before - so the echo names the fast mode as the default.
        self.assertIsNone(d["criteria"]["pair_max_width"])
        # D12: the four position keys are the 19th..22nd criterion. The echo stays
        # one serialisation for every field of the object that computed - the
        # position size came along with the object, not into the template.
        self.assertEqual(d["criteria"]["multiplier"], 100)
        self.assertEqual(d["criteria"]["quantity"], 1)
        self.assertEqual(d["criteria"]["fees"], 0.0)
        self.assertIsNone(d["criteria"]["budget"])
        self.assertEqual(len(d["criteria"]), 22)
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

    def test_the_position_fields_are_settable_through_the_request(self):
        """D12: die vier Positionsfelder stehen in den Kriterien-Feldlisten des
        Servers (`_CRITERIA_FIELDS`). Fehlten sie dort, meldete das Formular sie
        als UNBEKANNTE Parameter und der Wert verpuffte - der Nutzer stellt eine
        Positionsgroesse ein, die die Rechnung nie sieht.
        """
        werte = {"multiplier": "10", "quantity": "3", "fees": "1.5",
                 "budget": "500"}
        d = self._json(werte)
        self.assertEqual(d["criteria"]["multiplier"], 10)
        self.assertEqual(d["criteria"]["quantity"], 3)
        self.assertEqual(d["criteria"]["fees"], 1.5)
        self.assertEqual(d["criteria"]["budget"], 500.0)
        # ... und die Seite meldet sie nicht als unbekannt
        self.assertNotIn("Unbekannte Parameter", self._page(werte))



class TestSettableCriteria(unittest.TestCase):
    """Slice I: the eleven criteria that had no form field are settable now.

    Every test passes the field through the REQUEST. A test that built the
    criteria object directly would pass on the old code too, so it would pin
    nothing. The numbers are measured, not guessed
    (docs/debate/ausI_phase4_expectations.txt).
    """

    def _app(self, quotes=None):
        app = create_app(
            quote_adapter=_MockAdapter(make() if quotes is None else quotes),
            screener=None,
            now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _json(self, params, quotes=None):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params)
        return self._app(quotes).test_client().get(
            "/api/screen", query_string=query).get_json()

    def _page(self, params, quotes=None):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params)
        return self._app(quotes).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    @staticmethod
    def _gruende(d):
        return {g["reason"]: g["count"] for g in d["dropped_reasons"]}

    # --- the seven that change the SELECTION -------------------------------

    def test_min_bid_size_is_settable_and_moves_the_result(self):
        self.assertEqual(self._json({})["candidates"], 2)
        d = self._json({"min_bid_size": "200"})          # every fixture size is 100
        self.assertEqual(d["criteria"]["min_bid_size"], 200)
        self.assertEqual(d["candidates"], 0)
        self.assertEqual(self._gruende(d)["Größe unter Minimum"], 2)

    def test_min_ask_size_is_settable_and_moves_the_result(self):
        d = self._json({"min_ask_size": "200"})
        self.assertEqual(d["criteria"]["min_ask_size"], 200)
        self.assertEqual(d["candidates"], 0)
        self.assertEqual(self._gruende(d)["Größe unter Minimum"], 2)

    def test_the_penny_zone_and_the_volume_bound_are_named(self):
        """D9a/E6+E9 (a/b): the two field labels name the rule they enforce -
        the $ bound only bites in the penny zone, the volume bound is strict."""
        html_page = self._page({})
        self.assertIn("Spread max ($) · Standard 0.1 · greift nur bei Kursmitte "
                      "≤ 1 $ (Penny-Zone)", html_page)
        self.assertIn("Volumen min · Standard 0 · schließt Werte ≤ Grenze aus "
                      "(strikt größer)", html_page)
        # D11: the pair-width field sits DIRECTLY behind the volume field, and its
        # own text says what its empty state means (adjacent strikes only). Were it
        # anywhere else - or silent about "leer" - the reader would read the empty
        # field as "filter off", which is exactly the D1 trap.
        naechstes = re.search(
            r'<label>Volumen min[^<]*<input[^>]*name="min_volume"[^>]*></label>\s*'
            r'<label>Paar-Breite max \(\$\)[^<]*'
            r'<input[^>]*name="pair_max_width"', html_page)
        self.assertIsNotNone(
            naechstes,
            "das Paar-Breiten-Feld steht nicht direkt hinter dem Volumenfeld")

    def test_iv_min_drops_the_low_iv_contract(self):
        d = self._json({"iv_min": "29"})                 # IVs are 28 and 30
        self.assertEqual(d["criteria"]["iv_min"], 29.0)
        self.assertEqual(d["candidates"], 1)
        self.assertEqual([r["symbol"] for r in d["results"]], ["AAPL261016C00245000"])
        # two different IVs fall under the same bound; D9a/E7 shows ONE real
        # example (26, the contract beyond dte_max) instead of "IV # < #"
        self.assertEqual(self._gruende(d)["IV 26 < 29.0"], 2)

    def test_iv_max_drops_the_high_iv_contract(self):
        d = self._json({"iv_max": "29"})
        self.assertEqual(d["criteria"]["iv_max"], 29.0)
        self.assertEqual(d["candidates"], 1)
        self.assertEqual([r["symbol"] for r in d["results"]], ["AAPL261016C00240000"])
        self.assertEqual(self._gruende(d)["IV 30 > 29.0"], 2)

    def test_max_spread_abs_tightens_the_penny_zone(self):
        # the default bound is 0.10 and lets one penny contract through the
        # relative filter; at 0.01 the same filter catches three of them.
        # D9a/E7: their texts differ in the numbers, so the family collapses
        # into ONE line that shows a REAL example with the bound the request
        # set - no numbers-masked pattern any more (page and JSON read the
        # same list)
        d = self._json({"max_spread_abs": "0.01"})
        self.assertEqual(d["criteria"]["max_spread_abs"], 0.01)
        self.assertEqual(self._gruende(d)["absoluter Spread 0.02$ > 0.01$ (Mitte 0.31)"], 3)
        self.assertNotIn("absoluter Spread #$ > #$ (Mitte #)", self._gruende(d))
        # the other two members are the same ONE line, not a line of their own
        self.assertNotIn("absoluter Spread 0.15$ > 0.01$ (Mitte 0.88)", self._gruende(d))

    def test_require_oi_can_be_switched_off(self):
        # the OI-less contract only survives when the spread bounds are wide
        # enough - otherwise it dies in the penny filter first (measured)
        weit = {"max_spread_pct": "50", "max_spread_abs": "1"}
        an = self._json(weit)
        self.assertTrue(an["criteria"]["require_oi"])
        self.assertEqual(an["candidates"], 2)
        self.assertEqual(self._gruende(an)["OI fehlt"], 1)
        aus = self._json({**weit, "require_oi": "0"})
        self.assertFalse(aus["criteria"]["require_oi"])
        self.assertEqual(aus["candidates"], 3)
        self.assertIn("AAPL261016C00265000", [r["symbol"] for r in aus["results"]])
        self.assertNotIn("OI fehlt", self._gruende(aus))

    def test_require_volume_can_be_switched_on(self):
        # make() has no contract without volume, so this test brings its own
        kette = make() + [quote("AAPL261016C00270000", bid=1.00, ask=1.05, iv=27.0,
                                oi=3000, volume=None)]
        weit = {"max_spread_pct": "50", "max_spread_abs": "1"}
        aus = self._json(weit, quotes=kette)
        self.assertFalse(aus["criteria"]["require_volume"])
        self.assertEqual(aus["candidates"], 3)
        an = self._json({**weit, "require_volume": "1"}, quotes=kette)
        self.assertTrue(an["criteria"]["require_volume"])
        self.assertEqual(an["candidates"], 2)
        self.assertEqual(self._gruende(an)["Volumen fehlt"], 1)

    # --- the four that change the RANKING ---------------------------------

    def test_the_weights_change_the_score(self):
        d = self._json({"weight_spread": "1", "weight_oi": "0", "weight_iv": "0"})
        self.assertEqual(d["criteria"]["weight_spread"], 1.0)
        self.assertAlmostEqual(d["results"][0]["score"], 0.802, places=3)

    def test_a_zero_weight_sum_scores_zero_without_an_error(self):
        # measured: the score is normalised by the weight sum; sum 0 -> 0.0.
        # It is the documented answer, not a client error.
        d = self._json({"weight_spread": "0", "weight_oi": "0", "weight_iv": "0"})
        self.assertEqual([r["score"] for r in d["results"]], [0.0, 0.0])

    def test_iv_pref_turns_the_iv_note_around(self):
        band = {"iv_min": "20", "iv_max": "60"}
        hoch = self._json({**band, "iv_pref": "high"})
        tief = self._json({**band, "iv_pref": "low"})
        self.assertEqual(hoch["criteria"]["iv_pref"], "high")
        self.assertAlmostEqual(hoch["results"][0]["score"], 0.7209, places=3)
        self.assertAlmostEqual(tief["results"][0]["score"], 0.901, places=3)
        self.assertEqual(hoch["candidates"], tief["candidates"])   # selection identical

    # --- the guards that already existed, now reachable from the form -----

    def test_a_bad_switch_value_is_a_readable_400(self):
        res = self._app().test_client().get(
            "/api/screen", query_string={"ticker": "SPY", "expiration": "2026-10-16",
                                        "require_oi": "2"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("'0' oder '1'", res.get_json()["error"])

    def test_a_bad_choice_is_a_readable_400(self):
        res = self._app().test_client().get(
            "/api/screen", query_string={"ticker": "SPY", "expiration": "2026-10-16",
                                        "iv_pref": "bogus"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("iv_pref", res.get_json()["error"])

    def test_contradictory_iv_band_and_negative_weight_are_rejected(self):
        for params, text in (({"iv_min": "60", "iv_max": "20"},
                              "iv_min: muss ≤ iv_max sein (ist: 60.0 > 20.0)."),
                             ({"weight_spread": "-1"},
                              "weight_spread: muss ≥ 0 sein (ist: -1.0).")):
            res = self._app().test_client().get(
                "/api/screen", query_string={"ticker": "SPY", "expiration": "2026-10-16",
                                             **params})
            self.assertEqual(res.status_code, 400, params)
            self.assertIn(text, res.get_json()["error"], params)

    # --- the form itself and the "empty = default" rule --------------------

    def test_the_form_offers_every_criterion_and_names_no_unknown_parameter(self):
        html = self._page({})
        for feld in ("max_spread_abs", "pair_max_width", "min_bid_size", "min_ask_size",
                     "iv_min", "iv_max", "iv_pref", "require_oi", "require_volume",
                     "weight_spread", "weight_oi", "weight_iv"):
            self.assertIn(f'name="{feld}"', html, feld)
        self.assertNotIn("Unbekannte Parameter", html)
        # a real typo is still named (the old guard is untouched)
        self.assertIn("foo", self._page({"foo": "bar"}))

    def test_an_empty_field_keeps_the_default_instead_of_zero(self):
        # measured: min_bid_size=None means "off", 0 means "size >= 0" and drops
        # every quote whose size is 0 - so an empty field must not become 0
        d = self._json({"min_bid_size": "", "iv_min": "", "require_oi": ""})
        self.assertIsNone(d["criteria"]["min_bid_size"])
        self.assertIsNone(d["criteria"]["iv_min"])
        self.assertTrue(d["criteria"]["require_oi"])
        self.assertEqual(d["candidates"], 2)

    # --- D11 (review finding 4): the width decides the SEARCH, not the rules ---

    @staticmethod
    def _drei_strikes():
        """The reviewer's chain: 780 / 785 (OI 1) / 790, as real OCC symbols.

        SPY 2026-10-16, DTE 9 against the pinned clock. The middle strike is
        quoted like a good leg except for its OI - it fails the OI floor and so
        tears BOTH of its adjacent pairs with it (measured below: kept 0, the
        same drop reason twice). Deliberately separate from make(): that chain is
        pinned by the existing drop and display counts.
        """
        return [
            quote("SPY261016C00780000", bid=4.90, ask=5.00, iv=30.0, oi=1500, volume=300),
            quote("SPY261016C00785000", bid=3.00, ask=3.20, iv=30.0, oi=1, volume=300),
            quote("SPY261016C00790000", bid=1.50, ask=1.55, iv=30.0, oi=1500, volume=300),
        ]

    def test_the_illiquid_middle_strike_hides_the_wider_pair_until_the_width_is_set(self):
        """Review finding 4, reproduced through the whole call path.

        The chain alone yields NO spread: adjacency builds 780/785 and 785/790
        only, and the illiquid 785 falls out of both. The pair 780/790 - two good
        legs - is never formed. Only when the width is named does it appear, and
        then it runs through the same leg and pair checks as every other pair.
        Every number below is measured on this chain, not assumed.
        """
        kette = self._drei_strikes()
        eng = self._json({}, quotes=kette)
        self.assertIsNone(eng["criteria"]["pair_max_width"])     # default = fast mode
        self.assertEqual([v["symbol"] for v in eng["verticals"]], [])
        self.assertEqual(eng["vertical_drops"],
                         [{"reason": "strike 785.0 call: OI 1 < 500", "count": 2}])
        weit = self._json({"pair_max_width": "10.0"}, quotes=kette)
        self.assertEqual(weit["criteria"]["pair_max_width"], 10.0)
        self.assertEqual([v["symbol"] for v in weit["verticals"]],
                         ["SPY261016C00780000/SPY261016C00790000"])
        v = weit["verticals"][0]
        # debit = ask(780) - bid(790) = 5.00 - 1.50, width = 790 - 780,
        # profit = width - debit, break-even = 780 + debit
        self.assertEqual((v["long_symbol"], v["short_symbol"]),
                         ("SPY261016C00780000", "SPY261016C00790000"))
        self.assertEqual((v["net_debit"], v["strike_width"], v["max_profit"],
                          v["breakeven"]), (3.5, 10.0, 6.5, 783.5))
        # one dollar narrower than the gap and the adjacency is alone again
        knapp = self._json({"pair_max_width": "9.0"}, quotes=kette)
        self.assertEqual([v["symbol"] for v in knapp["verticals"]], [])
        self.assertEqual(knapp["criteria"]["pair_max_width"], 9.0)


class TestFutureTimestampLamp(unittest.TestCase):
    """Slice J: the lamp must not call a chain with a future timestamp fresh.

    Measured before the fix: with every timestamp one day ahead the page read
    "Datenalter: frisch (-1440 min, Grenze 30 min)" and the JSON said green.

    The lamp reads the RAW chain, so it is the backstop when the age guard is
    switched off - the assertion that carries the slice therefore runs with
    the guard OFF: with the guard on, the filter would drop the contract and
    mask the lamp.
    """

    def _app(self, quotes=None):
        app = create_app(quote_adapter=_MockAdapter(make() if quotes is None else quotes),
                         screener=None, now_fn=_clock())
        app.config["TESTING"] = True
        return app

    @staticmethod
    def _kette(ts):
        k = make()
        for q in k:
            q.quote_timestamp = ts
        return k

    def _page(self, quotes, params=None):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    def _json(self, quotes, params=None):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get(
            "/api/screen", query_string=query).get_json()

    def test_the_lamp_turns_red_with_the_guard_switched_off(self):
        page = self._page(self._kette("2026-10-08 14:35:00"), {"ignore_quote_age": "1"})
        self.assertIn("Zukunft", page)          # not "frisch", not "alt"
        self.assertNotIn("frisch", page)
        self.assertIn("-1440 min", page)        # the number stays honest
        self.assertIn("Grenze aus", page)
        self.assertEqual(self._json(self._kette("2026-10-08 14:35:00"),
                                    {"ignore_quote_age": "1"})["freshness"], "red")

    def test_the_filter_drops_a_future_timestamp_and_names_it(self):
        d = self._json(self._kette("2026-10-08 14:35:00"))
        self.assertEqual(d["candidates"], 0)
        self.assertIn("Zeitstempel in der Zukunft (+1440 Min.)",
                      [g["reason"] for g in d["dropped_reasons"]])

    def test_one_contract_of_seven_ahead_leaves_the_other_six_alone(self):
        k = make()
        k[0].quote_timestamp = "2026-10-08 14:35:00"     # one of the two winners
        d = self._json(k)
        self.assertEqual(d["candidates"], 1)
        self.assertEqual(d["freshness"], "red")          # raw chain: it is the youngest
        self.assertIn("Zeitstempel in der Zukunft (+1440 Min.)",
                      [g["reason"] for g in d["dropped_reasons"]])

    def test_half_a_minute_ahead_is_still_fresh(self):
        # separates the SHARED tolerance from a strict lamp: the filter keeps
        # this quote, so the lamp must not turn red for it
        page = self._page(self._kette("2026-10-07 14:35:30"))
        self.assertIn("frisch", page)
        self.assertNotIn("Zukunft", page)
        self.assertEqual(self._json(self._kette("2026-10-07 14:35:30"))["freshness"], "green")


class TestResultList(unittest.TestCase):
    """Ausbau-Slice 3 (D): what the result list shows, and in which order.

    The row order is produced by the SERVER (the template no longer sorts), so
    it is observable in the test client. Ties keep the historical order: a
    stable ascending sort, then reversed - of two equal scores the later
    contract comes first.
    """

    def _app(self, quotes=None, criteria=None):
        app = create_app(
            quote_adapter=_MockAdapter(make() if quotes is None else quotes),
            screener=None if criteria is None else OptionScreener(criteria),
            now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _page(self, params, quotes=None, criteria=None):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params)
        return self._app(quotes, criteria).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    @staticmethod
    def _stats(page):
        m = re.search(r'<div class="stats">(.*?)</div>', page, re.S)
        return " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split())

    @staticmethod
    def _symbols(page):
        # first cell of every row (all 9 cells of a row match <td> too - the
        # first extraction attempt collected all of them and proved nothing)
        body = page.split("<tbody>")[-1].split("</tbody>")[0]
        rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
        return [re.findall(r'<td(?: title="[^"]*")?>(.*?)</td>', row, re.S)[0].strip()
                for row in rows]

    # --- what the list shows ---

    def test_the_contract_cell_carries_the_score_reasons(self):
        self.assertIn('title="spread 2.0%; OI 8000; IV 28"', self._page({}))

    def test_the_reasons_come_from_the_contract(self):
        # a chain with different numbers: a fixed text in the template cannot
        # satisfy this one
        chain = [quote("AAPL261016C00240000", bid=1.00, ask=1.10, iv=21.0,
                       oi=4242, volume=7)]
        self.assertIn('title="spread 9.5%; OI 4242; IV 21"', self._page({}, chain))

    def test_the_delta_column_states_its_unit(self):
        page = self._page({})
        self.assertIn("<th>Δ×100</th>", page)
        self.assertNotIn("<th>Δ</th>", page)

    def test_the_header_names_the_dte_basis(self):
        # D8: the single "DTE 38 (Kalendertage)" line read like the DATA's own
        # maturity. The honest maturity is expiry minus the SCAN clock
        # (2026-10-16 - 2026-10-07 = 9), and the label now says so.
        stats = self._stats(self._page({}))
        self.assertIn("Verfall 2026-10-16", stats)
        self.assertIn("DTE 9 (Tage ab heute)", stats)
        self.assertNotIn("(Kalendertage)", stats)

    def test_the_header_names_the_feed_stamp_as_well(self):
        # D8 (E1 = c) requires the header to name BOTH numbers: the scan's
        # maturity AND the data's own stamp. The template asks for the second
        # one (screen.html:170 `{% if feed_time %}<span>Kursdatum ...`), but the
        # delivered server computes feed_time at server.py:407 and never hands
        # it to render_template (screen.html never receives it, ruff F841 on
        # exactly that line). RED ON PURPOSE - this is a REAL defect of the D8
        # artifact, not a fixture contradiction, so it is named rather than
        # papered over. It goes green the moment render_template gets
        # `feed_time=feed_time`.
        stats = self._stats(self._page({}))
        self.assertIn("Kursdatum 2026-10-07 14:30:00", stats)

    def test_the_header_dte_follows_the_contracts(self):
        # a chain on its OWN expiry: 2026-10-30 - 2026-10-07 = 23. A hardcoded
        # "DTE 9" in the template cannot satisfy this one.
        chain = [quote("AAPL261030C00240000", bid=5.00, ask=5.10, iv=28.0,
                       oi=8000, volume=1200)]
        self.assertIn("DTE 23 (Tage ab heute)", self._stats(self._page({}, chain)))

    def test_the_header_shows_no_dte_without_results(self):
        stats = self._stats(self._page({"min_oi": "999999"}))
        self.assertIn("Verfall 2026-10-16", stats)
        self.assertNotIn("DTE", stats)
        self.assertIn("Kandidaten: 0", stats)

    # --- in which order (pin: green before and after) ---

    def test_the_rows_are_ordered_by_score_and_ties_are_kept(self):
        tie_a = quote("AAPL261016C00240000", bid=2.40, ask=2.50, iv=30.0,
                      oi=1500, volume=300)
        best = quote("AAPL261016C00245000", bid=5.00, ask=5.10, iv=28.0,
                      oi=8000, volume=1200)
        tie_b = quote("AAPL261016C00261000", bid=2.40, ask=2.50, iv=30.0,
                      oi=1500, volume=300)
        page = self._page({}, [tie_a, best, tie_b])
        self.assertEqual(self._symbols(page),
                         ["AAPL261016C00245000", "AAPL261016C00261000",
                          "AAPL261016C00240000"])



class TestNonFiniteJsonBody(unittest.TestCase):
    """D8 (E2 = a): the body of /api/screen is STRICT JSON.

    Flask's encoder writes float('inf') as the bare token ``Infinity`` and a
    NaN as ``NaN``. Neither is valid JSON (RFC 8259), so one non-finite price
    made the WHOLE response unreadable for a strict consumer (the Phase 6
    bot). Measured before D8: the ``ask=inf`` contract passed the spread
    filter - mid became inf, the relative spread NaN, and ``NaN > bound`` is
    False - so it stood in ``results`` with an Infinity ask.
    """

    DATE = "2026-10-16"

    def _body(self):
        chain = [
            quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0,
                  oi=8000, volume=1200),                    # clean candidate
            quote("AAPL261016C00245000", bid=2.40, ask=float("inf"), iv=30.0,
                  oi=1500, volume=300),                     # ask = +inf
            quote("AAPL261016C00260000", bid=float("nan"), ask=float("inf"),
                  iv=30.0, oi=1500, volume=300),            # bid NaN, ask inf
        ]
        app = create_app(quote_adapter=_MockAdapter(chain), screener=None,
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app.test_client().get(
            "/api/screen",
            query_string={"ticker": "AAPL", "expiration": self.DATE}
        ).get_data(as_text=True)

    def test_the_body_carries_no_non_finite_token(self):
        body = self._body()
        self.assertNotIn("Infinity", body)
        self.assertNotIn("-Infinity", body)
        self.assertNotIn("NaN", body)

    def test_the_body_parses_with_a_strict_parser_and_drops_them(self):
        # json.loads calls parse_constant ONLY for the three literals
        # Infinity/-Infinity/NaN; the raising lambda makes a leak a failure
        def _reject(token):
            raise ValueError(f"non-finite JSON token: {token}")

        data = json.loads(self._body(), parse_constant=_reject)
        symbols = [r["symbol"] for r in data["results"]]
        self.assertNotIn("AAPL261016C00245000", symbols)
        self.assertNotIn("AAPL261016C00260000", symbols)
        self.assertIn("AAPL261016C00240000", symbols)
        self.assertEqual(data["candidates"], 1)
        self.assertEqual(data["chain_size"], 3)
        # the two non-finite contracts are named as rejected, not silently gone
        self.assertEqual(data["dropped_reasons"],
                         [{"reason": "Spread: kein Geld-/Briefkurs", "count": 2}])


class TestVerticalSpreadList(unittest.TestCase):
    """Ausbau-Slice B: the second list (vertical call spreads).

    The chain is the RAW one - the single-option filters of the first table
    never feed it. The mock adapter ignores the ticker, so one call covers
    every underlying of the fixture.
    """

    def _app(self, quotes=None, criteria=None):
        adapter = _MockAdapter(make_verticals() if quotes is None else quotes)
        # the fixture lives on its own date; /api/screen only accepts a date
        # the chain really lists
        adapter.expirations = ["2026-10-16"]
        app = create_app(
            quote_adapter=adapter,
            screener=OptionScreener(criteria or _WIDE),
            now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _json(self, quotes=None, criteria=None, params=None):
        query = {"ticker": "AAPL", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes, criteria).test_client().get(
            "/api/screen", query_string=query).get_json()

    def _page(self, quotes=None, criteria=None, params=None):
        query = {"ticker": "AAPL", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes, criteria).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    def test_the_api_lists_the_pairs_that_pass_the_leg_rules(self):
        # D7: the degenerated AAPL pair (debit == width) is dropped by the pair
        # rule; AMZN and TSLA survive the leg chain under the wide criteria.
        data = self._json()
        self.assertEqual([v["symbol"] for v in data["verticals"]],
                         ["AMZN261016C00260000/AMZN261016C00262500",
                          "TSLA261016C00290000/TSLA261016C00292500"])

    def test_a_pair_carries_the_four_numbers_and_the_ratio(self):
        v = self._json()["verticals"][0]          # AMZN 260/262.5
        self.assertEqual(v["long_symbol"], "AMZN261016C00260000")
        self.assertEqual(v["short_symbol"], "AMZN261016C00262500")
        self.assertEqual(v["expiration"], "2026-10-16")
        self.assertEqual(v["strike_width"], 2.5)
        self.assertEqual(v["net_debit"], 0.7)     # 1.20 - 0.50
        self.assertEqual(v["max_loss"], 0.7)
        self.assertEqual(v["max_profit"], 1.8)    # 2.5 - 0.7
        self.assertEqual(v["breakeven"], 260.7)   # 260.0 + 0.7
        self.assertEqual(v["debit_width_ratio"], 0.28)
        self.assertIs(v["debit_ge_width"], False)
        # D12: the pair now also carries the POSITION it would be. The report
        # names its currency instead of leaving the unit open (USD, not
        # converted), and the position follows from the size of the request's
        # criteria: the standard 100 x 1 x 0.7 + 0.0 fees = 70.0 USD.
        self.assertEqual(v["currency"], "USD")
        self.assertEqual((v["multiplier"], v["quantity"], v["fees"]), (100, 1, 0.0))
        self.assertIsNone(v["budget"])
        self.assertEqual(v["position_debit"], 70.0)

    def test_a_pair_without_profit_is_dropped_with_a_reason(self):
        """D7 (NEU 1 of the review): no pair with debit >= width is a result.

        The degenerated AAPL pair (debit 2.50 == width 2.50, max profit 0) used
        to stand in the list AS A RESULT, with a break-even outside the payoff
        range. Now the pair rule drops it and names the reason.
        """
        data = self._json()
        self.assertNotIn("AAPL261016C00250000/AAPL261016C00252500",
                         [v["symbol"] for v in data["verticals"]])
        self.assertEqual([v["symbol"] for v in data["verticals"]
                          if v["debit_ge_width"]], [])
        self.assertEqual([(g["count"], g["reason"]) for g in data["vertical_drops"]
                          if "kein Gewinn" in g["reason"]],
                         [(1, "Debit 2.5 außerhalb (0, 2.5) - kein Gewinn möglich")])
        self.assertIn("kein Gewinn möglich", self._page())

    def test_both_lists_are_rendered_side_by_side_on_one_page(self):
        # the spread table is additive: with loosened single-option criteria
        # BOTH tables have rows, and the page stays one document. D7: loosened
        # now includes the absolute spread bound - the fixture legs are 0.20 $
        # wide in the penny zone, and the pairs must survive for the table to
        # exist at all.
        criteria = ScreenerCriteria(max_quote_age_min=None, max_spread_pct=None,
                                    max_spread_abs=None, min_oi=None,
                                    dte_min=None, dte_max=None)
        page = self._page(criteria=criteria)
        self.assertEqual(page.count("<table>"), 2)
        self.assertIn("Vertikale Call-Spreads", page)
        self.assertIn("Kontrakt", page)

    def test_the_api_reports_why_pairs_were_dropped(self):
        # D7: every dropped pair carries the reason of the first rule that
        # rejects it - the pair rule for the degenerated AAPL pair, the leg
        # chain for MSFT (one-sided) and NVDA (short leg without OI).
        self.assertEqual(self._json()["vertical_drops"],
                         [{"reason": "Debit 2.5 außerhalb (0, 2.5) - kein Gewinn möglich",
                           "count": 1},
                          {"reason": "strike 270.0 call: Spread: kein Geld-/Briefkurs",
                           "count": 1},
                          {"reason": "strike 282.5 call: OI fehlt", "count": 1}])

    def test_a_put_pair_never_reaches_the_list(self):
        data = self._json()
        self.assertNotIn("P00", "".join(v["symbol"] for v in data["verticals"]))
        self.assertNotIn("P00", "".join(g["reason"] for g in data["vertical_drops"]))

    def test_the_page_shows_the_numbers_the_json_carries(self):
        page = self._page()
        for v in self._json()["verticals"]:
            for field in ("strike_width", "net_debit", "max_loss",
                          "max_profit", "breakeven"):
                self.assertIn(f"{v[field]:.2f}", page)
            self.assertIn(f"{v['debit_width_ratio']:.2f}", page)

    def test_the_first_table_is_untouched(self):
        criteria = ScreenerCriteria(max_quote_age_min=None)
        app = create_app(quote_adapter=_MockAdapter(make()),
                         screener=OptionScreener(criteria), now_fn=_clock())
        app.config["TESTING"] = True
        page = app.test_client().get(
            "/", query_string={"ticker": "SPY", "expiration": "2026-10-16"}
        ).get_data(as_text=True)
        # the single-option rows are still the FIRST tbody section, and the
        # new table must not introduce a second one
        body = page.split("<tbody>")[-1].split("</tbody>")[0]
        symbols = [re.findall(r'<td(?: title="[^"]*")?>(.*?)</td>', row, re.S)[0].strip()
                   for row in re.findall(r"<tr>(.*?)</tr>", body, re.S)]
        self.assertEqual(symbols, ["AAPL261016C00240000", "AAPL261016C00245000"])
        self.assertEqual(page.count("<tbody>"), 1)
        self.assertIn("<th>\u0394\u00d7100</th>", page)
        self.assertNotIn("<th>\u0394</th>", page)

    def test_the_feed_note_is_german_and_no_python_value_reaches_the_page(self):
        """Feed-Zeile deutsch, kein Python-Wert, Tooltips bleiben lesbar.

        Die Feed-Zeile war von keiner Zusicherung gepinnt (gemessen) - sie
        nennt als einzige den Datenursprung, also gehoert sie festgehalten.
        "None" war ein ausgedruckter Python-Wert, kein Wort. Die Hinweise
        stehen als Tooltip am Symbol und duerfen nicht unbrauchbar lang
        werden (Basisstand gemessen: laengster Tooltip 51 Zeichen).
        """
        client = self._app().test_client()
        html_page = client.get("/?ticker=AAPL&expiration=2026-10-16").get_data(as_text=True)
        self.assertIn("CBOE-Kursdaten zeitversetzt", html_page)
        self.assertIn("ca. 15 Minuten hinter Echtzeit", html_page)
        self.assertNotIn("delayed feed", html_page)
        self.assertNotIn("None", html_page)
        self.assertIn("OI fehlt", html_page)
        tooltips = re.findall(r'title="([^"]*)"', html_page)
        self.assertTrue(tooltips, "keine Tooltips mehr auf der Seite")
        self.assertLessEqual(max(len(t) for t in tooltips), 64)


    def test_a_chain_without_neighbours_reports_two_empty_lists(self):
        data = self._json(quotes=[quote("AAPL261016C00240000", bid=5.00, ask=5.10,
                                       iv=28.0, oi=8000, volume=1200)])
        self.assertEqual(data["verticals"], [])
        self.assertEqual(data["vertical_drops"], [])


_ENV_KEYS = ("PAPERBROKER_TREND_MCP_URL", "PAPERBROKER_TREND_TOKEN",
             "PAPERBROKER_TREND_TIMEOUT_S")


def _mcp_envelope(stage, symbol="AAPL"):
    """The MEASURED shape (2026-10-12): result.content[0].text is a JSON string
    whose numbers live under "technicals", not at the top level."""
    inner = json.dumps({"stock": {"symbol": symbol},
                        "technicals": {"symbol": symbol, "stage": stage}})
    return json.dumps({"jsonrpc": "2.0", "id": 1,
                       "result": {"content": [{"type": "text", "text": inner}]}}).encode()


class _FakeTransport:
    """Records every call; never touches the network."""

    def __init__(self, status=200, body=b""):
        self.status = status
        self.body = body
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return self.status, self.body


class _FakeProvider:
    def __init__(self, signal):
        self.signal = signal

    def get_trend(self, ticker):
        return self.signal


class _BrokenProvider:
    def get_trend(self, ticker):
        raise RuntimeError("boom")


class TestTrendDirectionRule(unittest.TestCase):
    def test_direction_two_is_up(self):
        self.assertEqual(server_module._direction_from_stage(2), "up")

    def test_direction_four_is_down(self):
        self.assertEqual(server_module._direction_from_stage(4), "down")

    def test_direction_one_is_flat(self):
        self.assertEqual(server_module._direction_from_stage(1), "flat")

    def test_direction_three_is_flat(self):
        # the only catcher for a "3 -> up" mutant
        self.assertEqual(server_module._direction_from_stage(3), "flat")

    def test_direction_missing_or_string_stage_raises(self):
        # no subTest here: a subtest failure is not catchable by xfail(strict)
        for bad in (None, 5, "2"):
            with self.assertRaises(ValueError):
                server_module._direction_from_stage(bad)


class TestTrendTransport(unittest.TestCase):
    def test_transport_returns_status_and_body(self):
        import io
        from unittest import mock

        class _Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(server_module.urllib.request, "urlopen",
                               return_value=_Resp(b'{"ok":1}')):
            status, body = server_module._urllib_transport("http://x/mcp/", {}, b"{}", 2.0)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":1}')

    def test_transport_http_error_carries_status_and_body(self):
        import io
        import urllib.error
        from unittest import mock

        err = urllib.error.HTTPError("http://x/mcp/", 401, "no", {}, io.BytesIO(b'{"detail":"auth"}'))
        with mock.patch.object(server_module.urllib.request, "urlopen", side_effect=err):
            status, body = server_module._urllib_transport("http://x/mcp/", {}, b"{}", 2.0)
        self.assertEqual(status, 401)
        self.assertEqual(body, b'{"detail":"auth"}')

    def test_transport_posts_with_headers_and_timeout(self):
        import io
        from unittest import mock

        seen = {}

        class _Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _fake_urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            seen["auth"] = req.get_header("Authorization")
            seen["ctype"] = req.get_header("Content-type")
            seen["body"] = req.data
            return _Resp(b"{}")

        with mock.patch.object(server_module.urllib.request, "urlopen", side_effect=_fake_urlopen):
            server_module._urllib_transport("http://x/mcp/", {"Authorization": "Bearer T",
                                                              "Content-Type": "application/json"},
                                            b'{"a":1}', 2.0)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], "http://x/mcp/")
        self.assertEqual(seen["timeout"], 2.0)
        self.assertEqual(seen["auth"], "Bearer T")
        self.assertEqual(seen["ctype"], "application/json")
        self.assertEqual(seen["body"], b'{"a":1}')


class TestTrendProvider(unittest.TestCase):
    def _provider(self, status=200, body=b""):
        fake = _FakeTransport(status, body)
        return server_module._MCPTrendProvider("http://x/mcp", "T", 2.0, transport=fake), fake

    def test_provider_url_is_normalised_to_mcp_slash(self):
        for raw in ("http://x/mcp", "http://x/mcp/"):
            prov = server_module._MCPTrendProvider(raw, "T", 2.0, transport=_FakeTransport())
            self.assertEqual(prov.url, "http://x/mcp/")

    def test_provider_sends_bearer_and_tool_call(self):
        prov, fake = self._provider(200, _mcp_envelope(2))
        prov.get_trend("AAPL")
        call = fake.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer T")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        sent = json.loads(call["body"])
        self.assertEqual(sent["method"], "tools/call")
        self.assertEqual(sent["params"]["name"], "stock_snapshot")
        self.assertEqual(sent["params"]["arguments"], {"symbol": "AAPL"})
        self.assertEqual(call["timeout"], 2.0)

    def test_provider_extracts_direction_from_stage(self):
        prov, _ = self._provider(200, _mcp_envelope(2))
        sig = prov.get_trend("AAPL")
        self.assertEqual(sig.direction, "up")
        self.assertEqual(sig.symbol, "AAPL")
        prov, _ = self._provider(200, _mcp_envelope(4, "MSFT"))
        sig = prov.get_trend("MSFT")
        self.assertEqual(sig.direction, "down")
        # the echo is the contract: the screener matches trend.symbol against the
        # underlying, so a constant would silently label the wrong ticker
        self.assertEqual(sig.symbol, "MSFT")

    def test_provider_raises_on_every_failure(self):
        cases = [
            ("status 401", 401, b'{"detail":"Authentication required"}'),
            ("empty body", 200, b""),
            ("html instead of json", 200, b"<html>nope</html>"),
            ("rpc error", 200, json.dumps({"error": {"code": -32600}}).encode()),
            ("rpc error NEXT TO a valid payload", 200, json.dumps(
                {"error": {"code": -32600}, "result": {"content": [
                    {"type": "text", "text": json.dumps({"technicals": {"stage": 2}})}]}}).encode()),
            ("401 with a VALID mcp body", 401, _mcp_envelope(2)),
            ("isError", 200, json.dumps({"result": {"isError": True, "content": []}}).encode()),
            ("stage missing", 200, json.dumps(
                {"result": {"content": [{"text": json.dumps({"symbol": "AAPL"})}]}}).encode()),
            ("stage string", 200, _mcp_envelope("2")),
        ]
        for name, status, body in cases:
            prov, _ = self._provider(status, body)
            with self.assertRaises((RuntimeError, ValueError), msg=name):
                prov.get_trend("AAPL")

    def test_provider_token_never_in_exception(self):
        secret = "SUPER-SECRET-TOKEN"
        prov = server_module._MCPTrendProvider("http://x/mcp", secret, 2.0,
                                               transport=_FakeTransport(401, b"nope"))
        try:
            prov.get_trend("AAPL")
        except Exception as exc:  # noqa: BLE001 - the text is the assertion
            self.assertNotIn(secret, str(exc))
            self.assertNotIn(secret, repr(exc))
        else:
            self.fail("kein Fehler geworfen")

    def test_provider_hostile_symbol_does_not_corrupt_body(self):
        prov, fake = self._provider(200, _mcp_envelope(2))
        prov.get_trend('AA"PL{')
        sent = json.loads(fake.calls[0]["body"])
        self.assertEqual(sent["params"]["arguments"]["symbol"], 'AA"PL{')


    def test_provider_reads_stage_from_technicals(self):
        """MEASURED 2026-10-12: the numbers live under "technicals"."""
        prov, _ = self._provider(200, _mcp_envelope(2))
        self.assertEqual(prov.get_trend("AAPL").direction, "up")
        # a stage at the TOP level is not the measured place and must not pass
        prov, _ = self._provider(200, json.dumps({"result": {"content": [
            {"type": "text", "text": json.dumps({"stage": 2})}]}}).encode())
        with self.assertRaises(RuntimeError):
            prov.get_trend("AAPL")


class TestTrendConfig(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in _ENV_KEYS}

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_gate_is_none_without_config(self):
        self.assertIsNone(server_module._get_trend_provider())

    def test_gate_is_none_on_partial_config(self):
        os.environ["PAPERBROKER_TREND_MCP_URL"] = "http://x/mcp"
        self.assertIsNone(server_module._get_trend_provider())
        os.environ.pop("PAPERBROKER_TREND_MCP_URL")
        os.environ["PAPERBROKER_TREND_TOKEN"] = "T"
        self.assertIsNone(server_module._get_trend_provider())
        os.environ["PAPERBROKER_TREND_MCP_URL"] = ""
        self.assertIsNone(server_module._get_trend_provider())

    def test_gate_builds_provider_with_full_config(self):
        os.environ["PAPERBROKER_TREND_MCP_URL"] = "http://x/mcp"
        os.environ["PAPERBROKER_TREND_TOKEN"] = "T"
        prov = server_module._get_trend_provider()
        self.assertIsNotNone(prov)
        self.assertEqual(prov.url, "http://x/mcp/")
        self.assertEqual(prov.token, "T")
        self.assertEqual(prov.timeout, 2.0)

    def test_gate_timeout_default_and_override(self):
        os.environ["PAPERBROKER_TREND_MCP_URL"] = "http://x/mcp"
        os.environ["PAPERBROKER_TREND_TOKEN"] = "T"
        os.environ["PAPERBROKER_TREND_TIMEOUT_S"] = "7.5"
        self.assertEqual(server_module._get_trend_provider().timeout, 7.5)
        for bad in ("", "abc", "0", "-1"):
            os.environ["PAPERBROKER_TREND_TIMEOUT_S"] = bad
            self.assertEqual(server_module._get_trend_provider().timeout, 2.0, bad)


class TestTrendWiring(unittest.TestCase):
    def _client(self):
        return create_app(quote_adapter=_MockAdapter(make()), screener=OptionScreener(_LOOSE), now_fn=_clock()).test_client()

    def test_page_without_provider_shows_no_trend_badge(self):
        html_out = self._client().get("/?ticker=AAPL&expiration=2026-10-16").get_data(as_text=True)
        self.assertNotIn("Trend:", html_out)

    def test_json_reports_the_unavailable_source_not_a_calm_market(self):
        import unittest.mock as mock
        with mock.patch.object(server_module, "_get_trend_provider", return_value=_BrokenProvider()):
            client = self._client()
            antwort = client.get("/api/screen?ticker=AAPL&expiration=2026-10-16")
            page = client.get("/?ticker=AAPL&expiration=2026-10-16").get_data(as_text=True)
        # D6: ein Ausfall der Trend-Quelle kostet die Trend-Anmerkung, nie das
        # Ergebnis. Vor D6 antwortete dieser Aufruf mit HTML/500 - und die alte
        # Zusicherung war trotzdem grün, weil get_json() für eine HTML-Seite
        # None liefert. Genau daran ist der Fehler durchgerutscht.
        self.assertEqual(200, antwort.status_code,
                         "unterbrochene Trend-Quelle darf keine 500 sein")
        self.assertEqual("application/json",
                         antwort.headers["Content-Type"].split(";")[0])
        data = antwort.get_json()
        self.assertIn("trend", data, "der Schluessel fehlt ganz")
        self.assertIsNone(data["trend"], "unterbrochene Quelle muss null melden")
        self.assertTrue(data["results"],
                        "kein Ergebnis, obwohl ohne Trend weiter gescreent wird")
        self.assertIn("Trend nicht verfügbar", page)
        self.assertIn("nicht erreichbar", page,
                      "die Seite muss die Ursache nennen")
        self.assertNotIn("Marktdaten-Fehler", page,
                         "ein Trend-Ausfall ist kein Marktdaten-Fehler")

    def test_json_and_page_agree_on_trend_reasons(self):
        import unittest.mock as mock
        signal = TrendSignal(symbol="AAPL", direction="up")
        with mock.patch.object(server_module, "_get_trend_provider",
                               return_value=_FakeProvider(signal)):
            client = self._client()
            data = client.get("/api/screen?ticker=AAPL&expiration=2026-10-16").get_json()
            page = client.get("/?ticker=AAPL&expiration=2026-10-16").get_data(as_text=True)
        json_reasons = [r for row in data["results"] for r in row["reasons"] if "Trend" in r]
        self.assertTrue(json_reasons, "kein Trend-Grund im JSON")
        for reason in json_reasons:
            self.assertIn(reason, page, "Seite und JSON gehen auseinander")
        self.assertIn("steigend", page)
        # das gesunde Signal muss auch in der Schnittstelle stehen,
        # sonst faellt nicht auf, wenn sie es verschweigt
        self.assertIn("trend", data)
        self.assertEqual("up", data["trend"]["direction"])
        self.assertEqual("AAPL", data["trend"]["symbol"])




class TestChainBoundary(unittest.TestCase):
    """Ausbau-Slice E (B-16): one unusable row must not take the screen down."""

    BAD = "NVDA260229C00115000"
    GOOD = "NVDA261016C00115000"

    def _chain(self, rows):
        return {"timestamp": "2026-10-12 09:28:47", "current_price": 229.4,
                "options": rows}

    def _row(self, symbol):
        return {"option": symbol, "bid": 5.0, "ask": 5.2, "last_trade_price": 5.1,
                "delta": 50.0, "iv": 40.0, "gamma": 1.0, "vega": 10.0,
                "theta": -5.0, "rho": 2.0, "open_interest": 100, "volume": 10}

    def _screen(self, rows, query):
        from paperbroker.adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter
        ad = CBOEQuoteAdapter(now_fn=_clock())
        ad._fetch = lambda symbol: self._chain(rows)
        app = server_module.create_app(quote_adapter=ad, screener=OptionScreener(_LOOSE), now_fn=_clock())
        app.config["PROPAGATE_EXCEPTIONS"] = False
        return app.test_client().get(query)

    def test_screen_survives_one_unusable_row(self):
        rows = [self._row(self.BAD), self._row(self.GOOD)]
        res = self._screen(rows, "/api/screen?ticker=NVDA&expiration=2026-10-16")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["chain_size"], 1)
        # MEASURED: with an expiration the unusable row is already dropped by the
        # date filter, so nothing reaches the per-contract boundary and the
        # counter stays 0 - the key must still be present for a consumer. The
        # counter itself is pinned at the adapter level (no date filter there).
        self.assertIn("chain_skipped", body)
        self.assertEqual(body["chain_skipped"], 0)

    def test_the_skip_count_reaches_the_screen(self):
        """Pins the echo itself: a chain that skipped rows must say how many.

        Measured blind spot: with a constant 0 the screen kept answering 200
        and the mutant survived, so the count is pinned with a stub adapter.
        """
        from paperbroker.adapters.quotes.CBOEQuoteAdapter import ChainResult
        from paperbroker.assets import asset_factory
        from paperbroker.quotes import OptionQuote

        class _Stub:
            def get_expiration_dates(self, underlying_asset=None):
                return ["2026-10-16"]

            def get_options(self, underlying_asset=None, expiration_date=None):
                asset = asset_factory(self._good)
                return ChainResult([OptionQuote("2026-10-12 09:28:47", asset,
                                                bid=5.0, ask=5.2)], skipped=2)

            _good = "NVDA261016C00115000"

        app = server_module.create_app(quote_adapter=_Stub(), screener=OptionScreener(_LOOSE), now_fn=_clock())
        app.config["PROPAGATE_EXCEPTIONS"] = False
        body = app.test_client().get(
            "/api/screen?ticker=NVDA&expiration=2026-10-16").get_json()
        self.assertEqual(body["chain_skipped"], 2)
        self.assertEqual(body["chain_size"], 1)

    def test_picker_survives_one_unusable_row(self):
        """The picker died before a date was even chosen (measured)."""
        rows = [self._row(self.BAD), self._row(self.GOOD)]
        res = self._screen(rows, "/api/screen?ticker=NVDA")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("2026-02-29", res.get_json()["listed_dates"])



class TheSpreadListIsReadyToDecide(unittest.TestCase):
    """Slice K: Rendite, Break-even-Abstand, Strikes im Klartext, Sortierung.

    Die Zahlen sind reine Arithmetik aus dem Spread-Objekt; die Reihenfolge der
    ANZEIGE darf von der Reihenfolge der Schnittstelle abweichen (dort haengen
    die Zusicherungen der bestehenden Tests).
    """

    @staticmethod
    def _app(quotes=None, criteria=None):
        adapter = _MockAdapter(make_verticals() if quotes is None else quotes)
        # die Fixture liegt auf ihrem eigenen Verfall; /api/screen und die Seite
        # nehmen nur ein Datum an, das die Kette wirklich listet
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(criteria or _WIDE),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _page(self, params=None, quotes=None):
        query = {"ticker": "AAPL", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get("/", query_string=query).get_data(as_text=True)

    def _json(self, quotes=None):
        return self._app(quotes).test_client().get(
            "/api/screen", query_string={"ticker": "AAPL", "expiration": "2026-10-16"}).get_json()

    @staticmethod
    def _spread_rows(page):
        """Nur die Spread-Tabelle - am KOPF erkannt, nicht an der Position.

        Seit D2 stehen die Spreads VOR den Kontrakten; ein [-1] wie zuvor
        griffe jetzt die Kontrakttabelle. Fehlt die Tabelle ganz, wird das
        benannt - ein leeres [] waere still und wuerde die Zusicherungen, die
        ueber die Zeilen laufen, zu Leerproben machen.
        """
        tabellen = [t for t in re.findall(r"<table>(.*?)</table>", page, re.S)
                    if "<th>Spread</th>" in t]
        if not tabellen:
            raise AssertionError("keine Spread-Tabelle auf der Seite")
        zeilen = re.findall(r"<tr>(.*?)</tr>", tabellen[0], re.S)
        return [[re.sub(r"<[^>]+>", "", c).strip() for c in
                 re.findall(r'<td(?: title="[^"]*")?>(.*?)</td>', zeile, re.S)]
                for zeile in zeilen[1:]]

    @staticmethod
    def _spread_header(page):
        """Die Kopfzellen der Spread-Tabelle in Dokumentreihenfolge (D13: 12).

        Am <thead> gelesen, nicht gezaehlt: nur so lässt sich die neue Spalte an
        ihrer STELLE pruefen (zwischen Breite und max Verlust) statt nur als
        Zahl. Ein Link im <th> (Rendite, BE-Abstand) wird auf seinen Text
        reduziert.
        """
        tabellen = [t for t in re.findall(r"<table>(.*?)</table>", page, re.S)
                    if "<th>Spread</th>" in t]
        if not tabellen:
            raise AssertionError("keine Spread-Tabelle auf der Seite")
        kopf = tabellen[0].split("<thead><tr>")[1].split("</tr></thead>")[0]
        return [re.sub(r"<[^>]+>", "", th).strip()
                for th in re.findall(r"<th[^>]*>(.*?)</th>", kopf, re.S)]

    # --- die neuen Spalten tragen gerechnete Zahlen ---

    def test_the_page_shows_the_return_of_every_spread(self):
        zeilen = self._spread_rows(self._page())
        # D7: the degenerated AAPL pair (debit == width, return 0.00 %) is no
        # longer a result - it is dropped with the pair rule's reason.
        self.assertEqual([z[0] for z in zeilen],
                         ["AMZN 260.00 / 262.50 Call",
                          "TSLA 290.00 / 292.50 Call"])
        # D13: the Position ($) cell slides in at index 4, so the return column
        # moved from 6 to 7 - same value, one cell further right.
        self.assertEqual([z[7] for z in zeilen], ["257.14 %", "257.14 %"])

    def test_the_page_shows_the_distance_to_the_break_even(self):
        zeilen = self._spread_rows(self._page())
        # D13: BE-Abstand moved from 8 to 9 (Position ($) is index 4 now).
        self.assertEqual([z[9] for z in zeilen], ["+4.28 %", "+16.28 %"])

    def test_the_spread_cell_names_the_trade_and_keeps_the_codes_in_the_tooltip(self):
        html = self._page()
        self.assertIn('title="AMZN261016C00260000 / AMZN261016C00262500"', html)
        self.assertIn(">AMZN 260.00 / 262.50 Call<", html)

    # --- D13: die Spalte Position ($) ---------------------------------------

    def test_the_header_carries_the_position_column_between_width_and_loss(self):
        """D13: the spread header has 12 columns, and `Debit (je Einheit)` and
        `Position ($)` stand DIRECTLY beside each other between `Breite` and
        `max Verlust` - the basis first, then what the account really pays."""
        kopf = self._spread_header(self._page())
        self.assertEqual(len(kopf), 12)
        self.assertEqual(kopf[2:6], ["Breite", "Debit (je Einheit)",
                                     "Position ($)", "max Verlust"])

    def test_the_position_cell_shows_what_the_row_costs(self):
        """D13: with a size from the form (multiplier 10, quantity 3, fees 1.5)
        the Position ($) cell is the row's OWN debit x 10 x 3 + 1.5, rounded to
        whole dollars. The debit is read out of the same rendered row, so the
        check measures the page instead of rebuilding the number by hand."""
        zeilen = self._spread_rows(
            self._page({"multiplier": "10", "quantity": "3", "fees": "1.5"}))
        self.assertEqual(len(zeilen), 2)  # the fixture still lists both spreads
        for zeile in zeilen:
            debit = float(zeile[3])
            self.assertEqual(zeile[4], f"{round(debit * 10 * 3 + 1.5, 2):.0f}",
                             f"Position von {zeile[0]!r} passt nicht zum Debit")

    def test_the_position_cell_shows_a_dash_without_a_size(self):
        """D13: a missing position size must not read as a silent 0. Without a
        multiplier OR without a quantity the cell is exactly `-`, never a zero."""
        for multiplier, quantity in ((None, 3), (10, None)):
            criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                        multiplier=multiplier, quantity=quantity)
            page = self._app(criteria=criteria).test_client().get(
                "/", query_string={"ticker": "AAPL", "expiration": "2026-10-16"}
            ).get_data(as_text=True)
            positionen = [z[4] for z in self._spread_rows(page)]
            self.assertEqual(positionen, ["-", "-"], (multiplier, quantity))
            for zelle in positionen:
                self.assertNotIn("0", zelle, (multiplier, quantity))

    def test_the_debit_stays_the_value_per_unit_and_the_note_explains_it(self):
        """D13: the new column supplements, never replaces, the per-unit debit -
        the same row still shows the debit with two decimals - and the note
        ABOVE the table names `Position ($)` and says the debit keeps being the
        value per unit."""
        page = self._page({"multiplier": "10", "quantity": "3", "fees": "1.5"})
        zeilen = self._spread_rows(page)
        self.assertEqual(len(zeilen), 2)
        for zeile in zeilen:
            self.assertRegex(zeile[3], r"^\d+\.\d{2}$")
        self.assertIn("Position ($)", page)
        hinweis = "der Debit bleibt der Wert je Einheit"
        self.assertIn(hinweis, page)
        self.assertLess(page.index(hinweis), page.index("<th>Spread</th>"),
                        "der Hinweis muss ueber der Tabelle stehen")

    def test_the_spread_row_keeps_the_shape_the_other_tests_parse(self):
        # die Muster der uebrigen Tests duerfen nicht brechen: <tr> ohne
        # Attribute, <td> hoechstens mit title, genau ein tbody, zwei Tabellen
        loose = ScreenerCriteria(max_quote_age_min=None, max_spread_pct=None,
                                 max_spread_abs=None, min_oi=None,
                                 dte_min=None, dte_max=None)
        page = self._app(criteria=loose).test_client().get(
            "/", query_string={"ticker": "AAPL", "expiration": "2026-10-16"}).get_data(as_text=True)
        for zeile in self._spread_rows(page):
            self.assertEqual(len(zeile), 12)  # D13: + Position ($)
        self.assertEqual(page.count("<tbody>"), 1)
        self.assertEqual(page.count("<table>"), 2)

    # --- die Reihenfolge: Seite sortiert, Schnittstelle nicht ---

    def test_the_page_sorts_by_return_per_default(self):
        self.assertEqual(self._spread_rows(self._page())[0][0], "AMZN 260.00 / 262.50 Call")
        self.assertEqual(self._spread_rows(self._page())[-1][0], "TSLA 290.00 / 292.50 Call")

    def test_the_page_can_sort_by_distance_to_break_even(self):
        zeilen = self._spread_rows(self._page({"sort": "be_asc"}))
        self.assertEqual([z[9] for z in zeilen], ["+4.28 %", "+16.28 %"])

    def test_the_page_can_keep_the_chain_order(self):
        self.assertEqual([z[0] for z in self._spread_rows(self._page({"sort": "none"}))],
                         ["AMZN 260.00 / 262.50 Call", "TSLA 290.00 / 292.50 Call"])

    def test_an_unknown_sort_mode_falls_back_to_the_return(self):
        self.assertEqual(self._spread_rows(self._page({"sort": "quatsch"}))[0][0],
                         self._spread_rows(self._page())[0][0])

    def test_the_api_keeps_the_chain_order_while_the_page_sorts(self):
        # die Entscheidung selbst: gleiche Werte, verschiedene Reihenfolge
        self.assertEqual([v["symbol"][:4] for v in self._json()["verticals"]],
                         ["AMZN", "TSLA"])
        self.assertEqual(self._spread_rows(self._page())[0][0][:4], "AMZN")

    def test_the_sort_links_keep_the_other_parameters(self):
        # D7: the spread section now runs through the same filters as the list, so
        # a tight relative bound empties it - and an empty section has no header
        # links to check. The value below keeps the fixture's pairs (14.3 % to
        # 33.3 %) and still proves that the link carries what the user typed.
        html = self._page({"max_spread_pct": "50"})
        self.assertIn("ticker=AAPL", html)
        self.assertIn("sort=rendite_desc", html)
        self.assertIn("sort=be_asc", html)
        self.assertIn("max_spread_pct=50", html)

    # --- die Faelle, die die Mock-Kette nicht triggert (Restrisiko des Urteils) ---

    def test_a_spread_without_loss_never_reaches_the_page_and_the_guard_still_holds(self):
        # GEMESSEN: die Bein-Regel sortiert `debit <= 0` schon aus, ein Spread
        # ohne Verlust kann die Seite also gar nicht erreichen. Der Schutz in
        # der Anzeige wird deshalb direkt geprueft - nicht ueber die Seite.
        kette = [quote("AAPL261016C00250000", bid=0.45, ask=0.50, iv=30.0, oi=1500),
                 quote("AAPL261016C00252500", bid=0.50, ask=0.55, iv=30.0, oi=1500)]
        behalten, verworfen = server_module.vertical_spreads(kette, _LOOSE, now_fn=_clock)
        self.assertEqual(behalten, [])
        self.assertEqual(verworfen[0][1], "Debit 0.0 außerhalb (0, 2.5) - kein Gewinn möglich")

        class OhneVerlust:
            max_loss = 0.0
            max_profit = 1.0

        class MitVerlust:
            max_loss = 2.0
            max_profit = 1.0

        self.assertIsNone(server_module._spread_return_pct(OhneVerlust()))
        self.assertEqual(server_module._spread_return_pct(MitVerlust()), 50.0)

    def test_a_spread_without_spot_shows_a_dash(self):
        kette = [quote("AAPL261016C00250000", bid=1.10, ask=1.20, iv=30.0, oi=1500,
                       underlying_price=None),
                 quote("AAPL261016C00252500", bid=0.45, ask=0.50, iv=30.0, oi=1500,
                       underlying_price=None)]
        # D13: BE-Abstand is index 9 now (was 8); the return column is index 7
        # (was 6). Same two cells, one step to the right of the Position ($).
        self.assertEqual(self._spread_rows(self._page(quotes=kette))[0][9], "-")
        # ... und nur dort: die Rendite ist rechenbar und bleibt eine Zahl
        self.assertNotEqual(self._spread_rows(self._page(quotes=kette))[0][7], "-")



class TheInterfaceSpeaksOneLanguage(unittest.TestCase):
    """Slice D0: die Oberflaeche schreibt deutsch MIT Umlauten, in EINER Schreibweise.

    Ein assertNotIn auf EINER Seite waere blind: gemessen
    (docs/debate/ausD_phase4_zustaende.txt) erscheinen die alten Schreibweisen
    nur in BESTIMMTEN Zustaenden - "aelter" nur bei gelber Lampe, "Nicht
    verfuegbar" nur ohne gelistete Verfallstage, "fuer" nur bei einem Verfall,
    den die Kette nicht kennt, "waehlen"/"druecken" nur auf der Startseite.
    Deshalb wird JEDER Zustand geprueft, in dem eines der Woerter stehen kann.
    """

    #: Die 11 alten Schreibweisen. Nach D0 darf KEINE mehr im HTML stehen.
    ALTE = ("aelter", "koennen", "Verfaelle", "verfuegbar", "Groesse",
            "zaehlt", "moeglich", "waehlen", "druecken", "Ungueltig", "fuer")

    @staticmethod
    def _client(clock=None, criteria=None):
        # make() + make_verticals(): die VOLLE Seite braucht beide Ketten,
        # sonst faellt die Einzelkontrakt-Tabelle weg (gemessen)
        adapter = _MockAdapter(make() + make_verticals())
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(criteria if criteria is not None
                                                 else ScreenerCriteria()),
                         now_fn=clock or _clock())
        app.config["TESTING"] = True
        return app.test_client()

    def _pages(self):
        """Jeder Zustand, in dem eine der alten Schreibweisen vorkommen kann.

        Zwei Klienten, weil sich zwei Zustaende ausschliessen: die volle Seite
        braucht die STANDARD-Kriterien, der Warnhinweis "koennen" braucht
        age_off (Alter-Grenze aus).
        """
        gruen = self._client()
        aus = self._client(criteria=ScreenerCriteria(max_quote_age_min=None))
        # 31 min statt 5: die Lampe steht dann auf gelb, und "aelter" erscheint
        gelb = self._client(clock=lambda: arrow.get("2026-10-07T14:30:00").shift(minutes=31))
        return {
            "leere Startseite": gruen.get("/").get_data(as_text=True),
            "Alter-Grenze aus": aus.get("/").get_data(as_text=True),
            "Ticker ohne Verfall": gruen.get("/?ticker=AAPL").get_data(as_text=True),
            "volle Seite": gruen.get("/?ticker=AAPL&expiration=2026-10-16").get_data(as_text=True),
            "ungueltiger Ticker": gruen.get("/?ticker=../etc&expiration=2026-10-16").get_data(as_text=True),
            "ungueltiges Datumsformat": gruen.get("/?ticker=AAPL&expiration=quatsch").get_data(as_text=True),
            "Verfall nicht gelistet": gruen.get("/?ticker=AAPL&expiration=2019-01-01").get_data(as_text=True),
            "gelbe Lampe": gelb.get("/?ticker=AAPL&expiration=2026-10-16").get_data(as_text=True),
        }

    def test_no_page_carries_an_old_spelling(self):
        for zustand, page in self._pages().items():
            for wort in self.ALTE:
                self.assertNotIn(wort, page,
                                 f"alte Schreibweise {wort!r} steht noch im Zustand {zustand!r}")

    def test_the_page_renders_the_correct_spellings(self):
        page = self._pages()["volle Seite"]
        for wort in ("Verfälle werden geladen", "kein Gewinn möglich", "zählt",
                     "Geld-Größe min", "Brief-Größe min"):
            self.assertIn(wort, page, f"{wort!r} fehlt auf der Seite")

    def test_the_trend_badge_is_german_when_the_source_fails(self):
        # server.py:546 - der einzige deutsche Text, der aus dem Server in die
        # Kopfzeile wandert; gemessen an der echten Route, nicht am Quelltext
        import unittest.mock as mock
        with mock.patch.object(server_module, "_get_trend_provider",
                               return_value=_BrokenProvider()):
            page = self._client().get(
                "/?ticker=AAPL&expiration=2026-10-16").get_data(as_text=True)
        self.assertIn("Trend nicht verfügbar", page)
        self.assertNotIn("verfuegbar", page)


class TheFormIsGroupedAndThePageIsWide(unittest.TestCase):
    """ausD1 (D1): die Filter liegen in vier Gruppen in einem Raster, die Seite
    ist breiter als ihre breiteste Tabelle, und das Feld mit der scharfen Null
    traegt seinen Hinweis.

    Zahlen aus der Messung (docs/210_ausD1_phase1_bis3.md): die Spread-Tabelle
    ist 837 px breit, der Inhaltsbereich war 760 px, der Leerraum betrug 37,4 %
    der Fensterbreite. Geprueft werden REGELN aus dem <style>-Block, nicht das
    blosse Wort "display:grid" - das kann auch in einer fremden Regel oder im
    <script> stehen und wuerde nichts beweisen.
    """

    #: Die vier Gruppen mit ihren Feldern IN DOKUMENTREIHENFOLGE. Die Reihenfolge
    #: ist der Inhalt dieses Slices: sie folgt der Frage des Nutzers, nicht der
    #: Reihenfolge der Kriterien im Rechenmodul.
    GRUPPEN = (
        # D11: pair_max_width stands directly behind min_volume - the order of
        # the criteria in the module, not appended at the end of the group.
        ("Liquidit\u00e4t", ("max_spread_pct", "max_spread_abs", "min_oi", "min_volume",
                             "pair_max_width", "min_bid_size", "min_ask_size",
                             "require_oi", "require_volume")),
        ("Laufzeit und Frische", ("dte_min", "dte_max", "max_quote_age_min",
                                  "ignore_quote_age")),
        ("Volatilit\u00e4t", ("iv_min", "iv_max", "iv_pref")),
        ("Bewertung", ("weight_spread", "weight_oi", "weight_iv")),
        # D12: the position size is its own question and stands AFTER the four
        # filter groups. It does not filter the chain: it turns the pair numbers
        # above (per unit) into the position the operator would really hold.
        ("Position", ("multiplier", "quantity", "fees", "budget")),
    )

    #: Die 19 Zahlenfelder - die Menge darf das Umgruppieren nicht veraendern.
    #: D11: pair_max_width kam dazu; min_volume BLEIBT (beide sind Kriterien).
    #: D12: die vier Felder der Gruppe Position (multiplier, quantity, fees,
    #: budget) sind ebenfalls <input type="number"> und zaehlen hier mit.
    ZAHLENFELDER = ("dte_min", "dte_max", "max_spread_pct", "max_spread_abs", "min_oi",
                    "min_volume", "pair_max_width", "min_bid_size", "min_ask_size",
                    "iv_min", "iv_max", "max_quote_age_min", "weight_spread",
                    "weight_oi", "weight_iv", "multiplier", "quantity", "fees",
                    "budget")

    @staticmethod
    def _page():
        adapter = _MockAdapter(make() + make_verticals())
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(_WIDE),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app.test_client().get(
            "/", query_string={"ticker": "AAPL", "expiration": "2026-10-16"}
        ).get_data(as_text=True)

    @staticmethod
    def _ohne_style_und_script(page):
        # Die Struktur-Zaehlungen duerfen nicht auf Literale im JS oder CSS
        # hereinfallen (am Basis-Blob gemessen: dort steht keins, aber ein
        # spaeterer Zusatz im <script> wuerde die Zaehlung sonst still brechen).
        return re.sub(r"<style>.*?</style>|<script>.*?</script>", "", page, flags=re.S)

    @staticmethod
    def _gruppen(page):
        """[(legend, (name, ...)), ...] in Dokumentreihenfolge.

        Eine Funkgruppe erscheint im HTML mehrfach (ein <input> je Knopf) und
        zaehlt hier EINMAL: aufeinanderfolgende gleiche Namen werden
        zusammengefasst. Gemessen am Basis-Blob liefert ein rohes findall
        ('require_oi', 'require_oi', 'require_volume', 'require_volume') -
        eine Zusicherung darauf wuerde die Knopfe zaehlen, nicht die Felder.
        """
        ergebnis = []
        for block in re.findall(r'<fieldset class="crit">(.*?)</fieldset>', page, re.S):
            legend = re.search(r"<legend>(.*?)</legend>", block).group(1)
            roh = re.findall(r'name="([^"]+)"', block)
            felder = tuple(n for i, n in enumerate(roh) if i == 0 or roh[i - 1] != n)
            ergebnis.append((legend, felder))
        return ergebnis

    @staticmethod
    def _namen(page):
        """{name: Anzahl} ueber das ganze Formular."""
        form = re.search(r"<form.*?</form>", page, re.S).group(0)
        gezaehlt = {}
        for name in re.findall(r'name="([^"]+)"', form):
            gezaehlt[name] = gezaehlt.get(name, 0) + 1
        return gezaehlt

    @staticmethod
    def _label(page, feld):
        """Das <label>, das dieses Feld traegt (D12: die Positionsfelder)."""
        for treffer in re.finditer(r"<label>(.*?)</label>", page, re.S):
            if f'name="{feld}"' in treffer.group(1):
                return treffer.group(1)
        raise AssertionError(f"kein <label> fuer {feld}")

    # --- die Breite ---

    def test_the_page_is_wider_than_its_widest_table(self):
        # die breiteste Tabelle braucht gemessen 837 px; mit 2x16 px Polster
        # sind das 869 px. Die Zahl wird AUS DER REGEL gelesen und geprueft -
        # ein "assertIn('1100', page)" waere blind (die Zahl kann aus Daten
        # stammen), ein "assertIn('max-width', page)" erst recht.
        page = self._page()
        regel = re.search(r"main\{([^}]*)\}", page).group(1)
        breite = int(re.search(r"max-width:(\d+)px", regel).group(1))
        # D2b: 1220 px - die Leiste ist auf 296 px gewachsen, der Abstand
        # auf 32 px (1220 - 296 - 32 = 892 >= 837)
        self.assertEqual(breite, 1220)
        self.assertGreaterEqual(breite, 869)
        self.assertNotIn("max-width:760px", page)

    # --- das Raster ---

    def test_the_grid_sits_on_the_fieldset_not_on_the_form(self):
        page = self._page()
        # ein Grid am <form> macht jedes <fieldset> zu EINEM Rasterfeld
        # (D0-Befund); ohne Spaltenangabe faellt das Raster auf eine Spalte
        # zurueck und waere optisch der alte Zustand
        self.assertIn("fieldset.crit{display:grid", page)
        self.assertIn("grid-template-columns:repeat(auto-fill,minmax(230px,1fr))", page)
        # D2: das <form> traegt jetzt selbst ein Raster - fuer die Leiste
        # (Instrument oben, Filter darunter). Die D0-Gefahr war, dass ein
        # <fieldset> dadurch zu EINER Spalte wird; dagegen steht jetzt, dass
        # jedes <fieldset> ueber die ganze Formularbreite spannt.
        self.assertIn("form>button,form>fieldset,form>span{grid-column:1/-1}", page)
        # ohne diese Angabe bleiben die Kaesten Flex-Elemente des <form> und
        # schrumpfen (gemessen: 206-345 px statt voller Breite)
        self.assertEqual(page.count("flex:1 1 100%"), 1)
        # grid-column steht im CSS, nicht am Element. Statt eine Zahl zu pflegen,
        # die mit jedem Slice waechst, wird die BEDEUTUNG geprueft: jede
        # Zeilenangabe spannt ueber die ganze Breite, keine Teilstuecke.
        werte = re.findall(r"grid-column:([^;}]+)", page)
        self.assertGreaterEqual(len(werte), 3)
        self.assertEqual(set(werte), {"1/-1"})

    def test_the_fields_are_grouped_by_the_question_they_answer(self):
        # Gleichheit, nicht Teilzeichenkette: Reihenfolge der Gruppen UND der
        # Felder je Gruppe sind der Inhalt dieses Slices
        self.assertEqual(self._gruppen(self._page()), list(self.GRUPPEN))

    def test_every_field_survives_the_regrouping(self):
        # die schaerfste Zusicherung: ein beim Umbau verlorenes oder doppeltes
        # Feld laesst sich nicht durch Textsuche finden
        erwartet = {"ticker": 1, "expiration": 1, "require_oi": 2,
                    "require_volume": 2, "iv_pref": 3, "ignore_quote_age": 1}
        for name in self.ZAHLENFELDER:
            erwartet[name] = 1
        self.assertEqual(self._namen(self._page()), erwartet)

    def test_no_control_is_added_or_lost(self):
        page = self._page()
        self.assertEqual(page.count("<input"), 28)   # D12: + 4 Positionsfelder
        self.assertEqual(page.count("<select"), 1)   # Slice-I-Falle: Seiten-Scans
        self.assertEqual(page.count("<button"), 1)
        self.assertEqual(page.count("<fieldset"), 5)  # D12: + Gruppe Position
        self.assertEqual(page.count("<legend>"), 5)

    def test_the_controls_stay_outside_the_groups(self):
        # Ticker, Verfallsauswahl und der Scan-Knopf gehoeren NICHT in eine
        # Filtergruppe: sie sind die Eingabe, die Gruppen sind die Filter.
        page = self._page()
        erstes = page.index('<fieldset class="crit">')
        self.assertLess(page.index('name="ticker"'), erstes)
        self.assertLess(page.index('<select name="expiration"'), erstes)
        self.assertLess(page.index("<button"), erstes)
        # ... und die Gruppen stehen alle im Formular (sonst werden die Filter
        # nicht mitgesendet)
        form = re.search(r"<form.*?</form>", page, re.S).group(0)
        self.assertEqual(form.count('<fieldset class="crit">'), 5)  # D12: + Position
        self.assertEqual(form.count("</fieldset>"), 5)

    # --- D12: die Gruppe Position -------------------------------------------

    def test_the_position_group_holds_the_four_position_fields(self):
        """D12: Multiplikator, Stueckzahl, Gebuehren und Budget sitzen im
        Formular UND in der Gruppe "Position" - und jedes nennt seinen Standard.

        Die vier Zahlen sind die Positionsgroesse: sie stehen in einer eigenen
        Gruppe, damit der Bediener die Groesse einstellt, ohne die Filter zu
        beruehren. Geprueft wird die Zugehoerigkeit UEBER die Gruppe, nicht per
        Textsuche - ein Feld ausserhalb der Gruppe waere im Formular, aber nicht
        als Position auffindbar.
        """
        page = self._page()
        self.assertEqual(dict(self._gruppen(page))["Position"],
                         ("multiplier", "quantity", "fees", "budget"))
        form = re.search(r"<form.*?</form>", page, re.S).group(0)
        for feld, wert in (("multiplier", "100"), ("quantity", "1"),
                           ("fees", "0.0"), ("budget", "")):
            treffer = re.findall(f'<input[^>]*name="{feld}"[^>]*>', form)
            self.assertEqual(len(treffer), 1, feld)
            self.assertIn('type="number"', treffer[0], feld)     # a number field
            self.assertIn(f'value="{wert}"', treffer[0], feld)   # its standard
            label = self._label(page, feld)
            self.assertIn("<input", label, feld)                 # inside the row
            self.assertIn("Standard", label[:label.index("<input")], feld)

    # --- die Beschriftungen ---

    def test_the_labels_carry_their_unit(self):
        page = self._page()
        # D3: der Standardvermerk steht seit D3 zwischen Beschriftung und Feld.
        # Die Einheit muss weiterhin am ANFANG der Beschriftung stehen; dass
        # unmittelbar das Feld folgt, sichert TheDefaultsAreVisible zu.
        for text in ("DTE von (Tage)", "DTE bis (Tage)", "Alter max (min)",
                     "Spread max (%)", "Spread max ($)", "IV min (%)", "IV max (%)"):
            self.assertIn("<label>" + text, page, text)
        for alt in ("Spread max %", "Spread max $", "Alter max min", "IV min %", "IV max %"):
            self.assertNotIn("<label>" + alt + " <input", page, alt)
        # die zwei in Slice D0 festgenagelten Texte bleiben Wort fuer Wort
        self.assertIn("Geld-Gr\u00f6\u00dfe min", page)
        self.assertIn("Brief-Gr\u00f6\u00dfe min", page)

    def test_the_sharp_zero_is_named_at_its_field(self):
        # server.py:69 - 0 ist eine scharfe Grenze, kein "aus"; gemessen
        # (docs/210_ausD1_phase1_bis3.md) loescht 0 die ganze Liste
        # (19 gefiltert, 0 Kandidaten)
        label = re.search(r"<label>Alter max \(min\).*?</label>", self._page(), re.S).group(0)
        self.assertIn('<small class="warn-min">0 streicht alle Kurse (nicht \u00bbaus\u00ab)'
                      "</small>", label)

    def test_no_legend_promises_a_standard_that_is_not_one(self):
        # "leer = Standard" stand zweimal auf der Seite und war gemessen
        # irrefuehrend (10 von 14 Zahlenfeldern sind vorbelegt). Seitenweit
        # geprueft, damit der Hinweis nicht in ein anderes Element wandern kann.
        self.assertNotIn("leer = Standard", self._page())

    # --- die Struktur, die alle anderen Tests parsen ---

    def test_the_page_keeps_the_shape_every_other_test_parses(self):
        page = self._ohne_style_und_script(self._page())
        self.assertEqual(page.count("<table>"), 2)
        self.assertEqual(page.count("<tbody>"), 1)
        # <tr> ohne Attribute: jede Zeile wird mit r"<tr>(.*?)</tr>" zerlegt
        self.assertEqual(len(re.findall(r"<tr[^>]*>", page)), page.count("<tr>"))
        # in der Spread-Tabelle trifft das Zellmuster der Bestandstests JEDE
        # Zelle (12 je Zeile) - dort ist kein <td class=...> erlaubt
        spread = [t for t in re.findall(r"<table>(.*?)</table>", page, re.S)
                  if "<th>Spread</th>" in t][0]
        zeilen = re.findall(r"<tr>(.*?)</tr>", spread, re.S)[1:]
        # D7: 3 of the 4 pairs survive - the degenerated AAPL pair (debit ==
        # width) is dropped by the pair rule now
        self.assertEqual(len(zeilen), 3)
        for zeile in zeilen:
            self.assertEqual(
                len(re.findall(r'<td(?: title="[^"]*")?>(.*?)</td>', zeile, re.S)), 12)


class TheFiltersStandBesideTheResult(unittest.TestCase):
    """ausD2 (D2): die Filter liegen in einer linken Leiste, das Ergebnis hat die
    Buehne - Spreads vor Kontrakten, die beiden Ausschuesse zugeklappt darunter.

    Messung vor dem Bau (docs/221_ausD2_phase1_bis3.md): der erste Ergebnisblock
    stand bei y=871 von 1446 px, weil das Formular (558 px) und der Ausschuss-
    Kasten "Warum gestrichen?" (162 px) davor lagen. Der zweite Ausschuss-Kasten
    stand hinter dem Ergebnis - die Erklaerung lag also einmal davor und einmal
    dahinter.
    """

    @staticmethod
    def _app(quotes=None):
        # make() + make_verticals(): beide Tabellen UND beide Ausschuss-Bloecke
        # brauchen die volle Kette. Gemessen: make() allein liefert kein
        # <tbody> (die Kontrakttabelle fehlt), make_verticals() allein liefert
        # keine "Warum gestrichen?"-Begruendungen.
        adapter = _MockAdapter(make() + make_verticals() if quotes is None else quotes)
        # die Fixture liegt auf ihrem eigenen Verfall; die Seite nimmt nur ein
        # Datum an, das die Kette wirklich listet
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(_WIDE),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _page(self, params=None, quotes=None):
        query = {"ticker": "AAPL", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    @staticmethod
    def _spread_tabelle(page):
        """Die Spread-Tabelle - am KOPF erkannt, nicht an der Position.

        Seit D2 stehen die Spreads VOR den Kontrakten; ein [-1] wie zuvor griffe
        jetzt die Kontrakttabelle.
        """
        return [t for t in re.findall(r"<table>(.*?)</table>", page, re.S)
                if "<th>Spread</th>" in t][0]

    @staticmethod
    def _erste_spread_zeile(page):
        tabelle = TheFiltersStandBesideTheResult._spread_tabelle(page)
        zeile = re.findall(r"<tr>(.*?)</tr>", tabelle, re.S)[1]
        return re.sub(r"<[^>]+>", "", zeile).strip().split(" ")[0]

    # --- die zwei Spalten ---

    def test_the_filters_stand_beside_the_result(self):
        page = self._page()
        # D2b: die Leiste ist breiter (296 statt 272) und der Umbruchpunkt
        # mitgerechnet (Ergebnisbreite = Fenster - 375, 837 px brauchen 1212)
        self.assertIn("max-width:1220px", page)
        self.assertIn(".shell{display:grid;grid-template-columns:296px minmax(0,1fr)", page)
        self.assertIn("@media (max-width:1212px){.shell{grid-template-columns:minmax(0,1fr)}}", page)
        self.assertLess(page.index('<div class="shell">'), page.index("<form"))
        self.assertLess(page.index("<form"), page.index('<div class="results">'))

    def test_the_page_notes_stay_outside_the_shell(self):
        # Hinweis, Warnung und Fehlermeldung gehoeren ueber beide Spalten, nicht
        # in die Ergebnisspalte
        page = self._page()
        self.assertIn('<p class="note">', page)
        self.assertLess(page.index('<p class="note">'), page.index('<div class="shell">'))

    # --- die Reihenfolge im Ergebnis ---

    def test_the_spreads_come_before_the_contracts(self):
        page = self._page()
        self.assertLess(page.index("<th>Spread</th>"), page.index("<th>Kontrakt</th>"))
        self.assertLess(page.index("<th>Kontrakt</th>"),
                        page.index('<details class="dropped-summary">'))

    def test_the_contracts_table_keeps_the_only_tbody(self):
        page = self._page()
        self.assertEqual(page.count("<tbody>"), 1)
        kontrakte = [t for t in re.findall(r"<table>(.*?)</table>", page, re.S)
                     if "<th>Kontrakt</th>" in t][0]
        self.assertIn("<tbody>", kontrakte)
        self.assertNotIn("<tbody>", self._spread_tabelle(page))

    # --- die Fuehrzeile ---

    def test_the_lead_line_names_the_best_return(self):
        page = self._page()
        # D7: 3 Spreads - the degenerated AAPL pair is no longer a result
        self.assertIn('<p class="lede">3 Spreads, bester 257.14 % Rendite</p>', page)
        self.assertLess(page.index('class="lede"'), page.index('<div class="stats">'))

    def test_the_lead_line_is_the_maximum_and_not_the_first_row(self):
        # sortiert der Nutzer nach BE-Abstand, steht ein anderer Spread oben -
        # die Fuehrzeile darf sich nicht mitdrehen
        nach_rendite = self._page()
        nach_abstand = self._page({"sort": "be_asc"})
        self.assertEqual(self._erste_spread_zeile(nach_rendite), "AMZN")
        self.assertEqual(self._erste_spread_zeile(nach_abstand), "AAPL")
        for page in (nach_rendite, nach_abstand):
            self.assertIn("3 Spreads, bester 257.14 % Rendite", page)

    def test_without_spreads_there_is_no_lead_line(self):
        # ein einzelner Kontrakt hat keinen Nachbarn -> keine Spreads
        einzeln = [quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0,
                         oi=8000, volume=1200)]
        page = self._page(quotes=einzeln)
        self.assertNotIn('class="lede"', page)

    # --- die Ausschuesse ---

    def test_the_rejected_blocks_are_collapsed_and_named(self):
        page = self._page()
        self.assertEqual(page.count('<details class="dropped-summary">'), 2)
        # zugeklappt: kein open-Attribut (ein ' open' als Textsuche waere blind)
        self.assertIsNone(re.search(r'<details class="dropped-summary"[^>]*\bopen\b', page))
        self.assertIn("<summary>Warum gestrichen?", page)
        self.assertIn("<summary>Warum kein Spread?", page)

    def test_the_rejected_items_are_still_in_the_document(self):
        # zugeklappt heisst nicht verschwunden: die Begruendungen muessen im HTML
        # stehen, damit die Seite sie ohne Server-Runde aufklappen kann
        page = self._page()
        self.assertIn('<li class="drop-reason">', page)
        self.assertIn('class="vertical-reason"', page)
        self.assertLess(page.index('<details class="dropped-summary">'),
                        page.index('<li class="drop-reason">'))


class TheRailIsTidyAndTheFieldsLineUp(unittest.TestCase):
    """D2b: die Leiste traegt keine Kaesten mehr, und jedes Feld steht
    rechtsbuendig auf einer gemeinsamen Kante.

    Der Slice ist reine Anzeige - die Wirkung der Regeln ist im Browser
    gemessen (14 Felder, rechte Kante x=341, Abweichung 0 px), pytest kann nur
    die Regeln selbst festhalten. Deshalb wird hier JEDE Regel zeichengenau
    gepinnt und zusaetzlich die Struktur, auf der sie beruht.
    """

    @staticmethod
    def _app(quotes=None):
        adapter = _MockAdapter(make() + make_verticals() if quotes is None else quotes)
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(ScreenerCriteria()),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _page(self, params=None, quotes=None):
        query = {"ticker": "AAPL", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    def test_the_rail_groups_carry_no_boxes(self):
        # Kaesten weg: die Gruppe ist eine Ueberschrift mit Zeilen darunter
        page = self._page()
        self.assertIn(".shell fieldset.crit{border:0;background:none;padding:0;margin:0 0 16px}", page)
        # und der Rahmen der Basisregel darf die Leiste nicht mehr treffen
        self.assertNotIn(".shell fieldset.crit{border:1px", page)

    def test_each_field_is_a_row_with_the_input_on_the_right(self):
        page = self._page()
        self.assertIn(".shell fieldset.crit>label{display:grid;"
                      "grid-template-columns:minmax(0,1fr) 84px;"
                      "align-items:center;gap:8px;margin:5px 0}", page)
        self.assertIn(".shell fieldset.crit>label>input[type=number]{width:100%;min-width:0;"
                      "box-sizing:border-box;text-align:right;"
                      "font-variant-numeric:tabular-nums;padding:6px 10px}", page)
        # ohne border-box ist das Feld 84 + 22 = 106 px breit und laeuft bei
        # 1024 px um 6 px aus dem Fenster (gemessen)
        self.assertIn(".shell form>input,.shell form>select{box-sizing:border-box}", page)

    def test_the_warning_line_spans_the_whole_row(self):
        # die Warnung am Alter-Feld steht in der Beschriftungszeile; ohne
        # Zeilenspanne landet sie in der schmalen linken Spalte und bricht um
        page = self._page()
        self.assertIn(".shell fieldset.crit>label>small{grid-column:1/-1", page)

    def test_the_checkbox_row_stays_left_aligned(self):
        # "Alter-Grenze aus" ist kein Zahlenfeld und darf nicht in die
        # rechte Spalte rutschen
        page = self._page()
        self.assertIn(".shell fieldset.crit>label:has(input[type=checkbox]){"
                      "grid-template-columns:auto minmax(0,1fr);justify-items:start}", page)

    def test_the_rail_is_titled_instrument(self):
        page = self._page()
        self.assertIn('<h2 class="rail-title">Instrument</h2>', page)
        self.assertLess(page.index('<h2 class="rail-title">'),
                        page.index('name="ticker"'))

    def test_every_number_field_sits_inside_its_label(self):
        # die Regel greift ueber >label: nur so wird die Beschriftung zur
        # linken und das Feld zur rechten Spalte. Ein Feld ausserhalb seines
        # <label> faellt aus dem Raster - genau davor schuetzt diese Pruefung.
        page = self._page()
        form = page[page.index("<form"):page.index("</form>")]
        felder = re.findall(r'<input type="number"[^>]*>', form)
        self.assertEqual(len(felder), 19)      # D12: + 4 Positionsfelder
        im_label = re.findall(r"<label>(?:(?!</label>).)*?<input type=\"number\"", form, re.S)
        self.assertEqual(len(im_label), 19)


class TheColoursCarryEnoughContrast(unittest.TestCase):
    """D2c: jede Schriftfarbe der Seite traegt 4,5:1, jeder Bedienrahmen 3,0:1.

    Gemessen im Browser (berechnete Stile, 1280 px):

        Sortier-Links      #0000ee auf #0f1420 = 1,96:1   -> #8ab4ff = 8,81:1
        Knoptschrift       #e6e9ef auf #2c6bed = 3,89:1   -> #ffffff = 4,74:1
        Bedienrahmen       #2c3650 auf #0f1420 = 1,53:1   -> #5b6f99 = 3,66:1
        Pfeil-Marker       #6b7689 auf #0f1420 = 4,01:1   -> #828da5 = 5,52:1

    Die Sortier-Links waren der schwerste Fall: die Seite hatte ueberhaupt keine
    a-Regel, deshalb griff das Browser-Blau #0000ee - auf dunklem Grund so gut wie
    unlesbar. Kein Werkzeug haette das gemeldet, nur die Messung am gerenderten
    Dokument.
    """

    @staticmethod
    def _app(quotes=None):
        adapter = _MockAdapter(make() + make_verticals() if quotes is None else quotes)
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(ScreenerCriteria()),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _page(self, params=None, quotes=None):
        query = {"ticker": "AAPL", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    # --- die Kontrastrechnung (WCAG 2.1, relative Leuchtdichte) -----------
    @staticmethod
    def _leuchtdichte(farbe):
        if len(farbe) == 4:            # Kurzform "#fff" -> "#ffffff"
            farbe = "#" + "".join(2 * z for z in farbe[1:])
        werte = []
        for i in (1, 3, 5):
            v = int(farbe[i:i + 2], 16) / 255
            werte.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * werte[0] + 0.7152 * werte[1] + 0.0722 * werte[2]

    def _kontrast(self, vorne, hinten):
        a, b = self._leuchtdichte(vorne), self._leuchtdichte(hinten)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    SEITE = "#0f1420"
    KNOPF = "#2c6bed"

    def test_the_sort_links_have_a_colour_of_their_own(self):
        # ohne diese Regel nimmt der Browser sein Blau (#0000ee) - 1,96:1
        page = self._page()
        self.assertIn("a,a:visited{color:#8ab4ff}", page)

    def test_the_button_text_is_white(self):
        page = self._page()
        self.assertIn("button{background:#2c6bed;border-color:#2c6bed;color:#fff;"
                      "font-weight:600}", page)

    def test_the_controls_have_a_visible_border(self):
        page = self._page()
        self.assertIn("input,button{font-size:1rem;padding:9px 12px;border-radius:8px;"
                      "border:1px solid #5b6f99;background:#1a2233;color:inherit}", page)
        self.assertIn("select{font-size:1rem;padding:9px;border-radius:8px;"
                      "border:1px solid #5b6f99;", page)

    def test_the_folded_blocks_carry_a_brighter_marker(self):
        page = self._page()
        self.assertIn('.dropped-summary summary::before{content:"\\25B8 ";color:#828da5}', page)

    def test_the_four_measured_pairs_clear_their_threshold(self):
        page = self._page()
        for vorne, hinten, grenze, wo in (("#8ab4ff", self.SEITE, 4.5, "Sortier-Link"),
                                          ("#fff", self.KNOPF, 4.5, "Knoptschrift"),   # die Seite schreibt die Kurzform
                                          ("#5b6f99", self.SEITE, 3.0, "Bedienrahmen"),
                                          ("#828da5", self.SEITE, 4.5, "Pfeil-Marker")):
            self.assertIn(vorne, page, wo)
            self.assertGreaterEqual(round(self._kontrast(vorne, hinten), 2), grenze,
                                    f"{wo}: {vorne} auf {hinten}")

    def test_every_text_colour_of_the_page_clears_four_and_a_half(self):
        """Die Klasse des Fehlers, nicht nur der eine Fall.

        Jede Schriftfarbe, die die Seite selbst setzt, muss 4,5:1 gegen die
        Seitenflaeche tragen. Flaechen und rein zierende Rahmen duerfen
        dunkler sein - sie sind keine Schrift.
        """
        page = self._page()
        css = page[page.index("<style>"):page.index("</style>")]
        schriftfarben = set(re.findall(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{6}|#fff)\b", css))
        self.assertGreaterEqual(len(schriftfarben), 5)
        for farbe in sorted(schriftfarben):
            self.assertGreaterEqual(
                round(self._kontrast(farbe, self.SEITE), 2), 4.5,
                f"Schriftfarbe {farbe} traegt nur {self._kontrast(farbe, self.SEITE):.2f}:1")


class TheDefaultsAreVisible(unittest.TestCase):
    """ausD3 (D3): jedes Feld sagt selbst, was der Standard ist.

    Gemessen vor dem Bau: 10 der 14 Zahlenfelder sind vorbelegt (aus
    ScreenerCriteria), 4 sind leer - und leer heisst "Filter abgeschaltet",
    NICHT "Standard". Die Seite nannte den Standard bis hierher nirgends; das
    fruehere "(leer = Standard)" war in D1 als gemessen falsch entfernt worden.

    Der Vermerk steht als reiner TEXT im <label>, direkt vor dem <input>.
    Gemessen: als eigenes Element verschiebt er die rechte Kante der Felder
    (319 -> 227 px) und macht die Leiste 689 px hoeher; als Text aendert er
    nichts (rechte Kante 319, Abweichung 0 px).
    """

    STANDARD = {"max_spread_pct": "10.0", "max_spread_abs": "0.1", "min_oi": "500",
                "min_volume": "0", "dte_min": "7", "dte_max": "45",
                "max_quote_age_min": "30", "weight_spread": "0.5", "weight_oi": "0.3",
                "weight_iv": "0.2"}
    AUS = ("min_bid_size", "min_ask_size", "iv_min", "iv_max")
    AUS_TEXT = "Standard aus (leer = Filter aus)"
    #: D11: das neue Feld ist ebenfalls leer, heisst aber nicht "Filter aus":
    #: eine leere Breite ist die Nachbarschaft - der schnelle Modus von vorher.
    #: Es sagt seinen Standard also selbst, und mit eigenem Wortlaut.
    BREITE = "pair_max_width"
    BREITE_TEXT = ("Standard aus (leer = nur benachbarte Strikes; "
                   "gesetzt = alle Paare innerhalb dieser Breite)")

    @staticmethod
    def _app(quotes=None):
        adapter = _MockAdapter(make() + make_verticals() if quotes is None else quotes)
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(ScreenerCriteria()),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _page(self, params=None, quotes=None):
        query = {"ticker": "AAPL", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get(
            "/", query_string=query).get_data(as_text=True)

    # --- Lesehilfen: das <label> und der Wert EINES Feldes ---------------
    @staticmethod
    def _label(page, feld):
        for treffer in re.finditer(r"<label>(.*?)</label>", page, re.S):
            if f'name="{feld}"' in treffer.group(1):
                return treffer.group(1)
        raise AssertionError(f"kein <label> fuer {feld}")

    @staticmethod
    def _wert(page, feld):
        m = re.search(f'name="{feld}"[^>]*?value="([^"]*)"', page)
        return m.group(1) if m else None

    def _alle(self):
        return list(self.STANDARD) + list(self.AUS) + [self.BREITE]

    # --- die Zusicherungen ----------------------------------------------
    def test_every_prefilled_field_shows_the_default_of_the_code(self):
        """Der Wahrheitsbeweis: der Standard wird AUS DEM CODE gelesen.

        Nicht im HTML festgenagelt - wer den Standard in ScreenerCriteria
        aendert, ohne das Formular nachzuziehen, faellt hier durch.
        """
        page = self._page()
        std = ScreenerCriteria()
        for feld in self.STANDARD:
            self.assertEqual(self._wert(page, feld), str(getattr(std, feld)), feld)
        # D11: das neue Feld zeigt den Standard AUS DEM CODE. pair_max_width ist
        # None (Nachbarschaft); das Formular schreibt dann nicht "None", sondern
        # die leere Zeichenkette - genau der Standard von vorher.
        self.assertIsNone(std.pair_max_width)
        self.assertEqual(self._wert(page, self.BREITE), "", self.BREITE)
        # ... und ein gesetzter Wert kommt am Feld an (eine Quelle fuer Formular
        # und Rechenmodul - kein zweiter Standard im HTML)
        self.assertEqual(self._wert(self._page({"pair_max_width": "10.0"}), self.BREITE),
                         "10.0")

    def test_every_prefilled_field_names_its_default(self):
        page = self._page()
        for feld, wert in self.STANDARD.items():
            label = self._label(page, feld)
            self.assertIn(f"Standard {wert}", label, feld)
            self.assertEqual(self._wert(page, feld), wert, feld)
        # D11: auch das neue, nicht vorbelegte Feld nennt seinen Standard - der
        # Vermerk steht am Feld, das er beschreibt (jetzt 15 Felder).
        self.assertIn(self.BREITE_TEXT, self._label(page, self.BREITE), self.BREITE)
        self.assertEqual(self._wert(page, self.BREITE), "", self.BREITE)

    def test_the_empty_fields_say_the_filter_is_off(self):
        # die D1-Falle: leer ist "aus", nicht "Standard"
        page = self._page()
        for feld in self.AUS:
            label = self._label(page, feld)
            self.assertIn(self.AUS_TEXT, label, feld)
            self.assertEqual(self._wert(page, feld), "", feld)

    def test_every_field_carries_a_note(self):
        # D12: die vier Positionsfelder kommen dazu - mit ihnen sind es
        # neunzehn. Geprueft wird weiter dieselbe Aussage: jedes Feld des
        # Formulars nennt seinen Standard.
        page = self._page()
        form = page[page.index("<form"):page.index("</form>")]
        self.assertEqual(len(re.findall(r"\u00b7\s?Standard", form)), 19)

    def test_the_reset_link_keeps_only_the_instrument(self):
        # der Link darf kein Filter- und kein Sortierparameter tragen
        page = self._page({"min_oi": "5000", "weight_spread": "0.9", "sort": "be_asc"})
        m = re.search(r'<a class="reset" href="([^"]+)"', page)
        self.assertIsNotNone(m, "kein Ruecksetz-Link")
        ziel = html.unescape(m.group(1))
        self.assertIn("ticker=AAPL", ziel)
        self.assertIn("expiration=2026-10-16", ziel)
        for verboten in ("min_oi", "max_spread_pct", "weight_spread", "iv_min", "sort"):
            self.assertNotIn(verboten, ziel)

    def test_the_note_keeps_the_default_after_a_change(self):
        # der Vermerk nennt den STANDARD, nicht den eingetippten Wert
        page = self._page({"max_spread_pct": "1000"})
        # der Wert hat sich geaendert (gerendert wird "1000.0"), der Vermerk nennt
        # weiterhin den Standard - er zeigt NICHT den eingetippten Wert
        self.assertNotEqual(self._wert(page, "max_spread_pct"), "10.0")
        self.assertIn("1000", self._wert(page, "max_spread_pct"))
        self.assertIn("Standard 10.0", self._label(page, "max_spread_pct"))

    def test_the_note_sits_inside_the_label_before_the_input(self):
        # das Raster aus D2b haengt daran: Text und Feld sind EIN Rasterfeld
        page = self._page()
        for feld in self._alle():
            label = self._label(page, feld)
            self.assertIn("<input", label, feld)
            vor_dem_feld = label[:label.index("<input")]
            self.assertIn("Standard", vor_dem_feld, feld)

    # --- D11: das neue Feld traegt seinen Namen und erklaert den leeren Zustand ---

    def test_the_width_field_is_named_and_explains_its_empty_state(self):
        """Das Formularfeld heisst `pair_max_width` und sagt in seinem Text, dass
        leer "nur benachbarte Strikes" bedeutet.

        Beides zusammen ist die Aussage: der Name allein liesse offen, was leer
        heisst, und der Text allein haenge an keinem Feld. Ohne den Zusatz waere
        die leere Breite als "Filter aus" zu lesen - sie IST aber der schnelle
        Modus von vorher (Nachbarschaft).
        """
        page = self._page()
        form = page[page.index("<form"):page.index("</form>")]
        treffer = re.findall(r'<input[^>]*name="pair_max_width"[^>]*>', form)
        self.assertEqual(len(treffer), 1)
        self.assertIn('type="number"', treffer[0])
        label = self._label(page, self.BREITE)
        self.assertIn("leer = nur benachbarte Strikes", label)
        self.assertIn("gesetzt = alle Paare innerhalb dieser Breite", label)


class TestErrorBehaviour(unittest.TestCase):
    """D6: error text, retained form input, the sort link, escaping.

    Each method is one finding of the external review (2026-10-13), measured
    before and after the fix.
    """

    def _client(self):
        # D7: _WIDE - with _LOOSE only the pair of make() survives and the sort
        # test below could not tell two orders apart (measured: 1 row for both).
        return create_app(quote_adapter=_MockAdapter(make() + make_verticals()),
                          screener=OptionScreener(_WIDE), now_fn=_clock()).test_client()

    def _page(self, query):
        """Die Seite, entschluesselt - Meldungen enthalten "&gt;".

        Die Maskierungs-Kanarie holt sich die Antwort bewusst roh.
        """
        antwort = self._client().get("/?ticker=AAPL&expiration=2026-10-16&" + query)
        return html.unescape(antwort.get_data(as_text=True))

    @staticmethod
    def _feld(html_out, name):
        treffer = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html_out)
        return None if treffer is None else treffer.group(1)

    @staticmethod
    def _spread_reihenfolge(html_out):
        """Die erste Zelle jeder Spread-Zeile, in Seitenreihenfolge."""
        return re.findall(r'<td title="[^"]+ / [^"]+">([^<]+)</td>', html_out)

    def test_every_violation_is_named_in_german_and_with_its_value(self):
        page = self._page("min_oi=-5&min_volume=-1")
        self.assertIn("min_oi: muss ≥ 0 sein (ist: -5).", page)
        self.assertIn("min_volume: muss ≥ 0 sein (ist: -1).", page)
        self.assertNotIn("ScreenerCriteria invalid", page)

    def test_the_contradiction_names_both_values(self):
        page = self._page("dte_min=45&dte_max=7")
        self.assertIn("dte_min: muss ≤ dte_max sein (ist: 45 > 7).", page)

    def test_the_size_filters_are_checked_like_the_rest(self):
        # min_bid_size/min_ask_size sind int-Felder - der Parser liefert -3, nicht -3.0
        self.assertIn("min_bid_size: muss ≥ 0 sein (ist: -3).",
                      self._page("min_bid_size=-3"))

    def test_the_form_keeps_what_was_typed(self):
        page = self._page("min_oi=-5&dte_max=30&weight_oi=0.3")
        self.assertEqual("-5", self._feld(page, "min_oi"))
        self.assertEqual("30", self._feld(page, "dte_max"))
        self.assertEqual("0.3", self._feld(page, "weight_oi"))

    def test_a_cleared_field_falls_back_to_its_default(self):
        # "leer = Standard" - ein geleertes Feld darf nicht leer stehen bleiben,
        # sonst sucht der Bediener einen Wert, den die Pruefung gar nicht nutzt
        self.assertEqual("500", self._feld(self._page("min_oi="), "min_oi"))
        self.assertEqual("7", self._feld(self._page("dte_min="), "dte_min"))

    def test_a_zero_is_kept_and_not_read_as_cleared(self):
        self.assertEqual("0", self._feld(self._page("min_oi=0"), "min_oi"))

    def test_the_sort_link_does_not_warn_any_more(self):
        page = self._page("sort=rendite_desc")
        self.assertNotIn("Unbekannte Parameter", page)

    def test_a_really_unknown_parameter_is_still_named(self):
        page = self._page("quatsch=1")
        self.assertIn("Unbekannte Parameter ignoriert: quatsch", page)
        # die Liste der bedienbaren Parameter muss sort nennen - sonst waere die
        # Warnung fuer den eigenen Sortierlink nur halb behoben
        # die Liste der bedienbaren Parameter muss sort nennen - ein blosses
        # "sort" faende sich auch in den Sortierlinks der Seite
        self.assertIn("ignore_quote_age, sort", page)

    def test_the_sort_link_still_orders_the_rows(self):
        nach_rendite = self._spread_reihenfolge(self._page("sort=rendite_desc"))
        nach_be = self._spread_reihenfolge(self._page("sort=be_asc"))
        self.assertTrue(nach_rendite, "keine Spread-Zeilen gefunden")
        self.assertNotEqual(nach_rendite, nach_be, "sort= wirkt nicht mehr")

    def test_a_script_tag_in_a_field_stays_text(self):
        roh = self._client().get(
            "/?ticker=AAPL&expiration=2026-10-16&"
            "min_oi=%22%3E%3Cscript%3Ealert(1)%3C%2Fscript%3E").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", roh)
        self.assertIn("&lt;script&gt;", roh, "der Wert wurde nicht maskiert")

class TestApiErrorContract(unittest.TestCase):
    """D9b: /api/screen answers JSON for its two failure modes.

    Before the fix the route called the broker with no net: a valid but
    unknown symbol reached the adapter's 404, CboeNotFoundError escaped, and
    the framework answered HTTP 500 with an HTML page - measured live against
    the real CBOE adapter. Every consumer that parses the body broke.

    The contract: an unknown symbol is the caller's mistake (404), a feed
    outage is nobody's (503), both JSON with the key "error". Status, content
    type and the body key are pinned TOGETHER on purpose: an assertion on the
    absence of a word is green on a broken body (get_json() returns None for an
    HTML page), which is exactly how the old failure stayed invisible.
    """

    class _UnknownSymbol:
        """Lists a real date, then fails the chain lookup: symbol unknown."""

        def get_expiration_dates(self, underlying_asset=None):
            return ["2026-10-16"]

        def get_options(self, underlying_asset=None, expiration_date=None):
            raise CboeNotFoundError("No asset for ZZZZZZ")

    class _FeedDown:
        """The provider is unreachable: nobody traded, so this is a 503."""

        def get_expiration_dates(self, underlying_asset=None):
            return ["2026-10-16"]

        def get_options(self, underlying_asset=None, expiration_date=None):
            raise CboeRequestError("CBOE feed unreachable")

    def _screen(self, adapter):
        app = server_module.create_app(quote_adapter=adapter,
                                       screener=OptionScreener(_LOOSE),
                                       now_fn=_clock())
        app.config["PROPAGATE_EXCEPTIONS"] = False
        return app.test_client().get(
            "/api/screen?ticker=ZZZZZZ&expiration=2026-10-16")

    def _assert_json_error(self, adapter, status, message):
        """Pin status AND content type AND the body key - never one alone."""
        res = self._screen(adapter)
        self.assertEqual(res.status_code, status,
                         res.get_data(as_text=True)[:200])
        self.assertTrue(res.content_type.startswith("application/json"),
                        res.content_type)
        body = res.get_json()
        self.assertIsNotNone(body, "body is not JSON: "
                             f"{res.get_data(as_text=True)[:200]!r}")
        self.assertEqual(body["error"], message)
        return body

    def test_an_unknown_symbol_answers_404_json_with_unknown_symbol(self):
        body = self._assert_json_error(self._UnknownSymbol(), 404,
                                       "unknown symbol")
        self.assertEqual(body["ticker"], "ZZZZZZ")

    def test_a_feed_outage_answers_503_json_with_market_data_unavailable(self):
        self._assert_json_error(self._FeedDown(), 503,
                                "market data unavailable")


if __name__ == "__main__":
    unittest.main()
