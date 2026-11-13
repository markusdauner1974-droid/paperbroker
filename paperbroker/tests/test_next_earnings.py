"""Offline tests for the next-earnings source (Finnhub calendar).

No test here touches the network: the transport is a callable that answers a
fixed ``(status, body)`` and counts its calls, the clock is pinned. What is
pinned, in the order of the requirement:

* the answer is UNSORTED and the EARLIEST date at or after today wins (measured
  live: CTVA answers 2027-05-03 first and 2027-02-01 second),
* a row of another symbol, a date in the past and an unparseable date are
  skipped, one bad row costs that row only,
* without ``FINNHUB_API_KEY`` no request is made at all and the error says
  ``no_api_key`` - "we did not ask" is not "there is no date",
* a title with no row in the window is the ordinary ``None`` answer, and that
  answer is cached too,
* the window is ``from = today``, ``to = today + 270 days``,
* the cache holds for 12 h and is asked again afterwards, in ONE file,
* every failure carries a stable code and never the token: an exception text
  from ``requests`` would embed the URL the key travels in.
"""

import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from paperbroker.next_earnings import (
    CODE_HTTP_ERROR,
    CODE_INVALID_PAYLOAD,
    CODE_NO_API_KEY,
    CODE_THROTTLED,
    CODE_TRANSPORT_ERROR,
    CODE_UNAUTHORIZED,
    DEFAULT_TTL_SECONDS,
    DEFAULT_WINDOW_DAYS,
    NextEarningsAdapter,
    NextEarningsError,
    parse_next_earnings,
    resolve_token,
)

TODAY = date(2026, 11, 14)


def clock_at(moment):
    return lambda: datetime(moment.year, moment.month, moment.day, 21, 0, tzinfo=UTC)


#: The measured live answer for CTVA, 2026-11-14 - deliberately unsorted.
CTVA_ROWS = {
    "earningsCalendar": [
        {"symbol": "CTVA", "date": "2027-05-03", "hour": "", "year": 2027, "quarter": 1},
        {"symbol": "CTVA", "date": "2027-02-01", "hour": "amc", "year": 2026, "quarter": 4},
    ]
}


class Transport:
    """A transport that answers from memory and counts its calls."""

    def __init__(self, status=200, body=None, boom=None):
        self.status = status
        self.body = body if body is not None else json.dumps(CTVA_ROWS).encode()
        self.boom = boom
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if self.boom is not None:
            raise self.boom
        return self.status, self.body


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = os.path.join(self._tmp.name, "next_earnings.json")

    def adapter(self, transport, **kwargs):
        kwargs.setdefault("token", "test-key")
        kwargs.setdefault("now_fn", clock_at(TODAY))
        kwargs.setdefault("cache_path", self.cache)
        return NextEarningsAdapter(transport=transport, **kwargs)


class ParseTests(_Case):
    def test_unsorted_answer_yields_the_earliest_date(self):
        # The first row is the LATER quarter: taking row[0] would be wrong.
        self.assertEqual(CTVA_ROWS["earningsCalendar"][0]["date"], "2027-05-03")
        self.assertEqual(parse_next_earnings(CTVA_ROWS, "CTVA", TODAY), date(2027, 2, 1))

    def test_past_dates_are_skipped(self):
        payload = {"earningsCalendar": [
            {"symbol": "AAA", "date": "2026-10-01"},
            {"symbol": "AAA", "date": "2026-11-20"},
            {"symbol": "AAA", "date": "2027-01-05"},
        ]}
        self.assertEqual(parse_next_earnings(payload, "AAA", TODAY), date(2026, 11, 20))

    def test_a_date_on_today_counts(self):
        payload = {"earningsCalendar": [{"symbol": "AAA", "date": TODAY.isoformat()}]}
        self.assertEqual(parse_next_earnings(payload, "AAA", TODAY), TODAY)

    def test_other_symbols_and_broken_rows_are_skipped(self):
        payload = {"earningsCalendar": [
            {"symbol": "BBB", "date": "2026-11-20"},
            {"symbol": "AAA", "date": "not a date"},
            {"symbol": "AAA"},
            {"symbol": "aaa", "date": "2027-03-01"},
            "not a row",
        ]}
        self.assertEqual(parse_next_earnings(payload, "AAA", TODAY), date(2027, 3, 1))

    def test_missing_or_null_calendar_is_no_date(self):
        self.assertIsNone(parse_next_earnings({}, "AAA", TODAY))
        self.assertIsNone(parse_next_earnings({"earningsCalendar": None}, "AAA", TODAY))
        self.assertIsNone(parse_next_earnings({"earningsCalendar": []}, "AAA", TODAY))
        self.assertIsNone(parse_next_earnings(None, "AAA", TODAY))

    def test_a_symbol_the_answer_does_not_list_has_no_date(self):
        self.assertIsNone(parse_next_earnings(CTVA_ROWS, "ZZZ", TODAY))


class KeyTests(_Case):
    def test_no_key_raises_and_never_asks(self):
        transport = Transport()
        adapter = self.adapter(transport, token="")
        with self.assertRaises(NextEarningsError) as caught:
            adapter.get_next_earnings("CTVA")
        self.assertEqual(caught.exception.code, CODE_NO_API_KEY)
        self.assertEqual(transport.calls, [])

    def test_the_environment_is_read_when_no_token_is_passed(self):
        os.environ["FINNHUB_API_KEY"] = "from-env"
        self.addCleanup(os.environ.pop, "FINNHUB_API_KEY", None)
        self.assertEqual(resolve_token(None), "from-env")
        # An explicit value wins, including the empty one ("there is no key").
        self.assertEqual(resolve_token(""), "")
        self.assertEqual(resolve_token(" explicit "), "explicit")


class QueryTests(_Case):
    def test_the_window_and_the_symbol_travel_in_the_url(self):
        transport = Transport()
        adapter = self.adapter(transport)
        self.assertEqual(adapter.get_next_earnings("ctva"), date(2027, 2, 1))
        self.assertEqual(len(transport.calls), 1)
        url = transport.calls[0]
        self.assertIn("symbol=CTVA", url)
        self.assertIn(f"from={TODAY.isoformat()}", url)
        self.assertIn(f"to={(TODAY + timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()}", url)
        # The token is a query parameter (the API's own shape) and appears once.
        self.assertEqual(url.count("token=test-key"), 1)

    def test_a_title_without_a_row_answers_none(self):
        transport = Transport(body=json.dumps({"earningsCalendar": []}).encode())
        self.assertIsNone(self.adapter(transport).get_next_earnings("AAA"))

    def test_the_answer_is_cached_and_the_ttl_holds_12_hours(self):
        transport = Transport()
        base = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
        moment = {"now": base}
        adapter = self.adapter(transport, now_fn=lambda: moment["now"])

        self.assertEqual(adapter.get_next_earnings("CTVA"), date(2027, 2, 1))
        # Still inside 12 h: no second request.
        moment["now"] = base + timedelta(hours=11, minutes=59)
        self.assertEqual(adapter.get_next_earnings("CTVA"), date(2027, 2, 1))
        self.assertEqual(len(transport.calls), 1)
        # Past the TTL: asked again.
        moment["now"] = base + timedelta(hours=12, seconds=1)
        self.assertEqual(adapter.get_next_earnings("CTVA"), date(2027, 2, 1))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(DEFAULT_TTL_SECONDS, 12 * 60 * 60)

    def test_a_null_answer_is_cached_too(self):
        body = json.dumps({"earningsCalendar": []}).encode()
        transport = Transport(body=body)
        adapter = self.adapter(transport)
        self.assertIsNone(adapter.get_next_earnings("AAA"))
        self.assertIsNone(adapter.get_next_earnings("AAA"))
        self.assertEqual(len(transport.calls), 1)
        stored = json.loads(Path(self.cache).read_text(encoding="utf-8"))
        self.assertIsNone(stored["symbols"]["AAA"]["next_earnings_date"])

    def test_one_file_holds_both_symbols(self):
        adapter = self.adapter(Transport(body=json.dumps({"earningsCalendar": [
            {"symbol": "AAA", "date": "2027-01-05"},
            {"symbol": "BBB", "date": "2027-02-05"},
        ]}).encode()))
        self.assertEqual(adapter.get_next_earnings("AAA"), date(2027, 1, 5))
        self.assertEqual(adapter.get_next_earnings("BBB"), date(2027, 2, 5))
        stored = json.loads(Path(self.cache).read_text(encoding="utf-8"))
        self.assertEqual(sorted(stored["symbols"]), ["AAA", "BBB"])

    def test_a_broken_cache_file_is_ignored_not_fatal(self):
        Path(self.cache).write_text("{not json", encoding="utf-8")
        transport = Transport()
        self.assertEqual(self.adapter(transport).get_next_earnings("CTVA"),
                         date(2027, 2, 1))
        self.assertEqual(len(transport.calls), 1)


class FailureTests(_Case):
    def test_http_classes_carry_their_own_code(self):
        for status, code in ((403, CODE_UNAUTHORIZED), (429, CODE_THROTTLED),
                             (500, CODE_HTTP_ERROR)):
            transport = Transport(status=status, body=b"")
            with self.assertRaises(NextEarningsError) as caught:
                self.adapter(transport).get_next_earnings("CTVA")
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.status, status)

    def test_a_transport_failure_names_no_url(self):
        transport = Transport(boom=RuntimeError(
            "ConnectionError: GET https://finnhub.io/api/v1/calendar/earnings"
            "?symbol=CTVA&token=SECRET-KEY"))
        with self.assertRaises(NextEarningsError) as caught:
            self.adapter(transport).get_next_earnings("CTVA")
        self.assertEqual(caught.exception.code, CODE_TRANSPORT_ERROR)
        # The key must never reach a message, a log or a 200 body.
        self.assertNotIn("SECRET-KEY", str(caught.exception))
        self.assertNotIn("SECRET-KEY", caught.exception.message)

    def test_an_unreadable_answer_is_named(self):
        transport = Transport(body=b"<html>no json</html>")
        with self.assertRaises(NextEarningsError) as caught:
            self.adapter(transport).get_next_earnings("CTVA")
        self.assertEqual(caught.exception.code, CODE_INVALID_PAYLOAD)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
