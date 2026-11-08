"""Offline tests for the pure earnings day calendar (slice 9, plan A1-A12, B1-B6).

No network: the adapter tests use the same fake-transport shape as
``test_earnings_adapter.py`` - the URL is recorded and checked, nothing is
fetched. The clock is injected, so "six hours later" is a variable.

The point of these tests is the fail-closed part: an unparseable market cap is
``None`` and not ``0``, a malformed payload is an outage and not an empty day,
and a stale day is labelled as stale instead of looking live.
"""

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from paperbroker.adapters.earnings.NasdaqEarningsAdapter import (
    CALENDAR_URL,
    DayCalendarStore,
    NasdaqEarningsAdapter,
    NullEarningsAdapter,
)
from paperbroker.earnings_calendar import SESSION_AFTER, SESSION_BEFORE, SESSION_UNKNOWN
from paperbroker.earnings_day_calendar import (
    DAY_NEXT,
    DAY_ORDER,
    DAY_PREV,
    DAY_TODAY,
    DEFAULT_TOP_N,
    build_calendar,
    parse_calendar_rows,
    parse_market_cap,
    rank_rows,
)


def _row(symbol="AAPL", **overrides):
    """One row in the shape Nasdaq's calendar really returns."""
    row = {
        "symbol": symbol,
        "name": f"{symbol} Inc. Common Stock",
        "marketCap": "$88,237,580,000",
        "time": "time-after-hours",
        "fiscalQuarterEnding": "Sep/2026",
        "epsForecast": "$1.41",
        "lastYearEPS": "$0.97",
        "noOfEsts": "12",
        "lastYearRptDt": "10/30/2025",
    }
    row.update(overrides)
    return row


def _payload(rows):
    return {"data": {"rows": rows}, "status": {"rCode": 200}}


class _Clock:
    def __init__(self, start="2026-11-08T06:00:00+00:00"):
        self.jetzt = datetime.fromisoformat(start)

    def now(self):
        return self.jetzt

    def advance(self, **kwargs):
        self.jetzt = self.jetzt + timedelta(**kwargs)


class _CalendarSource:
    """Fake transport: records every URL, can fail or answer badly on demand."""

    def __init__(self, rows_for=None, fail=False, payload=None):
        self.rows_for = rows_for or (lambda tag: [_row("AAA")])
        self.fail = fail
        self.payload = payload
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if self.fail:
            raise TimeoutError("source down")
        if self.payload is not None:
            return 200, json.dumps(self.payload).encode()
        tag = url.rsplit("date=", 1)[-1]
        return 200, json.dumps(_payload(self.rows_for(tag))).encode()


def _adapter(tmpdir, transport, clock=None, **kwargs):
    clock = clock or _Clock()
    return NasdaqEarningsAdapter(
        transport=transport,
        now_fn=clock.now,
        cache=DayCalendarStore(path=os.path.join(tmpdir, "earnings.json")),
        calendar_store=DayCalendarStore(path=os.path.join(tmpdir, "calendar.json")),
        **kwargs,
    )


DAYS = {DAY_PREV: "2026-11-07", DAY_TODAY: "2026-11-08", DAY_NEXT: "2026-11-09"}


# ---------------------------------------------------------------------- #
# A. pure module                                                          #
# ---------------------------------------------------------------------- #
class TestParseMarketCap(unittest.TestCase):
    def test_a_real_nasdaq_market_cap_is_an_integer(self):
        # A2
        self.assertEqual(parse_market_cap("$88,237,580,000"), 88237580000)

    def test_everything_unusable_is_none_never_zero(self):
        # A3
        for wert in ("", None, "N/A", "$abc", "   ", "abc123"):
            self.assertIsNone(parse_market_cap(wert), wert)

    def test_digits_without_a_dollar_sign_still_parse(self):
        self.assertEqual(parse_market_cap("1,500"), 1500)


class TestParseCalendarRows(unittest.TestCase):
    def test_a_real_row_keeps_every_field(self):
        # A1
        geparst = parse_calendar_rows(_payload([_row("AAPL")]), "2026-11-09")
        self.assertTrue(geparst["payload_ok"])
        self.assertEqual(geparst["date"], "2026-11-09")
        self.assertEqual(geparst["skipped"], 0)
        self.assertEqual(len(geparst["rows"]), 1)
        zeile = geparst["rows"][0]
        self.assertEqual(zeile["symbol"], "AAPL")
        self.assertEqual(zeile["name"], "AAPL Inc. Common Stock")
        self.assertEqual(zeile["session"], SESSION_AFTER)
        self.assertEqual(zeile["eps_forecast"], "$1.41")
        self.assertEqual(zeile["market_cap"], 88237580000)
        self.assertEqual(zeile["fiscal_quarter_ending"], "Sep/2026")

    def test_an_unusable_market_cap_keeps_the_row_and_does_not_invent_zero(self):
        # A3
        rows = [_row("AAA", marketCap=wert) for wert in ("N/A", "", None, "$abc")]
        geparst = parse_calendar_rows(_payload(rows), "2026-11-09")
        self.assertEqual(len(geparst["rows"]), 4)
        self.assertEqual(geparst["skipped"], 0)
        for zeile in geparst["rows"]:
            self.assertIsNone(zeile["market_cap"])

    def test_rows_without_a_usable_symbol_are_counted_as_skipped(self):
        # A4
        rows = [
            _row("AAPL"),
            {},
            {"symbol": 123},
            {"symbol": ""},
            {"symbol": "TOOLONG"},
            {"symbol": "a b"},
            {"symbol": "aapl"},
            "not a dict",
        ]
        geparst = parse_calendar_rows(_payload(rows), "2026-11-09")
        self.assertEqual([z["symbol"] for z in geparst["rows"]], ["AAPL"])
        self.assertEqual(geparst["skipped"], 7)

    def test_the_release_time_becomes_a_named_session(self):
        # A5
        faelle = {
            "time-pre-market": SESSION_BEFORE,
            "time-after-hours": SESSION_AFTER,
            "time-not-supplied": SESSION_UNKNOWN,
            "something-new": SESSION_UNKNOWN,
            None: SESSION_UNKNOWN,
        }
        for zeit, erwartet in faelle.items():
            with self.subTest(zeit=zeit):
                geparst = parse_calendar_rows(_payload([_row("AAA", time=zeit)]), "2026-11-09")
                self.assertEqual(geparst["rows"][0]["session"], erwartet)

    def test_the_eps_forecast_stays_text(self):
        # A6
        for wert in ("$1.41", "1.41", None):
            with self.subTest(wert=wert):
                geparst = parse_calendar_rows(_payload([_row("AAA", epsForecast=wert)]), "2026-11-09")
                self.assertEqual(geparst["rows"][0]["eps_forecast"], wert)

    def test_a_payload_without_rows_is_not_an_empty_day(self):
        for payload in ({}, {"data": {}}, {"data": {"rows": "kaputt"}}, {"data": {"rows": None}}, None):
            with self.subTest(payload=payload):
                geparst = parse_calendar_rows(payload, "2026-11-09")
                self.assertEqual(geparst["rows"], [])
                self.assertEqual(geparst["skipped"], 0)
                self.assertFalse(geparst["payload_ok"])
                self.assertEqual(geparst["date"], "2026-11-09")


class TestRankRows(unittest.TestCase):
    def test_top_n_keeps_the_largest_market_caps_in_order(self):
        # A7
        rows = [{"symbol": f"S{i:02d}", "market_cap": i * 1000} for i in range(1, 31)]
        geordnet = rank_rows(rows, top_n=25)
        self.assertEqual(DEFAULT_TOP_N, 25)
        self.assertEqual(len(geordnet["rows"]), 25)
        self.assertEqual(geordnet["total"], 30)
        self.assertEqual(geordnet["hidden"], 5)
        werte = [z["market_cap"] for z in geordnet["rows"]]
        self.assertEqual(werte, sorted(werte, reverse=True))
        self.assertEqual(geordnet["rows"][0]["symbol"], "S30")

    def test_equal_market_caps_are_ordered_by_symbol_and_stable(self):
        # A8
        rows = [
            {"symbol": "MSFT", "market_cap": 500},
            {"symbol": "AAPL", "market_cap": 500},
            {"symbol": "NVDA", "market_cap": 900},
        ]
        einmal = [z["symbol"] for z in rank_rows(rows)["rows"]]
        zweimal = [z["symbol"] for z in rank_rows(rows)["rows"]]
        self.assertEqual(einmal, ["NVDA", "AAPL", "MSFT"])
        self.assertEqual(einmal, zweimal)

    def test_rows_without_a_market_cap_go_last_and_are_counted(self):
        # A9
        rows = [
            {"symbol": "AAA", "market_cap": None},
            {"symbol": "BBB", "market_cap": 100},
            {"symbol": "CCC", "market_cap": None},
            {"symbol": "DDD", "market_cap": 500},
        ]
        geordnet = rank_rows(rows, top_n=25)
        self.assertEqual([z["symbol"] for z in geordnet["rows"]], ["DDD", "BBB", "AAA", "CCC"])
        self.assertEqual(geordnet["without_market_cap"], 2)
        self.assertEqual(geordnet["total"], 4)
        self.assertEqual(geordnet["hidden"], 0)

    def test_hidden_is_never_negative(self):
        # A11
        rows = [{"symbol": f"S{i}", "market_cap": i} for i in range(3)]
        self.assertEqual(rank_rows(rows, top_n=25)["hidden"], 0)
        self.assertEqual(rank_rows(rows, top_n=1)["hidden"], 2)

    def test_an_empty_list_ranks_to_an_empty_group(self):
        geordnet = rank_rows([], top_n=25)
        self.assertEqual(geordnet, {"rows": [], "total": 0, "hidden": 0, "without_market_cap": 0})


class TestBuildCalendar(unittest.TestCase):
    def _days(self):
        return {
            DAY_NEXT: parse_calendar_rows(_payload([_row("CCC")]), DAYS[DAY_NEXT]),
            DAY_TODAY: parse_calendar_rows(_payload([_row("AAA"), _row("BBB")]), DAYS[DAY_TODAY]),
            DAY_PREV: parse_calendar_rows(_payload([_row("ZZZ")]), DAYS[DAY_PREV]),
        }

    def test_the_three_groups_keep_calendar_order(self):
        # A10
        kalender = build_calendar(self._days())
        self.assertEqual([g["key"] for g in kalender["groups"]], list(DAY_ORDER))
        self.assertEqual(DAY_ORDER, (DAY_PREV, DAY_TODAY, DAY_NEXT))
        for gruppe, tag in zip(kalender["groups"], ("2026-11-07", "2026-11-08", "2026-11-09"), strict=True):
            self.assertEqual(gruppe["date"], tag)
            self.assertIn("rows", gruppe)
            self.assertIn("total", gruppe)
            self.assertIn("hidden", gruppe)
            self.assertIn("without_market_cap", gruppe)
            self.assertTrue(gruppe["payload_ok"])
            self.assertEqual(gruppe["skipped"], 0)

    def test_hidden_counts_what_the_group_leaves_out(self):
        # A11
        rows = [_row(f"{chr(65 + i // 26)}{chr(65 + i % 26)}", marketCap=f"${i + 1},000") for i in range(40)]
        days = {DAY_TODAY: parse_calendar_rows(_payload(rows), DAYS[DAY_TODAY])}
        gruppe = build_calendar(days, top_n=25)["groups"][1]
        self.assertEqual(gruppe["total"], 40)
        self.assertEqual(len(gruppe["rows"]), 25)
        self.assertEqual(gruppe["hidden"], 15)

    def test_a_day_without_entries_is_an_empty_group_not_an_error(self):
        # A12
        leer = parse_calendar_rows(_payload([]), "2026-11-08")
        gruppe = build_calendar({DAY_TODAY: leer})["groups"][1]
        self.assertEqual(gruppe["rows"], [])
        self.assertEqual(gruppe["total"], 0)
        self.assertEqual(gruppe["hidden"], 0)
        self.assertTrue(gruppe["payload_ok"])


# ---------------------------------------------------------------------- #
# B. adapter (fake transport, no network)                                 #
# ---------------------------------------------------------------------- #
class TestCalendarDaysAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_three_days_cost_three_queries_with_iso_dates_only(self):
        # B1
        quelle = _CalendarSource()
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 3)
        self.assertEqual(ergebnis["state"], "live")
        for tag in DAYS.values():
            self.assertTrue(any(url == CALENDAR_URL.format(date=tag) for url in quelle.calls), tag)
            self.assertIn(tag, ergebnis["days"])
            self.assertEqual(ergebnis["days"][tag]["state"], "live")
            self.assertEqual(ergebnis["days"][tag]["date"], tag)

    def test_a_second_call_inside_the_ttl_does_not_query_again(self):
        # B2
        clock = _Clock()
        quelle = _CalendarSource()
        adapter = _adapter(self.tmpdir, quelle, clock=clock)
        adapter.get_calendar_days(DAYS)
        clock.advance(hours=5)
        zweite = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 3)
        self.assertEqual(zweite["state"], "live")
        self.assertTrue(all(d["state"] == "live" for d in zweite["days"].values()))

    def test_a_failed_live_query_serves_the_old_rows_as_stale(self):
        # B3
        clock = _Clock()
        quelle = _CalendarSource(rows_for=lambda tag: [_row("AAA"), _row("BBB")])
        adapter = _adapter(self.tmpdir, quelle, clock=clock)
        erste = adapter.get_calendar_days(DAYS)
        alter_stempel = erste["days"][DAYS[DAY_TODAY]]["fetched_at_utc"]
        self.assertIsNotNone(alter_stempel)
        clock.advance(hours=7)
        quelle.fail = True
        zweite = adapter.get_calendar_days(DAYS)
        self.assertEqual(zweite["state"], "stale")
        self.assertEqual(len(quelle.calls), 6)   # three more: the TTL had expired
        for tag in DAYS.values():
            eintrag = zweite["days"][tag]
            self.assertEqual(eintrag["state"], "stale")
            self.assertEqual([z["symbol"] for z in eintrag["rows"]], ["AAA", "BBB"])
            self.assertEqual(eintrag["fetched_at_utc"], alter_stempel)
            self.assertTrue(eintrag["live_query_failed"])

    def test_a_failed_live_query_without_cache_is_unavailable_and_empty(self):
        # B4
        quelle = _CalendarSource(fail=True)
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_calendar_days(DAYS)
        self.assertEqual(ergebnis["state"], "unavailable")
        for tag in DAYS.values():
            eintrag = ergebnis["days"][tag]
            self.assertEqual(eintrag["state"], "unavailable")
            self.assertEqual(eintrag["rows"], [])
            self.assertEqual(eintrag["error"]["code"], "source_unreachable")

    def test_a_payload_without_rows_is_an_outage_not_an_empty_day(self):
        # B5
        quelle = _CalendarSource(payload={"data": {}})
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(ergebnis["state"], "unavailable")
        eintrag = ergebnis["days"][DAYS[DAY_TODAY]]
        self.assertEqual(eintrag["rows"], [])
        self.assertFalse(eintrag["payload_ok"])
        self.assertEqual(eintrag["error"]["code"], "invalid_payload")

    def test_a_broken_cache_file_is_a_miss_not_a_crash(self):
        pfad = os.path.join(self.tmpdir, "calendar.json")
        with open(pfad, "w", encoding="utf-8") as fh:
            fh.write("{kaputt")
        quelle = _CalendarSource()
        adapter = NasdaqEarningsAdapter(
            transport=quelle,
            now_fn=_Clock().now,
            cache=DayCalendarStore(path=os.path.join(self.tmpdir, "earnings.json")),
            calendar_store=DayCalendarStore(path=pfad),
        )
        ergebnis = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(ergebnis["state"], "live")
        self.assertEqual(len(quelle.calls), 1)

    def test_the_null_adapter_says_not_configured(self):
        # B6
        ergebnis = NullEarningsAdapter().get_calendar_days(DAYS)
        self.assertEqual(ergebnis, {"days": {}, "state": "not_configured", "error": None})


if __name__ == "__main__":
    unittest.main()
