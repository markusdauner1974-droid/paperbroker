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
import threading
import unittest
from datetime import UTC, date, datetime, timedelta

from paperbroker.adapters.earnings.NasdaqEarningsAdapter import (
    CALENDAR_URL,
    DEFAULT_FAILURE_RETRY_SECONDS,
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
    parse_as_of,
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


# The answer api.nasdaq.com really gave for Sunday 2026-11-08 (measured, not
# constructed): HTTP 200, nothing listed, but the source names the day it
# answered for. That stamp is what separates "empty" from "broken".
SUNDAY_PAYLOAD = {
    "data": {"asOf": "Sun, Nov 8, 2026", "headers": None, "rows": None},
    "status": {"rCode": 200},
}


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


class TestParseAsOf(unittest.TestCase):
    """The stamp is read without the process locale, on every machine.

    A German locale prints Sunday as ``Son`` and is why ``strptime`` with
    ``%a``/``%b`` is not an option here: these cases must pass identically
    under ``LC_ALL=de_DE.UTF-8``.
    """

    def test_a_real_stamp_becomes_the_date(self):
        self.assertEqual(parse_as_of("Sun, Nov 8, 2026"), date(2026, 11, 8))

    def test_leading_zero_case_and_whitespace_do_not_matter(self):
        for text in (
            "Sun, Nov 08, 2026",
            "sun, nov 8, 2026",
            "SUN, NOV 08, 2026",
            "  Sun,  Nov   8,   2026  ",
            "Sun,Nov 8,2026",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_as_of(text), date(2026, 11, 8))

    def test_every_english_month_and_weekday_token_is_understood(self):
        monate = {m: i for i, m in enumerate(
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
        for monat, nummer in monate.items():
            with self.subTest(monat=monat):
                self.assertEqual(parse_as_of(f"Mon, {monat} 15, 2026"), date(2026, nummer, 15))
        for tag in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            with self.subTest(tag=tag):
                self.assertEqual(parse_as_of(f"{tag}, Jan 1, 2026"), date(2026, 1, 1))

    def test_everything_unusable_is_none(self):
        for text in (
            None,
            20261108,
            20261108.0,
            [],
            {},
            "",
            "   ",
            "Sun, Nov 8",                 # no year
            "Nov 8, 2026",                # no weekday
            "Sun, Nov 8, 26",             # two-digit year
            "Sunday, Nov 8, 2026",        # full weekday name
            "Son, Nov 8, 2026",           # German weekday
            "Sun, Nov, 8, 2026",          # stray comma
            "Sun, Foo 8, 2026",           # unknown month
            "Xyz, Nov 8, 2026",           # unknown weekday
            "Sun, Feb 31, 2026",          # impossible date
            "Sun, Nov 0, 2026",           # day zero
            "2026-11-08",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_as_of(text))


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

    def test_a_sunday_with_null_rows_and_a_date_stamp_is_a_valid_empty_day(self):
        # The live payload for 2026-11-08: rows is null, asOf names the day.
        geparst = parse_calendar_rows(SUNDAY_PAYLOAD, "2026-11-08")
        self.assertTrue(geparst["payload_ok"])
        self.assertEqual(geparst["rows"], [])
        self.assertEqual(geparst["skipped"], 0)
        self.assertEqual(geparst["date"], "2026-11-08")
        # ... and the page shows an empty group, not an error
        gruppe = build_calendar({DAY_TODAY: geparst})["groups"][1]
        self.assertTrue(gruppe["payload_ok"])
        self.assertEqual(gruppe["rows"], [])
        self.assertEqual(gruppe["total"], 0)
        self.assertEqual(gruppe["hidden"], 0)

    def test_null_rows_without_a_usable_date_stamp_stays_an_outage(self):
        # Without the source's own date there is no proof a day was answered,
        # so "empty" is not established - and guessing it would hide an outage.
        for payload in (
            {"data": {"rows": None}},
            {"data": {"rows": None, "headers": None}},
            {"data": {"asOf": "", "rows": None}},
            {"data": {"asOf": "   ", "rows": None}},
            {"data": {"asOf": None, "rows": None}},
            {"data": {"asOf": 20261108, "rows": None}},
        ):
            with self.subTest(payload=payload):
                geparst = parse_calendar_rows(payload, "2026-11-08")
                self.assertFalse(geparst["payload_ok"])
                self.assertEqual(geparst["rows"], [])
                self.assertEqual(geparst["skipped"], 0)

    def test_a_stamp_for_another_day_is_an_outage(self):
        # Monday's answer says nothing about Sunday: taking it as proof would
        # cache an empty Sunday that was never answered.
        payload = {"data": {"asOf": "Mon, Nov 9, 2026", "headers": None, "rows": None},
                   "status": {"rCode": 200}}
        geparst = parse_calendar_rows(payload, "2026-11-08")
        self.assertFalse(geparst["payload_ok"])
        self.assertEqual(geparst["rows"], [])
        self.assertEqual(geparst["skipped"], 0)
        self.assertEqual(geparst["date"], "2026-11-08")

    def test_a_stamp_without_a_year_is_an_outage(self):
        payload = {"data": {"asOf": "Sun, Nov 8", "headers": None, "rows": None}}
        geparst = parse_calendar_rows(payload, "2026-11-08")
        self.assertFalse(geparst["payload_ok"])
        self.assertEqual(geparst["rows"], [])

    def test_a_german_weekday_in_the_stamp_is_an_outage(self):
        payload = {"data": {"asOf": "Son, Nov 8, 2026", "headers": None, "rows": None}}
        geparst = parse_calendar_rows(payload, "2026-11-08")
        self.assertFalse(geparst["payload_ok"])
        self.assertEqual(geparst["rows"], [])

    def test_an_impossible_date_in_the_stamp_is_an_outage(self):
        payload = {"data": {"asOf": "Sun, Feb 31, 2026", "headers": None, "rows": None}}
        geparst = parse_calendar_rows(payload, "2026-11-08")
        self.assertFalse(geparst["payload_ok"])
        self.assertEqual(geparst["rows"], [])

    def test_an_unparseable_requested_day_keeps_everything_an_outage(self):
        # Fail closed: without a day to compare against, the stamp proves nothing.
        for tag in (None, "", "08.11.2026", "2026-11-8", 20261108):
            with self.subTest(tag=tag):
                geparst = parse_calendar_rows(SUNDAY_PAYLOAD, tag)
                self.assertFalse(geparst["payload_ok"])
                self.assertEqual(geparst["rows"], [])


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
        self.assertEqual(geordnet, {"rows": [], "total": 0, "hidden": 0,
                                    "without_market_cap": 0, "without_options": 0})


class TestOptionFilterBeforeTopN(unittest.TestCase):
    """Plan C: the optionable filter runs BEFORE the Top-N cut.

    The five titles without listed options are the LARGEST five on purpose:
    filtering after the cut would leave them in and cost five optionable
    titles their place - exactly the bug this section pins down.
    """

    @staticmethod
    def _rows():
        return [{"symbol": f"S{i:02d}", "market_cap": (30 - i) * 1000} for i in range(30)]

    @staticmethod
    def _optionable():
        return {f"S{i:02d}" for i in range(5, 30)}

    def test_the_without_options_rows_cost_no_place_in_the_top_25(self):
        # C1
        geordnet = rank_rows(self._rows(), top_n=25, optionable=self._optionable())
        self.assertEqual([z["symbol"] for z in geordnet["rows"]], [f"S{i:02d}" for i in range(5, 30)])
        self.assertEqual(geordnet["total"], 25)
        self.assertEqual(geordnet["hidden"], 0)
        self.assertEqual(geordnet["without_options"], 5)

    def test_the_largest_row_without_options_never_appears(self):
        # C2
        geordnet = rank_rows(self._rows(), top_n=25, optionable=self._optionable())
        self.assertNotIn("S00", [z["symbol"] for z in geordnet["rows"]])
        # Without the filter the same row IS first - the proof that the
        # difference comes from the filter and not from the sort.
        self.assertEqual(rank_rows(self._rows(), top_n=25)["rows"][0]["symbol"], "S00")

    def test_no_filter_is_the_old_behaviour_and_counts_nothing(self):
        # C3
        geordnet = rank_rows(self._rows(), top_n=25)
        self.assertEqual(geordnet["without_options"], 0)
        self.assertEqual(geordnet["total"], 30)

    def test_an_empty_symbol_set_hides_every_row(self):
        geordnet = rank_rows(self._rows(), top_n=25, optionable=set())
        self.assertEqual(geordnet["rows"], [])
        self.assertEqual(geordnet["total"], 0)
        self.assertEqual(geordnet["without_options"], 30)

    def test_build_calendar_passes_the_filter_to_every_group(self):
        # C4
        days = {DAY_TODAY: parse_calendar_rows(_payload([_row("AAPL"), _row("IX")]), DAYS[DAY_TODAY])}
        kalender = build_calendar(days, optionable={"AAPL"})
        gruppe = kalender["groups"][1]
        self.assertEqual([z["symbol"] for z in gruppe["rows"]], ["AAPL"])
        self.assertEqual(gruppe["without_options"], 1)
        self.assertEqual(gruppe["total"], 1)
        # The other groups are empty but carry the field as well.
        self.assertEqual([g["without_options"] for g in kalender["groups"]], [0, 1, 0])


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

    def test_a_sunday_with_a_date_stamp_is_live_and_is_cached(self):
        # Live measurement: a Sunday answers rows: null plus asOf. That is an
        # empty day worth caching - not an outage, and not a blank refusal.
        quelle = _CalendarSource(payload=SUNDAY_PAYLOAD)
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(ergebnis["state"], "live")
        eintrag = ergebnis["days"][DAYS[DAY_TODAY]]
        self.assertEqual(eintrag["state"], "live")
        self.assertNotEqual(eintrag["state"], "unavailable")
        self.assertTrue(eintrag["payload_ok"])
        self.assertEqual(eintrag["rows"], [])
        self.assertFalse(eintrag["live_query_failed"])
        self.assertNotIn("error", eintrag)
        gespeichert = DayCalendarStore(path=os.path.join(self.tmpdir, "calendar.json")).get(DAYS[DAY_TODAY])
        self.assertIsNotNone(gespeichert)
        self.assertEqual(gespeichert["rows"], [])
        self.assertTrue(gespeichert["payload_ok"])
        self.assertEqual(gespeichert["date"], DAYS[DAY_TODAY])

    def test_a_stamp_for_another_day_is_unavailable_and_is_not_cached(self):
        # The answer is about 2026-11-09, not about the requested 2026-11-08:
        # no empty Sunday may enter the cache under a date nobody answered for.
        falscher_stempel = {"data": {"asOf": "Mon, Nov 9, 2026", "headers": None, "rows": None},
                            "status": {"rCode": 200}}
        quelle = _CalendarSource(payload=falscher_stempel)
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(ergebnis["state"], "unavailable")
        eintrag = ergebnis["days"][DAYS[DAY_TODAY]]
        self.assertEqual(eintrag["state"], "unavailable")
        self.assertFalse(eintrag["payload_ok"])
        self.assertEqual(eintrag["rows"], [])
        self.assertTrue(eintrag["live_query_failed"])
        self.assertEqual(eintrag["error"]["code"], "invalid_payload")
        gespeichert = DayCalendarStore(path=os.path.join(self.tmpdir, "calendar.json")).get(DAYS[DAY_TODAY])
        self.assertIsNone(gespeichert)

    def test_a_stamp_for_another_day_serves_the_old_day_as_stale(self):
        # With an earlier, real answer on file the failure falls back to it -
        # and the stored rows stay exactly the old ones, not an empty day.
        clock = _Clock()
        quelle = _CalendarSource(rows_for=lambda tag: [_row("AAA")])
        adapter = _adapter(self.tmpdir, quelle, clock=clock)
        erste = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        alter_stempel = erste["days"][DAYS[DAY_TODAY]]["fetched_at_utc"]
        self.assertIsNotNone(alter_stempel)
        clock.advance(hours=7)                      # TTL expired: the source is asked again
        quelle.payload = {"data": {"asOf": "Mon, Nov 9, 2026", "headers": None, "rows": None},
                          "status": {"rCode": 200}}
        zweite = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(zweite["state"], "stale")
        eintrag = zweite["days"][DAYS[DAY_TODAY]]
        self.assertEqual(eintrag["state"], "stale")
        self.assertTrue(eintrag["live_query_failed"])
        self.assertEqual([z["symbol"] for z in eintrag["rows"]], ["AAA"])
        self.assertEqual(eintrag["fetched_at_utc"], alter_stempel)
        gespeichert = DayCalendarStore(path=os.path.join(self.tmpdir, "calendar.json")).get(DAYS[DAY_TODAY])
        self.assertIsNotNone(gespeichert)
        self.assertEqual([z["symbol"] for z in gespeichert["rows"]], ["AAA"])
        self.assertEqual(gespeichert["fetched_at_utc"], alter_stempel)


class _BarrierSource:
    """Transport that answers only when all expected requests wait together.

    Every call meets at a barrier sized to the number of days. Fetched one
    after another, the first call finds no peer: the barrier times out, the
    call fails and the day ends up unavailable - so a serial implementation
    cannot pass this test. That is the concurrency proof; no stopwatch and no
    timing threshold is involved, only the fact that a peer must be waiting.
    """

    def __init__(self, workers, timeout=2.0):
        self._barrier = threading.Barrier(workers)
        self._timeout = timeout
        self.calls = []
        self._lock = threading.Lock()
        self.serial_failure = False

    def __call__(self, url, headers, timeout):
        with self._lock:
            self.calls.append(url)
        try:
            self._barrier.wait(timeout=self._timeout)
        except threading.BrokenBarrierError:
            self.serial_failure = True
            raise TimeoutError("no peer request: the days were fetched one by one") from None
        tag = url.rsplit("date=", 1)[-1]
        return 200, json.dumps(_payload(self._rows(tag))).encode()

    @staticmethod
    def _rows(tag):
        return [_row("AAA"), _row("BBB")]


class TestDayCalendarStore(unittest.TestCase):
    """The day cache reads its file once per page view, not once per day."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self):
        return DayCalendarStore(path=os.path.join(self.tmpdir, "calendar.json"))

    @staticmethod
    def _entry(day, stamp="2026-11-08T06:00:00Z"):
        return {
            "date": day,
            "rows": [{"symbol": "AAA"}],
            "fetched_at_utc": stamp,
            "state": "live",
            "payload_ok": True,
            "skipped": 0,
        }

    def test_get_many_returns_the_entry_per_requested_day(self):
        store = self._store()
        tage = ["2026-11-07", "2026-11-08", "2026-11-09"]
        for tag in tage:
            store.put(tag, self._entry(tag))
        gesammelt = store.get_many(tage)
        self.assertEqual(sorted(gesammelt), sorted(tage))
        for tag in tage:
            self.assertEqual(gesammelt[tag]["date"], tag)
            self.assertEqual(gesammelt[tag]["rows"], [{"symbol": "AAA"}])

    def test_get_many_treats_a_broken_entry_as_absent(self):
        store = self._store()
        with open(store.path, "w", encoding="utf-8") as fh:
            json.dump({"entries": {
                # rows is not a list of objects
                "2026-11-07": {"date": "2026-11-07", "rows": "kaputt",
                               "fetched_at_utc": "2026-11-08T06:00:00Z"},
                "2026-11-08": self._entry("2026-11-08"),
                # carries a different date than the key it is filed under
                "2026-11-09": {"date": "2026-11-10", "rows": [],
                               "fetched_at_utc": "2026-11-08T06:00:00Z"},
            }}, fh)
        gesammelt = store.get_many(["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"])
        self.assertIsNone(gesammelt["2026-11-07"])
        self.assertIsNotNone(gesammelt["2026-11-08"])
        self.assertIsNone(gesammelt["2026-11-09"])
        self.assertIsNone(gesammelt["2026-11-10"])
        # get() is a wrapper over the same validation, not a second contract
        self.assertIsNone(store.get("2026-11-07"))
        self.assertEqual(store.get("2026-11-08")["date"], "2026-11-08")

    def test_get_many_reads_the_file_only_once(self):
        store = self._store()
        for tag in ("2026-11-07", "2026-11-08", "2026-11-09"):
            store.put(tag, self._entry(tag))
        gelesen = []
        original = store._read_all

        def zaehlend():
            gelesen.append(1)
            return original()

        store._read_all = zaehlend
        store.get_many(["2026-11-07", "2026-11-08", "2026-11-09"])
        self.assertEqual(len(gelesen), 1)

    def test_put_many_writes_three_days_with_one_read_and_one_write(self):
        # A page view stores three days; the store must read and replace the
        # file ONCE, not once per day (each replace costs its own fsync).
        store = self._store()
        tage = ["2026-11-07", "2026-11-08", "2026-11-09"]
        gelesen = []
        geschrieben = []
        original_read = store._read_all
        original_write = store._write_locked

        def read_zaehlend():
            gelesen.append(1)
            return original_read()

        def write_zaehlend(entries, verzeichnis):
            geschrieben.append(sorted(entries))
            return original_write(entries, verzeichnis)

        store._read_all = read_zaehlend
        store._write_locked = write_zaehlend

        self.assertTrue(store.put_many({tag: self._entry(tag) for tag in tage}))

        self.assertEqual(len(gelesen), 1)
        self.assertEqual(len(geschrieben), 1)
        self.assertEqual(geschrieben[0], tage)
        for tag in tage:
            self.assertEqual(store.get(tag)["date"], tag)

    def test_put_is_still_a_working_one_day_wrapper(self):
        store = self._store()
        self.assertTrue(store.put("2026-11-08", self._entry("2026-11-08")))
        self.assertEqual(store.get("2026-11-08")["rows"], [{"symbol": "AAA"}])
        self.assertIsNone(store.get("2026-11-07"))

    def test_put_many_in_an_unwritable_directory_answers_false_without_raising(self):
        # A file where the directory should be: mkdir fails, the cache is
        # simply not writable, and the request still gets its answer.
        blockiert = os.path.join(self.tmpdir, "kein-verzeichnis")
        with open(blockiert, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        store = DayCalendarStore(path=os.path.join(blockiert, "calendar.json"))
        self.assertFalse(store.put_many({"2026-11-08": self._entry("2026-11-08")}))
        self.assertFalse(store.put("2026-11-08", self._entry("2026-11-08")))

    def test_put_many_without_entries_does_not_create_or_touch_the_file(self):
        store = self._store()
        self.assertTrue(store.put_many({}))
        self.assertFalse(os.path.exists(store.path))
        self.assertTrue(store.put("2026-11-08", self._entry("2026-11-08")))
        vorher = os.stat(store.path).st_mtime_ns
        self.assertTrue(store.put_many({}))
        self.assertEqual(os.stat(store.path).st_mtime_ns, vorher)

    def test_put_many_still_applies_the_entry_cap(self):
        store = DayCalendarStore(path=os.path.join(self.tmpdir, "calendar.json"), max_entries=2)
        eintraege = {
            "2026-11-07": self._entry("2026-11-07", stamp="2026-11-07T06:00:00Z"),
            "2026-11-08": self._entry("2026-11-08", stamp="2026-11-08T06:00:00Z"),
            "2026-11-09": self._entry("2026-11-09", stamp="2026-11-09T06:00:00Z"),
        }
        self.assertTrue(store.put_many(eintraege))
        gespeichert = store.get_many(list(eintraege))
        self.assertEqual(
            sorted(tag for tag, eintrag in gespeichert.items() if eintrag is not None),
            ["2026-11-08", "2026-11-09"],
        )


class TestCalendarDaysFetching(unittest.TestCase):
    """The three cold days are fetched side by side, the output stays ordered."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_three_days_without_cache_cost_three_queries_and_are_complete(self):
        quelle = _CalendarSource(rows_for=lambda tag: [_row("AAA"), _row("BBB")])
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 3)
        self.assertEqual(
            sorted(url.rsplit("date=", 1)[-1] for url in quelle.calls),
            sorted(DAYS.values()),
        )
        self.assertEqual(ergebnis["state"], "live")
        self.assertEqual(sorted(ergebnis["days"]), sorted(DAYS.values()))
        for tag in DAYS.values():
            eintrag = ergebnis["days"][tag]
            self.assertEqual(eintrag["state"], "live")
            self.assertTrue(eintrag["payload_ok"])
            self.assertFalse(eintrag["from_cache"])
            self.assertEqual([z["symbol"] for z in eintrag["rows"]], ["AAA", "BBB"])
            self.assertEqual(eintrag["fetched_at_utc"], "2026-11-08T06:00:00Z")
        self.assertEqual(ergebnis["fetched_at_utc"], "2026-11-08T06:00:00Z")

    def test_a_cold_start_page_writes_the_cache_in_one_go(self):
        # Three missing days: the answer stores all three, but the file is
        # read-modify-written ONCE - three replaces would cost three fsyncs.
        quelle = _CalendarSource()
        adapter = _adapter(self.tmpdir, quelle)
        store = adapter._calendar_store
        geschrieben = []
        original = store._write_locked

        def write_zaehlend(entries, verzeichnis):
            geschrieben.append(sorted(entries))
            return original(entries, verzeichnis)

        store._write_locked = write_zaehlend

        ergebnis = adapter.get_calendar_days(DAYS)

        self.assertEqual(len(geschrieben), 1)
        self.assertEqual(geschrieben[0], sorted(DAYS.values()))
        for tag in DAYS.values():
            self.assertTrue(ergebnis["days"][tag]["persisted"])
            self.assertEqual(store.get(tag)["date"], tag)

    def test_an_unwritable_cache_still_answers_with_persisted_false(self):
        # Same cold start, but the cache cannot be written: the page keeps its
        # answer and says so per day instead of failing or claiming success.
        blockiert = os.path.join(self.tmpdir, "kein-verzeichnis")
        with open(blockiert, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        adapter = NasdaqEarningsAdapter(
            transport=_CalendarSource(),
            now_fn=_Clock().now,
            cache=DayCalendarStore(path=os.path.join(self.tmpdir, "earnings.json")),
            calendar_store=DayCalendarStore(path=os.path.join(blockiert, "calendar.json")),
        )
        ergebnis = adapter.get_calendar_days(DAYS)
        self.assertEqual(ergebnis["state"], "live")
        for tag in DAYS.values():
            eintrag = ergebnis["days"][tag]
            self.assertEqual(eintrag["state"], "live")
            self.assertFalse(eintrag["persisted"])
            self.assertEqual([z["symbol"] for z in eintrag["rows"]], ["AAA"])

    def test_all_missing_days_are_fetched_concurrently(self):
        # See _BarrierSource: a serial loop times out on the first day, so this
        # test fails unless the three queries really wait for each other.
        quelle = _BarrierSource(workers=3)
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_calendar_days(DAYS)
        self.assertFalse(quelle.serial_failure)
        self.assertEqual(len(quelle.calls), 3)
        self.assertEqual(ergebnis["state"], "live")
        for tag in DAYS.values():
            self.assertEqual([z["symbol"] for z in ergebnis["days"][tag]["rows"]], ["AAA", "BBB"])

    def test_a_fresh_cache_day_costs_no_query_while_the_rest_run_concurrently(self):
        # Only the two missing days may be in flight, so the barrier is sized
        # to two: a third parallel query (a cache hit ignored) would break it.
        clock = _Clock()
        _adapter(self.tmpdir, _CalendarSource(), clock=clock).get_calendar_days(
            {DAY_TODAY: DAYS[DAY_TODAY]}
        )
        quelle = _BarrierSource(workers=2)
        adapter = _adapter(self.tmpdir, quelle, clock=clock)
        ergebnis = adapter.get_calendar_days(DAYS)
        self.assertFalse(quelle.serial_failure)
        self.assertEqual(len(quelle.calls), 2)
        self.assertEqual(ergebnis["days"][DAYS[DAY_TODAY]]["from_cache"], True)
        self.assertEqual([z["symbol"] for z in ergebnis["days"][DAYS[DAY_TODAY]]["rows"]], ["AAA"])
        for tag in (DAYS[DAY_PREV], DAYS[DAY_NEXT]):
            self.assertEqual(ergebnis["days"][tag]["state"], "live")
            self.assertEqual([z["symbol"] for z in ergebnis["days"][tag]["rows"]], ["AAA", "BBB"])


class _FailedDaysSource(_CalendarSource):
    """Fails only for the days it was told to fail - the rest answer normally."""

    def __init__(self, fail_days, **kwargs):
        super().__init__(**kwargs)
        self.fail_days = set(fail_days)

    def __call__(self, url, headers, timeout):
        if url.rsplit("date=", 1)[-1] in self.fail_days:
            self.calls.append(url)
            raise TimeoutError("source down for this day")
        return super().__call__(url, headers, timeout)


class TestCalendarDaysOutageMemory(unittest.TestCase):
    """A failed day is remembered per PROCESS and per DAY, not globally.

    A dead source costs the full timeout once; while the source is down the
    page is reloaded, and every reload used to pay that timeout again. These
    tests pin the memory: a day that failed inside ``failure_retry_seconds``
    answers like the failure without a transport call, and is queried again
    once the window has passed.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_default_retry_window_is_sixty_seconds(self):
        self.assertEqual(DEFAULT_FAILURE_RETRY_SECONDS, 60)
        adapter = _adapter(self.tmpdir, _CalendarSource())
        self.assertEqual(adapter.failure_retry_seconds, DEFAULT_FAILURE_RETRY_SECONDS)

    def test_a_recent_failure_is_not_retried_and_answers_unavailable(self):
        # (a) no cache: the reload inside the window is honest and empty, and
        # costs no second timeout.
        clock = _Clock()
        quelle = _CalendarSource(fail=True)
        adapter = _adapter(self.tmpdir, quelle, clock=clock)

        erste = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 3)
        self.assertEqual(erste["state"], "unavailable")
        self.assertEqual({d["error"]["code"] for d in erste["days"].values()}, {"source_unreachable"})

        clock.advance(seconds=30)
        zweite = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 3)          # NOT asked again
        self.assertEqual(zweite["state"], "unavailable")
        self.assertIsNone(zweite["fetched_at_utc"])
        for tag in DAYS.values():
            eintrag = zweite["days"][tag]
            self.assertEqual(eintrag["state"], "unavailable")
            self.assertEqual(eintrag["rows"], [])
            self.assertIsNone(eintrag["fetched_at_utc"])
            self.assertTrue(eintrag["live_query_failed"])
            self.assertEqual(eintrag["error"]["code"], "recent_failure")

    def test_a_recent_failure_with_cache_serves_the_old_rows_as_stale(self):
        # (a) with cache: the same stale answer a just-failed query would give,
        # old rows and old stamp, labelled recent_failure.
        clock = _Clock()
        quelle = _CalendarSource(rows_for=lambda tag: [_row("AAA")])
        adapter = _adapter(self.tmpdir, quelle, clock=clock)

        erste = adapter.get_calendar_days(DAYS)
        stempel = erste["days"][DAYS[DAY_TODAY]]["fetched_at_utc"]
        self.assertEqual(stempel, "2026-11-08T06:00:00Z")

        clock.advance(hours=7)                          # TTL expired
        quelle.fail = True
        zweite = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 6)
        self.assertEqual(zweite["state"], "stale")

        clock.advance(seconds=30)                       # inside the retry window
        dritte = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 6)          # NOT asked again
        self.assertEqual(dritte["state"], "stale")
        self.assertEqual(dritte["fetched_at_utc"], stempel)   # oldest delivered stamp
        for tag in DAYS.values():
            eintrag = dritte["days"][tag]
            self.assertEqual(eintrag["state"], "stale")
            self.assertEqual([z["symbol"] for z in eintrag["rows"]], ["AAA"])
            self.assertEqual(eintrag["fetched_at_utc"], stempel)
            self.assertTrue(eintrag["live_query_failed"])
            self.assertTrue(eintrag["from_cache"])
            self.assertEqual(eintrag["error"]["code"], "recent_failure")

    def test_after_the_retry_window_the_source_is_asked_again(self):
        # (b) recovery must be visible: at exactly the window's end the day is
        # queried again, even though it is still failing.
        clock = _Clock()
        quelle = _CalendarSource(fail=True)
        adapter = _adapter(self.tmpdir, quelle, clock=clock)
        adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 3)

        clock.advance(seconds=DEFAULT_FAILURE_RETRY_SECONDS)
        ergebnis = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 6)          # asked again
        for tag in DAYS.values():
            self.assertEqual(ergebnis["days"][tag]["error"]["code"], "source_unreachable")

    def test_a_successful_query_forgets_the_failure(self):
        # (c) the memory must not become a permanent block. The retry window
        # (60 s) is far shorter than the TTL (6 h), so no end-to-end sequence
        # can separate "forgotten on success" from "expired by age" - the
        # memory is therefore asserted empty, plus the behavioural query after
        # the cache expired again.
        clock = _Clock()
        quelle = _CalendarSource(fail=True)
        adapter = _adapter(self.tmpdir, quelle, clock=clock, ttl_seconds=10)

        erste = adapter.get_calendar_days(DAYS)
        self.assertEqual(erste["state"], "unavailable")
        self.assertEqual(sorted(adapter._day_failures), sorted(DAYS.values()))

        clock.advance(seconds=DEFAULT_FAILURE_RETRY_SECONDS)
        quelle.fail = False
        zweite = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 6)
        self.assertEqual(zweite["state"], "live")
        self.assertEqual(adapter._day_failures, {})     # success cleared the memory

        clock.advance(seconds=11)                       # cache (ttl 10 s) expired again
        dritte = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 9)          # asked, not blocked
        self.assertEqual(dritte["state"], "live")
        self.assertTrue(all(d["state"] == "live" for d in dritte["days"].values()))

    def test_an_invalid_payload_counts_as_a_failure(self):
        # A payload without a rows list is an outage, so it is remembered like one.
        clock = _Clock()
        quelle = _CalendarSource(payload={"data": {}})
        adapter = _adapter(self.tmpdir, quelle, clock=clock)

        erste = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(erste["days"][DAYS[DAY_TODAY]]["error"]["code"], "invalid_payload")

        clock.advance(seconds=30)
        zweite = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(len(quelle.calls), 1)
        self.assertEqual(zweite["state"], "unavailable")
        self.assertEqual(zweite["days"][DAYS[DAY_TODAY]]["error"]["code"], "recent_failure")

    def test_one_failed_day_does_not_block_the_healthy_days(self):
        # (d) the memory is per day: Sunday is blocked, Saturday and Monday are
        # still fetched and still delivered live.
        clock = _Clock()
        quelle = _FailedDaysSource({DAYS[DAY_TODAY]})
        adapter = _adapter(self.tmpdir, quelle, clock=clock)

        erste = adapter.get_calendar_days({DAY_TODAY: DAYS[DAY_TODAY]})
        self.assertEqual(erste["state"], "unavailable")
        self.assertEqual(len(quelle.calls), 1)

        clock.advance(seconds=5)                        # well inside the window
        zweite = adapter.get_calendar_days(DAYS)
        self.assertEqual(len(quelle.calls), 3)
        self.assertEqual(
            sorted(url.rsplit("date=", 1)[-1] for url in quelle.calls[1:]),
            sorted([DAYS[DAY_PREV], DAYS[DAY_NEXT]]),
        )
        self.assertEqual(zweite["state"], "unavailable")
        self.assertEqual(zweite["days"][DAYS[DAY_TODAY]]["state"], "unavailable")
        self.assertEqual(zweite["days"][DAYS[DAY_TODAY]]["error"]["code"], "recent_failure")
        for tag in (DAYS[DAY_PREV], DAYS[DAY_NEXT]):
            self.assertEqual(zweite["days"][tag]["state"], "live")
            self.assertEqual([z["symbol"] for z in zweite["days"][tag]["rows"]], ["AAA"])


if __name__ == "__main__":
    unittest.main()
