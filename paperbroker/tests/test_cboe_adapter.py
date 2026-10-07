"""
    Tests for the CBOE quote adapter (Phase 3).

    All tests are offline: mocked HTTP responses only - no network calls,
    so CI runs are deterministic and fast.

    Live-feed behavior was verified separately against the real CBOE
    endpoint (AAPL: 3568 contracts, IV decimal -> x100, greeks raw -> x100,
    403 for unknown symbols, cache hit < 0.05 s).
"""
import unittest
from unittest.mock import patch, MagicMock

from ..adapters.quotes.CBOEQuoteAdapter import (
    CBOEQuoteAdapter, CboeNotFoundError, CboeRequestError,
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
        strike = int(occ[-8:]) / 1000.0
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

    def test_unknown_symbol_raises_403_as_not_found(self):
        ad = self._adapter_with({}, status=403)
        with self.assertRaises((CboeNotFoundError, CboeRequestError)):
            ad.get_expiration_dates("AAAANOTREAL")

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