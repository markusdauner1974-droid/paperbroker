"""Offline tests for the Phase-5 web display (paperbroker/server.py).

Flask test client + mock quote chain: no network, deterministic.
Covers the debate-judge requirements: input validation (no path
injection into the CBOE URL), trend failure != flat, freshness lamp,
healthz liveness without external calls.
"""
import html
import json
import os
import pathlib
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
    CboeUnusableChainError,
)
from paperbroker.assets import asset_factory
from paperbroker.screener import OptionScreener, ScreenerCriteria, TrendSignal, VerticalPair
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

    def _json(self, quotes, criteria=None):
        return self._app(quotes, criteria).test_client().get(
            "/api/screen",
            query_string={"ticker": "SPY", "expiration": "2026-10-16"}
        ).get_json()

    def test_two_bounds_of_one_family_share_one_real_example(self):
        """D9a/E7 (a): the grouping still collapses a family - now it names a
        real member as the example instead of the numbers-masked pattern.

        The page block that used to render these groups is gone with the old
        screen; the grouping itself lives on in ``dropped_reasons``, so this
        case is pinned on the answer that still carries it.
        """
        chain = [
            quote("AAPL261016C00240000", bid=5.00, ask=5.10, iv=28.0, oi=8000),  # candidate
            quote("AAPL261016C00270000", bid=1.20, ask=1.30, iv=30.0, oi=200),  # OI 200 < 500
            quote("AAPL261016C00271000", bid=1.20, ask=1.30, iv=30.0, oi=40),   # OI 40 < 500
        ]
        gruppen = self._json(chain)["dropped_reasons"]
        self.assertEqual(gruppen, [{"reason": "OI 200 < 500", "count": 2}])
        self.assertIn(gruppen[0]["reason"], {"OI 200 < 500", "OI 40 < 500"})  # a real member
        self.assertNotIn("#", gruppen[0]["reason"])                          # not the pattern

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


class TestCriteriaPanel(unittest.TestCase):
    """Ausbau-Slice 2 (A): the filter criteria are settable through the request.

    Six fields plus the switch `ignore_quote_age`. The criteria of the app the
    request runs against stay the BASE: only values the request really sets
    override them (the picker tests inject their own criteria).
    0 is a valid, sharp age bound - the switch is the only way to turn the
    guard off. The old form that carried these fields is gone; the request path
    and the criteria echo are what is left to pin.
    """

    def _app(self, quotes=None, criteria=None):
        app = create_app(
            quote_adapter=_MockAdapter(make() if quotes is None else quotes),
            screener=None if criteria is None else OptionScreener(criteria),
            now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _request(self, params, quotes=None, criteria=None, path="/"):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params)
        return self._app(quotes, criteria).test_client().get(path, query_string=query)

    def _json(self, params, quotes=None, criteria=None):
        return self._request(params, criteria=criteria, quotes=quotes,
                             path="/api/screen").get_json()

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

    # --- contradictions and bad values are errors, not silence ---

    def test_contradicting_criteria_are_reported(self):
        res = self._request({"dte_min": "45", "dte_max": "7"}, path="/api/screen")
        self.assertEqual(res.status_code, 400)
        self.assertIn("dte_min: muss ≤ dte_max sein (ist: 45 > 7).",
                      res.get_json()["error"])

    def test_bad_values_are_reported_readably(self):
        for params, needle in (({"min_oi": "abc"}, "keine ganze Zahl"),
                               ({"min_volume": "-5"},
                                "min_volume: muss ≥ 0 sein (ist: -5)."),
                               ({"max_spread_pct": "nan"}, "endliche Zahl")):
            res = self._request(params, path="/api/screen")
            self.assertEqual(res.status_code, 400, params)
            self.assertIn(needle, res.get_json()["error"], params)

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
        # The age mode is the 23rd criterion and travels with the object: the
        # echo of the criteria that REALLY computed, not a template copy.
        self.assertEqual(d["criteria"]["data_mode"], "fresh")
        # the pair search mode is the 24th criterion (None = derived from the width)
        self.assertIsNone(d["criteria"]["pair_mode"])
        self.assertEqual(len(d["criteria"]), 24)
        picker = self._app().test_client().get(
            "/api/screen", query_string={"ticker": "SPY"}).get_json()
        self.assertNotIn("criteria", picker)                     # no picker echo
        # the request criteria must reach the PICKER too (B-14: one source):
        # dte_min=45 leaves only the 45-dte date of the fixture chain
        narrow = self._app().test_client().get(
            "/api/screen", query_string={"ticker": "SPY", "dte_min": "45"}).get_json()
        self.assertEqual(narrow["listed_dates"], ["2026-11-21"])

    def test_the_position_fields_are_settable_through_the_request(self):
        """D12: die vier Positionsfelder stehen in den Kriterien-Feldlisten des
        Servers (`_CRITERIA_FIELDS`). Fehlten sie dort, gaelten sie als
        UNBEKANNTE Parameter und der Wert verpuffte - der Nutzer stellt eine
        Positionsgroesse ein, die die Rechnung nie sieht.
        """
        werte = {"multiplier": "10", "quantity": "3", "fees": "1.5",
                 "budget": "500"}
        d = self._json(werte)
        self.assertEqual(d["criteria"]["multiplier"], 10)
        self.assertEqual(d["criteria"]["quantity"], 3)
        self.assertEqual(d["criteria"]["fees"], 1.5)
        self.assertEqual(d["criteria"]["budget"], 500.0)

class TestThePositionFieldsInTheRequestPath(unittest.TestCase):
    """D16 request path: the position size is checked where it enters.

    The browser bounds (min="1"/min="0") of the old form were convenience, not
    a check - the criteria check in validate() is the one that decides, and it
    is reachable through /api/screen. The JSON status and body below are
    measured, not guessed: multiplier=0 is a client error (400).
    """

    def _app(self, quotes=None, criteria=None):
        app = create_app(
            quote_adapter=_MockAdapter(make() if quotes is None else quotes),
            screener=None if criteria is None else OptionScreener(criteria),
            now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _request(self, params, quotes=None, criteria=None, path="/"):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params)
        return self._app(quotes, criteria).test_client().get(path,
                                                             query_string=query)

    def test_the_json_route_reports_multiplier_zero_with_status_and_body(self):
        # measured: 400, body {"error": "multiplier: ... (ist: 0)."} - abort()
        # runs through the JSON error handler, not into the HTML page
        res = self._request({"multiplier": "0"}, path="/api/screen")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json(),
                         {"error": "multiplier: muss eine positive ganze "
                                   "Zahl sein (ist: 0)."})

    def test_multiplier_100_runs_normally_on_the_api(self):
        res = self._request({"multiplier": "100"}, path="/api/screen")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["criteria"]["multiplier"], 100)

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

    # --- the guards that already existed, now reachable from the request ---

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

    # --- the "empty = default" rule ----------------------------------------

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

    def _json(self, quotes, params=None):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params or {})
        return self._app(quotes).test_client().get(
            "/api/screen", query_string=query).get_json()

    def test_the_lamp_turns_red_with_the_guard_switched_off(self):
        # the traffic light itself is only carried by the answer now: the page
        # that used to paint it is gone with the old screen.
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
        self.assertEqual(self._json(self._kette("2026-10-07 14:35:30"))["freshness"], "green")

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

    def test_the_api_carries_the_net_fields_for_every_vertical_row(self):
        """D18: every vertical row of the API names the five net numbers, all
        from the SAME cost assumption (multiplier x quantity x debit + fees).
        With a size set (the default 100 x 1) none of them may be None - a
        None here would read as "no costs" instead of "not computed"."""
        data = self._json()
        self.assertEqual([v["symbol"] for v in data["verticals"]],
                         ["AMZN261016C00260000/AMZN261016C00262500",
                          "TSLA261016C00290000/TSLA261016C00292500"])
        for v in data["verticals"]:
            for key in ("net_max_loss", "net_max_profit", "net_breakeven",
                        "net_return_pct", "profitable_after_costs"):
                self.assertIn(key, v, v["symbol"])
                self.assertIsNotNone(v[key], (v["symbol"], key))
        # AMZN 260/262.5 at the default 100 x 1 with 0.0 fees, measured on the
        # built tree: the same position the position_debit names, after costs
        v = data["verticals"][0]
        self.assertEqual(v["position_debit"], 70.0)
        self.assertEqual(v["net_max_loss"], 70.0)
        self.assertEqual(v["net_max_profit"], 180.0)    # (2.5 - 0.7) * 100 - 0
        self.assertEqual(v["net_breakeven"], 260.7)     # 260.0 + 0.7 + 0 / 100
        self.assertEqual(v["net_return_pct"], 257.14)   # 180.0 / 70.0 * 100
        self.assertIs(v["profitable_after_costs"], True)

    def test_a_pair_that_loses_after_costs_leaves_the_list_with_its_reason(self):
        """D18 end to end: with 500.00 USD fees every fixture pair's best case
        turns negative, so `verticals` empties and the drop list carries the
        cost reason. The five pairs share ONE normalized reason key, so they
        arrive as a single group; its example is the lexicographically smallest
        raw text (the one-sided MSFT pair: debit 0.50 x 100 + 500 = 550 paid,
        -300 left). Every entry of the group must name the cost, never a leg
        rule."""
        data = self._json(params={"multiplier": "100", "quantity": "1",
                                  "fees": "500"})
        self.assertEqual(data["verticals"], [])
        self.assertEqual(data["criteria"]["fees"], 500.0)
        self.assertEqual(len(data["vertical_drops"]), 1)
        gruppe = data["vertical_drops"][0]
        self.assertEqual(gruppe["count"], 5)
        self.assertEqual(gruppe["reason"], "Nach Kosten nicht gewinnfähig: "
                                           "netto -300.00 USD (Einsatz 550.00 USD)")

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

    def test_the_ratio_is_carried_unrounded_in_the_json(self):
        """D17 (finding 5b): debit 0.01 over a width of 5.00 is a ratio of 0.002.

        The old page scaled and rounded it for its cell; that cell is gone. What
        is left to pin is that the answer carries the raw fraction instead of a
        pre-rounded 0.00 - a consumer that wants percent scales it itself.
        """
        kette = [
            quote("SPY261016C00100000", bid=1.00, ask=1.01,
                  iv=30.0, oi=1500, volume=300),
            quote("SPY261016C00105000", bid=1.00, ask=1.05,
                  iv=30.0, oi=1500, volume=300),
        ]
        v = self._json(quotes=kette)["verticals"][0]
        self.assertEqual((v["strike_width"], v["net_debit"]), (5.00, 0.01))
        self.assertEqual(v["debit_width_ratio"], 0.002)
        self.assertNotEqual(v["debit_width_ratio"], 0.0)

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
            antwort = self._client().get("/api/screen?ticker=AAPL&expiration=2026-10-16")
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

    def test_json_names_the_trend_and_its_reason(self):
        import unittest.mock as mock
        signal = TrendSignal(symbol="AAPL", direction="up")
        with mock.patch.object(server_module, "_get_trend_provider",
                               return_value=_FakeProvider(signal)):
            data = self._client().get(
                "/api/screen?ticker=AAPL&expiration=2026-10-16").get_json()
        json_reasons = [r for row in data["results"] for r in row["reasons"] if "Trend" in r]
        self.assertTrue(json_reasons, "kein Trend-Grund im JSON")
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

    The table that rendered these numbers went with the old screen; the two
    cases left here pin the arithmetic and the order of the answer itself.
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

    def _json(self, quotes=None):
        return self._app(quotes).test_client().get(
            "/api/screen", query_string={"ticker": "AAPL", "expiration": "2026-10-16"}).get_json()

    def test_the_api_keeps_the_chain_order(self):
        # die Entscheidung selbst: die Liste kommt in Kettenreihenfolge, das
        # Sortieren war eine Eigenschaft der entfernten Seite
        self.assertEqual([v["symbol"][:4] for v in self._json()["verticals"]],
                         ["AMZN", "TSLA"])

    # --- die Faelle, die die Mock-Kette nicht triggert (Restrisiko des Urteils) ---

    def test_a_spread_without_loss_never_reaches_the_list(self):
        # GEMESSEN: die Bein-Regel sortiert `debit <= 0` schon aus, ein Spread
        # ohne Verlust kann die Liste also gar nicht erreichen. Der Schutz in
        # der Anzeige wird deshalb direkt geprueft - nicht ueber die Seite.
        # D19: der Vorfilter liest die Laufzeit jetzt ueber die Uhr, ein
        # fehlender Aufruf (`_clock` statt `_clock()`) waere bis dahin nie
        # ausgewertet worden. Der Clock-Wert ist hier derselbe wie der
        # gespeicherte (9 Tage) - die Aussage bleibt unveraendert.
        kette = [quote("AAPL261016C00250000", bid=0.45, ask=0.50, iv=30.0, oi=1500),
                 quote("AAPL261016C00252500", bid=0.50, ask=0.55, iv=30.0, oi=1500)]
        behalten, verworfen = server_module.vertical_spreads(kette, _LOOSE, now_fn=_clock())
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


class TheStrategyIsTheChoice(unittest.TestCase):
    """L: die Wahl ist eine Strategie, keine Kettenseite.

    Bis D14 fragte das Formular nach "Calls" oder "Puts" - einer SEITE der Kette.
    Das sagt nicht, welches Bein gekauft wird. Die Wahl traegt jetzt zwei stabile
    Kennungen, und Rechnung und JSON folgen derselben Wahl; ``option_type=call|put``
    bleibt als alte Adresse gueltig. Das Formular ist mit der alten Seite
    entfallen - gepinnt ist die Antwort, die die Wahl ausfuehrt.

    Die Kette traegt BEIDE Seiten und MEHR Put- als Call-Paare, damit der
    Vergleich der beiden Strategien ein echter Vergleich bleibt.
    """

    @staticmethod
    def _quotes():
        return [
            # Calls: ein benachbartes Paar (100 / 105)
            quote("SPY261016C00100000", bid=2.40, ask=2.50, iv=30.0, oi=1500, volume=300),
            quote("SPY261016C00105000", bid=1.00, ask=1.20, iv=30.0, oi=1500, volume=300),
            # Puts: drei benachbarte Paare (100/105, 105/110, 110/115). Bei Puts ist
            # der HOEHERE Strike die lange Seite, der Debit ask(oben) - bid(unten)
            # liegt also je Paar in (0, 5) und passiert die Paar-Regel.
            quote("SPY261016P00100000", bid=1.00, ask=1.20, iv=30.0, oi=1500, volume=300),
            quote("SPY261016P00105000", bid=2.40, ask=2.60, iv=30.0, oi=1500, volume=300),
            quote("SPY261016P00110000", bid=3.50, ask=3.70, iv=30.0, oi=1500, volume=300),
            quote("SPY261016P00115000", bid=4.50, ask=4.70, iv=30.0, oi=1500, volume=300),
        ]

    def _client(self):
        adapter = _MockAdapter(self._quotes())
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(_WIDE), now_fn=_clock())
        app.config["TESTING"] = True
        return app.test_client()

    def _query(self, params=None):
        query = {"ticker": "SPY", "expiration": "2026-10-16"}
        query.update(params or {})
        return query

    def _json(self, params=None):
        return self._client().get(
            "/api/screen", query_string=self._query(params)).get_json()

    # --- Widerspruch und unbekannte Werte ---

    def test_a_broken_legacy_alias_never_replaces_a_valid_strategy(self):
        """Kody-Fund (PR #72): `?strategy=bear_put_debit&option_type=bogus` zeigte
        still den Bull Call.

        Die Anfrage ist nicht erfuellbar - die alte Adresse ist unbekannt, die
        Uebereinstimmung also unbeweisbar. Die API lehnt dieselbe Anfrage mit 400
        ab, statt still die Voreinstellung zu rechnen.
        """
        params = {"strategy": "bear_put_debit", "option_type": "bogus"}
        # die API lehnt die unerfuellbare Anfrage ab und rechnet nichts
        antwort = self._client().get("/api/screen", query_string=self._query(params))
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"], "unknown spread type")

    # --- das JSON nennt beides ---

    def test_the_json_names_the_strategy_and_the_side_it_used(self):
        for params, strategie, seite in (
                (None, "bull_call_debit", "call"),
                ({"strategy": "bear_put_debit"}, "bear_put_debit", "put"),
                ({"option_type": "put"}, "bear_put_debit", "put")):
            daten = self._json(params)
            self.assertEqual(daten["strategy"], strategie, params)
            self.assertEqual(daten["spread_type"], seite, params)
            self.assertEqual(daten["strategy_label"],
                             "Bear Put Spread (Debit)" if seite == "put"
                             else "Bull Call Spread (Debit)", params)

    # --- die zwei Credit-Strategien in derselben Antwort ----------------

    def test_the_json_carries_both_kinds_without_mixing_them(self):
        credit = self._json({"strategy": "bull_put_credit"})
        self.assertEqual(credit["strategy"], "bull_put_credit")
        self.assertEqual(credit["strategy_label"], "Bull Put Spread (Credit)")
        self.assertEqual(credit["spread_type"], "put")
        self.assertEqual(len(credit["verticals"]), 3)
        v = credit["verticals"][0]                  # P100/P105
        self.assertEqual(v["strategy_kind"], "credit")
        self.assertEqual(v["net_credit"], 1.20)
        self.assertIsNone(v["net_debit"])
        self.assertIsNone(v["position_debit"])
        self.assertEqual(v["risk_budget"], 380.0)
        self.assertEqual(v["net_max_loss"], 380.0)
        self.assertEqual(v["net_max_profit"], 120.0)
        self.assertEqual(v["budget_fits"], None)   # keine Grenze gesetzt
        # die Debit-Spalte der alten Strategie bleibt unveraendert
        debit = self._json({"strategy": "bull_call_debit"})["verticals"][0]
        self.assertEqual(debit["net_debit"], 1.50)
        self.assertIsNone(debit["net_credit"])
        self.assertEqual(debit["position_debit"], 150.0)
        self.assertEqual(debit["risk_budget"], 150.0)
        self.assertIs(debit["budget_fits"], None)

    def test_the_budget_bounds_the_credit_risk_budget_not_the_credit(self):
        data = self._json({"strategy": "bear_call_credit", "budget": "379"})
        self.assertEqual(data["verticals"], [])
        self.assertEqual([g["reason"] for g in data["vertical_drops"]],
                         ["Risikobudget 380.00 USD > Budget 379.00 USD"])
        data = self._json({"strategy": "bear_call_credit", "budget": "380"})
        self.assertEqual(len(data["verticals"]), 1)
        self.assertIs(data["verticals"][0]["budget_fits"], True)

    # --- die Struktur, die alle anderen Tests parsen ---

class TheColoursCarryEnoughContrast(unittest.TestCase):
    """D2c, CoachBen-Schnitt: jede Schriftfarbe traegt 4,5:1, jede Bedienkante 3,0:1.

    Gemessen mit der Kontrastrechnung unten (WCAG 2.1, relative Leuchtdichte)
    auf der neuen Seitenflaeche #091217:

        Text               #fafafa                 = 18,12:1
        Sekundaertext      rgba(250,250,250,.62)   =  7,35:1
        Mikro-Label        rgba(250,250,250,.48)   =  4,81:1
        Akzent / Link      #e05534                 =  4,97:1
        Knoptschrift       #0b0704 auf #e05534     =  5,27:1
        Bedienkante        rgba(250,250,250,.36)   =  3,24:1
        Warnung            #d9a441                 =  8,41:1

    Der schwaechste Wert der ersten Fassung war das Mikro-Label mit .42 Deckkraft
    = 3,94:1, also UNTER der Grenze, die diese Klasse selbst zieht. Auf .48
    angehoben (4,81:1). Die dekorative Haarlinie (--hair, .20 -> 1,79:1) ist
    bewusst keine Schrift und keine Bedienkante.
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

    SEITE = "#091217"
    KNOPF = "#e05534"
    KNOPFSCHRIFT = "#0b0704"
    # --- Lesehilfen: Regeln zeichengenau, Leerraum egal ------------------

    @staticmethod
    def _css(page):
        """Der <style>-Block mit zusammengefasstem Leerraum.

        Gepinnt wird der Inhalt der Regel, nicht die Einrueckung: ein
        umformatierter <style>-Block soll die Zusicherung nicht brechen.
        """
        return re.sub(r"\s+", " ", page[page.index("<style>"):page.index("</style>")])

    def _variablen(self, page):
        """{--name: wert} aus dem :root-Block der Seite."""
        css = self._css(page)
        anfang = css.index(":root{")
        return dict(re.findall(r"(--[a-z-]+):([^;}]+)",
                               css[anfang:css.index("}", anfang)]))

    def _farbe(self, wert):
        """var()/rgba()/#hex -> deckende Farbe, gemischt auf die Seitenflaeche.

        Die Palette der Seite ist teilweise durchscheinend (--muted, --faint,
        --edge). Der Kontrast einer Schrift haengt davon ab, was am Ende
        WIRKLICH auf der Seitenflaeche steht - deshalb wird gemischt und nicht
        der Alphawert geprueft.
        """
        wert = wert.strip()
        basis = [int(self.SEITE[i:i + 2], 16) for i in (1, 3, 5)]
        treffer = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", wert)
        if not treffer:
            return wert
        werte = [int(treffer.group(i)) for i in (1, 2, 3)]
        alpha = float(treffer.group(4)) if treffer.group(4) else 1.0
        gemischt = [round(v * alpha + b * (1 - alpha)) for v, b in zip(werte, basis, strict=True)]
        return "#{:02x}{:02x}{:02x}".format(*gemischt)

    def _farbe_von(self, page, name):
        """Der aufgeloeste Wert einer CSS-Variablen."""
        return self._farbe(self._variablen(page)[name])

    # --- die Zusicherungen ----------------------------------------------

    def test_the_folded_blocks_carry_a_brighter_marker(self):
        # der Marker war der schwaechste Text der Seite (4,01:1); jetzt traegt
        # er den Akzent (4,97:1) und ist damit auch als Bedienelement lesbar
        page = self._page()
        self.assertIn('.dropped-summary summary::before{content:"\\203A ";color:var(--primary)}',
                      self._css(page))
        self.assertGreaterEqual(round(self._kontrast(self._farbe_von(page, "--primary"),
                                                     self.SEITE), 2), 4.5)

    def test_the_four_measured_pairs_clear_their_threshold(self):
        page = self._page()
        for vorne, hinten, grenze, wo in (
                (self._farbe_von(page, "--primary"), self.SEITE, 4.5, "Akzent"),
                (self._farbe_von(page, "--muted"), self.SEITE, 4.5, "Sekundaertext"),
                (self._farbe_von(page, "--edge"), self.SEITE, 3.0, "Bedienkante"),
                (self._farbe_von(page, "--faint"), self.SEITE, 4.5, "Mikro-Label")):
            self.assertGreaterEqual(round(self._kontrast(vorne, hinten), 2), grenze,
                                    f"{wo}: {vorne} auf {hinten}")

    def test_every_text_colour_of_the_page_clears_four_and_a_half(self):
        """Die Klasse des Fehlers, nicht nur der eine Fall.

        Jede Schriftfarbe, die die Seite selbst setzt, muss 4,5:1 gegen die
        Seitenflaeche tragen. var() wird aufgeloest, rgba() auf die Flaeche
        gemischt; Flaechen und rein zierende Rahmen duerfen dunkler sein.
        """
        page = self._page()
        css = self._css(page)
        variablen = self._variablen(page)
        farben = set()
        for roh in re.findall(r"(?<![-\w])color:\s*([^;}]+)", css):
            roh = roh.strip()
            treffer = re.match(r"var\((--[a-z-]+)\)", roh)
            if treffer:
                roh = variablen[treffer.group(1)]
            farbe = self._farbe(roh)
            if farbe == self.KNOPFSCHRIFT:
                # steht auf dem gefuellten Knopf, nicht auf der Seitenflaeche -
                # dieses Paar prueft test_the_button_text_clears_four_and_a_half
                continue
            farben.add(farbe)
        self.assertGreaterEqual(len(farben), 5)
        for farbe in sorted(farben):
            self.assertGreaterEqual(
                round(self._kontrast(farbe, self.SEITE), 2), 4.5,
                f"Schriftfarbe {farbe} traegt nur {self._kontrast(farbe, self.SEITE):.2f}:1")

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

    class _UnusableChain:
        """The chain arrived and was read, but no contract of it could be built.

        Not an unknown symbol: the source knows the ticker and answered. It is
        a source-side failure (503) and must say so in its own words.
        """

        def get_expiration_dates(self, underlying_asset=None):
            return ["2026-10-16"]

        def get_options(self, underlying_asset=None, expiration_date=None):
            raise CboeUnusableChainError(
                "all 4314 contracts of ZZZZZZ unusable", attempts=2)

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

    def test_an_unusable_chain_is_not_a_404(self):
        """A delivered chain that yielded no contract is no unknown symbol.

        Before, the adapter raised CboeNotFoundError for it, so this route
        answered 404 "unknown symbol" for a ticker the source had just
        answered for. Status, content type and body text are pinned together -
        an assertion on a missing word alone also passes on an HTML page.
        """
        body = self._assert_json_error(self._UnusableChain(), 503,
                                       "chain unreadable")
        self.assertEqual(body["ticker"], "ZZZZZZ")
        self.assertNotEqual(body["error"], "unknown symbol")

    def test_the_unusable_chain_answer_stays_a_source_failure(self):
        """503, not 404: nothing about the request was wrong."""
        res = self._screen(self._UnusableChain())
        self.assertEqual(res.status_code, 503,
                         res.get_data(as_text=True)[:200])
        self.assertNotIn("unknown symbol", res.get_data(as_text=True))
        # the source detail (contract counts) stays in the log, not the body
        self.assertNotIn("4314", res.get_data(as_text=True))

    def test_an_unknown_symbol_stays_a_404_next_to_the_unusable_one(self):
        """The two answers must stay distinguishable, not both become 503."""
        unbekannt = self._screen(self._UnknownSymbol())
        unbrauchbar = self._screen(self._UnusableChain())
        self.assertEqual(unbekannt.status_code, 404)
        self.assertEqual(unbrauchbar.status_code, 503)
        self.assertNotEqual(unbekannt.get_json()["error"],
                            unbrauchbar.get_json()["error"])

    def test_an_unknown_strategy_answers_400_json_with_the_two_ids(self):
        """L: eine unbekannte Strategie ist der Fehler des Aufrufers. Status,
        Inhaltstyp, Koerperschluessel und die erlaubten Kennungen zusammen -
        eine blosse get_json()-Pruefung bestuende auch einen HTML-Koerper.
        """
        app = server_module.create_app(quote_adapter=_MockAdapter(make_verticals()),
                                       screener=OptionScreener(_LOOSE), now_fn=_clock())
        app.config["PROPAGATE_EXCEPTIONS"] = False
        res = app.test_client().get(
            "/api/screen?ticker=AAPL&expiration=2026-10-16&strategy=bogus")
        self.assertEqual(res.status_code, 400, res.get_data(as_text=True)[:200])
        self.assertTrue(res.content_type.startswith("application/json"), res.content_type)
        body = res.get_json()
        self.assertIsNotNone(body, "body is not JSON: "
                             f"{res.get_data(as_text=True)[:200]!r}")
        self.assertEqual(body["error"], "unknown strategy")
        self.assertEqual(body["allowed"],
                         ["bull_call_debit", "bear_put_debit",
                          "bull_put_credit", "bear_call_credit"])

    def test_a_contradiction_answers_400_json_and_names_both_values(self):
        """L: Strategie und alte Adresse widersprechen sich - die Antwort nennt
        beide und die gueltige Zuordnung, statt eine der beiden zu waehlen.
        """
        app = server_module.create_app(quote_adapter=_MockAdapter(make_verticals()),
                                       screener=OptionScreener(_LOOSE), now_fn=_clock())
        app.config["PROPAGATE_EXCEPTIONS"] = False
        res = app.test_client().get(
            "/api/screen?ticker=AAPL&expiration=2026-10-16"
            "&strategy=bear_put_debit&option_type=call")
        self.assertEqual(res.status_code, 400, res.get_data(as_text=True)[:200])
        self.assertTrue(res.content_type.startswith("application/json"), res.content_type)
        body = res.get_json()
        self.assertEqual(body["error"], "conflicting strategy and option_type")
        self.assertEqual(body["option_type_to_strategy"],
                         {"call": "bull_call_debit", "put": "bear_put_debit"})

    def test_an_unknown_spread_type_answers_400_json(self):
        """D14: ein unbekanntes `option_type` ist der Fehler des Aufrufers.

        Wie der D9b-Vertrag werden Status UND Inhaltstyp UND der Koerperschluessel
        ZUSAMMEN gepinnt: ein HTML-Koerper bestuende eine blosse get_json()-Pruefung
        still (sie liefert None), genau so blieb der alte Fehler unsichtbar.
        """
        app = server_module.create_app(quote_adapter=_MockAdapter(make_verticals()),
                                       screener=OptionScreener(_LOOSE), now_fn=_clock())
        app.config["PROPAGATE_EXCEPTIONS"] = False
        res = app.test_client().get(
            "/api/screen?ticker=AAPL&expiration=2026-10-16&option_type=bogus")
        self.assertEqual(res.status_code, 400, res.get_data(as_text=True)[:200])
        self.assertTrue(res.content_type.startswith("application/json"),
                        res.content_type)
        body = res.get_json()
        self.assertIsNotNone(body, "body is not JSON: "
                             f"{res.get_data(as_text=True)[:200]!r}")
        self.assertEqual(body["error"], "unknown spread type")
        self.assertEqual(body["allowed"], ["call", "put"])

if __name__ == "__main__":
    unittest.main()

class TheFontsComeFromThePackage(unittest.TestCase):
    """Die Seite laedt ihre Schriften aus dem eigenen Paket, nicht von Google.

    Gemessen im Browser (Chromium, 1440 px): mit den drei <link>-Tags auf
    fonts.googleapis.com brauchte die Seite drei Fremdanfragen und zeigte ohne
    Internet die Ersatzschrift. Die woff2-Dateien liegen jetzt unter
    paperbroker/static/fonts (6 Dateien, 102,9 KB) und werden von
    static/fonts.css referenziert.

    Geprueft wird die Klasse, nicht die einzelne Datei: keine Schrift-URL
    ausserhalb des Pakets, und jede im CSS genannte Datei muss es geben.
    """

    @staticmethod
    def _page():
        adapter = _MockAdapter(make())
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(ScreenerCriteria()),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app.test_client().get(
            "/", query_string={"ticker": "AAPL", "expiration": "2026-10-16"}
        ).get_data(as_text=True)

    def test_the_page_asks_no_font_host(self):
        page = self._page()
        self.assertIn('<link rel="stylesheet" href="/static/fonts.css">', page)
        for fremd in ("fonts.googleapis.com", "fonts.gstatic.com"):
            self.assertNotIn(fremd, page, fremd)

    def test_every_declared_font_file_exists(self):
        static = pathlib.Path(__file__).resolve().parents[1] / "static"
        css = (static / "fonts.css").read_text(encoding="utf-8")
        dateien = re.findall(r"url\((fonts/[^)]+\.woff2)\)", css)
        self.assertGreaterEqual(len(dateien), 3)
        for datei in sorted(set(dateien)):
            self.assertTrue((static / datei).is_file(), datei)

    def test_the_three_families_carry_the_weights_the_page_uses(self):
        static = pathlib.Path(__file__).resolve().parents[1] / "static"
        css = (static / "fonts.css").read_text(encoding="utf-8")
        seite = self._page()
        for familie in ("Oswald", "Instrument Sans", "Martian Mono"):
            self.assertIn(f"'{familie}'", css)
            self.assertIn(f'"{familie}"', seite)
        # die Gewichte, die das CSS der Seite wirklich anfordert
        for familie, gewicht in (("Oswald", "700"),
                                 ("Instrument Sans", "400"),
                                 ("Instrument Sans", "600"),
                                 ("Martian Mono", "400"),
                                 ("Martian Mono", "600")):
            block = [b for b in re.findall(r"@font-face \{(.*?)\}", css, re.S)
                     if f"'{familie}'" in b and f"font-weight: {gewicht};" in b]
            self.assertTrue(block, f"{familie} {gewicht} fehlt")

class TheAgeModeOnBothSurfaces(unittest.TestCase):
    """Both age modes, measured through /api/screen.

    One chain, two requests. The preparation mode must keep what the default
    mode drops, the answer must say which mode produced it, and the source time
    and the fetch time must stay two separate facts.
    """

    ALT = "2026-10-07 10:00:00"     # 275 min before the pinned clock (limit 30)

    def _chain(self, stamp=None):
        kette = make()
        for q in kette:
            q.quote_timestamp = self.ALT if stamp is None else stamp
        return kette

    def _app(self, criteria=None, stamp=None):
        adapter = _MockAdapter(self._chain(stamp))
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter,
                         screener=OptionScreener(criteria or _LOOSE),
                         now_fn=_clock())
        app.config["TESTING"] = True
        return app

    def _json(self, params=None, criteria=None, stamp=None):
        return self._app(criteria, stamp).test_client().get(
            "/api/screen",
            query_string={"ticker": "AAPL", "expiration": "2026-10-16", **(params or {})},
        ).get_json()

    # --- the decision itself ---

    def test_the_default_mode_drops_the_old_chain(self):
        body = self._json()
        self.assertEqual(0, body["candidates"])
        self.assertEqual("fresh", body["data_state"]["mode"])
        self.assertFalse(body["data_state"]["provisional"])
        self.assertGreater(body["data_state"]["counts"]["stale"], 0)

    def test_the_preparation_mode_keeps_the_same_chain(self):
        body = self._json({"data_mode": "last_available"})
        self.assertGreater(body["candidates"], 0)
        state = body["data_state"]
        self.assertEqual("last_available", state["mode"])
        self.assertEqual("Letzter verfügbarer Marktstand", state["mode_label"])
        self.assertTrue(state["provisional"])
        self.assertIn("nicht ein aktueller Kurs", state["provisional_note"])
        # the JSON echoes the mode that REALLY computed
        self.assertEqual("last_available", body["criteria"]["data_mode"])

    def test_the_mode_is_the_only_difference_between_the_two_answers(self):
        """Same chain, same filters: the mode decides, nothing else.

        The preparation mode on OLD data must find exactly what the default
        mode finds on FRESH data - otherwise the mode would change the result
        beyond the age rule.
        """
        alt = self._json()
        vorbereitung = self._json({"data_mode": "last_available"})
        frisch = self._json(stamp="2026-10-07 14:30:00")     # 5 min, default mode
        self.assertEqual(frisch["candidates"], vorbereitung["candidates"])
        self.assertNotEqual(alt["candidates"], vorbereitung["candidates"])
        self.assertEqual(
            [r["symbol"] for r in frisch["results"]],
            [r["symbol"] for r in vorbereitung["results"]],
        )

    # --- the data state: source time vs fetch time vs zone ---

    def test_the_answer_separates_source_time_from_fetch_time(self):
        state = self._json({"data_mode": "last_available"})["data_state"]
        self.assertEqual(self.ALT, state["source_timestamp_raw"])
        # the CBOE stamp names no zone: no UTC value may be invented for it
        self.assertIsNone(state["source_timestamp_utc"])
        self.assertEqual("assumed_utc", state["source_timestamp_zone"])
        self.assertFalse(state["age_proven"])
        self.assertIn("ohne Zeitzonenangabe", state["source_timestamp_zone_note"])
        # the fetch time is a real UTC instant and is reported as one
        self.assertEqual("2026-10-07T14:35:00Z", state["fetched_at_utc"])
        self.assertAlmostEqual(275.0, state["source_age_minutes"], places=1)

    def test_an_unknown_mode_is_a_client_error_not_a_silent_default(self):
        antwort = self._app().test_client().get(
            "/api/screen",
            query_string={"ticker": "AAPL", "expiration": "2026-10-16",
                          "data_mode": "irgendwas"},
        )
        self.assertEqual(400, antwort.status_code)
        self.assertIn("data_mode", antwort.get_json()["error"])

    def test_the_preparation_mode_does_not_rescue_undated_data(self):
        """No stamp is not "old data" - the mode must not admit it.

        The reason text is the one the page shows, so it is pinned verbatim.
        """
        kette = self._chain()
        for q in kette:
            q.quote_timestamp = None
        adapter = _MockAdapter(kette)
        adapter.expirations = ["2026-10-16"]
        app = create_app(quote_adapter=adapter, screener=OptionScreener(_LOOSE),
                         now_fn=_clock())
        app.config["TESTING"] = True
        body = app.test_client().get(
            "/api/screen",
            query_string={"ticker": "AAPL", "expiration": "2026-10-16",
                          "data_mode": "last_available"},
        ).get_json()
        self.assertEqual(0, body["candidates"])
        gruende = {g["reason"] for g in body["dropped_reasons"]}
        self.assertIn("Aktualität unbekannt (kein Zeitstempel)", gruende)
        # an undated contract is never dropped as "too old" - the labels stay apart
        self.assertFalse([g for g in gruende if g.startswith("veraltet")])
        state = body["data_state"]
        self.assertIsNone(state["source_timestamp_raw"])
        self.assertGreater(state["counts"]["undated"], 0)


class _CountingEarnings:
    """An earnings source that records every question the page asks it."""

    def __init__(self):
        self.calls = []

    def get_calendar_days(self, days):
        self.calls.append("get_calendar_days")
        return {"days": {}, "state": "not_configured", "error": None}

    def get_optionable_symbols(self):
        self.calls.append("get_optionable_symbols")
        return {"symbols": None, "state": "unavailable",
                "fetched_at_utc": None, "error": None}

    def get_earnings(self, ticker):
        self.calls.append("get_earnings")
        return {"earnings": {}, "cache": {}}


class TestThePageReadsNoSource(unittest.TestCase):
    def test_the_page_asks_no_source_for_what_it_no_longer_shows(self):
        # The funnel page carries neither the calendar nor the per-ticker
        # earnings panel. Building both views on every load cost three adapter
        # questions (and, with a live source, network round trips) for markup
        # the template does not contain any more.
        quelle = _CountingEarnings()
        client = create_app(quote_adapter=_MockAdapter(make()),
                            earnings_adapter=quelle, now_fn=_clock()).test_client()
        for pfad in ("/", "/?ticker=AAPL&expiration=2026-10-16"):
            with self.subTest(pfad=pfad):
                antwort = client.get(pfad)
                self.assertEqual(200, antwort.status_code)
                self.assertIn("Gap-Spread-Trichter", antwort.get_data(as_text=True))
        self.assertEqual([], quelle.calls)

    def test_the_page_still_answers_without_any_source(self):
        # No earnings adapter configured at all. The page used to survive only
        # because the "not configured" failures landed in the two views it
        # built and never read; with nobody building them it must still answer.
        app = create_app(quote_adapter=_MockAdapter(make()), now_fn=_clock())
        antwort = app.test_client().get("/")
        self.assertEqual(200, antwort.status_code)
        seite = antwort.get_data(as_text=True)
        self.assertIn("Gap-Spread-Trichter", seite)
        self.assertIn('id="gap-funnel"', seite)
