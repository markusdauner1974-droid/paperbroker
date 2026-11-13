"""Offline tests for the earnings-overview panel on ``GET /`` (Slice O).

The panel is the page's SECOND action: it runs the day scan the route already
offers with the strategies and filters the form currently shows, and prints per
ticker the hits of the chosen strategies, the best expiry, the max loss of that
best result and the state of the data.

What is pinned here is the part a browser can only show by accident:

* the form fields the page may copy are exactly the fields the route accepts -
  a new filter is picked up automatically, and the page cannot send something
  the route refuses as ``unknown_parameter``,
* the panel exists only where a day exists, with one action per day, and a day
  the source did not answer cannot be scanned at all,
* the states the requirement names keep their own words: "not checked" is not
  spelled like "no hits", "stale data" not like "nothing fits",
* the page still starts no scan on load - the expensive path stays an explicit
  click.

The rendering itself (filter, stale marking, late answers, click-through) is
exercised in a real browser; see docs/270_slice_O_earnings_overview.md.
"""

import json
import re
import unittest
from datetime import date
from pathlib import Path

import paperbroker
from paperbroker import quote_age
from paperbroker.earnings_scan import STATUSES
from paperbroker.screener import ScreenerCriteria
from paperbroker.server import (
    _CRITERIA_FIELDS,
    create_app,
    scan_view_data,
)
from paperbroker.tests.test_earnings_scan import (
    _LOOSE,
    CLOCK,
    EXP_A,
    TODAY,
    FakeChainAdapter,
    FakeEarningsAdapter,
    calendar_row,
    call_pair,
    entry_of,
    run_scan,
)

TODAY_ISO = TODAY.isoformat()

#: The panel's behaviour lives in one static file; its state table is the part
#: that must agree with the route, so the tests read the file itself.
PANEL_JS = Path(paperbroker.__file__).parent / "static" / "earnings_scan.js"


def _panel_fields():
    return scan_view_data()["fields"]


def _query_for_all_fields():
    """One plausible value per field the page may send - as the form would.

    The values are deliberately loose (a 100 % spread cap, OI 1) so the answer
    is a scan and not a filter artefact: this test is about the parameter
    CONTRACT, not about how many spreads survive.
    """
    werte = {
        "max_spread_pct": "100.0",
        "max_spread_abs": "0.5",
        "min_oi": "1",
        "min_volume": "0",
        "pair_max_width": "10",
        "min_bid_size": "1",
        "min_ask_size": "1",
        "require_oi": "1",
        "require_volume": "0",
        "iv_min": "1",
        "iv_max": "200",
        "iv_pref": "low",
        "weight_spread": "0.5",
        "weight_oi": "0.3",
        "weight_iv": "0.2",
        "multiplier": "100",
        "quantity": "1",
        "fees": "0",
        "budget": "500",
        "dte_min": "7",
        "dte_max": "45",
        "max_quote_age_min": "30",
        "ignore_quote_age": "1",
        # the age mode travels to the scan exactly like any other criterion:
        # the multi-ticker scan must be able to run in the preparation mode
        "data_mode": "last_available",
        # the search mode is a criteria field, so the scan must accept it too
        "pair_mode": "within_width",
        "strategy": "bull_call_debit",
    }
    fehlend = [name for name in _panel_fields() if name not in werte]
    if fehlend:                       # a new field reached the page without a value
        raise AssertionError(f"no fixture value for: {', '.join(fehlend)}")
    return "&".join(f"{name}={werte[name]}" for name in _panel_fields())


def _config_of(seite):
    """The panel's JSON data block - the one statement the page and the route share."""
    treffer = re.search(
        r'<script type="application/json" id="escan-config">(.*?)</script>', seite, re.S)
    if treffer is None:
        raise AssertionError("the panel carries no JSON config block")
    return json.loads(treffer.group(1))


def _client(rows=None, chains=None, earnings=None):
    rows = [calendar_row("AAPL", 3_000_000), calendar_row("MSFT", 2_000_000)] \
        if rows is None else rows
    if chains is None:
        chains = {"AAPL": call_pair("AAPL", EXP_A), "MSFT": call_pair("MSFT", EXP_A)}
    return create_app(
        quote_adapter=FakeChainAdapter(chains),
        earnings_adapter=earnings or FakeEarningsAdapter(rows),
        now_fn=CLOCK,
    ).test_client()


class TestThePanelSpeaksTheRoutesLanguage(unittest.TestCase):
    """The field list on the page and the parser in the route are one contract."""

    def test_every_field_the_page_may_send_is_accepted(self):
        antwort = _client().get(f"/api/v1/earnings-scan?day={EXP_A}&{_query_for_all_fields()}")
        self.assertEqual(antwort.status_code, 200, antwort.get_data(as_text=True)[:400])

    def test_the_field_list_is_the_criteria_table_plus_the_two_switches(self):
        felder = _panel_fields()
        self.assertEqual(felder[:len(_CRITERIA_FIELDS)], list(_CRITERIA_FIELDS))
        self.assertEqual(sorted(felder[len(_CRITERIA_FIELDS):]),
                         ["ignore_quote_age", "strategy"])

    def test_the_single_view_parameters_stay_out(self):
        # ticker/expiration/sort belong to GET / and are refused by the scan
        # route - sending them would turn every scan into a 400.
        felder = _panel_fields()
        for name in ("ticker", "expiration", "sort"):
            self.assertNotIn(name, felder)
        antwort = _client().get(f"/api/v1/earnings-scan?day={EXP_A}&ticker=AAPL")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "unknown_parameter")

    def test_the_strategy_headers_are_short_and_unique(self):
        specs = scan_view_data()["strategies"]
        self.assertEqual([s["id"] for s in specs],
                         ["bull_call_debit", "bear_put_debit",
                          "bull_put_credit", "bear_call_credit"])
        kurz = [s["short"] for s in specs]
        self.assertEqual(kurz, ["Bull Call", "Bear Put", "Bull Put", "Bear Call"])
        self.assertEqual(len(set(kurz)), len(kurz))
        # the long label stays available for the tooltip
        self.assertEqual(specs[0]["label"], "Bull Call Spread (Debit)")


class TestThePanelIsAttachedToTheDay(unittest.TestCase):
    """One action per calendar day, and only where a day can be scanned."""

    def test_there_is_one_scan_action_per_day_and_it_names_the_day(self):
        seite = _client().get("/").get_data(as_text=True)
        tage = re.findall(r'data-scan-day="([0-9-]{10})"', seite)
        # the calendar shows yesterday/today/tomorrow, all three answered live
        self.assertEqual(len(tage), 3, tage)
        self.assertIn(TODAY_ISO, tage)
        self.assertEqual(len(set(tage)), 3)

    def test_the_panel_carries_the_route_the_fields_and_the_strategies(self):
        seite = _client().get("/").get_data(as_text=True)
        self.assertIn('id="earnings-scan"', seite)
        config = _config_of(seite)
        self.assertEqual(config["api"], "/api/v1/earnings-scan")
        self.assertEqual(config["fields"], _panel_fields())
        self.assertEqual([s["id"] for s in config["strategies"]],
                         [s["id"] for s in scan_view_data()["strategies"]])
        self.assertIn('id="escan-only-hits"', seite)
        self.assertIn('id="escan-table"', seite)
        # the behaviour is one file, loaded by the page - not a second inline copy
        self.assertIn('/static/earnings_scan.js', seite)
        self.assertTrue(PANEL_JS.is_file())

    def test_a_day_the_source_did_not_answer_cannot_be_scanned(self):
        class _OnlyToday(FakeEarningsAdapter):
            """Answers today only - prev and next are unavailable, as on an outage."""

            def get_calendar_days(self, days):
                voll = super().get_calendar_days(days)
                for iso in days.values():
                    if iso != TODAY_ISO:
                        voll["days"].pop(iso, None)
                return voll

        seite = _client(earnings=_OnlyToday([calendar_row("AAPL", 3_000_000)])).get("/") \
            .get_data(as_text=True)
        knoepfe = re.findall(
            r'<button type="button" class="cal-scan" data-scan-day="([0-9-]{10})"([^>]*)>',
            seite)
        zustand = {tag: ("disabled" in rest) for tag, rest in knoepfe}
        # All three days keep a button (a reader sees the action exists), but the
        # two unanswered ones are switched off: a click could only produce a 503.
        self.assertEqual(sorted(zustand), ["2026-10-06", "2026-10-07", "2026-10-08"])
        self.assertFalse(zustand[TODAY_ISO])
        self.assertTrue(zustand["2026-10-06"])
        self.assertTrue(zustand["2026-10-08"])


    def test_the_page_still_starts_no_scan_on_load(self):
        chains = FakeChainAdapter({"AAPL": call_pair("AAPL", EXP_A)})
        earnings = FakeEarningsAdapter([calendar_row("AAPL", 3_000_000)])
        client = create_app(quote_adapter=chains, earnings_adapter=earnings,
                            now_fn=CLOCK).test_client()
        client.get("/")
        self.assertEqual(chains.calls, [])
        self.assertEqual(earnings.earnings_calls, [])


class TestTheStatesKeepTheirOwnWords(unittest.TestCase):
    """Four states a reader must not confuse, four different labels."""

    def _status_table(self):
        quelle = PANEL_JS.read_text(encoding="utf-8")
        block = re.search(r"var STATUS = \{(.*?)\n  \};", quelle, re.S)
        if block is None:
            raise AssertionError("the page has no STATUS table")
        treffer = {}
        for name, text, cls in re.findall(
                r"(\w+):\s*\{\s*text:\s*'([^']*)',\s*cls:\s*'([^']*)'\s*\}", block.group(1)):
            treffer[name] = (text, cls)
        return treffer

    def test_every_state_of_the_scan_has_a_label_and_a_colour(self):
        tabelle = self._status_table()
        self.assertEqual(sorted(tabelle), sorted(STATUSES))

    def test_the_four_named_states_are_worded_differently(self):
        tabelle = self._status_table()
        texte = [tabelle[name][0] for name in ("not_checked", "fetch_failed",
                                               "stale_data", "no_match")]
        self.assertEqual(texte, ["nicht geprüft", "Abruffehler",
                                 "Daten zu alt", "keine passenden Spreads"])
        self.assertEqual(len(set(texte)), 4)
        klassen = [tabelle[name][1] for name in ("not_checked", "fetch_failed",
                                                 "stale_data", "no_match")]
        self.assertEqual(len(set(klassen)), 4)

    def test_a_hit_is_not_painted_like_a_miss(self):
        tabelle = self._status_table()
        self.assertNotEqual(tabelle["hits"][1], tabelle["no_match"][1])
        self.assertNotEqual(tabelle["hits"][1], tabelle["not_checked"][1])


class TestTheCellsSayWhatHappened(unittest.TestCase):
    """A missing chain and a throttled chain are different events."""

    def _quelle(self):
        return PANEL_JS.read_text(encoding="utf-8")

    def test_a_deliberately_unread_chain_says_so_instead_of_kette_leer(self):
        quelle = self._quelle()
        self.assertIn("daten.chain_requested === false", quelle)
        self.assertIn("nicht geladen (Fenster verhindert)", quelle)

    def test_a_throttled_call_is_labelled_gedrosselt_not_abruffehler(self):
        quelle = self._quelle()
        self.assertIn("entry.exclusion.reason === 'throttled'", quelle)
        self.assertIn("text: 'gedrosselt'", quelle)
        # The class travels in the tooltip next to the German sentence.
        self.assertIn("entry.exclusion.reason", quelle)


class TestThePanelKeepsTheOldPromises(unittest.TestCase):
    """The calendar and the single view are untouched by the new block."""

    def test_the_calendar_symbol_click_survives(self):
        seite = _client().get("/").get_data(as_text=True)
        self.assertIn("a[data-symbol]", seite)

    def test_the_page_still_renders_without_a_calendar(self):
        antwort = _client().get("/?no_calendar")
        self.assertEqual(antwort.status_code, 200)
        seite = antwort.get_data(as_text=True)
        self.assertNotIn('id="earnings-scan"', seite)
        self.assertIn('id="screener-form"', seite)


class TestTheClockOfThePage(unittest.TestCase):
    """The day the button sends is the day the calendar shows, not "now"."""

    def test_the_action_dates_are_the_calendar_dates(self):
        # The three days around the pinned clock, spelled out - not a second
        # computation of the same offset, which would agree with a wrong page.
        seite = _client().get("/").get_data(as_text=True)
        tage = re.findall(r'data-scan-day="([0-9-]{10})"', seite)
        self.assertEqual(sorted(tage), ["2026-10-06", "2026-10-07", "2026-10-08"])



class TestTheAgeModeInTheMultiTickerScan(unittest.TestCase):
    """The day scan reads the SAME age rule as the single-ticker view.

    One day, two tickers, one old chain. The default mode finds nothing and
    says why; the preparation mode finds the same pairs, marks the hits
    provisional and returns the same ``data_state`` block the single view
    returns - one rule, one block, three surfaces.
    """

    ALT = "2026-10-07 10:00:00"          # 275 min before the pinned clock

    def _chains(self, stamp=ALT):
        return {"AAPL": call_pair("AAPL", EXP_A, quote_timestamp=stamp),
                "MSFT": call_pair("MSFT", EXP_A, quote_timestamp=stamp)}

    def _rows(self):
        return [calendar_row("AAPL", 3_000_000), calendar_row("MSFT", 2_000_000)]

    def _body(self, mode=None, stamp=ALT):
        criteria = _LOOSE
        if mode is not None:
            criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                        data_mode=mode)
        return run_scan(self._chains(stamp), self._rows(), criteria=criteria)

    def test_the_default_mode_reports_stale_data_instead_of_hits(self):
        body = self._body()
        aapl = entry_of(body, "AAPL")
        self.assertEqual("stale_data", aapl["strategies"]["bull_call_debit"]["status"])
        self.assertEqual(0, aapl["strategies"]["bull_call_debit"]["hits"])
        self.assertEqual("stale", aapl["data"]["freshness"])
        self.assertEqual("assumed_utc", aapl["data"]["chain_quote_zone"])
        self.assertFalse(aapl["data"]["chain_quote_age_proven"])
        self.assertIsNone(aapl["data"]["chain_quote_timestamp_utc"])
        self.assertFalse(body["data_state"]["provisional"])

    def test_the_preparation_mode_finds_the_same_pairs_and_marks_them(self):
        body = self._body(quote_age.MODE_LAST_AVAILABLE)
        aapl = entry_of(body, "AAPL")
        bull = aapl["strategies"]["bull_call_debit"]
        self.assertEqual("hits", bull["status"])
        self.assertGreater(bull["hits"], 0)
        self.assertTrue(bull["provisional"])          # old data, clearly labelled
        # the freshness WORD does not change with the mode: the quotes are old,
        # the mode only decides whether they may be used
        self.assertEqual("stale", aapl["data"]["freshness"])
        state = body["data_state"]
        self.assertEqual(quote_age.MODE_LAST_AVAILABLE, state["mode"])
        self.assertTrue(state["provisional"])
        self.assertEqual(self.ALT, state["source_timestamp_raw"])
        self.assertEqual("2026-10-07T14:35:00Z", state["fetched_at_utc"])

    def test_the_scan_and_the_single_view_agree_on_the_block(self):
        """Same clock, same stamps, same rule - the two blocks must match."""
        from paperbroker.server import _data_state
        from paperbroker.tests.test_earnings_scan import FRESH

        kette = self._chains(FRESH)["AAPL"]
        criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                    data_mode=quote_age.MODE_LAST_AVAILABLE)
        einzeln = _data_state(kette, criteria, now_fn=CLOCK)
        body = run_scan(self._chains(FRESH), self._rows(), criteria=criteria)
        self.assertEqual(einzeln["mode"], body["data_state"]["mode"])
        self.assertEqual(einzeln["fetched_at_utc"], body["data_state"]["fetched_at_utc"])
        self.assertEqual(einzeln["source_timestamp_raw"],
                         body["data_state"]["source_timestamp_raw"])
        # fresh data stays fresh on both surfaces - no provisional theatre
        self.assertFalse(einzeln["provisional"])
        self.assertFalse(body["data_state"]["provisional"])

    def test_the_panel_says_the_word_provisional(self):
        js = PANEL_JS.read_text(encoding="utf-8")
        self.assertIn("vorläufig (letzter verfügbarer Marktstand)", js)
        self.assertIn("state.provisional", js)


class TestThePairSearchInTheMultiTickerScan(unittest.TestCase):
    """The scan searches the way the request asked - same rule, same numbers.

    One illiquid strike in the middle, as in the single view: the adjacent
    search finds nothing, the bounded wide search finds the outer pair. If the
    scan ignored the mode, this test would report 0 hits in both cases.
    """

    def _rows(self):
        return [calendar_row("AAPL", 3_000_000)]

    def _chains(self):
        from paperbroker.tests.test_earnings_scan import quote

        exp = EXP_A
        ymd = exp[2:4] + exp[5:7] + exp[8:10]
        kette = [
            quote(f"AAPL{ymd}C00240000", 5.00, 5.10, oi=8000),
            quote(f"AAPL{ymd}C00245000", 1.00, 1.10, oi=0, iv=0.0),      # illiquid
            quote(f"AAPL{ymd}C00250000", 2.40, 2.50, oi=4000),
        ]
        return {"AAPL": kette}

    def _scan(self, criteria):
        return run_scan(self._chains(), self._rows(), criteria=criteria)

    def test_adjacent_finds_nothing_and_the_wide_search_finds_the_pair(self):
        eng = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None)
        weit = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                pair_mode="within_width", pair_max_width=10.0)
        nah = entry_of(self._scan(eng), "AAPL")["strategies"]["bull_call_debit"]
        breit = entry_of(self._scan(weit), "AAPL")["strategies"]["bull_call_debit"]
        self.assertEqual(0, nah["hits"])
        self.assertGreater(breit["hits"], 0)
        # the search mode is named in the answer, exactly as in the single view
        body = self._scan(weit)
        self.assertEqual("within_width", body["pair_search"]["mode"])
        self.assertEqual("within_width", body["criteria"]["pair_mode"])


class TestTheScanReportsACappedSearch(unittest.TestCase):
    """Kody finding (PR #78): the scan swallowed the coverage.

    The scan loops over many expiries. A section that hit MAX_PAIRS_PER_SECTION
    used to leave no trace in the answer, so a cut search looked like a complete
    one - on the surface with the most sections of all.
    """

    def _rows(self):
        return [calendar_row("BIG", 3_000_000)]

    def _chain(self):
        from paperbroker.tests.test_earnings_scan import quote

        ymd = EXP_A[2:4] + EXP_A[5:7] + EXP_A[8:10]
        # 320 strikes: enough that a huge width exceeds the ceiling
        return {"BIG": [quote(f"BIG{ymd}C{int((100.0 + i) * 1000):08d}", 2.40, 2.50, oi=4000)
                        for i in range(320)]}

    def test_a_cut_section_is_named_in_the_answer(self):
        from unittest import mock

        from paperbroker import screener as screener_mod

        grenze = 500          # a small ceiling keeps the test fast
        criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                    pair_mode="within_width",
                                    pair_max_width=100_000.0)
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", grenze):
            body = run_scan(self._chain(), self._rows(), criteria=criteria)
        cov = body["pair_search"]["coverage"]
        self.assertTrue(cov["capped"])
        self.assertFalse(cov["possible_exact"])
        self.assertEqual(grenze, cov["limit"])
        # several sections per (title, expiry, strategy) - the cut ones are counted
        self.assertGreaterEqual(cov["capped_sections"], 1)
        self.assertLessEqual(cov["capped_sections"], cov["sections"])
        self.assertGreaterEqual(cov["enumerated"], cov["capped_sections"] * grenze)

    def test_a_complete_search_says_nothing_about_a_cap(self):
        criteria = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None,
                                    pair_mode="within_width", pair_max_width=3.0)
        cov = run_scan(self._chain(), self._rows(), criteria=criteria)["pair_search"]["coverage"]
        self.assertFalse(cov["capped"])
        self.assertTrue(cov["possible_exact"])
        self.assertNotIn("capped_sections", cov)


class TestTheCoverageIsAlwaysThere(unittest.TestCase):
    """Kody findings (PR #78) on the fix commit itself.

    Two defects in the scan's coverage tally: the key was missing whenever no
    section was searched, and the tally was written by parallel workers without
    a lock. Both would make the very number this slice exists to report wrong.
    """

    def test_the_key_is_present_when_nothing_was_searched(self):
        from paperbroker.tests.test_earnings_scan import quote

        # one ticker, no chain at all -> no section to search
        body = run_scan({}, [calendar_row("AAPL", 3_000_000)], criteria=ScreenerCriteria())
        cov = body["pair_search"]["coverage"]      # must not raise
        self.assertFalse(cov["capped"])
        self.assertTrue(cov["possible_exact"])
        self.assertEqual(0, cov["sections"])
        self.assertEqual(0, cov["enumerated"])

    def test_the_tally_survives_parallel_workers(self):
        import threading as _threading

        from paperbroker.earnings_scan import _Context, _coverage_total, _merke_suche

        ctx = _Context(fetch_earnings=None, fetch_chain=None, strategies=(),
                       criteria=ScreenerCriteria(), today=date(2026, 11, 11),
                       now_fn=None, limits=None, counter=None, day_state="live",
                       search_coverage={})
        abschnitt = {"possible": 7, "enumerated": 5, "limit": 500, "capped": True}

        def arbeite():
            for _ in range(200):
                _merke_suche(ctx, abschnitt)

        fäden = [_threading.Thread(target=arbeite) for _ in range(8)]
        for f in fäden:
            f.start()
        for f in fäden:
            f.join()

        gesamt = _coverage_total(ctx.search_coverage)
        self.assertEqual(1600, gesamt["sections"])
        self.assertEqual(1600 * 7, gesamt["possible"])
        self.assertEqual(1600 * 5, gesamt["enumerated"])
        self.assertEqual(1600, gesamt["capped_sections"])
        self.assertTrue(gesamt["capped"])
