"""Offline tests for the Nasdaq earnings adapter and its cache.

No network: a fake transport returns the recorded payloads. The clock is
injected, so "six hours later" is a variable, not a sleep.

The point of these tests is the part that is easy to get wrong: a cached date
must never look like a live check, an old proof must not authorise anything,
and a broken cache file must cost a query, not the page.
"""

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from paperbroker.adapters.earnings.NasdaqEarningsAdapter import (
    EarningsCache,
    NasdaqEarningsAdapter,
    NullEarningsAdapter,
)
from paperbroker.earnings_calendar import (
    FLAG_CACHE_USED,
    FLAG_CALENDAR_CONFIRMED,
    FLAG_CALENDAR_NOT_LISTED,
    FLAG_LIVE_FAILED,
    FLAG_SOURCE_NOT_CONFIGURED,
    STATUS_FRESH,
    STATUS_STALE,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
)

TEXT_ESTIMATED = (
    "Apple Inc. Common Stock is expected* to report earnings on  11/02/2026 after market close. "
    "The report will be for the fiscal Quarter ending Sep 2026."
)
TEXT_NO_DATE = (
    "Our vendor, Zacks Investment Research, hasn't provided us with the upcoming earnings report date."
)


def _date_payload(text):
    return {"data": {"reportText": text, "announcement": text}, "status": {"rCode": 200}}


def _calendar_payload(symbols=("AAPL",), zeit="time-after-hours"):
    return {"data": {"rows": [{"symbol": s, "time": zeit, "fiscalQuarterEnding": "Sep/2026"}
                              for s in symbols]}}


class _Clock:
    def __init__(self, start="2026-10-24T06:00:00+00:00"):
        self.jetzt = datetime.fromisoformat(start)

    def now(self):
        return self.jetzt

    def advance(self, **kwargs):
        self.jetzt = self.jetzt + timedelta(**kwargs)


class _Source:
    """Fake transport: records every URL, can fail on demand."""

    def __init__(self, text=TEXT_ESTIMATED, calendar=None, status=200, fail=False):
        self.text = text
        self.calendar = calendar if calendar is not None else _calendar_payload()
        self.status = status
        self.fail = fail
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if self.fail:
            raise TimeoutError("source down")
        if "/analyst/" in url:
            return self.status, json.dumps(_date_payload(self.text)).encode()
        if "/calendar/earnings" in url:
            return 200, json.dumps(self.calendar).encode()
        raise AssertionError(f"unexpected url {url}")

    @property
    def date_calls(self):
        return [u for u in self.calls if "/analyst/" in u]

    @property
    def calendar_calls(self):
        return [u for u in self.calls if "/calendar/earnings" in u]


class _Basis(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_path = os.path.join(self._tmp.name, "earnings.json")
        self.clock = _Clock()
        self.source = _Source()

    def _adapter(self, **kwargs):
        kwargs.setdefault("cache", EarningsCache(self.cache_path))
        return NasdaqEarningsAdapter(
            transport=self.source, now_fn=self.clock.now, **kwargs
        )


class TestLiveQuery(_Basis):
    def test_live_answer_is_fresh_and_complete(self):
        body = self._adapter().get_earnings("aapl")
        self.assertEqual(body["schema_version"], "1.0")
        self.assertEqual(body["ticker"], "AAPL")
        eintrag = body["earnings"]
        self.assertEqual(eintrag["next_earnings_date"], "2026-11-02")
        self.assertEqual(eintrag["release_session"], "after_market")
        self.assertEqual(eintrag["date_confirmation"], "estimated")
        self.assertEqual(eintrag["status"], STATUS_FRESH)
        self.assertEqual(eintrag["source"], "nasdaq_earnings_date")
        self.assertEqual(eintrag["source_checked_at_utc"], "2026-10-24T06:00:00Z")
        self.assertEqual(body["cache"]["state"], "live")

    def test_the_calendar_is_only_asked_for_the_named_date(self):
        self._adapter().get_earnings("AAPL")
        self.assertEqual(len(self.source.date_calls), 1)
        self.assertEqual(len(self.source.calendar_calls), 1)
        self.assertIn("date=2026-11-02", self.source.calendar_calls[0])

    def test_the_calendar_confirmation_is_a_flag_not_an_upgrade(self):
        eintrag = self._adapter().get_earnings("AAPL")["earnings"]
        self.assertIn(FLAG_CALENDAR_CONFIRMED, eintrag["quality_flags"])
        self.assertEqual(eintrag["date_confirmation"], "estimated")

    def test_a_symbol_missing_from_the_calendar_is_flagged(self):
        self.source.calendar = _calendar_payload(symbols=("MSFT",))
        eintrag = self._adapter().get_earnings("AAPL")["earnings"]
        self.assertIn(FLAG_CALENDAR_NOT_LISTED, eintrag["quality_flags"])
        self.assertEqual(eintrag["date_confirmation"], "estimated")

    def test_a_dead_calendar_does_not_kill_the_date(self):
        def transport(url, headers, timeout):
            if "/calendar/earnings" in url:
                raise TimeoutError("calendar down")
            return 200, json.dumps(_date_payload(TEXT_ESTIMATED)).encode()

        body = NasdaqEarningsAdapter(
            transport=transport, now_fn=self.clock.now, cache=EarningsCache(self.cache_path)
        ).get_earnings("AAPL")
        self.assertEqual(body["earnings"]["next_earnings_date"], "2026-11-02")
        self.assertIn("calendar_cross_check_unavailable", body["earnings"]["quality_flags"])
        self.assertEqual(body["earnings"]["status"], STATUS_FRESH)

    def test_cross_check_can_be_switched_off(self):
        self._adapter(cross_check=False).get_earnings("AAPL")
        self.assertEqual(self.source.calendar_calls, [])


class TestMissingAndInvalidAnswers(_Basis):
    def test_a_source_without_a_date_is_unknown_not_guessed(self):
        self.source.text = TEXT_NO_DATE
        body = self._adapter().get_earnings("TSLA")
        self.assertIsNone(body["earnings"]["next_earnings_date"])
        self.assertEqual(body["earnings"]["status"], STATUS_UNKNOWN)
        self.assertEqual(body["earnings"]["date_confirmation"], "unknown")
        # nothing to cross-check when there is no date
        self.assertEqual(self.source.calendar_calls, [])

    def test_an_unusable_answer_is_invalid(self):
        self.source.text = "Earnings announcement pending."
        body = self._adapter().get_earnings("AAPL")
        self.assertIsNone(body["earnings"]["next_earnings_date"])
        self.assertEqual(body["earnings"]["status"], "invalid")

    def test_an_http_error_without_cache_is_unavailable(self):
        quelle = _Source(status=503)
        adapter = NasdaqEarningsAdapter(
            transport=quelle, now_fn=self.clock.now, cache=EarningsCache(self.cache_path)
        )
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["earnings"]["status"], STATUS_UNAVAILABLE)
        self.assertIsNone(body["earnings"]["next_earnings_date"])
        self.assertIn(FLAG_LIVE_FAILED, body["earnings"]["quality_flags"])
        self.assertEqual(body["cache"]["state"], "miss")
        self.assertEqual(body["cache"]["error"]["code"], "http_error")
        self.assertEqual(body["cache"]["error"]["http_status"], 503)

    def test_a_transport_exception_without_cache_is_unavailable(self):
        quelle = _Source(fail=True)
        adapter = NasdaqEarningsAdapter(
            transport=quelle, now_fn=self.clock.now, cache=EarningsCache(self.cache_path)
        )
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["earnings"]["status"], STATUS_UNAVAILABLE)
        self.assertEqual(body["cache"]["error"]["code"], "source_unreachable")

    def test_non_json_is_reported_as_such(self):
        def transport(url, headers, timeout):
            return 200, b"<html>nope</html>"

        body = NasdaqEarningsAdapter(
            transport=transport, now_fn=self.clock.now, cache=EarningsCache(self.cache_path)
        ).get_earnings("AAPL")
        self.assertEqual(body["earnings"]["status"], STATUS_UNAVAILABLE)
        self.assertEqual(body["cache"]["error"]["code"], "invalid_json")


class TestCache(_Basis):
    def test_a_second_call_inside_the_ttl_uses_the_cache(self):
        adapter = self._adapter()
        adapter.get_earnings("AAPL")
        self.clock.advance(hours=1)
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["cache"]["state"], "hit")
        self.assertEqual(len(self.source.date_calls), 1)
        self.assertIn(FLAG_CACHE_USED, body["earnings"]["quality_flags"])
        self.assertEqual(body["cache"]["age_seconds"], 3600)

    def test_the_ttl_expiry_causes_exactly_one_new_query(self):
        adapter = self._adapter(ttl_seconds=6 * 3600)
        adapter.get_earnings("AAPL")
        self.clock.advance(hours=7)
        body = adapter.get_earnings("AAPL")
        self.assertEqual(len(self.source.date_calls), 2)
        self.assertEqual(body["cache"]["state"], "live")

    def test_a_cached_proof_keeps_its_own_timestamp(self):
        adapter = self._adapter()
        adapter.get_earnings("AAPL")
        self.clock.advance(hours=2)
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["earnings"]["source_checked_at_utc"], "2026-10-24T06:00:00Z")
        self.assertEqual(body["earnings"]["retrieved_at_utc"], "2026-10-24T08:00:00Z")

    def test_a_failed_live_query_never_promotes_the_cache_to_a_live_check(self):
        adapter = self._adapter()
        adapter.get_earnings("AAPL")
        self.clock.advance(hours=7)
        self.source.fail = True
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["cache"]["state"], "stale_fallback")
        self.assertIn(FLAG_LIVE_FAILED, body["earnings"]["quality_flags"])
        self.assertEqual(body["earnings"]["source_checked_at_utc"], "2026-10-24T06:00:00Z")
        self.assertEqual(body["cache"]["stored_at_utc"], "2026-10-24T06:00:00Z")
        self.assertEqual(body["cache"]["error"]["code"], "source_unreachable")

    def test_a_proof_older_than_the_freshness_limit_is_stale(self):
        # TTL deliberately longer than the freshness limit: the cache is a HIT
        # and the record is still stale - the two limits answer two questions.
        adapter = self._adapter(ttl_seconds=10 * 24 * 3600, freshness_limit_seconds=48 * 3600)
        adapter.get_earnings("AAPL")
        self.clock.advance(hours=72)
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["cache"]["state"], "hit")
        self.assertEqual(body["earnings"]["status"], STATUS_STALE)

    def test_a_future_cache_entry_is_not_trusted(self):
        adapter = self._adapter()
        cache = EarningsCache(self.cache_path)
        cache.put("AAPL", {
            "next_earnings_date": "2099-01-01",
            "release_session": "after_market",
            "date_confirmation": "estimated",
            "source": "nasdaq_earnings_date",
            "source_checked_at_utc": "2026-10-25T06:00:00Z",
            "quality_flags": [],
        })
        self.clock.jetzt = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["cache"]["state"], "live")
        self.assertEqual(body["earnings"]["next_earnings_date"], "2026-11-02")

    def test_a_corrupt_cache_file_is_a_miss_not_a_crash(self):
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        body = self._adapter().get_earnings("AAPL")
        self.assertEqual(body["earnings"]["status"], STATUS_FRESH)
        self.assertEqual(len(self.source.date_calls), 1)

    def test_an_entry_without_a_valid_timestamp_is_a_miss(self):
        cache = EarningsCache(self.cache_path)
        cache.put("AAPL", {
            "next_earnings_date": "2026-11-02",
            "release_session": "after_market",
            "date_confirmation": "estimated",
            "source": "nasdaq_earnings_date",
            "source_checked_at_utc": "yesterday",
            "quality_flags": [],
        })
        self.assertIsNone(cache.get("AAPL"))

    def test_the_write_is_atomic_and_leaves_no_temp_files(self):
        adapter = self._adapter()
        adapter.get_earnings("AAPL")
        dateien = os.listdir(self._tmp.name)
        self.assertEqual(dateien, ["earnings.json"])
        with open(self.cache_path, encoding="utf-8") as fh:
            gespeichert = json.load(fh)
        self.assertEqual(gespeichert["schema_version"], "1.0")
        self.assertEqual(gespeichert["entries"]["AAPL"]["next_earnings_date"], "2026-11-02")

    def test_an_unwritable_cache_does_not_fail_the_request(self):
        adapter = NasdaqEarningsAdapter(
            transport=self.source,
            now_fn=self.clock.now,
            cache=EarningsCache(os.path.join(self._tmp.name, "kein", "verzeichnis", "x.json")),
        )
        # A path whose parent cannot be created as a directory: a file sits there.
        sperre = os.path.join(self._tmp.name, "kein")
        with open(sperre, "w", encoding="utf-8") as fh:
            fh.write("x")
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["earnings"]["next_earnings_date"], "2026-11-02")
        self.assertEqual(body["cache"]["state"], "live_unpersisted")


class TestNullAdapter(unittest.TestCase):
    def test_no_source_says_so_instead_of_inventing_a_date(self):
        body = NullEarningsAdapter(now_fn=_Clock().now).get_earnings("AAPL")
        self.assertIsNone(body["earnings"]["next_earnings_date"])
        self.assertEqual(body["earnings"]["status"], STATUS_UNKNOWN)
        self.assertIn(FLAG_SOURCE_NOT_CONFIGURED, body["earnings"]["quality_flags"])
        self.assertIsNone(body["earnings"]["source"])
        self.assertEqual(body["cache"]["state"], "none")


if __name__ == "__main__":
    unittest.main()
