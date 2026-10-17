"""Acceptance tests for the unfiltered option-chain interface (Ausbau-Slice OC1).

Subject: GET /api/v1/option-chain and the pure serializer behind it
(paperbroker/option_chain_api.py). Every expectation below is derived from a
fixture built INSIDE this file. No live CBOE measurement is asserted here and
no test touches the network:

  - the route is driven through the Flask test client with an injected adapter
    (acceptance test 12 replaces the live smoke of the order with exactly this),
  - the cache property (acceptance test 10) needs the REAL CBOEQuoteAdapter, so
    only its HTTP transport is replaced by a counting stub - the adapter, the
    route and the serializer under test stay the shipped code.

The twelve acceptance tests of the order, one class each:

   1. TestUnfilteredChain ............ nothing is screened away
   2. TestNoScreenerCoupling ......... screener and trend are never consulted
   3. TestAllExpirations ............. no selection vs. one selection
   4. TestCompletenessAccounting ..... every source row is accounted for
   5. TestMissingZeroAndNonFinite .... null, 0 and non-finite stay distinct
   6. TestUnitsAreSourceDecimals ..... source 0.25 stays 0.25
   7. TestTimeHandling ............... timezone-free source time, two ages
   8. TestContractMetadataUnknown .... no invented multiplier
   9. TestErrorContract .............. the eight measured error paths
  10. TestSnapshotCacheReuse ......... one source fetch per ticker
  11. TestScreenerRoutesPreserved .... /, /api/screen, /healthz untouched
  12. TestRouteOverTestClient ........ route round trip without network

Rules observed throughout: floats are compared with a tolerance
(assertAlmostEqual); a missing key is only asserted AFTER status code and
content type have been pinned; the injected fixture is never the source of the
expectation itself (the expectation is computed from the fixture's literal
values).
"""

import copy
import json
import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import arrow

from paperbroker import server as server_module
from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
    CboeNotFoundError,
    CBOEQuoteAdapter,
    CboeRequestError,
)
from paperbroker.option_chain_api import SCHEMA_VERSION, listed_expirations, serialise_snapshot
from paperbroker.server import create_app

# --------------------------------------------------------------------- #
# pinned facts of the fixtures (all local, all reproducible)
# --------------------------------------------------------------------- #

# The frozen clock of every route test. A Saturday, so the weekend case of
# acceptance test 7 is not accidentally a weekday.
CLOCK = "2026-10-17T12:00:00+00:00"

# The envelope timestamp exactly as the CBOE feed ships it: no timezone, so
# no UTC value and no age can be derived from it.
SOURCE_STAMP = "2026-10-17 03:33:37"

CALL_NEAR = "TST261120C00100000"      # 2026-11-20 call, strike 100.0
PUT_NEAR = "TST261120P00095000"       # 2026-11-20 put,  strike  95.0
CALL_SAT = "TST261121C00110000"       # 2026-11-21 (Saturday) call, strike 110.0
CALL_FAR = "TST261218C00105000"       # 2026-12-18 call, strike 105.0
PUT_FAR = "TST261218P00100000"        # 2026-12-18 put,  strike 100.0
CALL_2027 = "TST270618C00120000"      # 2027-06-18 call, strike 120.0
BAD_SYMBOL = "NOT-AN-OCC-SYMBOL"      # no OCC shape at all

# Names the screener interface uses. The chain endpoint may not carry any of
# them - that is the whole point of an UNfiltered view.
SCREENER_KEYS = frozenset({
    "score", "spread_pct", "spread_abs", "verticals", "vertical_drops",
    "criteria", "candidates", "dropped", "dropped_reasons", "freshness",
    "budget", "fees", "quantity", "max_spread_pct", "min_oi",
})


def _clock():
    """Injected route clock: arrow, because the route calls .to('utc')."""
    return arrow.get(CLOCK)


def _now_dt():
    """Injected serializer clock: a datetime, what serialise_snapshot expects."""
    return datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


def _row(symbol, **overrides):
    """One well-formed CBOE-style source row, with single fields overridden."""
    row = {
        "option": symbol,
        "bid": 1.10,
        "ask": 1.25,
        "bid_size": 12,
        "ask_size": 14,
        "iv": 0.25,
        "delta": 0.40,
        "gamma": 0.01,
        "theta": -0.02,
        "vega": 0.03,
        "rho": 0.01,
        "theo": 1.18,
        "open_interest": 640,
        "volume": 35,
        "last_trade_price": 1.15,
        "last_trade_time": "2026-10-16T11:40:42",
        "open": 1.05,
        "high": 1.30,
        "low": 0.98,
        "change": 0.04,
        "percent_change": 3.6,
        "prev_day_close": 1.11,
        "tick": "-",
    }
    row.update(overrides)
    return row


def _fixture_records():
    """Five clean rows: two expirations with call AND put, plus a Saturday date."""
    return [
        _row(CALL_NEAR),
        _row(PUT_NEAR, bid=1.05, ask=1.20, delta=-0.35),
        _row(CALL_SAT, bid=0.90, ask=1.05, delta=0.30),
        _row(CALL_FAR, bid=2.00, ask=2.20, delta=0.55),
        _row(PUT_FAR, bid=1.80, ask=2.00, delta=-0.45),
    ]


def _fixture_expirations():
    return ["2026-11-20", "2026-11-21", "2026-12-18"]


def _messy_records():
    """Nine rows a screener would mostly throw away - or not understand.

    Row by row (source values in brackets, every one of them deliberate):
      0  CALL_NEAR  OI 3, volume 0, bid_size missing, bid 0.05 / ask 4.00
                    (wide spread, low open interest, zero volume, old trade)
      1  PUT_NEAR   bid 0 (a real zero, not a missing quote), iv missing,
                    volume missing
      2  CALL_SAT   bid 2.00 > ask 1.80 (crossed), OI 0
      3  CALL_FAR   OI 5, volume 0, iv 0.9
      4  CALL_2027  OI 1, volume 0, bid/ask missing, 244 calendar days out
      5  CALL_NEAR  again - a duplicate source symbol
      6  BAD_SYMBOL a row whose option symbol is not OCC
      7  option=98765421 (not a string) - unusable, must not vanish
      8  "not a json object" - not even a mapping, must not vanish
    """
    return [
        _row(CALL_NEAR, open_interest=3, volume=0, bid_size=None, bid=0.05,
             ask=4.00, last_trade_price=0.60),
        _row(PUT_NEAR, bid=0.0, ask=0.05, open_interest=12, volume=None,
             iv=None, delta=-0.05, last_trade_price=0.02),
        _row(CALL_SAT, bid=2.00, ask=1.80, open_interest=0, volume=0),
        _row(CALL_FAR, open_interest=5, volume=0, iv=0.9),
        _row(CALL_2027, open_interest=1, volume=0, bid=None, ask=None),
        _row(CALL_NEAR, open_interest=3, volume=0, bid_size=None, bid=0.05,
             ask=4.00, last_trade_price=0.60),
        {"option": BAD_SYMBOL, "bid": 1.0, "ask": 1.1},
        {"option": 98765421, "bid": 1.0, "ask": 1.1},
        "not a json object",
    ]


def _messy_expirations():
    return ["2026-11-20", "2026-11-21", "2026-12-18", "2027-06-18"]


def _snapshot(records, *, current_price=100.0, source_timestamp_raw=SOURCE_STAMP,
              fetched_at=None, underlying_symbol="TST"):
    """A raw adapter snapshot - the same five keys the adapter really returns."""
    return {
        "underlying_symbol": underlying_symbol,
        "source_timestamp_raw": source_timestamp_raw,
        "current_price": current_price,
        "records": list(records),
        "fetched_at": arrow.get(CLOCK) if fetched_at is None else fetched_at,
    }


def _keys_in(payload):
    """Every mapping key anywhere in a JSON payload, recursively."""
    gefunden = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            gefunden.add(key)
            gefunden |= _keys_in(value)
    elif isinstance(payload, list):
        for item in payload:
            gefunden |= _keys_in(item)
    return gefunden


def _reject_constant(name):
    """json.loads(parse_constant=...): NaN/Infinity are not JSON (RFC 8259)."""
    raise AssertionError(f"non-finite JSON constant in the body: {name}")


class _FakeChainAdapter:
    """Hands the route a canned snapshot and counts every request for it.

    Only the CBOE adapter is substituted: the route's own argument parsing, the
    facade delegation and the serializer stay the shipped code.
    """

    def __init__(self, records=None, expirations=None, current_price=100.0,
                 source_timestamp_raw=SOURCE_STAMP, fetched_at=None, error=None):
        self.records = list(records) if records is not None else []
        self.expirations = list(expirations) if expirations is not None else []
        self.current_price = current_price
        self.source_timestamp_raw = source_timestamp_raw
        self.fetched_at = arrow.get(CLOCK) if fetched_at is None else fetched_at
        self.error = error
        self.snapshot_calls = 0
        self.expiration_calls = 0

    def get_option_chain_snapshot(self, underlying_symbol):
        self.snapshot_calls += 1
        if self.error is not None:
            raise self.error
        return {
            "underlying_symbol": underlying_symbol,
            "source_timestamp_raw": self.source_timestamp_raw,
            "current_price": self.current_price,
            "records": list(self.records),
            "fetched_at": self.fetched_at,
        }

    def get_expiration_dates(self, underlying_asset=None):
        self.expiration_calls += 1
        return list(self.expirations)

    def get_options(self, underlying_asset=None, expiration_date=None):
        return []


class _HostileScreener:
    """Explodes on ANY use. Touching it is the failure, not the exception."""

    @property
    def criteria(self):
        raise AssertionError("the option-chain endpoint read screener.criteria")

    def screen_chain(self, *args, **kwargs):
        raise AssertionError("the option-chain endpoint called the screener")


class _HostileTrendProvider:
    """A trend provider that is reachable, counted - and always fails."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = 0
        _HostileTrendProvider.instances.append(self)

    def get_trend(self, symbol):
        self.calls += 1
        raise RuntimeError(f"trend source down for {symbol}")


class _StubResponse:
    """Minimal requests.Response stand-in: status_code plus json()."""

    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("stub response carries no payload")
        return self._payload


class _CountingSession:
    """HTTP transport stub that records the symbol of every fetch it served."""

    def __init__(self, payloads, missing_status=404):
        self.payloads = payloads
        self.missing_status = missing_status
        self.headers = {}
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        payload = self.payloads.get(self.symbols_of(url))
        if payload is None:
            return _StubResponse(status=self.missing_status)
        return _StubResponse(payload=payload)

    @staticmethod
    def symbols_of(url):
        return url.rsplit("/", 1)[-1].removesuffix(".json")

    @property
    def fetched_symbols(self):
        return [self.symbols_of(url) for url in self.urls]


def _cboe_payload(records, symbol="TST", current_price=100.0, timestamp=SOURCE_STAMP):
    """The real CBOE envelope the adapter's own validation accepts."""
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "data": {
            "symbol": symbol,
            "current_price": current_price,
            "options": records,
        },
    }


class _ChainTestCase(unittest.TestCase):
    """Shared plumbing: an app with an injected adapter, JSON access helpers."""

    def _app_and_client(self, adapter, screener=None, now_fn=None):
        app = create_app(quote_adapter=adapter, screener=screener,
                         now_fn=now_fn if now_fn is not None else _clock)
        app.config["TESTING"] = True
        return app, app.test_client()

    def _chain(self, client, **params):
        return client.get("/api/v1/option-chain", query_string=params)

    def _body(self, res, expected_status=200):
        """Pin status AND content type before anything is read out of the body."""
        self.assertEqual(res.status_code, expected_status, res.get_data(as_text=True))
        self.assertTrue(res.content_type.startswith("application/json"),
                        f"content type was {res.content_type!r}")
        return json.loads(res.get_data(as_text=True))

    def _contract(self, body, symbol):
        treffer = [c for c in body["contracts"] if c["symbol"] == symbol]
        self.assertEqual(len(treffer), 1, f"expected exactly one contract for {symbol}")
        return treffer[0]

    def _contracts(self, body, symbol):
        return [c for c in body["contracts"] if c["symbol"] == symbol]

    def _first_contract(self, body, symbol):
        """First contract for a symbol - for fixtures that carry it twice."""
        treffer = self._contracts(body, symbol)
        self.assertGreaterEqual(len(treffer), 1, f"no contract for {symbol}")
        return treffer[0]


# --------------------------------------------------------------------- #
# 1 - unfiltered
# --------------------------------------------------------------------- #

class TestUnfilteredChain(_ChainTestCase):
    """1: a chain every screener would mostly drop is still delivered in full."""

    def setUp(self):
        self.adapter = _FakeChainAdapter(records=_messy_records(),
                                         expirations=_messy_expirations())
        _, self.client = self._app_and_client(self.adapter)

    def test_low_oi_zero_volume_and_wide_spread_survive(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        low = self._first_contract(body, CALL_NEAR)     # the fixture carries it twice
        self.assertEqual(low["open_interest"], 3)          # far below min_oi 500
        self.assertEqual(low["volume"], 0)                 # volume floor not applied
        self.assertAlmostEqual(low["bid"], 0.05, places=10)
        self.assertAlmostEqual(low["ask"], 4.00, places=10)  # ~195 % of mid, kept
        self.assertIsNone(low["bid_size"])
        self.assertIn("BID_SIZE_MISSING", low["quality_flags"])

    def test_zero_and_crossed_quotes_and_missing_values_are_reported_not_removed(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        # a real zero bid is data, not a gap
        put = self._contract(body, PUT_NEAR)
        self.assertEqual(put["bid"], 0.0)
        self.assertIn("BID_ZERO", put["quality_flags"])
        self.assertIsNone(put["iv_decimal"])
        self.assertIn("IV_MISSING", put["quality_flags"])
        self.assertIsNone(put["volume"])
        # a crossed quote is delivered as the source gave it, only flagged
        crossed = self._contract(body, CALL_SAT)
        self.assertAlmostEqual(crossed["bid"], 2.00, places=10)
        self.assertAlmostEqual(crossed["ask"], 1.80, places=10)
        self.assertIn("CROSSED_QUOTE", crossed["quality_flags"])
        # neither side of the quote exists
        thin = self._contract(body, CALL_2027)
        self.assertIsNone(thin["bid"])
        self.assertIsNone(thin["ask"])
        self.assertIn("BID_MISSING", thin["quality_flags"])
        self.assertIn("ASK_MISSING", thin["quality_flags"])

    def test_expiration_more_than_150_days_out_is_still_listed_and_returned(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        weit = self._contract(body, CALL_2027)
        self.assertEqual(weit["expiration"], "2027-06-18")
        # 2026-10-17 -> 2027-06-18 is 244 calendar days: no DTE window is applied
        self.assertGreater((arrow.get("2027-06-18").date() - arrow.get(CLOCK[:10]).date()).days, 150)
        self.assertIn("2027-06-18", body["listed_expirations"])

    def test_old_source_time_is_carried_but_not_repaired(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        self.assertEqual(body["snapshot"]["source_timestamp_raw"], SOURCE_STAMP)
        low = self._first_contract(body, CALL_NEAR)
        self.assertEqual(low["last_trade_time_raw"], "2026-10-16T11:40:42")
        self.assertIn("LAST_TRADE_TIME_NO_TIMEZONE", low["quality_flags"])

    def test_no_score_no_spread_no_criteria_in_the_body(self):
        res = self._chain(self.client, ticker="TST")
        body = self._body(res)
        gefunden = _keys_in(body)
        self.assertEqual(sorted(SCREENER_KEYS & gefunden), [],
                         "the unfiltered view must not carry screener concepts")

    def test_every_source_row_is_still_present_or_explicitly_counted(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        snap = body["snapshot"]
        self.assertEqual(snap["source_record_count"], len(_messy_records()))
        self.assertEqual(
            snap["returned_record_count"] + snap["unassigned_record_count"]
            + snap["invalid_record_count"] + snap["outside_requested_expiration_count"],
            snap["source_record_count"],
        )


# --------------------------------------------------------------------- #
# 2 - no screener coupling
# --------------------------------------------------------------------- #

class TestNoScreenerCoupling(_ChainTestCase):
    """2: screener and trend provider raise on use - the endpoint answers anyway."""

    def test_chain_answers_with_a_screener_that_raises_on_use(self):
        screener = _HostileScreener()
        adapter = _FakeChainAdapter(records=_fixture_records(),
                                    expirations=_fixture_expirations())
        app, client = self._app_and_client(adapter, screener=screener)
        res = self._chain(client, ticker="TST")
        body = self._body(res)
        self.assertEqual(len(body["contracts"]), len(_fixture_records()))
        # the object really is armed: on the screener route it does explode
        self.assertTrue(app.config["TESTING"])
        with self.assertRaises(AssertionError):
            client.get("/api/screen", query_string={"ticker": "TST"})

    def test_chain_never_consults_the_trend_provider(self):
        _HostileTrendProvider.instances = []
        adapter = _FakeChainAdapter(records=_fixture_records(),
                                    expirations=_fixture_expirations())
        _, client = self._app_and_client(adapter)
        env = {"PAPERBROKER_TREND_MCP_URL": "http://127.0.0.1:9/trend",
               "PAPERBROKER_TREND_TOKEN": "test-token"}
        with patch.dict(os.environ, env), \
                patch.object(server_module, "_MCPTrendProvider", _HostileTrendProvider):
            res = self._chain(client, ticker="TST")
            body = self._body(res)
            self.assertNotIn("trend", body)
            self.assertEqual([p.calls for p in _HostileTrendProvider.instances], [],
                             "the chain endpoint must not reach the trend source")
            # the injection is live: the screener route does reach it ...
            scan = client.get("/api/screen",
                              query_string={"ticker": "TST", "expiration": "2026-11-20"})
            self.assertEqual(scan.status_code, 200)
            self.assertEqual(sum(p.calls for p in _HostileTrendProvider.instances), 1)

    def test_chain_answers_without_a_screener_object_at_all(self):
        adapter = _FakeChainAdapter(records=_fixture_records(),
                                    expirations=_fixture_expirations())
        _, client = self._app_and_client(adapter, screener=None)
        body = self._body(self._chain(client, ticker="TST"))
        self.assertEqual(sorted(_keys_in(body) & SCREENER_KEYS), [])


# --------------------------------------------------------------------- #
# 3 - all expirations
# --------------------------------------------------------------------- #

class TestAllExpirations(_ChainTestCase):
    """3: without a selection every listed contract; with one, that date plus counts."""

    def setUp(self):
        self.records = _fixture_records() + [{"option": BAD_SYMBOL, "bid": 1.0, "ask": 1.1}]
        self.adapter = _FakeChainAdapter(records=self.records,
                                         expirations=_fixture_expirations())
        _, self.client = self._app_and_client(self.adapter)

    def test_without_expiration_calls_and_puts_of_every_expiration(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        paare = {(c["expiration"], c["option_type"]) for c in body["contracts"]}
        self.assertEqual(paare, {
            ("2026-11-20", "call"), ("2026-11-20", "put"),
            ("2026-11-21", "call"),
            ("2026-12-18", "call"), ("2026-12-18", "put"),
        })
        self.assertEqual(body["requested_expiration"], None)
        self.assertEqual(body["snapshot"]["returned_record_count"], 5)
        self.assertEqual(body["snapshot"]["outside_requested_expiration_count"], 0)
        self.assertEqual(body["listed_expirations"], _fixture_expirations())
        # the strike comes from the symbol, not from a lookup table
        self.assertAlmostEqual(self._contract(body, CALL_NEAR)["strike"], 100.0, places=10)
        self.assertAlmostEqual(self._contract(body, PUT_NEAR)["strike"], 95.0, places=10)

    def test_with_expiration_only_that_date_plus_reported_unassignable_rows(self):
        body = self._body(self._chain(self.client, ticker="TST", expiration="2026-11-20"))
        self.assertEqual(body["requested_expiration"], "2026-11-20")
        self.assertEqual({c["expiration"] for c in body["contracts"]}, {"2026-11-20"})
        self.assertEqual({c["symbol"] for c in body["contracts"]}, {CALL_NEAR, PUT_NEAR})
        snap = body["snapshot"]
        self.assertEqual(snap["returned_record_count"], 2)
        self.assertEqual(snap["outside_requested_expiration_count"], 3)
        self.assertEqual(snap["source_record_count"], len(self.records))
        # the unreadable row is still reported, not silently absorbed
        self.assertEqual(snap["unassigned_record_count"], 1)
        self.assertEqual(body["unassigned_records"][0]["symbol"], BAD_SYMBOL)
        # the full expiration list is still published, not narrowed to the selection
        self.assertEqual(body["listed_expirations"], _fixture_expirations())

    def test_empty_expiration_means_no_selection(self):
        body = self._body(self._chain(self.client, ticker="TST", expiration=""))
        self.assertIsNone(body["requested_expiration"])
        self.assertEqual(body["snapshot"]["returned_record_count"], 5)
        self.assertEqual(body["snapshot"]["outside_requested_expiration_count"], 0)


# --------------------------------------------------------------------- #
# 4 - completeness accounting
# --------------------------------------------------------------------- #

class TestCompletenessAccounting(_ChainTestCase):
    """4: the counters add up to the source rows; nothing disappears quietly."""

    def setUp(self):
        self.records = _messy_records()
        self.adapter = _FakeChainAdapter(records=self.records,
                                         expirations=_messy_expirations())
        _, self.client = self._app_and_client(self.adapter)

    def test_counters_balance_every_source_row(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        snap = body["snapshot"]
        self.assertEqual(snap["source_record_count"], 9)
        self.assertEqual(snap["returned_record_count"], 6)
        self.assertEqual(snap["unassigned_record_count"], 1)
        self.assertEqual(snap["invalid_record_count"], 2)   # bad int option + non-object
        self.assertEqual(snap["outside_requested_expiration_count"], 0)
        self.assertEqual(
            snap["returned_record_count"] + snap["unassigned_record_count"]
            + snap["invalid_record_count"] + snap["outside_requested_expiration_count"],
            snap["source_record_count"],
        )
        self.assertEqual(snap["completeness"], "partial")

    def test_unreadable_symbol_is_reported_with_its_original_payload(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        eintrag = body["unassigned_records"][0]
        self.assertEqual(eintrag["symbol"], BAD_SYMBOL)
        self.assertEqual(eintrag["parse_status"], "unsupported_symbol")
        self.assertIn("UNSUPPORTED_SYMBOL", eintrag["quality_flags"])
        # the row keeps its content and gets nothing guessed for it:
        # no call/put, no strike, no expiration is invented for it
        self.assertAlmostEqual(eintrag["source_record"]["bid"], 1.0, places=10)
        self.assertNotIn("option_type", eintrag)
        self.assertNotIn("strike", eintrag)
        self.assertNotIn("expiration", eintrag)

    def test_non_string_and_non_object_rows_are_counted_as_invalid(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        snap = body["snapshot"]
        # One row whose "option" is not a string and one row that is not a
        # mapping at all. Both are counted AND both are listed: a row that is
        # only counted, with no payload anywhere, is exactly the "disappears
        # silently" the order forbids. The mapping-less row is reported with
        # its raw value instead.
        self.assertEqual(snap["invalid_record_count"], 2)
        self.assertEqual(len(body["invalid_records"]), 2)
        symbols = [e.get("symbol") for e in body["invalid_records"]]
        self.assertIn(98765421, symbols)
        nicht_objekt = [e for e in body["invalid_records"] if e.get("symbol") is None]
        self.assertEqual(len(nicht_objekt), 1)
        self.assertEqual(nicht_objekt[0]["reason"], "source record is not a JSON object")
        self.assertIn("raw", nicht_objekt[0]["source_record"])
        erste = [e for e in body["invalid_records"] if e.get("symbol") == 98765421][0]
        self.assertEqual(erste["parse_status"], "unsupported_symbol")
        self.assertEqual(erste["reason"],
                         "option symbol does not match the OCC format")
        self.assertTrue(any("not JSON objects" in w for w in snap["warnings"]),
                        snap["warnings"])

    def test_duplicate_source_symbols_are_kept_and_marked(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        doppelt = self._contracts(body, CALL_NEAR)
        self.assertEqual(len(doppelt), 2)
        for c in doppelt:
            self.assertIn("DUPLICATE_SOURCE_SYMBOL", c["quality_flags"])
        self.assertEqual([c["open_interest"] for c in doppelt], [3, 3])
        self.assertTrue(any("not deduplicated" in w for w in body["snapshot"]["warnings"]),
                        body["snapshot"]["warnings"])
        # an unrelated contract carries no duplicate mark
        self.assertNotIn("DUPLICATE_SOURCE_SYMBOL", self._contract(body, CALL_FAR)["quality_flags"])

    def test_a_clean_chain_is_complete_and_an_empty_one_unknown(self):
        sauber = _FakeChainAdapter(records=_fixture_records(),
                                   expirations=_fixture_expirations())
        _, client = self._app_and_client(sauber)
        body = self._body(self._chain(client, ticker="TST"))
        self.assertEqual(body["snapshot"]["completeness"], "complete")
        self.assertEqual(body["snapshot"]["returned_record_count"], 5)

        leer = _FakeChainAdapter(records=[], expirations=[])
        _, client2 = self._app_and_client(leer)
        leer_body = self._body(self._chain(client2, ticker="TST"))
        self.assertEqual(leer_body["contracts"], [])
        self.assertEqual(leer_body["snapshot"]["source_record_count"], 0)
        self.assertEqual(leer_body["snapshot"]["completeness"], "unknown")
        self.assertEqual(leer_body["listed_expirations"], [])


# --------------------------------------------------------------------- #
# 5 - null, zero and non-finite
# --------------------------------------------------------------------- #

class TestMissingZeroAndNonFinite(_ChainTestCase):
    """5: missing stays null, a real 0 stays 0, a last trade is never a mid."""

    def setUp(self):
        self.records = [
            _row("TST261120C00100000", bid=None, ask=None, bid_size=None,
                 ask_size=None, open_interest=None, volume=None, iv=None,
                 delta=None, last_trade_price=None),
            _row("TST261120C00105000", bid=0.0, ask=0.0, bid_size=0, ask_size=0,
                 open_interest=0, volume=0, iv=0.0, delta=0.0,
                 last_trade_price=None, bid_size_extra=0),
            _row("TST261120P00095000", bid=1.00, ask=1.20, last_trade_price=None),
            _row("TST261218C00105000", bid=float("nan"), delta=float("inf"),
                 open_interest=float("-inf"), volume=7),
        ]
        self.adapter = _FakeChainAdapter(records=self.records, expirations=[])
        _, self.client = self._app_and_client(self.adapter)
        self.res = self._chain(self.client, ticker="TST")
        self.raw = self.res.get_data(as_text=True)
        self.body = self._body(self.res)

    def test_missing_values_stay_null(self):
        c = self._contract(self.body, "TST261120C00100000")
        for feld in ("bid", "ask", "bid_size", "ask_size", "open_interest",
                     "volume", "iv_decimal", "delta", "last_trade_price"):
            self.assertIsNone(c[feld], feld)
            self.assertIsNot(c[feld], 0, feld)

    def test_a_real_zero_stays_zero(self):
        c = self._contract(self.body, "TST261120C00105000")
        for feld in ("bid", "ask", "bid_size", "ask_size", "open_interest",
                     "volume", "iv_decimal", "delta"):
            self.assertIsNotNone(c[feld], feld)
            self.assertEqual(c[feld], 0, feld)
        self.assertIn("BID_ZERO", c["quality_flags"])
        self.assertIn("ASK_ZERO", c["quality_flags"])
        # iv 0 is not "missing iv": the two facts carry different flags
        self.assertNotIn("IV_MISSING", c["quality_flags"])

    def test_last_trade_price_is_never_replaced_by_the_mid(self):
        mitte = self._contract(self.body, "TST261120P00095000")
        self.assertIsNone(mitte["last_trade_price"])
        self.assertAlmostEqual(mitte["bid"], 1.00, places=10)
        self.assertAlmostEqual(mitte["ask"], 1.20, places=10)
        self.assertAlmostEqual(mitte["theoretical_price"], 1.18, places=10)

    def test_non_finite_values_become_null_and_flagged(self):
        c = self._contract(self.body, "TST261218C00105000")
        self.assertIsNone(c["bid"])
        self.assertIsNone(c["delta"])
        self.assertIsNone(c["open_interest"])
        self.assertIn("NONFINITE_VALUE", c["quality_flags"])
        self.assertEqual(c["volume"], 7)
        self.assertIsNone(c["source_record"]["bid"])

    def test_the_raw_body_carries_no_nan_or_infinity_and_is_strict_json(self):
        self.assertNotIn("NaN", self.raw)
        self.assertNotIn("Infinity", self.raw)
        # a throwing parse_constant makes the strictness itself the assertion
        geparst = json.loads(self.raw, parse_constant=_reject_constant)
        self.assertEqual(geparst["schema_version"], SCHEMA_VERSION)


# --------------------------------------------------------------------- #
# 6 - units
# --------------------------------------------------------------------- #

class TestUnitsAreSourceDecimals(_ChainTestCase):
    """6: the serializer reports the source decimals, not the x100 OptionQuote scale."""

    def setUp(self):
        self.adapter = _FakeChainAdapter(records=_fixture_records(),
                                         expirations=_fixture_expirations())
        _, self.client = self._app_and_client(self.adapter)

    def test_source_iv_and_delta_are_passed_through_unscaled(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        c = self._contract(body, CALL_NEAR)
        self.assertAlmostEqual(c["iv_decimal"], 0.25, places=10)   # source said 0.25
        self.assertAlmostEqual(c["delta"], 0.40, places=10)        # source said 0.40
        self.assertNotEqual(c["iv_decimal"], 25.0)                 # not the x100 field
        self.assertNotEqual(c["delta"], 40.0)

    def test_the_other_greeks_keep_the_source_scale_too(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        c = self._contract(body, CALL_NEAR)
        self.assertAlmostEqual(c["gamma"], 0.01, places=10)
        self.assertAlmostEqual(c["theta"], -0.02, places=10)
        self.assertAlmostEqual(c["vega"], 0.03, places=10)
        self.assertAlmostEqual(c["rho"], 0.01, places=10)

    def test_the_serializer_reads_the_raw_row_the_same_way(self):
        # same expectation one level below the route, on the same fixture value
        body = serialise_snapshot(_snapshot(_fixture_records()), ticker="TST",
                                  requested_expiration=None, now_fn=_now_dt)
        c = self._contract(body, CALL_NEAR)
        self.assertAlmostEqual(c["iv_decimal"], 0.25, places=10)
        self.assertAlmostEqual(c["delta"], 0.40, places=10)


# --------------------------------------------------------------------- #
# 7 - time
# --------------------------------------------------------------------- #

class TestTimeHandling(_ChainTestCase):
    """7: timezone-free source time, two separate ages, weekend date is a date."""

    def setUp(self):
        self.adapter = _FakeChainAdapter(
            records=_messy_records(), expirations=_messy_expirations(),
            fetched_at=arrow.get("2026-10-17T10:00:00+00:00"))
        _, self.client = self._app_and_client(self.adapter)

    def test_served_at_follows_the_injected_clock(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        self.assertEqual(body["snapshot"]["served_at_utc"], "2026-10-17T12:00:00+00:00")

    def test_source_time_without_a_timezone_yields_nulls_and_a_flag(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        snap = body["snapshot"]
        self.assertEqual(snap["source_timestamp_raw"], SOURCE_STAMP)
        self.assertIsNone(snap["source_timestamp_utc"])
        self.assertIsNone(snap["source_timestamp_timezone"])
        self.assertIsNone(snap["source_age_seconds"])
        self.assertIn("SOURCE_TIMEZONE_UNKNOWN", body["quality_flags"])
        self.assertTrue(any("timezone" in w for w in snap["warnings"]), snap["warnings"])

    def test_missing_source_time_is_null_not_the_fetch_time(self):
        body = serialise_snapshot(_snapshot(_fixture_records(), source_timestamp_raw=None),
                                  ticker="TST", requested_expiration=None, now_fn=_now_dt)
        snap = body["snapshot"]
        self.assertIsNone(snap["source_timestamp_raw"])
        self.assertIsNone(snap["source_timestamp_utc"])
        self.assertIsNone(snap["source_age_seconds"])
        # fetched_at is a second, independent fact and stays available
        self.assertEqual(snap["fetched_at_utc"], "2026-10-17T12:00:00+00:00")

    def test_cache_age_and_quote_age_are_two_different_numbers(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        snap = body["snapshot"]
        # fetched_at 10:00, clock 12:00 -> 7200 s of CACHE age ...
        self.assertAlmostEqual(snap["cache_age_seconds"], 7200.0, places=3)
        # ... while the source time itself carries no timezone, so the
        # snapshot's own age cannot be derived and stays null
        self.assertIsNone(snap["source_age_seconds"])
        c = self._first_contract(body, CALL_NEAR)
        self.assertEqual(c["last_trade_time_raw"], "2026-10-16T11:40:42")
        self.assertIsNone(c["last_trade_time_utc"])

    def test_fetched_at_never_replaces_the_source_time(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        snap = body["snapshot"]
        self.assertEqual(snap["fetched_at_utc"], "2026-10-17T10:00:00+00:00")
        self.assertEqual(snap["source_timestamp_raw"], SOURCE_STAMP)
        self.assertNotEqual(snap["fetched_at_utc"], snap["source_timestamp_raw"])
        self.assertNotIn("03:33:37", snap["fetched_at_utc"])

    def test_a_future_fetch_time_has_no_negative_cache_age(self):
        body = serialise_snapshot(_snapshot(_fixture_records(),
                                            fetched_at=arrow.get("2026-10-17T13:00:00+00:00")),
                                  ticker="TST", requested_expiration=None, now_fn=_now_dt)
        self.assertIsNone(body["snapshot"]["cache_age_seconds"])

    def test_a_weekend_expiration_is_a_date_like_any_other(self):
        # 2026-11-21 is a Saturday: the source lists it, so the endpoint serves it
        self.assertEqual(arrow.get("2026-11-21").format("ddd"), "Sat")
        res = self._chain(self.client, ticker="TST", expiration="2026-11-21")
        body = self._body(res)
        self.assertEqual({c["expiration"] for c in body["contracts"]}, {"2026-11-21"})
        self.assertEqual([c["symbol"] for c in body["contracts"]], [CALL_SAT])
        self.assertEqual(body["snapshot"]["returned_record_count"], 1)
        self.assertEqual(body["snapshot"]["outside_requested_expiration_count"], 5)


# --------------------------------------------------------------------- #
# 8 - contract metadata
# --------------------------------------------------------------------- #

class TestContractMetadataUnknown(_ChainTestCase):
    """8: what the source does not name stays null - no inferred 100."""

    def setUp(self):
        self.adapter = _FakeChainAdapter(records=_fixture_records(),
                                         expirations=_fixture_expirations())
        _, self.client = self._app_and_client(self.adapter)

    def test_multiplier_and_metadata_stay_null_and_marked_unknown(self):
        body = self._body(self._chain(self.client, ticker="TST"))
        c = self._contract(body, CALL_NEAR)
        for feld in ("multiplier", "deliverable", "currency", "exercise_style",
                     "settlement_type"):
            self.assertIsNone(c[feld], feld)
            self.assertNotEqual(c[feld], 100, feld)
        self.assertEqual(c["contract_metadata_status"], "unknown")
        self.assertIn("CONTRACT_METADATA_UNKNOWN", body["quality_flags"])
        self.assertIsNone(body["underlying"]["currency"])

    def test_no_multiplier_100_is_written_into_the_body(self):
        res = self._chain(self.client, ticker="TST")
        self._body(res)
        raw = res.get_data(as_text=True)
        self.assertNotIn('"multiplier": 100', raw)
        self.assertNotIn('"multiplier":100', raw)

    def test_the_source_row_really_carries_no_multiplier(self):
        # the null is therefore the honest answer, not a mapping failure
        for row in _fixture_records():
            self.assertNotIn("multiplier", row)
            self.assertNotIn("deliverable", row)
            self.assertNotIn("currency", row)


# --------------------------------------------------------------------- #
# 9 - error contract
# --------------------------------------------------------------------- #

class TestErrorContract(_ChainTestCase):
    """9: eight measured error paths - status, content type and error.code."""

    def _assert_error(self, res, status, code, label):
        self.assertEqual(res.status_code, status, label)
        self.assertTrue(res.content_type.startswith("application/json"),
                        f"{label}: content type was {res.content_type!r}")
        body = json.loads(res.get_data(as_text=True))
        self.assertEqual(body["schema_version"], SCHEMA_VERSION, label)
        self.assertEqual(body["error"]["code"], code, label)
        self.assertIsInstance(body["error"]["message"], str, label)
        self.assertNotEqual(body["error"]["message"], "", label)
        # the error body carries no chain - pinned only after status and type
        self.assertNotIn("contracts", body, label)
        self.assertNotIn("snapshot", body, label)

    def test_the_eight_error_paths(self):
        gut = _FakeChainAdapter(records=_messy_records(), expirations=_messy_expirations())
        faelle = [
            ("unknown parameter", gut, {"ticker": "TST", "min_oi": "500"},
             400, "unknown_parameter"),
            ("duplicate parameter", gut, [("ticker", "TST"), ("ticker", "QQQ")],
             400, "duplicate_parameter"),
            ("missing ticker", gut, {}, 400, "ticker_required"),
            ("invalid ticker", gut, {"ticker": "TOOLONGTICKER"}, 400, "invalid_ticker"),
            ("impossible calendar date", gut, {"ticker": "TST", "expiration": "2026-13-99"},
             400, "invalid_expiration"),
            ("valid but unlisted date", gut, {"ticker": "TST", "expiration": "2027-01-15"},
             400, "expiration_not_listed"),
            ("unknown symbol", _FakeChainAdapter(error=CboeNotFoundError("ZZZQ")),
             {"ticker": "ZZZQ"}, 404, "unknown_symbol"),
            ("source outage", _FakeChainAdapter(error=CboeRequestError("unreachable")),
             {"ticker": "TST"}, 503, "market_data_unavailable"),
        ]
        for label, adapter, params, status, code in faelle:
            with self.subTest(path=label):
                _, client = self._app_and_client(adapter)
                res = client.get("/api/v1/option-chain", query_string=params)
                self._assert_error(res, status, code, label)

    def test_empty_ticker_is_required_not_invalid(self):
        adapter = _FakeChainAdapter(records=_fixture_records())
        _, client = self._app_and_client(adapter)
        res = self._chain(client, ticker="")
        self._assert_error(res, 400, "ticker_required", "empty ticker")

    def test_malformed_ticker_values_are_rejected(self):
        adapter = _FakeChainAdapter(records=_fixture_records())
        _, client = self._app_and_client(adapter)
        for roh in ("../etc", "T S T", "TST-1", "TST1234567"):
            with self.subTest(ticker=roh):
                res = self._chain(client, ticker=roh)
                self._assert_error(res, 400, "invalid_ticker", roh)

    def test_a_timeout_is_a_source_outage_for_the_client(self):
        # the adapter wraps a socket timeout in CboeRequestError - the caller
        # must see market_data_unavailable, never a 500
        adapter = _FakeChainAdapter(error=CboeRequestError("CBOE feed unreachable: timed out"))
        _, client = self._app_and_client(adapter)
        res = self._chain(client, ticker="TST")
        self._assert_error(res, 503, "market_data_unavailable", "timeout")

    def test_a_failed_source_never_reaches_the_serializer(self):
        adapter = _FakeChainAdapter(records=_messy_records(),
                                    error=CboeRequestError("unreachable"))
        _, client = self._app_and_client(adapter)
        res = self._chain(client, ticker="TST")
        self._assert_error(res, 503, "market_data_unavailable", "outage")
        self.assertEqual(adapter.snapshot_calls, 1)


# --------------------------------------------------------------------- #
# 10 - cache
# --------------------------------------------------------------------- #

class TestSnapshotCacheReuse(_ChainTestCase):
    """10: one source fetch per ticker, an untouched cache, no cross-ticker fallback."""

    def setUp(self):
        self.payload = _cboe_payload(_fixture_records())
        self.session = _CountingSession({"TST": self.payload})
        self.adapter = CBOEQuoteAdapter(now_fn=_clock, session=self.session)
        _, self.client = self._app_and_client(self.adapter)

    def test_several_expirations_of_one_ticker_cost_one_source_fetch(self):
        for expiration in ("2026-11-20", "2026-11-21", "2026-11-20", ""):
            res = self._chain(self.client, ticker="TST",
                              **({"expiration": expiration} if expiration else {}))
            self._body(res)
        self.assertEqual(self.session.fetched_symbols, ["TST"])
        self.assertEqual(len(self.session.urls), 1)

    def test_the_serializer_does_not_alter_the_cached_entry(self):
        self._body(self._chain(self.client, ticker="TST", expiration="2026-11-20"))
        entry = self.adapter._cache["TST"]
        vorher = copy.deepcopy(entry["options"])
        reihenfolge_vorher = [list(row) for row in entry["options"]]
        stamp_vorher = entry["timestamp"]

        res = self._chain(self.client, ticker="TST", expiration="2026-11-21")
        self._body(res)

        self.assertEqual(entry["options"], vorher)
        self.assertEqual([list(row) for row in entry["options"]], reihenfolge_vorher)
        self.assertEqual(entry["timestamp"], stamp_vorher)
        self.assertEqual(len(entry["options"]), len(_fixture_records()))
        self.assertEqual(self.session.fetched_symbols, ["TST"])

    def test_the_returned_snapshot_is_a_copy_of_the_cached_list(self):
        snap = self.adapter.get_option_chain_snapshot("TST")
        lange_vorher = len(self.adapter._cache["TST"]["options"])
        snap["records"].pop()
        snap["records"].clear()
        self.assertEqual(len(self.adapter._cache["TST"]["options"]), lange_vorher)

    def test_an_unknown_ticker_gets_no_cached_data_of_another(self):
        self._body(self._chain(self.client, ticker="TST"))
        res = self._chain(self.client, ticker="ZZZQ")
        body = self._body(res, 404)
        self.assertEqual(body["error"]["code"], "unknown_symbol")
        self.assertNotIn("contracts", body)
        self.assertNotIn("TST", res.get_data(as_text=True))
        self.assertEqual(self.session.fetched_symbols, ["TST", "ZZZQ"])
        self.assertNotIn("ZZZQ", self.adapter._cache)


# --------------------------------------------------------------------- #
# 11 - the screener routes are untouched
# --------------------------------------------------------------------- #

class TestScreenerRoutesPreserved(_ChainTestCase):
    """11: /healthz, /api/screen and / behave as before the new route."""

    def setUp(self):
        self.adapter = _FakeChainAdapter(records=_fixture_records(),
                                         expirations=_fixture_expirations())
        _, self.client = self._app_and_client(self.adapter)

    def test_healthz_stays_green_without_touching_the_source(self):
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.content_type.startswith("application/json"))
        self.assertEqual(res.get_json()["status"], "ok")
        self.assertEqual(self.adapter.snapshot_calls, 0)
        self.assertEqual(self.adapter.expiration_calls, 0)

    def test_api_screen_still_answers_the_picker_structure(self):
        res = self.client.get("/api/screen", query_string={"ticker": "TST"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.content_type.startswith("application/json"))
        body = res.get_json()
        for schluessel in ("ticker", "expiration", "listed_dates", "results",
                           "freshness", "dropped_reasons", "verticals",
                           "vertical_drops", "trend"):
            self.assertIn(schluessel, body)
        self.assertEqual(body["ticker"], "TST")
        self.assertIsNone(body["expiration"])
        self.assertEqual(body["results"], [])
        self.assertIsNone(body["freshness"])
        self.assertEqual(body["dropped_reasons"], [])
        # 2026-10-17 (pinned clock) -> 34 and 35 calendar days: inside the
        # default window [7, 45]; 62 and 244 days are outside it
        self.assertEqual(body["listed_dates"], ["2026-11-20", "2026-11-21"])

    def test_the_page_still_renders(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Options-Screener", res.get_data(as_text=True))


# --------------------------------------------------------------------- #
# 12 - route round trip (instead of a live smoke test)
# --------------------------------------------------------------------- #

class TestRouteOverTestClient(_ChainTestCase):
    """12: the route end to end with an injected adapter - no network needed."""

    def test_round_trip_returns_source_values_unchanged(self):
        adapter = _FakeChainAdapter(records=_fixture_records(),
                                    expirations=_fixture_expirations(),
                                    current_price=101.25)
        _, client = self._app_and_client(adapter)
        res = self._chain(client, ticker="tst")        # lower case goes in
        self.assertEqual(res.content_type.split(";")[0], "application/json")
        body = self._body(res)
        self.assertEqual(body["schema_version"], SCHEMA_VERSION)
        self.assertEqual(body["ticker"], "TST")        # upper case comes back
        self.assertAlmostEqual(body["underlying"]["price"], 101.25, places=10)
        self.assertEqual(len(body["contracts"]), len(_fixture_records()))
        c = self._contract(body, CALL_NEAR)
        self.assertAlmostEqual(c["bid"], 1.10, places=10)
        self.assertAlmostEqual(c["ask"], 1.25, places=10)
        self.assertEqual(c["open_interest"], 640)
        self.assertEqual(c["volume"], 35)
        self.assertEqual(c["last_trade_time_raw"], "2026-10-16T11:40:42")
        self.assertEqual(body["source"]["delayed"], True)
        self.assertEqual(adapter.snapshot_calls, 1)

    def test_an_empty_source_snapshot_is_a_legal_200(self):
        adapter = _FakeChainAdapter(records=[], expirations=[])
        _, client = self._app_and_client(adapter)
        res = self._chain(client, ticker="TST")
        body = self._body(res)
        self.assertEqual(body["contracts"], [])
        self.assertEqual(body["snapshot"]["source_record_count"], 0)
        self.assertEqual(body["snapshot"]["completeness"], "unknown")
        self.assertEqual(body["snapshot"]["served_at_utc"], "2026-10-17T12:00:00+00:00")

    def test_the_response_is_strict_json_without_any_network_access(self):
        adapter = _FakeChainAdapter(records=_messy_records(),
                                    expirations=_messy_expirations())
        _, client = self._app_and_client(adapter)
        res = self._chain(client, ticker="TST")
        raw = res.get_data(as_text=True)
        geparst = json.loads(raw, parse_constant=_reject_constant)
        self.assertEqual(sorted(SCREENER_KEYS & _keys_in(geparst)), [])
        self.assertEqual(adapter.snapshot_calls, 1)
        # the fixture adapter has no HTTP transport at all, so this route run
        # cannot have reached the feed
        self.assertFalse(hasattr(adapter, "urls"))
        self.assertEqual(listed_expirations(_snapshot(_fixture_records())),
                         _fixture_expirations())
