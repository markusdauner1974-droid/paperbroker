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
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from paperbroker.adapters.earnings.NasdaqEarningsAdapter import (
    _HEADERS,
    DayCalendarStore,
    EarningsCache,
    NasdaqEarningsAdapter,
    NullEarningsAdapter,
    OptionableSymbolsStore,
    requests_transport,
    urllib_transport,
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
        self.assertIn("earnings.json", dateien)
        self.assertEqual([d for d in dateien if d.startswith(".earnings-")], [])
        with open(self.cache_path, encoding="utf-8") as fh:
            gespeichert = json.load(fh)
        self.assertEqual(gespeichert["schema_version"], "1.0")
        self.assertEqual(gespeichert["entries"]["AAPL"]["next_earnings_date"], "2026-11-02")

    def test_two_tickers_do_not_lose_each_other(self):
        adapter = self._adapter()
        adapter.get_earnings("AAPL")
        adapter.get_earnings("MSFT")
        gespeichert = json.load(open(self.cache_path, encoding="utf-8"))
        self.assertEqual(sorted(gespeichert["entries"]), ["AAPL", "MSFT"])

    def test_parallel_writes_keep_every_entry(self):
        import threading

        adapter = self._adapter()
        fehler = []

        def schreiben(ticker):
            try:
                adapter.get_earnings(ticker)
            except Exception as exc:      # noqa: BLE001 - collected, then asserted
                fehler.append(exc)

        threads = [threading.Thread(target=schreiben, args=(t,))
                   for t in ("AAPL", "MSFT", "NVDA", "AMZN", "KO", "JPM", "TSLA", "META")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(fehler, [])
        gespeichert = json.load(open(self.cache_path, encoding="utf-8"))
        self.assertEqual(len(gespeichert["entries"]), 8)

    def test_the_cache_works_without_fcntl(self):
        # Windows has no flock. The in-process lock must still serialise, and
        # the adapter must import at all - an unconditional import would break
        # the whole module there.
        from paperbroker.adapters.earnings import NasdaqEarningsAdapter as modul

        alt = modul.fcntl
        modul.fcntl = None
        try:
            cache = EarningsCache(self.cache_path)
            cache.put("AAPL", {
                "next_earnings_date": "2026-11-02", "release_session": "after_market",
                "date_confirmation": "estimated", "source": "nasdaq_earnings_date",
                "source_checked_at_utc": "2026-10-24T06:00:00Z", "quality_flags": [],
            })
            cache.put("MSFT", {
                "next_earnings_date": "2026-10-28", "release_session": "after_market",
                "date_confirmation": "estimated", "source": "nasdaq_earnings_date",
                "source_checked_at_utc": "2026-10-24T06:00:00Z", "quality_flags": [],
            })
        finally:
            modul.fcntl = alt
        gespeichert = json.load(open(self.cache_path, encoding="utf-8"))
        self.assertEqual(sorted(gespeichert["entries"]), ["AAPL", "MSFT"])

    def test_the_cache_is_bounded(self):
        cache = EarningsCache(self.cache_path, max_entries=2)
        for i, ticker in enumerate(("AAPL", "MSFT", "NVDA")):
            cache.put(ticker, {
                "next_earnings_date": "2026-11-02",
                "release_session": "after_market",
                "date_confirmation": "estimated",
                "source": "nasdaq_earnings_date",
                "source_checked_at_utc": f"2026-10-2{i}T06:00:00Z",
                "quality_flags": [],
            })
        gespeichert = json.load(open(self.cache_path, encoding="utf-8"))
        self.assertEqual(sorted(gespeichert["entries"]), ["MSFT", "NVDA"])

    def test_a_future_proof_never_authorises_after_a_failed_live_query(self):
        adapter = self._adapter()
        EarningsCache(self.cache_path).put("AAPL", {
            "next_earnings_date": "2026-11-02",
            "release_session": "after_market",
            "date_confirmation": "estimated",
            "source": "nasdaq_earnings_date",
            "source_checked_at_utc": "2026-10-25T06:00:00Z",   # one day ahead
            "quality_flags": [],
        })
        self.source.fail = True
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["cache"]["state"], "stale_fallback")
        self.assertEqual(body["earnings"]["status"], STATUS_STALE)
        self.assertIn("cache_timestamp_in_future", body["earnings"]["quality_flags"])

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


class TestRequestHeaders(unittest.TestCase):
    """The request headers must never regress to empty or missing values."""

    def test_the_request_names_a_user_agent_and_asks_for_json(self):
        # Measured 2026-11-11: the refusal hits the Python stdlib http stack,
        # not the client string (curl and requests answered 200 with the SAME
        # bytes). The minimal user agent therefore stays, and the two headers a
        # browser sends alongside it are added. None of them may be empty - an
        # empty value changes what the source sees, and an absent one reads
        # like a script.
        self.assertEqual(sorted(_HEADERS),
                         ["Accept", "Accept-Language", "Referer", "User-Agent"])
        for name, wert in _HEADERS.items():
            with self.subTest(header=name):
                self.assertIsInstance(wert, str)
                self.assertTrue(wert.strip())
        self.assertEqual(_HEADERS["User-Agent"], "Mozilla/5.0")
        self.assertEqual(_HEADERS["Accept"], "application/json")
        self.assertEqual(_HEADERS["Referer"], "https://www.nasdaq.com/")


class _HeaderRecorder(BaseHTTPRequestHandler):
    """Answers one request and keeps the headers it really received."""

    received = {}

    def do_GET(self):                                   # noqa: N802 - http.server API
        type(self).received = dict(self.headers)
        body = b'{"data": {"reportText": "ok"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):               # noqa: A002 - http.server's signature
        pass


class TestDefaultTransport(unittest.TestCase):
    """The Nasdaq path must go through ``requests`` - the stdlib stack gets 403.

    The header test above pins the constant; these tests pin that the constant
    is really SENT and that the default transport is the requests one. A
    regression to ``urllib`` would keep every other test green (the fake
    transport is injected everywhere) while the deployed source answers 403.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _HeaderRecorder)
        self.addCleanup(self._server.server_close)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.addCleanup(self._server.shutdown)
        port = self._server.server_address[1]
        self._url = f"http://127.0.0.1:{port}/api/analyst/NVDA/earnings-date?assetclass=stocks"
        _HeaderRecorder.received = {}

    def _adapter(self, **kwargs):
        ordner = self._tmp.name
        kwargs.setdefault("cache", EarningsCache(os.path.join(ordner, "earnings.json")))
        kwargs.setdefault("calendar_store", DayCalendarStore(os.path.join(ordner, "calendar.json")))
        kwargs.setdefault("optionable_store",
                          OptionableSymbolsStore(os.path.join(ordner, "optionable.json")))
        return NasdaqEarningsAdapter(now_fn=_Clock().now, **kwargs)

    def test_the_default_transport_sends_the_documented_headers(self):
        status, body = requests_transport(self._url, dict(_HEADERS), 5.0)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"data": {"reportText": "ok"}})
        empfangen = {name.lower(): wert for name, wert in _HeaderRecorder.received.items()}
        for name in ("user-agent", "accept", "accept-language", "referer"):
            with self.subTest(header=name):
                self.assertIn(name, empfangen)
                self.assertTrue(empfangen[name].strip())
        self.assertEqual(empfangen["accept"], "application/json")
        self.assertEqual(empfangen["referer"], "https://www.nasdaq.com/")

    def test_the_default_transport_is_the_requests_one(self):
        self.assertIs(self._adapter()._transport, requests_transport)

    def test_the_csv_path_keeps_urllib_and_still_follows_an_injected_transport(self):
        # The CBOE download is a different host and was never blocked: it keeps
        # the stdlib transport. An injected transport still serves BOTH paths -
        # that is how every offline test in this repo drives the adapter.
        self.assertIs(self._adapter()._text_transport, urllib_transport)
        eigen = lambda url, headers, timeout: (200, b"")   # noqa: E731 - identity only
        self.assertIs(self._adapter(transport=eigen)._text_transport, eigen)


class TestBlockedSource(_Basis):
    """A 403/429 is the source answering - not a silent source.

    The deployed symptom was ``code: http_error`` for a systematic 403 and a
    start page that only said "does not answer". Both are named here.
    """

    def test_a_403_is_a_blocked_source_and_not_a_generic_http_error(self):
        body = NasdaqEarningsAdapter(
            transport=_Source(status=403), now_fn=self.clock.now,
            cache=EarningsCache(self.cache_path),
        ).get_earnings("AAPL")
        self.assertEqual(body["earnings"]["status"], STATUS_UNAVAILABLE)
        self.assertIn(FLAG_LIVE_FAILED, body["earnings"]["quality_flags"])
        self.assertEqual(body["cache"]["state"], "miss")
        self.assertEqual(body["cache"]["error"]["code"], "source_blocked")
        self.assertEqual(body["cache"]["error"]["http_status"], 403)
        self.assertIn("403", body["cache"]["error"]["message"])

    def test_a_429_is_a_throttled_source_and_not_a_generic_http_error(self):
        body = NasdaqEarningsAdapter(
            transport=_Source(status=429), now_fn=self.clock.now,
            cache=EarningsCache(self.cache_path),
        ).get_earnings("AAPL")
        self.assertEqual(body["cache"]["error"]["code"], "source_throttled")
        self.assertEqual(body["cache"]["error"]["http_status"], 429)
        self.assertIn("429", body["cache"]["error"]["message"])

    def test_any_other_status_stays_a_generic_http_error(self):
        # The two named codes are an exception list, not a replacement: a 500
        # is still "some HTTP error" and keeps its old code.
        body = NasdaqEarningsAdapter(
            transport=_Source(status=500), now_fn=self.clock.now,
            cache=EarningsCache(self.cache_path),
        ).get_earnings("AAPL")
        self.assertEqual(body["cache"]["error"]["code"], "http_error")
        self.assertEqual(body["cache"]["error"]["http_status"], 500)

    def test_a_stored_proof_survives_a_403(self):
        adapter = self._adapter()
        erst = adapter.get_earnings("AAPL")
        self.assertEqual(erst["cache"]["state"], "live")
        self.assertEqual(erst["earnings"]["next_earnings_date"], "2026-11-02")

        self.clock.advance(hours=7)          # past the 6 h TTL, inside the 48 h window
        self.source.status = 403
        body = adapter.get_earnings("AAPL")

        # The proof is served, but never as a live check: both flags say why.
        self.assertEqual(body["cache"]["state"], "stale_fallback")
        self.assertIn(FLAG_CACHE_USED, body["earnings"]["quality_flags"])
        self.assertIn(FLAG_LIVE_FAILED, body["earnings"]["quality_flags"])
        self.assertEqual(body["earnings"]["next_earnings_date"], "2026-11-02")
        # A failed attempt is not a check: the stamp of the last REAL check stays.
        self.assertEqual(body["earnings"]["source_checked_at_utc"], "2026-10-24T06:00:00Z")
        self.assertEqual(body["cache"]["stored_at_utc"], "2026-10-24T06:00:00Z")
        self.assertEqual(body["cache"]["age_seconds"], 7 * 3600)
        self.assertEqual(body["cache"]["error"]["code"], "source_blocked")
        self.assertEqual(body["cache"]["error"]["http_status"], 403)
        # Unchanged freshness rule: a 7 h old proof may still authorise (48 h).
        # Exactly that is why the two flags above have to be read.
        self.assertEqual(body["earnings"]["status"], STATUS_FRESH)

    def test_a_403_after_the_freshness_limit_is_stale(self):
        adapter = self._adapter()
        adapter.get_earnings("AAPL")
        self.clock.advance(hours=60)         # past 48 h: the proof may not authorise
        self.source.status = 403
        body = adapter.get_earnings("AAPL")
        self.assertEqual(body["cache"]["state"], "stale_fallback")
        self.assertEqual(body["earnings"]["status"], STATUS_STALE)
        self.assertEqual(body["cache"]["error"]["code"], "source_blocked")


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
