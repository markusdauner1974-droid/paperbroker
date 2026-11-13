"""
    Tests for the CBOE quote adapter (Phase 3).

    All tests are offline: mocked HTTP responses only - no network calls,
    so CI runs are deterministic and fast.

    Live-feed behavior was verified separately against the real CBOE
    endpoint (AAPL: 3568 contracts, IV decimal -> x100, greeks raw -> x100,
    403 for unknown symbols, cache hit < 0.05 s).
"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import requests

from ..adapters.quotes.CBOEQuoteAdapter import (
    CHAIN_BLOCKED,
    CHAIN_MALFORMED,
    CHAIN_NOT_LISTED,
    CHAIN_REASONS,
    CHAIN_THROTTLED,
    CHAIN_UNREACHABLE,
    CHAIN_UNUSABLE,
    CboeNotFoundError,
    CBOEQuoteAdapter,
    CboeRequestError,
    RetryBudget,
)
from ..quotes import OptionQuote


def _fake_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _chain_payload(occ_syms, current_price=100.0, timestamp='2026-10-07 06:49:35'):
    """Minimal CBOE-style chain payload for the given OCC symbols."""
    options = []
    for occ in occ_syms:
        # deterministic pseudo data derived from the symbol
        int(occ[-8:]) / 1000.0
        cp = occ[-9]
        options.append({
            "option": occ,
            "bid": 1.0 if cp == "C" else 2.0,
            "ask": 1.2,
            "bid_size": 10,
            "ask_size": 11,
            "iv": 0.25,          # CBOE decimal -> should become 25
            "open_interest": 1234,
            "volume": 567,
            "delta": -0.45 if cp == "P" else 0.45,   # raw -> becomes -45/45
            "gamma": 0.001,
            "theta": -0.02,
            "vega": 0.04,
            "rho": 0.01,
            "theo": 1.1,
            "last_trade_price": 1.15,
            "last_trade_time": "2026-10-07 15:59:59",
        })
    return {
        "timestamp": timestamp,
        "symbol": "FAKE",
        "data": {
            "symbol": "FAKE",
            "current_price": current_price,
            "options": options,
        },
    }


class TestCboeParsing(unittest.TestCase):

    def test_parse_occ_standard(self):
        r = CBOEQuoteAdapter._parse_occ("AAPL261007C00245000")
        self.assertEqual(r, ("AAPL", "2026-10-07", "call", 245.0))

    def test_parse_occ_put_strike(self):
        r = CBOEQuoteAdapter._parse_occ("SPX260118P05500000")
        self.assertEqual(r, ("SPX", "2026-01-18", "put", 5500.0))

    def test_parse_occ_mini_strike(self):
        # 8th digit is the decimal: 00100000 -> 100.0, 00550000 -> 5500.0
        r = CBOEQuoteAdapter._parse_occ("FAKE261016C00100000")
        self.assertEqual(r[3], 100.0)

    def test_parse_occ_nonstandard_returns_none(self):
        # adjusted symbols (9-digit date etc.) must not break parsing
        self.assertIsNone(CBOEQuoteAdapter._parse_occ("WEIRD"))
        self.assertIsNone(CBOEQuoteAdapter._parse_occ("AAPL000000C00245000"))


    def test_parse_occ_impossible_dates_rejected(self):
        # 21st month, 30 February - must be None, not a fake date
        self.assertIsNone(CBOEQuoteAdapter._parse_occ("FAKE262118C00100000"))
        self.assertIsNone(CBOEQuoteAdapter._parse_occ("FAKE260230C00100000"))


class TestCboeAdapter(unittest.TestCase):

    def _adapter_with(self, payload, status=200):
        ad = CBOEQuoteAdapter()
        ad._session = MagicMock()
        ad._session.get.return_value = _fake_response(payload, status)
        return ad

    def test_expiration_dates_sorted_deduplicated(self):
        payload = _chain_payload([
            "FAKE261016C00100000", "FAKE261009C00100000", "FAKE261016P00110000",
        ])
        ad = self._adapter_with(payload)
        dates = ad.get_expiration_dates("FAKE")
        self.assertEqual(dates, ["2026-10-09", "2026-10-16"])

    def test_options_maps_units_x100(self):
        payload = _chain_payload([
            "FAKE261016C00100000", "FAKE261016P00110000",
        ])
        ad = self._adapter_with(payload)
        quotes = ad.get_options("FAKE", expiration_date="2026-10-16")
        self.assertEqual(len(quotes), 2)
        q = [x for x in quotes if x.asset.option_type == 'call'][0]
        self.assertEqual(q.iv, 25.0)         # 0.25 decimal -> 25
        self.assertEqual(q.delta, 45.0)      # 0.45 raw -> 45
        self.assertEqual(q.gamma, 0.1)
        self.assertEqual(q.open_interest, 1234)
        self.assertEqual(q.volume, 567)
        self.assertEqual(q.underlying_price, 100.0)
        self.assertEqual(q.greeks_source, 'adapter')
        self.assertEqual(q.quote_timestamp, '2026-10-07 06:49:35')

    def test_options_zero_stays_zero_null_becomes_none(self):
        payload = _chain_payload(["FAKE261016C00100000"])
        payload["data"]["options"][0]["iv"] = 0        # illiquid: 0 stays 0
        payload["data"]["options"][0]["delta"] = None  # missing -> None
        ad = self._adapter_with(payload)
        q = ad.get_options("FAKE")[0]
        self.assertEqual(q.iv, 0.0)
        self.assertIsNone(q.delta)

    def test_a_blocked_client_is_named_blocked_not_unknown_symbol(self):
        # 403 is the feed refusing THIS client, not "no such symbol" - and a
        # retry cannot change a block.
        ad = self._adapter_with({}, status=403)
        with self.assertRaises(CboeRequestError) as ctx:
            ad.get_expiration_dates("AAAANOTREAL")
        self.assertEqual(ctx.exception.reason, CHAIN_BLOCKED)

    def test_get_quote_option_by_occ_symbol(self):
        payload = _chain_payload(["FAKE261016C00100000"])
        ad = self._adapter_with(payload)
        q = ad.get_quote("FAKE261016C00100000")
        self.assertIsInstance(q, OptionQuote)
        self.assertEqual(q.asset.symbol, "FAKE261016C00100000")
        self.assertEqual(q.strike, 100.0)

    def test_get_quote_option_not_in_chain(self):
        payload = _chain_payload(["FAKE261016C00100000"])
        ad = self._adapter_with(payload)
        with self.assertRaises(CboeNotFoundError):
            ad.get_quote("FAKE261016C00990000")

    def test_get_quote_stock(self):
        payload = _chain_payload(["FAKE261016C00100000"])
        ad = self._adapter_with(payload)
        stock = ad.get_quote("FAKE")
        self.assertEqual(stock.asset.symbol, "FAKE")
        self.assertEqual(stock.price, 100.0)

    def test_quote_timestamp_on_both_get_quote_paths(self):
        payload = _chain_payload(["FAKE261016C00100000"])
        ad = self._adapter_with(payload)
        q = ad.get_quote("FAKE261016C00100000")
        self.assertEqual(q.quote_timestamp, '2026-10-07 06:49:35')
        stock = ad.get_quote("FAKE")
        self.assertEqual(stock.quote_timestamp, '2026-10-07 06:49:35')

    def test_malformed_response_raises_request_error(self):
        ad = CBOEQuoteAdapter()
        ad._session = MagicMock()
        bad = MagicMock()
        bad.status_code = 200
        bad.json.return_value = {"data": {"current_price": "not-a-number", "options": "nope"}}
        ad._session.get.return_value = bad
        with self.assertRaises(CboeRequestError):
            ad.get_expiration_dates("FAKE")

    def test_malformed_option_entries_rejected_before_cache(self):
        # {"data": null} -> TypeError must map to CboeRequestError
        ad = CBOEQuoteAdapter(); ad._session = MagicMock()
        r1 = MagicMock(); r1.status_code = 200
        r1.json.return_value = {"data": None}
        ad._session.get.return_value = r1
        with self.assertRaises(CboeRequestError):
            ad.get_expiration_dates("FAKE")
        # [null] entry -> rejected before caching
        ad2 = CBOEQuoteAdapter(); ad2._session = MagicMock()
        r2 = MagicMock(); r2.status_code = 200
        r2.json.return_value = _chain_payload(["FAKE261016C00100000"])
        r2.json.return_value["data"]["options"] = [None, {"option": "ok-but-not-a-real-occ"}]
        ad2._session.get.return_value = r2
        with self.assertRaises(CboeRequestError):
            ad2.get_expiration_dates("FAKE")
        # non-string 'option' value -> rejected
        ad3 = CBOEQuoteAdapter(); ad3._session = MagicMock()
        r3 = MagicMock(); r3.status_code = 200
        r3.json.return_value = _chain_payload(["FAKE261016C00100000"])
        r3.json.return_value["data"]["options"] = [{"option": 12345}]
        ad3._session.get.return_value = r3
        with self.assertRaises(CboeRequestError):
            ad3.get_expiration_dates("FAKE")

    def test_as_date_six_digit(self):
        self.assertEqual(CBOEQuoteAdapter._as_date("261016"), "2026-10-16")
        self.assertIsNone(CBOEQuoteAdapter._as_date("000000"))

    def test_cache_hit_no_second_http(self):
        payload = _chain_payload(["FAKE261016C00100000"])
        ad = self._adapter_with(payload)
        ad.get_expiration_dates("FAKE")
        ad.get_expiration_dates("FAKE")
        ad.get_options("FAKE")
        self.assertEqual(ad._session.get.call_count, 1)  # 3 calls, 1 fetch


    def test_missing_spot_yields_none_and_does_not_crash_the_getter(self):
        # the feed may deliver a null current_price (tolerated by _fetch); the
        # value getter must not subtract that None silently
        payload = _chain_payload(["FAKE261016C00100000"], current_price=None)
        ad = self._adapter_with(payload)
        q = ad.get_options("FAKE", expiration_date="2026-10-16")[0]
        self.assertIsNone(q.underlying_price)
        self.assertIsNone(q.get_extrinsic_value())


    # ---------------------------------------------------------- Ausbau-Slice E
    def test_one_unusable_row_costs_exactly_that_row(self):
        """B-16: a row the parser accepts but the option constructor cannot
        build (29 Feb in a non-leap year) killed the whole chain before."""
        payload = _chain_payload([
            "FAKE261016C00100000", "FAKE261016P00110000", "FAKE261016C00120000",
            "FAKE260229C00130000",
        ])
        ad = self._adapter_with(payload)
        quotes = ad.get_options("FAKE")
        self.assertEqual(len(quotes), 3)
        self.assertEqual(quotes.skipped, 1)
        self.assertNotIn("FAKE260229C00130000", [q.asset.symbol for q in quotes])

    def test_a_clean_chain_skips_nothing(self):
        payload = _chain_payload(["FAKE261016C00100000", "FAKE261016P00110000"])
        ad = self._adapter_with(payload)
        quotes = ad.get_options("FAKE")
        self.assertEqual(len(quotes), 2)
        self.assertEqual(quotes.skipped, 0)

    def test_a_chain_of_only_unusable_rows_raises(self):
        """A totally unusable chain must not look like "no candidates"."""
        payload = _chain_payload(["FAKE260229C00100000", "FAKE260229P00110000"])
        ad = self._adapter_with(payload)
        with self.assertRaises(CboeNotFoundError):
            ad.get_options("FAKE")

    def test_single_option_lookup_still_reports_not_found(self):
        payload = _chain_payload(["FAKE261016C00100000"])
        ad = self._adapter_with(payload)
        with self.assertRaises(CboeNotFoundError):
            ad.get_quote("FAKE260229C00100000")

    def test_expiration_dates_offer_only_real_dates(self):
        """The 29 Feb of a non-leap year must not be offered."""
        payload = _chain_payload(["FAKE261016C00100000", "FAKE260229C00100000"])
        ad = self._adapter_with(payload)
        dates = ad.get_expiration_dates("FAKE")
        self.assertNotIn("2026-02-29", dates)
        self.assertIn("2026-10-16", dates)

    def test_a_real_leap_day_stays_selectable(self):
        payload = _chain_payload(["FAKE280229C00100000", "FAKE261016C00100000"])
        ad = self._adapter_with(payload)
        self.assertIn("2028-02-29", ad.get_expiration_dates("FAKE"))


class TestChainFailuresAreClassified(unittest.TestCase):
    """Each failure gets its own class, and only the retryable ones repeat.

    Fake session, no network: the responses are handed out in order, the sleep
    is recorded instead of waited, so the retry POLICY is what is asserted.
    """

    def _adapter(self, responses):
        ad = CBOEQuoteAdapter()
        ad._session = MagicMock()
        ad._session.get.side_effect = list(responses)
        geschlafen = []
        ad._sleep = geschlafen.append
        return ad, geschlafen

    @staticmethod
    def _ok():
        return _fake_response(_chain_payload(["FAKE261016C00100000"]))

    def test_a_throttled_call_is_retried_once_and_then_succeeds(self):
        ad, geschlafen = self._adapter([_fake_response({}, 429), self._ok()])
        quotes = ad.get_options("FAKE")
        self.assertEqual(len(quotes), 1)
        self.assertEqual(ad._session.get.call_count, 2)
        self.assertEqual(quotes.attempts, 2)
        # 0.4 s plus jitter, and only one wait.
        self.assertEqual(len(geschlafen), 1)
        self.assertAlmostEqual(geschlafen[0], 0.4, delta=0.11)

    def test_a_persistent_throttle_stops_after_three_attempts(self):
        ad, geschlafen = self._adapter([_fake_response({}, 429)] * 3)
        with self.assertRaises(CboeRequestError) as ctx:
            ad.get_options("FAKE")
        self.assertEqual(ctx.exception.reason, CHAIN_THROTTLED)
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(ad._session.get.call_count, 3)
        self.assertEqual(len(geschlafen), 2)
        self.assertAlmostEqual(geschlafen[1], 1.2, delta=0.11)

    def test_a_blocked_client_is_not_retried(self):
        ad, geschlafen = self._adapter([_fake_response({}, 403)])
        with self.assertRaises(CboeRequestError) as ctx:
            ad.get_options("FAKE")
        self.assertEqual(ctx.exception.reason, CHAIN_BLOCKED)
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(ad._session.get.call_count, 1)
        self.assertEqual(geschlafen, [])

    def test_an_unlisted_symbol_is_not_retried(self):
        ad, geschlafen = self._adapter([_fake_response({}, 404)])
        with self.assertRaises(CboeNotFoundError) as ctx:
            ad.get_options("FAKE")
        self.assertEqual(ctx.exception.reason, CHAIN_NOT_LISTED)
        self.assertEqual(ad._session.get.call_count, 1)
        self.assertEqual(geschlafen, [])

    def test_a_timeout_is_retried_and_then_succeeds(self):
        ad, geschlafen = self._adapter([requests.Timeout("slow"), self._ok()])
        quotes = ad.get_options("FAKE")
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes.attempts, 2)
        self.assertEqual(ad._session.get.call_count, 2)
        self.assertEqual(len(geschlafen), 1)

    def test_a_5xx_is_retried_like_an_unreachable_feed(self):
        ad, _ = self._adapter([_fake_response({}, 503), self._ok()])
        self.assertEqual(ad.get_options("FAKE").attempts, 2)

    def test_an_unreadable_body_is_named_malformed_and_not_retried(self):
        kaputt = MagicMock()
        kaputt.status_code = 200
        kaputt.json.return_value = {"data": None}
        ad, geschlafen = self._adapter([kaputt])
        with self.assertRaises(CboeRequestError) as ctx:
            ad.get_options("FAKE")
        self.assertEqual(ctx.exception.reason, CHAIN_MALFORMED)
        self.assertEqual(ad._session.get.call_count, 1)
        self.assertEqual(geschlafen, [])

    def test_a_cache_hit_costs_no_source_call(self):
        ad, _ = self._adapter([self._ok()])
        self.assertEqual(ad.get_options("FAKE").attempts, 1)
        self.assertEqual(ad.get_options("FAKE").attempts, 0)
        self.assertEqual(ad._session.get.call_count, 1)

    def test_every_class_is_reachable_and_distinct(self):
        self.assertEqual(len({CHAIN_BLOCKED, CHAIN_THROTTLED, CHAIN_NOT_LISTED,
                              CHAIN_UNREACHABLE, CHAIN_MALFORMED}), 5)


class _FakeClock:
    """A pinned, manually advanced monotonic clock (no real waiting)."""

    def __init__(self, start=1_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class TestAnUnusableChainIsNotUnlisted(unittest.TestCase):
    """A delivered chain with no usable row is not "no chain for this symbol".

    Before, the raise used CboeNotFoundError's class reason ``not_listed``, so
    the scan told the reader "the source lists no chain" although the source
    had delivered one and the adapter had read it. The attempts the chain cost
    were lost too (default 1).
    """

    @staticmethod
    def _unusable_payload():
        # 29 February 2026 does not exist: the OCC parse accepts it, the option
        # constructor refuses it - so every row is skipped.
        return _chain_payload(["FAKE260229C00100000", "FAKE260229P00110000"])

    def test_an_unusable_chain_is_not_called_unlisted(self):
        ad = CBOEQuoteAdapter()
        ad._session = MagicMock()
        # one throttle, then the chain arrives: it cost two attempts and the
        # error must say so instead of resetting to the default 1.
        ad._session.get.side_effect = [
            _fake_response({}, 429),
            _fake_response(self._unusable_payload()),
        ]
        ad._sleep = lambda _seconds: None
        with self.assertRaises(CboeNotFoundError) as ctx:
            ad.get_options("FAKE")
        self.assertEqual(ctx.exception.reason, CHAIN_UNUSABLE)
        self.assertNotEqual(ctx.exception.reason, CHAIN_NOT_LISTED)
        self.assertEqual(ctx.exception.attempts, 2)

    def test_the_unusable_reason_has_its_own_text(self):
        from ..earnings_scan import _chain_exclusion

        fehler = CboeNotFoundError("all 2 contracts of FAKE unusable",
                                   reason=CHAIN_UNUSABLE, attempts=2)
        ausschluss = _chain_exclusion(fehler)
        self.assertEqual(ausschluss["reason"], "unusable")
        self.assertEqual(ausschluss["message"],
                         "Die Kette wurde gelesen, aber kein Kontrakt ist brauchbar.")
        self.assertNotIn("listet", ausschluss["message"])

    def test_the_new_reason_is_a_class_of_its_own(self):
        from ..earnings_scan import CHAIN_REASON_TEXT

        self.assertIn(CHAIN_UNUSABLE, CHAIN_REASONS)
        self.assertEqual(len(set(CHAIN_REASONS)), len(CHAIN_REASONS))
        # Fail closed: a class without its own German sentence would fall back
        # to the generic "could not be read" text and lose the distinction.
        self.assertEqual(set(CHAIN_REASONS) - set(CHAIN_REASON_TEXT), set())


class TestTheRetryBudgetCapsTheWaiting(unittest.TestCase):
    """The backoff sleeps are paid from one bucket shared by all workers.

    Eight permanently failing symbols used to add ~1.7 s per worker to the
    scan's wall clock. The bucket caps the SUM; a call that finds it empty goes
    out at once with its reason instead of retrying under throttling.
    """

    def test_a_burst_of_failures_spends_at_most_the_budget(self):
        budget = RetryBudget(capacity_seconds=2.0, refill_per_second=0.0)
        ad = CBOEQuoteAdapter(retry_budget=budget)
        ad._session = MagicMock()
        ad._session.get.side_effect = lambda url, timeout=None: _fake_response({}, 429)

        geschlafen = []
        sperre = threading.Lock()

        def merken(sekunden):
            # The fake sleep records instead of waiting, so the sum of the
            # granted waits is exactly what the scan would have paid.
            with sperre:
                geschlafen.append(sekunden)

        ad._sleep = merken

        def lauf(symbol):
            try:
                ad.get_options(symbol)
            except CboeRequestError as exc:
                return exc
            raise AssertionError(f"{symbol} should have failed")

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(lauf, f"SY{i:02d}") for i in range(12)]
            ergebnisse = [f.result(timeout=10) for f in futures]

        # Nobody hangs, every run fails with its own class.
        self.assertEqual(len(ergebnisse), 12)
        for exc in ergebnisse:
            self.assertEqual(exc.reason, CHAIN_THROTTLED)
        # The sum of all waits never exceeds the capacity (+ rounding).
        self.assertLessEqual(sum(geschlafen), 2.0 + 0.01)
        # 0.4 + 1.2 + 0.4 is the most the bucket can pay for.
        self.assertLessEqual(len(geschlafen), 3)
        # And the bucket really runs dry: at least one call could not wait.
        self.assertIn(1, [exc.attempts for exc in ergebnisse])

    def test_without_budget_the_call_does_not_retry(self):
        ad = CBOEQuoteAdapter(retry_budget=RetryBudget(capacity_seconds=0,
                                                       refill_per_second=0))
        ad._session = MagicMock()
        ad._session.get.return_value = _fake_response({}, 429)
        geschlafen = []
        ad._sleep = geschlafen.append
        with self.assertRaises(CboeRequestError) as ctx:
            ad.get_options("FAKE")
        self.assertEqual(ctx.exception.reason, CHAIN_THROTTLED)
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(ad._session.get.call_count, 1)
        self.assertEqual(geschlafen, [])

    def test_the_budget_refills_over_time(self):
        uhr = _FakeClock()
        budget = RetryBudget(capacity_seconds=0.5, refill_per_second=1.0, clock=uhr)
        ad = CBOEQuoteAdapter(retry_budget=budget)
        ad._session = MagicMock()
        ad._sleep = lambda _seconds: None
        ad._session.get.side_effect = [
            _fake_response({}, 429),                                     # FAKE, attempt 1
            _fake_response(_chain_payload(["FAKE261016C00100000"])),     # FAKE, attempt 2
            _fake_response({}, 429),                                     # MSFT: no budget left
            _fake_response({}, 429),                                     # MSFT again after refill
            _fake_response(_chain_payload(["MSFT261016C00100000"])),     # MSFT, attempt 2
        ]

        self.assertEqual(ad.get_options("FAKE").attempts, 2)   # the wait was paid
        with self.assertRaises(CboeRequestError) as ctx:
            ad.get_options("MSFT")                             # the bucket is empty
        self.assertEqual(ctx.exception.attempts, 1)

        uhr.advance(1.0)                                       # the source calmed down
        self.assertEqual(ad.get_options("MSFT").attempts, 2)   # a retry is possible again
        self.assertEqual(ad._session.get.call_count, 5)


class TestOptionQuoteExtension(unittest.TestCase):

    def test_new_fields_default_none(self):
        from ..assets import asset_factory
        q = OptionQuote('2026-10-07', asset_factory("FAKE261016C00100000"))
        self.assertIsNone(q.open_interest)
        self.assertIsNone(q.volume)

    def test_new_fields_set(self):
        from ..assets import asset_factory
        q = OptionQuote('2026-10-07', asset_factory("FAKE261016C00100000"),
                        open_interest=77, volume=88)
        self.assertEqual(q.open_interest, 77)
        self.assertEqual(q.volume, 88)


if __name__ == '__main__':
    unittest.main()
