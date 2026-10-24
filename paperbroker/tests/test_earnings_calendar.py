"""Offline tests for the earnings contract and the expiry deadline rule.

Pure logic only: no clock, no network, no cache. Every fixture is a literal, so
the expectations are readable next to the assertion.

The cases are exactly the ones the phase order names: confirmed / estimated /
missing / invalid date, unknown session, before-open and after-close reports,
weekends and US market holidays, an expiry before, on and after the earnings
date, an unlisted expiry, no suitable expiry at all, and the rule that UNKNOWN
is never upgraded.
"""

import unittest
from datetime import date

from paperbroker.earnings_calendar import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_ESTIMATED,
    CONFIRMATION_UNKNOWN,
    FLAG_CALENDAR_CONFIRMED,
    FLAG_ESTIMATED,
    FLAG_SESSION_CONFLICT,
    SCHEMA_VERSION,
    SESSION_AFTER,
    SESSION_BEFORE,
    SESSION_UNKNOWN,
    STATUS_FRESH,
    STATUS_INVALID,
    STATUS_STALE,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    combine_earnings_and_expirations,
    easter_sunday,
    empty_earnings,
    is_us_trading_day,
    last_trading_day_before,
    merge_calendar_check,
    parse_nasdaq_calendar_row,
    parse_nasdaq_earnings_date,
    us_market_holidays,
)

TODAY = date(2026, 10, 24)


def _nachrichten(text):
    """Minimal Nasdaq `analyst/<symbol>/earnings-date` envelope."""
    return {"data": {"reportText": text, "announcement": text}, "status": {"rCode": 200}}


def _record(datum="2026-11-02", session=SESSION_AFTER, confirmation=CONFIRMATION_ESTIMATED,
            status=STATUS_FRESH, flags=None):
    return {
        "next_earnings_date": datum,
        "release_session": session,
        "date_confirmation": confirmation,
        "source": "nasdaq_earnings_date",
        "source_checked_at_utc": "2026-10-24T06:00:00Z",
        "retrieved_at_utc": "2026-10-24T06:00:00Z",
        "status": status,
        "quality_flags": list(flags or []),
    }


def _codes(ergebnis):
    return [grund["code"] for grund in ergebnis["decision"]["reasons"]]


class TestContract(unittest.TestCase):
    def test_empty_contract_has_every_field_and_nothing_invented(self):
        eintrag = empty_earnings("AAPL")
        self.assertEqual(
            sorted(eintrag),
            ["date_confirmation", "next_earnings_date", "quality_flags", "release_session",
             "retrieved_at_utc", "source", "source_checked_at_utc", "status"],
        )
        self.assertIsNone(eintrag["next_earnings_date"])
        self.assertEqual(eintrag["release_session"], SESSION_UNKNOWN)
        self.assertEqual(eintrag["date_confirmation"], CONFIRMATION_UNKNOWN)
        self.assertEqual(eintrag["status"], STATUS_UNKNOWN)
        self.assertIsNone(eintrag["source"])

    def test_schema_version_is_stable(self):
        self.assertEqual(SCHEMA_VERSION, "1.0")


class TestSourceParsing(unittest.TestCase):
    def test_confirmed_date_is_read_as_confirmed(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten(
            "Apple Inc. will report earnings on 11/02/2026 after market close."
        ))
        self.assertEqual(geparst["parse_status"], "ok")
        self.assertEqual(geparst["next_earnings_date"], "2026-11-02")
        self.assertEqual(geparst["date_confirmation"], CONFIRMATION_CONFIRMED)
        self.assertEqual(geparst["release_session"], SESSION_AFTER)

    def test_estimated_date_is_never_promoted(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten(
            "Apple Inc. is expected* to report earnings on 11/02/2026 after market close."
        ))
        self.assertEqual(geparst["date_confirmation"], CONFIRMATION_ESTIMATED)

    def test_algorithm_derived_date_is_an_estimate_without_a_session(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten(
            "NVIDIA Corporation is estimated to report earnings on 11/18/2026. "
            "The upcoming earnings date is derived from an algorithm based on a company's historical reporting dates."
        ))
        self.assertEqual(geparst["parse_status"], "ok")
        self.assertEqual(geparst["date_confirmation"], CONFIRMATION_ESTIMATED)
        self.assertEqual(geparst["release_session"], SESSION_UNKNOWN)

    def test_missing_date_stays_missing(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten(
            "Our vendor, Zacks Investment Research, hasn't provided us with the upcoming earnings report date."
        ))
        self.assertEqual(geparst["parse_status"], "no_date")
        self.assertNotIn("next_earnings_date", geparst)

    def test_impossible_date_is_invalid_not_clamped(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten(
            "Example Corp is expected* to report earnings on 02/30/2026 after market close."
        ))
        self.assertEqual(geparst["parse_status"], "invalid")
        self.assertIn("not a real calendar date", geparst["reason"])

    def test_text_without_a_date_is_invalid(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten("Earnings announcement pending."))
        self.assertEqual(geparst["parse_status"], "invalid")

    def test_empty_payload_is_invalid(self):
        self.assertEqual(parse_nasdaq_earnings_date(None)["parse_status"], "invalid")
        self.assertEqual(parse_nasdaq_earnings_date({})["parse_status"], "invalid")

    def test_before_open_is_read_as_before_market(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten(
            "The Coca-Cola Company is expected* to report earnings on 10/27/2026 before market open."
        ))
        self.assertEqual(geparst["release_session"], SESSION_BEFORE)

    def test_after_close_is_read_as_after_market(self):
        geparst = parse_nasdaq_earnings_date(_nachrichten(
            "Amazon.com is expected* to report earnings on 10/29/2026 after market close."
        ))
        self.assertEqual(geparst["release_session"], SESSION_AFTER)


class TestCalendarCrossCheck(unittest.TestCase):
    def _kalender(self, gelistet=True, session=SESSION_AFTER, datum="2026-11-02"):
        rows = []
        if gelistet:
            rows.append({"symbol": "AAPL", "time": {
                SESSION_BEFORE: "time-pre-market",
                SESSION_AFTER: "time-after-hours",
                SESSION_UNKNOWN: "time-not-supplied",
            }[session], "fiscalQuarterEnding": "Sep/2026"})
        return {"data": {"rows": rows}}

    def test_calendar_row_is_found_and_mapped(self):
        zeile = parse_nasdaq_calendar_row(self._kalender(), "aapl", "2026-11-02")
        self.assertTrue(zeile["listed"])
        self.assertEqual(zeile["release_session"], SESSION_AFTER)

    def test_calendar_confirmation_only_sets_a_flag(self):
        eintrag = _record(confirmation=CONFIRMATION_ESTIMATED)
        zeile = parse_nasdaq_calendar_row(self._kalender(), "AAPL", "2026-11-02")
        eintrag, flag = merge_calendar_check(eintrag, zeile)
        self.assertEqual(flag, FLAG_CALENDAR_CONFIRMED)
        # The calendar listing the symbol does NOT turn an estimate into a
        # confirmed announcement.
        self.assertEqual(eintrag["date_confirmation"], CONFIRMATION_ESTIMATED)
        self.assertIn(FLAG_CALENDAR_CONFIRMED, eintrag["quality_flags"])

    def test_contradicting_date_makes_the_record_invalid(self):
        eintrag = _record()
        eintrag, flag = merge_calendar_check(
            eintrag, {"listed": True, "date": "2026-11-03", "release_session": SESSION_AFTER}
        )
        self.assertEqual(eintrag["status"], STATUS_INVALID)
        self.assertIn("date_conflict", flag)

    def test_contradicting_session_clears_the_session_but_keeps_the_date(self):
        eintrag = _record(session=SESSION_AFTER)
        eintrag, _flag = merge_calendar_check(
            eintrag, {"listed": True, "date": "2026-11-02", "release_session": SESSION_BEFORE}
        )
        self.assertEqual(eintrag["release_session"], SESSION_UNKNOWN)
        self.assertIn(FLAG_SESSION_CONFLICT, eintrag["quality_flags"])
        self.assertEqual(eintrag["next_earnings_date"], "2026-11-02")

    def test_calendar_fills_an_unknown_session_from_a_structured_field(self):
        eintrag = _record(session=SESSION_UNKNOWN)
        eintrag, _flag = merge_calendar_check(
            eintrag, {"listed": True, "date": "2026-11-02", "release_session": SESSION_AFTER}
        )
        self.assertEqual(eintrag["release_session"], SESSION_AFTER)

    def test_unreachable_cross_check_is_flagged_and_changes_nothing(self):
        eintrag = _record()
        eintrag, flag = merge_calendar_check(eintrag, None)
        self.assertEqual(flag, "calendar_cross_check_unavailable")
        self.assertEqual(eintrag["status"], STATUS_FRESH)


class TestTradingCalendar(unittest.TestCase):
    def test_weekend_is_not_a_trading_day(self):
        self.assertFalse(is_us_trading_day(date(2026, 10, 31)))   # Saturday
        self.assertFalse(is_us_trading_day(date(2026, 11, 1)))    # Sunday
        self.assertTrue(is_us_trading_day(date(2026, 10, 30)))    # Friday

    def test_the_ten_holidays_are_closed(self):
        for tag in (
            date(2026, 1, 1),    # New Year
            date(2026, 1, 19),   # MLK
            date(2026, 2, 16),   # Washington's Birthday
            date(2026, 4, 3),    # Good Friday
            date(2026, 5, 25),   # Memorial Day
            date(2026, 6, 19),   # Juneteenth
            date(2026, 7, 3),    # Independence Day observed (4 July is a Saturday)
            date(2026, 9, 7),    # Labor Day
            date(2026, 11, 26),  # Thanksgiving
            date(2026, 12, 25),  # Christmas
        ):
            self.assertFalse(is_us_trading_day(tag), tag)

    def test_holiday_set_has_ten_entries_in_2026(self):
        self.assertEqual(len(us_market_holidays(2026)), 10)

    def test_good_friday_follows_easter(self):
        self.assertEqual(easter_sunday(2026), date(2026, 4, 5))
        self.assertEqual(easter_sunday(2027), date(2027, 3, 28))
        self.assertFalse(is_us_trading_day(date(2027, 3, 26)))

    def test_new_year_observed_falls_into_the_previous_year(self):
        # 1 Jan 2022 was a Saturday: the market was closed on 31 Dec 2021.
        self.assertFalse(is_us_trading_day(date(2021, 12, 31)))

    def test_documented_special_closures(self):
        self.assertFalse(is_us_trading_day(date(2012, 10, 29)))   # Hurricane Sandy
        self.assertFalse(is_us_trading_day(date(2012, 10, 30)))
        self.assertFalse(is_us_trading_day(date(2018, 12, 5)))    # George H. W. Bush

    def test_exit_day_before_a_monday_is_the_friday(self):
        self.assertEqual(last_trading_day_before(date(2026, 11, 2)), date(2026, 10, 30))

    def test_exit_day_skips_a_holiday(self):
        # Earnings on Friday 27 Nov 2026 -> the Thursday is Thanksgiving, so the
        # planned exit is Wednesday 25 Nov.
        self.assertEqual(last_trading_day_before(date(2026, 11, 27)), date(2026, 11, 25))

    def test_exit_day_is_none_for_a_non_date(self):
        self.assertIsNone(last_trading_day_before("2026-11-02"))
        self.assertIsNone(last_trading_day_before(None))


class TestWindowRule(unittest.TestCase):
    def test_valid_estimate_with_a_listed_expiry_after_earnings_is_eligible(self):
        ergebnis = combine_earnings_and_expirations(
            _record(), ["2026-10-30", "2026-11-20", "2026-12-18"], today=TODAY
        )
        self.assertTrue(ergebnis["decision"]["eligible"])
        self.assertEqual(ergebnis["decision"]["status"], "ok")
        self.assertEqual(ergebnis["expirations"]["selected"], "2026-11-20")
        self.assertEqual(ergebnis["exit"]["planned_last_exit_date"], "2026-10-30")
        self.assertTrue(ergebnis["exit"]["is_trading_day"])
        self.assertEqual(ergebnis["expirations"]["after_earnings"], ["2026-11-20", "2026-12-18"])

    def test_confirmed_date_is_eligible(self):
        ergebnis = combine_earnings_and_expirations(
            _record(confirmation=CONFIRMATION_CONFIRMED), ["2026-11-20"], today=TODAY
        )
        self.assertTrue(ergebnis["decision"]["eligible"])

    def test_unknown_confirmation_is_not_eligible_and_not_upgraded(self):
        ergebnis = combine_earnings_and_expirations(
            _record(confirmation=CONFIRMATION_UNKNOWN), ["2026-11-20"], today=TODAY
        )
        self.assertFalse(ergebnis["decision"]["eligible"])
        self.assertEqual(ergebnis["decision"]["status"], "unknown")
        self.assertIn("date_confirmation_unknown", _codes(ergebnis))
        self.assertEqual(ergebnis["earnings"]["date_confirmation"], CONFIRMATION_UNKNOWN)

    def test_missing_date_blocks(self):
        ergebnis = combine_earnings_and_expirations(
            _record(datum=None, confirmation=CONFIRMATION_UNKNOWN, status=STATUS_UNKNOWN),
            ["2026-11-20"], today=TODAY,
        )
        self.assertFalse(ergebnis["decision"]["eligible"])
        self.assertIn("earnings_date_unknown", _codes(ergebnis))
        self.assertIsNone(ergebnis["exit"]["planned_last_exit_date"])
        # Without a date nothing can be "after" it - a selected expiry here was
        # the one field a caller would have acted on.
        self.assertIsNone(ergebnis["expirations"]["selected"])
        self.assertEqual(ergebnis["expirations"]["after_earnings"], [])
        self.assertEqual(ergebnis["expirations"]["listed"], ["2026-11-20"])

    def test_unavailable_source_blocks(self):
        ergebnis = combine_earnings_and_expirations(
            _record(datum=None, confirmation=CONFIRMATION_UNKNOWN, status=STATUS_UNAVAILABLE),
            ["2026-11-20"], today=TODAY,
        )
        self.assertIn("earnings_source_unavailable", _codes(ergebnis))
        self.assertEqual(ergebnis["decision"]["status"], "unknown")

    def test_invalid_record_blocks(self):
        ergebnis = combine_earnings_and_expirations(
            _record(datum=None, confirmation=CONFIRMATION_UNKNOWN, status=STATUS_INVALID),
            ["2026-11-20"], today=TODAY,
        )
        self.assertIn("earnings_data_invalid", _codes(ergebnis))
        self.assertFalse(ergebnis["decision"]["eligible"])

    def test_unparseable_date_blocks(self):
        ergebnis = combine_earnings_and_expirations(
            _record(datum="2026-13-40"), ["2026-11-20"], today=TODAY
        )
        self.assertFalse(ergebnis["decision"]["eligible"])
        self.assertIn("earnings_data_invalid", _codes(ergebnis))

    def test_stale_record_blocks(self):
        ergebnis = combine_earnings_and_expirations(
            _record(status=STATUS_STALE), ["2026-11-20"], today=TODAY
        )
        self.assertFalse(ergebnis["decision"]["eligible"])
        self.assertEqual(ergebnis["decision"]["status"], "blocked")
        self.assertIn("earnings_data_stale", _codes(ergebnis))

    def test_past_earnings_date_blocks(self):
        ergebnis = combine_earnings_and_expirations(
            _record(datum="2026-10-01"), ["2026-11-20"], today=TODAY
        )
        self.assertIn("earnings_date_in_past", _codes(ergebnis))
        self.assertFalse(ergebnis["decision"]["eligible"])

    def test_expiry_before_earnings_is_excluded(self):
        ergebnis = combine_earnings_and_expirations(
            _record(), ["2026-10-30", "2026-11-20"], today=TODAY
        )
        self.assertEqual(ergebnis["expirations"]["on_or_before_earnings"], ["2026-10-30"])
        self.assertEqual(ergebnis["expirations"]["selected"], "2026-11-20")

    def test_expiry_on_the_earnings_date_is_excluded(self):
        ergebnis = combine_earnings_and_expirations(
            _record(), ["2026-11-02", "2026-11-20"], today=TODAY
        )
        self.assertEqual(ergebnis["expirations"]["on_or_before_earnings"], ["2026-11-02"])
        self.assertEqual(ergebnis["expirations"]["selected"], "2026-11-20")
        self.assertEqual(
            ergebnis["expirations"]["excluded"],
            [{"date": "2026-11-02", "reason": "expiration_on_or_before_earnings"}],
        )

    def test_only_expiries_before_earnings_block(self):
        ergebnis = combine_earnings_and_expirations(
            _record(), ["2026-10-30", "2026-11-02"], today=TODAY
        )
        self.assertFalse(ergebnis["decision"]["eligible"])
        self.assertIn("no_listed_expiration_after_earnings", _codes(ergebnis))
        self.assertIsNone(ergebnis["expirations"]["selected"])

    def test_no_expiry_is_never_replaced_by_an_invented_one(self):
        ergebnis = combine_earnings_and_expirations(
            _record(), ["2026-10-30", "2026-11-02"], today=TODAY
        )
        self.assertIsNone(ergebnis["expirations"]["selected"])
        self.assertEqual(
            ergebnis["expirations"]["listed"], ["2026-10-30", "2026-11-02"]
        )
        # No date that is not in the chain may appear anywhere in the answer.
        alle = (ergebnis["expirations"]["listed"] + ergebnis["expirations"]["after_earnings"]
                + ergebnis["expirations"]["on_or_before_earnings"])
        self.assertTrue(all(d in ("2026-10-30", "2026-11-02") for d in alle))

    def test_unknown_expirations_block_without_a_number(self):
        ergebnis = combine_earnings_and_expirations(_record(), None, today=TODAY)
        self.assertIn("expirations_unavailable", _codes(ergebnis))
        self.assertEqual(ergebnis["expirations"]["listed"], [])
        self.assertIsNone(ergebnis["expirations"]["selected"])

    def test_empty_expiry_list_blocks(self):
        ergebnis = combine_earnings_and_expirations(_record(), [], today=TODAY)
        self.assertIn("no_expirations_listed", _codes(ergebnis))
        self.assertFalse(ergebnis["decision"]["eligible"])

    def test_unparseable_expiries_are_reported_not_used(self):
        ergebnis = combine_earnings_and_expirations(
            _record(), ["2026-11-20", "not-a-date", ""], today=TODAY
        )
        self.assertEqual(ergebnis["expirations"]["listed"], ["2026-11-20"])
        self.assertEqual(ergebnis["expirations"]["unparseable"], ["not-a-date"])
        self.assertEqual(ergebnis["expirations"]["selected"], "2026-11-20")

    def test_weekend_earnings_uses_the_friday_before(self):
        ergebnis = combine_earnings_and_expirations(
            _record(datum="2026-11-07"), ["2026-11-20"], today=TODAY
        )
        self.assertEqual(ergebnis["exit"]["planned_last_exit_date"], "2026-11-06")

    def test_exit_day_is_always_a_trading_day(self):
        for datum in ("2026-11-02", "2026-11-26", "2026-11-27", "2026-12-25"):
            ergebnis = combine_earnings_and_expirations(
                _record(datum=datum), ["2027-01-15"], today=TODAY
            )
            if ergebnis["exit"]["planned_last_exit_date"]:
                self.assertTrue(
                    is_us_trading_day(date.fromisoformat(ergebnis["exit"]["planned_last_exit_date"])),
                    datum,
                )

    def test_past_exit_day_blocks(self):
        ergebnis = combine_earnings_and_expirations(
            _record(datum="2026-10-24"), ["2026-11-20"], today=TODAY
        )
        self.assertIn("exit_day_in_past", _codes(ergebnis))

    def test_reasons_are_stable_codes_with_a_german_message(self):
        ergebnis = combine_earnings_and_expirations(_record(), [], today=TODAY)
        grund = ergebnis["decision"]["reasons"][0]
        self.assertEqual(sorted(grund), ["code", "message"])
        self.assertTrue(grund["message"].endswith("."))

    def test_result_is_json_ready(self):
        import json

        ergebnis = combine_earnings_and_expirations(_record(), ["2026-11-20"], today=TODAY)
        json.dumps(ergebnis)   # must not raise: the route serialises exactly this
        self.assertEqual(ergebnis["as_of"], "2026-10-24")



class TestFailClosedOnEveryStatus(unittest.TestCase):
    """Only a fresh proof may authorise a window.

    Every other status - including a missing one and a value nobody defined -
    has to block, even when the date parses, the confirmation is known and a
    listed expiry lies after the earnings date. Otherwise "we do not know"
    silently becomes "trade it": the caller reads ``selected`` and ``eligible``,
    not the reasons.
    """

    LISTED = ["2026-10-30", "2026-11-20"]

    def _combine(self, **kwargs):
        parameter = {"confirmation": CONFIRMATION_CONFIRMED}
        parameter.update(kwargs)
        return combine_earnings_and_expirations(_record(**parameter), self.LISTED, today=TODAY)

    def _assert_blocked(self, ergebnis, code, entscheidung="blocked"):
        self.assertFalse(ergebnis["decision"]["eligible"])
        self.assertIn(code, _codes(ergebnis))
        self.assertEqual(ergebnis["decision"]["status"], entscheidung)

    def test_fresh_with_every_other_rule_met_is_eligible(self):
        ergebnis = self._combine(status=STATUS_FRESH)
        self.assertTrue(ergebnis["decision"]["eligible"])
        self.assertEqual(ergebnis["decision"]["status"], "ok")
        self.assertEqual(ergebnis["expirations"]["selected"], "2026-11-20")

    def test_unknown_status_blocks(self):
        self._assert_blocked(
            self._combine(status=STATUS_UNKNOWN), "earnings_status_unknown", "unknown"
        )

    def test_invalid_status_blocks(self):
        self._assert_blocked(
            self._combine(status=STATUS_INVALID), "earnings_data_invalid", "unknown"
        )

    def test_stale_status_blocks(self):
        # The label stays "blocked": a proof that is merely too old is a known
        # "no", and the existing contract pins that. Only the verdict matters.
        self._assert_blocked(
            self._combine(status=STATUS_STALE), "earnings_data_stale", "blocked"
        )

    def test_unavailable_status_blocks(self):
        self._assert_blocked(
            self._combine(status=STATUS_UNAVAILABLE), "earnings_source_unavailable", "unknown"
        )

    def test_missing_status_blocks(self):
        ohne_status = _record(confirmation=CONFIRMATION_CONFIRMED)
        del ohne_status["status"]
        ergebnis = combine_earnings_and_expirations(ohne_status, self.LISTED, today=TODAY)
        self._assert_blocked(ergebnis, "earnings_status_unknown", "unknown")

    def test_empty_status_blocks(self):
        self._assert_blocked(
            self._combine(status=""), "earnings_status_unknown", "unknown"
        )

    def test_unexpected_status_value_blocks_and_is_named(self):
        ergebnis = self._combine(status="half-fresh")
        self._assert_blocked(ergebnis, "earnings_status_unrecognised", "unknown")
        # The value is echoed, not silently rewritten into a known one.
        self.assertEqual(ergebnis["earnings"]["status"], "half-fresh")

    def test_no_other_status_ever_reaches_ok(self):
        # Table form of the rule: fresh authorises, everything else does not -
        # including a value the contract never defined.
        for status in ("unknown", "invalid", "stale", "unavailable", "", "half-fresh"):
            with self.subTest(status=status):
                ergebnis = self._combine(status=status)
                self.assertFalse(ergebnis["decision"]["eligible"])
                self.assertNotEqual(ergebnis["decision"]["status"], "ok")
                self.assertTrue(ergebnis["decision"]["reasons"])

    def test_unknown_confirmation_still_blocks_a_fresh_record(self):
        ergebnis = self._combine(status=STATUS_FRESH, confirmation=CONFIRMATION_UNKNOWN)
        self._assert_blocked(ergebnis, "date_confirmation_unknown", "unknown")

    def test_an_estimate_is_never_promoted_to_confirmed(self):
        ergebnis = self._combine(status=STATUS_FRESH, confirmation=CONFIRMATION_ESTIMATED)
        self.assertTrue(ergebnis["decision"]["eligible"])
        self.assertEqual(ergebnis["earnings"]["date_confirmation"], CONFIRMATION_ESTIMATED)
        self.assertNotEqual(ergebnis["earnings"]["date_confirmation"], CONFIRMATION_CONFIRMED)

if __name__ == "__main__":
    unittest.main()
