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
from pathlib import Path

import paperbroker
from paperbroker.earnings_scan import STATUSES
from paperbroker.server import (
    _CRITERIA_FIELDS,
    create_app,
    scan_view_data,
)
from paperbroker.tests.test_earnings_scan import (
    CLOCK,
    EXP_A,
    TODAY,
    FakeChainAdapter,
    FakeEarningsAdapter,
    calendar_row,
    call_pair,
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

