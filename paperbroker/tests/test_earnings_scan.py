"""Offline tests for the whole-day earnings scan (earnings_scan.py + its route).

Every fixture is local: a fake chain adapter, a fake earnings adapter and a
pinned clock. Nothing here touches CBOE or Nasdaq.

What the tests are here to prove, in the order of the requirement:

* several tickers AND several expiries are really scanned,
* one broken ticker does not cancel the scan and is named as such,
* the scan's hits are the SINGLE-TICKER view's hits (same function, same
  criteria, same clock) - not a second opinion,
* a title outside the page's Top 25 is scanned,
* every one of the eight states is reachable and distinguishable,
* a cut scan declares its own coverage instead of looking complete.
"""

import json
import unittest
from datetime import date

import arrow

from paperbroker.adapters.quotes.CBOEQuoteAdapter import (
    CHAIN_UNUSABLE,
    CboeNotFoundError,
    CboeRequestError,
    CboeUnusableChainError,
    ChainResult,
)
from paperbroker.assets import asset_factory
from paperbroker.earnings_calendar import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_ESTIMATED,
    CONFIRMATION_UNKNOWN,
    SESSION_AFTER,
    SESSION_UNKNOWN,
    STATUS_FRESH,
    STATUS_STALE,
    STATUS_UNKNOWN,
    empty_earnings,
)
from paperbroker.earnings_day_calendar import rank_rows
from paperbroker.earnings_scan import (
    FLAG_CALENDAR_ROW_SOURCE,
    FLAG_CALENDAR_ROW_STALE_DAY,
    LIMIT_DEADLINE,
    LIMIT_EXPIRATION_BEFORE_EARNINGS,
    LIMIT_MAX_EXPIRATIONS,
    LIMIT_MAX_SYMBOLS,
    SOURCE_CALENDAR_ROW,
    STATUS_FETCH_FAILED,
    STATUS_HITS,
    STATUS_INCOMPLETE_DATA,
    STATUS_NO_MATCH,
    STATUS_NOT_CHECKED,
    STATUS_STALE_DATA,
    STATUS_WINDOW_BLOCKED,
    STATUS_WINDOW_UNKNOWN,
    ScanLimits,
    best_sort_key,
    earnings_from_calendar_row,
    scan_day,
)
from paperbroker.quotes import OptionQuote
from paperbroker.screener import (
    BEAR_PUT_DEBIT,
    BULL_CALL_DEBIT,
    STRATEGY_IDS,
    ScreenerCriteria,
    vertical_spreads,
)
from paperbroker.server import _vertical_sort_key, create_app

CLOCK = lambda: arrow.get("2026-10-07T14:35:00")      # noqa: E731 - a fixture
FRESH = "2026-10-07 14:30:00"
TODAY = date(2026, 10, 7)
EARNINGS_DATE = "2026-10-20"      # after today, exit day 2026-10-19
EXP_A = "2026-10-30"              # DTE 23, inside [7, 45]
EXP_B = "2026-11-20"              # DTE 44, inside [7, 45]
EXP_BEFORE_EARNINGS = "2026-10-16"   # DTE 9, but on/before the earnings date

_LOOSE = ScreenerCriteria(max_spread_pct=100.0, max_spread_abs=None)

#: The calendar day a fixture scan runs for. The rows below ARE that day's
#: listing, and since the scan derives the earnings date from the row, this day
#: is the day the fixture title reports on - it equals ``EARNINGS_DATE``, which
#: the fake adapter answers with too, so both paths describe the same symbol.
SCAN_DAY = EARNINGS_DATE


def quote(occ, bid, ask, *, iv=30.0, oi=1500, volume=100,
          underlying_price=250.0, quote_timestamp=FRESH):
    """One fresh OptionQuote on a real OCC symbol (same shape the adapter builds)."""
    asset = asset_factory(occ)
    q = OptionQuote(
        quote_date="2026-10-07 14:30:00", asset=asset, price=(bid + ask) / 2,
        bid=bid, ask=ask, bid_size=100, ask_size=100, delta=50, iv=iv,
        gamma=0.5, vega=5.0, theta=-3.0, rho=0.5,
        underlying_price=underlying_price, open_interest=oi, volume=volume,
        greeks_source="adapter",
    )
    q.quote_timestamp = quote_timestamp
    return q


def call_pair(symbol, expiration, *, long_bid=2.40, long_ask=2.50,
              short_bid=0.50, short_ask=0.55, oi=1500, **kw):
    """A bullish call pair 250/252.5 on one expiry (debit 2.00 by default).

    The sold leg quotes 5 cents wide at a mid of 0.525 - 9.5 % relative and
    0.05 $ absolute, so BOTH default spread bounds pass and the route tests can
    run with the operator's real defaults.
    """
    ymd = expiration[2:4] + expiration[5:7] + expiration[8:10]
    return [
        quote(f"{symbol}{ymd}C00250000", long_bid, long_ask, oi=oi, **kw),
        quote(f"{symbol}{ymd}C00252500", short_bid, short_ask, oi=oi, **kw),
    ]


def put_pair(symbol, expiration, *, long_bid=2.40, long_ask=2.50,
             short_bid=0.50, short_ask=0.55, oi=1500, **kw):
    """A bearish put pair, long the HIGHER strike (250.0 bought, 247.5 sold)."""
    ymd = expiration[2:4] + expiration[5:7] + expiration[8:10]
    return [
        quote(f"{symbol}{ymd}P00250000", long_bid, long_ask, oi=oi, **kw),
        quote(f"{symbol}{ymd}P00247500", short_bid, short_ask, oi=oi, **kw),
    ]


def calendar_row(symbol, market_cap=None, name=None):
    return {"symbol": symbol, "name": name or f"{symbol} Inc.",
            "session": SESSION_AFTER, "market_cap": market_cap,
            "eps_forecast": "$1.00", "fiscal_quarter_ending": "Sep/2026"}


class FakeChainAdapter:
    """Per-symbol chains; a stored exception value is raised on access."""

    def __init__(self, chains):
        self.chains = dict(chains)
        self.calls = []

    def _symbol(self, underlying_asset):
        return (underlying_asset.upper() if isinstance(underlying_asset, str)
                else underlying_asset.symbol)

    def get_options(self, underlying_asset=None, expiration_date=None):
        symbol = self._symbol(underlying_asset)
        self.calls.append((symbol, expiration_date))
        wert = self.chains.get(symbol)
        if isinstance(wert, Exception):
            raise wert
        if wert is None:
            return ChainResult([], skipped=0)
        if isinstance(wert, ChainResult):
            rows = list(wert)
        else:
            rows = list(wert)
        if expiration_date is not None:
            rows = [q for q in rows if q.asset.expiration_date == expiration_date]
        ergebnis = ChainResult(rows, skipped=getattr(wert, "skipped", 0))
        return ergebnis

    def get_expiration_dates(self, underlying_asset=None):
        return sorted({q.asset.expiration_date for q in self.get_options(underlying_asset)})

    def get_quote(self, asset):
        return None

    def get_option_chain_snapshot(self, underlying_asset=None):
        symbol = self._symbol(underlying_asset)
        wert = self.chains.get(symbol)
        if isinstance(wert, Exception):
            raise wert
        return {"records": [], "fetched_at": None, "current_price": 250.0}


class FakeEarningsAdapter:
    def __init__(self, rows, *, symbols=None, day_state="live",
                 earnings_status=STATUS_FRESH, earnings_date=EARNINGS_DATE,
                 failures=None):
        self.rows = list(rows)
        self.symbols = symbols
        self.day_state = day_state
        self.earnings_status = earnings_status
        self.earnings_date = earnings_date
        self.failures = dict(failures or {})
        self.earnings_calls = []

    def get_calendar_days(self, days):
        geliefert = {}
        for iso in days.values():
            geliefert[iso] = {"date": iso, "rows": [dict(r) for r in self.rows],
                              "state": self.day_state,
                              "fetched_at_utc": "2026-10-07T13:00:00Z",
                              "payload_ok": self.day_state == "live",
                              "skipped": 0}
        return {"days": geliefert, "state": self.day_state,
                "fetched_at_utc": "2026-10-07T13:00:00Z"}

    def get_earnings(self, ticker):
        self.earnings_calls.append(ticker)
        fehler = self.failures.get(ticker)
        if fehler is not None:
            raise fehler
        return {
            "schema_version": "1.0",
            "ticker": ticker,
            "earnings": {
                "next_earnings_date": self.earnings_date,
                "release_session": SESSION_AFTER,
                "date_confirmation": CONFIRMATION_ESTIMATED,
                "source": "nasdaq_earnings_date",
                "source_checked_at_utc": "2026-10-07T06:00:00Z",
                "retrieved_at_utc": "2026-10-07T14:00:00Z",
                "status": self.earnings_status,
                "quality_flags": [],
            },
            "cache": {"state": "live", "stored_at_utc": "2026-10-07T06:00:00Z",
                      "age_seconds": 3600, "ttl_seconds": 21600,
                      "freshness_limit_seconds": 172800},
        }

    def get_optionable_symbols(self):
        return {"symbols": self.symbols, "state": "live",
                "fetched_at_utc": "2026-10-07T05:00:00Z", "error": None}


def run_scan(chains, rows, **kwargs):
    """scan_day with the pinned clock and the fixture fixtures applied."""
    kwargs.setdefault("criteria", _LOOSE)
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("now_fn", CLOCK)
    kwargs.setdefault("day", SCAN_DAY)
    # The state the day cache reported. Every test that does not say otherwise
    # scans a LIVE day, so its previously proven behaviour stays unchanged.
    kwargs.setdefault("day_state", "live")
    adapter = kwargs.pop("adapter", FakeChainAdapter(chains))
    return scan_day(
        rows,
        fetch_earnings=kwargs.pop("fetch_earnings", FakeEarningsAdapter(rows).get_earnings),
        fetch_chain=adapter.get_options,
        **kwargs,
    )


def entry_of(body, symbol):
    for eintrag in body["entries"]:
        if eintrag["symbol"] == symbol:
            return eintrag
    raise AssertionError(f"{symbol} is not in the answer")


class TestAReportTodayCostsNoChain(unittest.TestCase):
    """Measured 2026-11-13: 32 chain requests, 5.5 s, for 32 rows the window
    blocked afterwards. The calendar settles those rows before any quote."""

    def test_a_report_today_is_named_and_the_chain_is_not_requested(self):
        rows = [calendar_row("SY00", 30), calendar_row("SY01", 20)]
        adapter = FakeChainAdapter({})
        koerper = run_scan({}, rows, adapter=adapter, day=TODAY.isoformat())

        self.assertEqual(adapter.calls, [])          # not one chain request
        for symbol in ("SY00", "SY01"):
            eintrag = entry_of(koerper, symbol)
            self.assertEqual(eintrag["status"], STATUS_WINDOW_BLOCKED)
            self.assertFalse(eintrag["data"]["chain_requested"])
            self.assertIsNone(eintrag["data"]["chain_contracts"])
            self.assertEqual(eintrag["data"]["chain_note"],
                             "nicht geladen (Fenster verhindert)")
            self.assertEqual(eintrag["window"]["reasons"][0]["code"], "earnings_today")
            self.assertIn("Bericht ist heute", eintrag["exclusion"]["message"])
            self.assertIsNone(eintrag["window"]["selected_expiration"])
        self.assertEqual(koerper["coverage"]["source_calls"], {"earnings": 0, "chains": 0})
        self.assertEqual(koerper["counts"][STATUS_WINDOW_BLOCKED], 2)

    def test_a_past_report_keeps_its_own_date_reason_and_also_costs_no_chain(self):
        adapter = FakeChainAdapter({})
        koerper = run_scan({}, [calendar_row("OLD", 10)], adapter=adapter,
                           day="2026-10-06")
        eintrag = entry_of(koerper, "OLD")
        self.assertEqual(adapter.calls, [])
        codes = [g["code"] for g in eintrag["window"]["reasons"]]
        self.assertIn("exit_day_in_past", codes)
        self.assertNotIn("earnings_today", codes)

    def test_a_future_report_still_reads_the_chain(self):
        # The precheck must not become a general "skip the chain" switch.
        kette = call_pair("AAPL", EXP_A)
        adapter = FakeChainAdapter({"AAPL": kette})
        koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 10)], adapter=adapter)
        self.assertEqual([c[0] for c in adapter.calls], ["AAPL"])
        self.assertTrue(entry_of(koerper, "AAPL")["data"]["chain_requested"])


class TestAChainFailureNamesItsCause(unittest.TestCase):
    """One code, one stable class, one German sentence - never the exception text."""

    def test_a_throttled_feed_is_named_and_counted_per_attempt(self):
        fehler = CboeRequestError("CBOE feed HTTP 429 for AAPL", reason="throttled", attempts=3)
        koerper = run_scan({"AAPL": fehler}, [calendar_row("AAPL", 30)])
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_FETCH_FAILED)
        self.assertEqual(aapl["exclusion"]["code"], "chain_fetch_failed")
        self.assertEqual(aapl["exclusion"]["reason"], "throttled")
        self.assertIn("drosselt", aapl["exclusion"]["message"])
        # Three attempts are three source calls - not one.
        self.assertEqual(koerper["coverage"]["source_calls"]["chains"], 3)
        rohtext = json.dumps(koerper)
        # The fixed sentence may name the status; the EXCEPTION text may not.
        self.assertNotIn("CBOE feed HTTP 429 for AAPL", rohtext)
        self.assertNotIn("CBOE feed", rohtext)

    def test_every_class_reads_differently(self):
        for reason, wort in (("blocked", "blockt"), ("not_listed", "listet"),
                             ("unreachable", "erreichbar"), ("malformed", "nicht lesbar")):
            with self.subTest(reason=reason):
                fehler = CboeRequestError("detail that must not leak", reason=reason)
                aapl = entry_of(run_scan({"AAPL": fehler}, [calendar_row("AAPL", 30)]), "AAPL")
                self.assertEqual(aapl["exclusion"]["reason"], reason)
                self.assertIn(wort, aapl["exclusion"]["message"])
                self.assertNotIn("detail that must not leak", json.dumps(aapl))

    def test_an_unusable_chain_reads_as_unusable(self):
        """The chain was delivered and read - it just held no usable contract.

        That is not "the source lists no chain": the old class reason sent the
        not_listed sentence, and the attempts the chain cost were dropped.
        """
        fehler = CboeUnusableChainError("all 2 contracts of AAPL unusable",
                                        attempts=2)
        aapl = entry_of(run_scan({"AAPL": fehler}, [calendar_row("AAPL", 30)]), "AAPL")
        self.assertEqual(aapl["status"], STATUS_FETCH_FAILED)
        self.assertEqual(aapl["exclusion"]["code"], "chain_fetch_failed")
        self.assertEqual(aapl["exclusion"]["reason"], "unusable")
        self.assertIn("kein Kontrakt ist brauchbar", aapl["exclusion"]["message"])
        self.assertNotIn("listet", aapl["exclusion"]["message"])
        # The exception text stays a log detail, as for every other class.
        self.assertNotIn("all 2 contracts of AAPL unusable", json.dumps(aapl))

    def test_an_uncoded_exception_keeps_the_old_wording(self):
        # A fake or a bug names no class: it is not upgraded to a claim about
        # the source, and no reason field is invented for it.
        aapl = entry_of(run_scan({"AAPL": RuntimeError("boom")},
                                 [calendar_row("AAPL", 30)]), "AAPL")
        self.assertEqual(aapl["exclusion"],
                         {"code": "chain_fetch_failed",
                          "message": "option chain could not be read"})


class TestTheScanCoversTheDay(unittest.TestCase):
    """Several tickers, several expiries, all four strategies."""

    def _chains(self):
        return {
            "AAPL": call_pair("AAPL", EXP_A) + call_pair("AAPL", EXP_B),
            "MSFT": call_pair("MSFT", EXP_A),
            "NVDA": put_pair("NVDA", EXP_A),
        }

    def test_every_ticker_and_expiry_is_scanned(self):
        rows = [calendar_row("AAPL", 3_000_000), calendar_row("MSFT", 2_000_000),
                calendar_row("NVDA", 1_000_000)]
        koerper = run_scan(self._chains(), rows)
        self.assertEqual([e["symbol"] for e in koerper["entries"]],
                         ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(koerper["coverage"]["selected"], 3)
        self.assertEqual(koerper["coverage"]["processed"], 3)
        self.assertTrue(koerper["coverage"]["complete"])
        self.assertEqual(koerper["strategies"], list(STRATEGY_IDS))

        aapl = entry_of(koerper, "AAPL")
        # Both listed expiries are inside the window and after the earnings
        # date, so both are checked - not only the first one the window picked.
        self.assertEqual(aapl["checked_expirations"], [EXP_A, EXP_B])
        self.assertEqual(aapl["status"], STATUS_HITS)
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["hits"], 2)
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["checked_expirations"],
                         [EXP_A, EXP_B])

    def test_only_the_strategy_whose_side_is_listed_can_hit(self):
        rows = [calendar_row("AAPL", 3_000_000)]
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows)
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["status"], STATUS_HITS)
        self.assertEqual(aapl["strategies"][BEAR_PUT_DEBIT]["status"], STATUS_NO_MATCH)
        self.assertEqual(aapl["strategies"][BEAR_PUT_DEBIT]["hits"], 0)

    def test_the_listed_expiry_before_earnings_is_named_not_scanned(self):
        rows = [calendar_row("AAPL", 3_000_000)]
        kette = call_pair("AAPL", EXP_BEFORE_EARNINGS) + call_pair("AAPL", EXP_A)
        koerper = run_scan({"AAPL": kette}, rows)
        aapl = entry_of(koerper, "AAPL")
        self.assertIn(EXP_A, aapl["checked_expirations"])
        self.assertNotIn(EXP_BEFORE_EARNINGS, aapl["checked_expirations"])
        gruende = [s["reason"] for s in aapl["skipped_expirations"]]
        self.assertIn(LIMIT_EXPIRATION_BEFORE_EARNINGS, gruende)


class TestFilterParityWithTheSingleScan(unittest.TestCase):
    """The scan must not be a second opinion on what a spread is."""

    def test_hits_equal_the_single_ticker_view(self):
        chains = {
            "AAPL": call_pair("AAPL", EXP_A) + call_pair(
                "AAPL", EXP_B, long_bid=1.40, long_ask=1.50,
                short_bid=0.40, short_ask=0.50),
        }
        rows = [calendar_row("AAPL", 3_000_000)]
        adapter = FakeChainAdapter(chains)
        koerper = run_scan(chains, rows, adapter=adapter)

        for strategy in STRATEGY_IDS:
            from_scan = set()
            aapl = entry_of(koerper, "AAPL")
            for iso in aapl["strategies"][strategy]["checked_expirations"]:
                # The single view is called with ONE expiration, exactly as
                # GET /api/screen does it.
                quotes = adapter.get_options("AAPL", iso)
                kept, _dropped = vertical_spreads(quotes, _LOOSE, strategy=strategy,
                                                  now_fn=CLOCK)
                from_scan |= {pair.symbol for pair in kept}
            gemeldet = set()
            best = aapl["strategies"][strategy]["best"]
            if best is not None:
                gemeldet.add(best["symbol"])
            self.assertTrue(gemeldet <= from_scan,
                            f"{strategy}: {gemeldet} not among {from_scan}")
            self.assertEqual(aapl["strategies"][strategy]["hits"], len(from_scan),
                             f"{strategy}: hit count differs from the single view")

    def test_the_drop_reasons_are_the_screener_own_texts(self):
        # The long leg carries 10 open interest: the single view drops that leg
        # with "OI 10 < 500". The scan must repeat that text, not paraphrase it.
        chains = {"AAPL": [quote("AAPL261030C00250000", 2.40, 2.50, oi=10),
                           quote("AAPL261030C00252500", 0.50, 0.55)]}
        rows = [calendar_row("AAPL", 3_000_000)]
        koerper = run_scan(chains, rows)
        aapl = entry_of(koerper, "AAPL")
        gruende = " | ".join(g["reason"] for g
                             in aapl["strategies"][BULL_CALL_DEBIT]["drop_reasons"])
        self.assertIn("OI 10 < 500", gruende)
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["status"], STATUS_NO_MATCH)


class TestTheTop25CutIsNotTheUniverse(unittest.TestCase):
    def test_a_title_outside_the_page_top_25_is_scanned(self):
        # 30 titles, market cap strictly descending: SMALLCAP is rank 30.
        rows = [calendar_row(f"BIG{i:02d}", 100_000_000 - i) for i in range(1, 30)]
        rows.append(calendar_row("SMALLCAP", 10))
        self.assertNotIn("SMALLCAP", [z["symbol"] for z in rank_rows(rows)["rows"]])
        # ... while the day itself does contain it.
        self.assertEqual(rank_rows(rows)["total"], 30)

        koerper = run_scan({"SMALLCAP": call_pair("SMALLCAP", EXP_A)}, rows,
                           limits=ScanLimits(max_symbols=40, concurrency=2))
        klein = entry_of(koerper, "SMALLCAP")
        self.assertEqual(klein["status"], STATUS_HITS)
        self.assertEqual(koerper["coverage"]["optionable_rows"], 30)
        self.assertEqual(koerper["coverage"]["selected"], 30)

    def test_the_budget_cut_is_declared_by_name(self):
        # Letter-only roots: the OCC parser accepts letters, not digits.
        rows = [calendar_row(f"SYM{chr(ord('A') + i)}", 100_000_000 - i) for i in range(5)]
        chains = {row["symbol"]: call_pair(row["symbol"], EXP_A) for row in rows}
        koerper = run_scan(chains, rows, limits=ScanLimits(max_symbols=2, concurrency=1))
        self.assertEqual(koerper["coverage"]["selected"], 2)
        self.assertFalse(koerper["coverage"]["complete"])
        self.assertTrue(koerper["coverage"]["truncated"])
        self.assertEqual(len(koerper["coverage"]["hidden_symbols"]), 3)
        self.assertEqual(koerper["coverage"]["truncation_reasons"][0]["code"],
                         LIMIT_MAX_SYMBOLS)
        self.assertEqual(koerper["coverage"]["optionable_rows"], 5)

    def test_the_optionable_list_prefilters_and_is_reported(self):
        rows = [calendar_row("AAPL", 30), calendar_row("NOOPT", 20)]
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           optionable=frozenset({"AAPL"}))
        self.assertEqual(koerper["coverage"]["optionable_rows"], 1)
        self.assertEqual(koerper["coverage"]["without_options"], 1)
        self.assertTrue(koerper["option_filter"]["applied"])
        self.assertEqual([e["symbol"] for e in koerper["entries"]], ["AAPL"])

    def test_without_the_list_the_scan_runs_unfiltered_and_says_so(self):
        rows = [calendar_row("AAPL", 30), calendar_row("NOOPT", 20)]
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows, optionable=None)
        self.assertFalse(koerper["option_filter"]["applied"])
        self.assertEqual(koerper["coverage"]["optionable_rows"], 2)


class TestEveryStateIsReachable(unittest.TestCase):
    def test_stale_quotes_are_not_a_result(self):
        old = call_pair("AAPL", EXP_A, quote_timestamp="2026-10-07 09:00:00")
        koerper = run_scan({"AAPL": old}, [calendar_row("AAPL", 30)])
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_STALE_DATA)
        self.assertEqual(aapl["data"]["freshness"], "stale")
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["status"], STATUS_STALE_DATA)
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["hits"], 0)

    def test_a_partly_stale_chain_is_not_reported_as_no_match(self):
        # Measured (PR #74 review): a pair whose sold leg carries a 335-minute-old
        # timestamp produced zero hits - the leg is dropped inside
        # vertical_spreads by the very same filter_stale - but the row said
        # "no_match", i.e. "nothing fits". The stale leg IS the answer.
        kette = [quote("AAPL261030C00250000", 2.40, 2.50),
                 quote("AAPL261030C00252500", 0.50, 0.55,
                       quote_timestamp="2026-10-07 09:00:00")]
        koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 30)])
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["data"]["freshness"], "partial")
        self.assertEqual(aapl["status"], STATUS_STALE_DATA)
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["hits"], 0)
        gruende = " | ".join(g["reason"] for g
                             in aapl["strategies"][BULL_CALL_DEBIT]["drop_reasons"])
        self.assertIn("veraltet", gruende)

    def test_a_fresh_pair_next_to_a_stale_one_still_counts(self):
        # The other side of the same finding: a valid pair must not be hidden
        # because ANOTHER contract of the chain is old - but the row says so.
        kette = (call_pair("AAPL", EXP_A)
                 + call_pair("AAPL", EXP_B, quote_timestamp="2026-10-07 09:00:00"))
        koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 30)])
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_HITS)
        self.assertEqual(aapl["strategies"][BULL_CALL_DEBIT]["hits"], 1)
        self.assertIn("partially_stale_chain", aapl["quality_flags"])

    def test_a_stale_contract_on_a_skipped_expiry_does_not_flip_the_verdict(self):
        # Review finding on PR #74 (second pass). The status keys on the quotes a
        # strategy really looked at: a 3-day-old chain section on an expiry that
        # was skipped anyway (here: 2026-10-16, before the earnings date) must
        # leave a fresh, empty result at no_match - not turn it into stale_data.
        kette = [quote("AAPL261016C00250000", 2.40, 2.50,
                       quote_timestamp="2026-10-04 09:00:00"),
                 quote("AAPL261016C00252500", 0.50, 0.55,
                       quote_timestamp="2026-10-04 09:00:00")]
        # ... plus a fresh checked expiry whose spread is dropped for illiquidity
        kette += [quote("AAPL261030C00250000", 2.40, 2.50, oi=10),
                  quote("AAPL261030C00252500", 0.50, 0.55)]
        koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 30)])
        aapl = entry_of(koerper, "AAPL")
        bull = aapl["strategies"][BULL_CALL_DEBIT]
        self.assertEqual(bull["checked_expirations"], [EXP_A])
        self.assertEqual(bull["checked_freshness"], "fresh")
        self.assertEqual(bull["status"], STATUS_NO_MATCH)
        self.assertEqual(aapl["status"], STATUS_NO_MATCH)

    def test_an_incomplete_chain_is_named_instead_of_quiet(self):
        kette = ChainResult(call_pair("AAPL", EXP_A), skipped=7)
        koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 30)])
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["data"]["completeness"], "partial")
        self.assertEqual(aapl["data"]["chain_skipped_contracts"], 7)
        self.assertIn("chain_contracts_skipped", aapl["quality_flags"])

    def test_an_incomplete_chain_without_hits_is_its_own_state(self):
        # Calls listed, but only puts would satisfy the strategy -> nothing
        # matches AND the chain is partial: "incomplete" outranks "no match".
        kette = ChainResult(call_pair("AAPL", EXP_A, oi=10), skipped=7)
        koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 30)])
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_INCOMPLETE_DATA)

    def test_one_failing_ticker_does_not_stop_the_others(self):
        chains = {
            "AAPL": call_pair("AAPL", EXP_A),
            "BROKEN": RuntimeError("feed down"),
            "MSFT": call_pair("MSFT", EXP_A),
        }
        rows = [calendar_row("AAPL", 30), calendar_row("BROKEN", 20),
                calendar_row("MSFT", 10)]
        koerper = run_scan(chains, rows)
        self.assertEqual(entry_of(koerper, "BROKEN")["status"], STATUS_FETCH_FAILED)
        self.assertEqual(entry_of(koerper, "BROKEN")["exclusion"]["code"],
                         "chain_fetch_failed")
        self.assertEqual(entry_of(koerper, "AAPL")["status"], STATUS_HITS)
        self.assertEqual(entry_of(koerper, "MSFT")["status"], STATUS_HITS)
        self.assertEqual(koerper["coverage"]["processed"], 2)

    def test_the_answer_carries_codes_but_no_adapter_internals(self):
        # Review finding on PR #74: `type(exc).__name__` in a 200 body exposes
        # adapter internals. The code names WHICH call failed; the exception
        # type belongs to the log, which is what `on_ticker_error` is for.
        gesehen = []

        class _Boom(RuntimeError):
            pass

        koerper = run_scan({"AAPL": _Boom("cdn.cboe.com refused")},
                           [calendar_row("AAPL", 30)],
                           on_ticker_error=lambda sym, exc: gesehen.append((sym, type(exc).__name__)))
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_FETCH_FAILED)
        self.assertEqual(aapl["exclusion"]["code"], "chain_fetch_failed")
        self.assertEqual(aapl["exclusion"]["message"], "option chain could not be read")
        rohtext = json.dumps(koerper)
        self.assertNotIn("_Boom", rohtext)
        self.assertNotIn("cdn.cboe.com", rohtext)
        self.assertEqual(gesehen, [("AAPL", "_Boom")])

    def test_a_failing_earnings_lookup_is_its_own_state(self):
        rows = [calendar_row("AAPL", 30)]
        adapter = FakeEarningsAdapter(rows, failures={"AAPL": RuntimeError("boom")})
        # ``day=None``: the record has to come from the lookup, so the lookup's
        # own failure handling is what this test exercises.
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           fetch_earnings=adapter.get_earnings, day=None)
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_FETCH_FAILED)
        self.assertEqual(aapl["exclusion"]["code"], "earnings_fetch_failed")
        # No chain request was wasted on a ticker we could not judge.
        self.assertEqual(koerper["coverage"]["source_calls"],
                         {"earnings": 0, "chains": 0})

    def test_a_stale_earnings_record_cannot_authorise_a_window(self):
        rows = [calendar_row("AAPL", 30)]
        adapter = FakeEarningsAdapter(rows, earnings_status=STATUS_STALE)
        # The record has to come from the adapter, so this scan carries no day.
        chain = FakeChainAdapter({"AAPL": call_pair("AAPL", EXP_A)})
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           fetch_earnings=adapter.get_earnings, day=None,
                           adapter=chain)
        aapl = entry_of(koerper, "AAPL")
        # A stale record can never authorise a window, so the chain is not
        # requested at all and the record decides alone. Without expiries the
        # gate also names ``expirations_unavailable``, which is why the verdict
        # reads "unknown" (undecidable) instead of the "blocked" a chain used to
        # supply - the entry is ineligible either way, and nothing was spent on
        # an answer that could not change.
        self.assertEqual(aapl["status"], STATUS_WINDOW_UNKNOWN)
        self.assertFalse(aapl["window"]["eligible"])
        self.assertEqual(aapl["window"]["status"], "unknown")
        codes = [g["code"] for g in aapl["window"]["reasons"]]
        self.assertIn("earnings_data_stale", codes)
        self.assertIn("expirations_unavailable", codes)
        self.assertIs(aapl["data"]["chain_requested"], False)
        self.assertEqual(chain.calls, [])
        self.assertEqual(aapl["strategies"], {})

    def test_an_unknown_earnings_record_is_not_a_blocked_one(self):
        rows = [calendar_row("AAPL", 30)]
        adapter = FakeEarningsAdapter(rows, earnings_status=STATUS_UNKNOWN,
                                       earnings_date=None)
        # "the source has no date" can only come from the adapter, so no day.
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           fetch_earnings=adapter.get_earnings, day=None)
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_WINDOW_UNKNOWN)
        self.assertEqual(aapl["window"]["status"], "unknown")
        # Still named: the fallback found no date, so the reason says exactly
        # that - and the second code says no expiry list was read either.
        self.assertEqual([g["code"] for g in aapl["window"]["reasons"]],
                         ["earnings_date_unknown", "expirations_unavailable"])

    def test_an_expiry_outside_the_window_is_named(self):
        rows = [calendar_row("AAPL", 30)]
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_B)}, rows,
                           criteria=ScreenerCriteria(max_quote_age_min=30,
                                                     dte_min=7, dte_max=30))
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["checked_expirations"], [])
        self.assertEqual(aapl["status"], STATUS_NOT_CHECKED)
        self.assertEqual(aapl["exclusion"]["code"], "no_expiration_in_window")
        self.assertEqual(aapl["skipped_expirations"][0]["dte"], 44)
        # Nothing was checked, so the per-strategy freshness says exactly that -
        # not the chain-wide verdict (review finding on PR #74).
        for sid in STRATEGY_IDS:
            self.assertEqual(aapl["strategies"][sid]["checked_freshness"], "unknown")
        self.assertEqual(aapl["data"]["freshness"], "fresh")

    def test_the_expiration_budget_names_what_it_left_out(self):
        rows = [calendar_row("AAPL", 30)]
        kette = call_pair("AAPL", EXP_A) + call_pair("AAPL", EXP_B)
        koerper = run_scan({"AAPL": kette}, rows,
                           limits=ScanLimits(max_expirations_per_symbol=1))
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["checked_expirations"], [EXP_A])
        self.assertEqual(aapl["skipped_expirations"][0]["reason"], LIMIT_MAX_EXPIRATIONS)
        self.assertEqual(aapl["data"]["completeness"], "partial")
        self.assertIn("expiration_limit_reached", aapl["quality_flags"])

    def test_both_not_checked_paths_answer_with_the_same_rule(self):
        # Window path and budget path must agree: nothing checked -> "unknown".
        rows = [calendar_row("AAPL", 30)]
        fenster = run_scan({"AAPL": call_pair("AAPL", EXP_B)}, rows,
                           criteria=ScreenerCriteria(dte_min=7, dte_max=30))
        budget = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                          limits=ScanLimits(max_seconds=0.0, concurrency=1))
        for koerper in (fenster, budget):
            eintrag = entry_of(koerper, "AAPL")
            self.assertEqual(eintrag["status"], STATUS_NOT_CHECKED)
            for sid in STRATEGY_IDS:
                self.assertEqual(eintrag["strategies"][sid]["checked_freshness"], "unknown")

    def test_without_the_age_guard_an_empty_set_is_unchecked_not_unknown(self):
        # The other half of the same rule: no guard configured -> nothing can be
        # called unknown about the age.
        rows = [calendar_row("AAPL", 30)]
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           criteria=ScreenerCriteria(max_quote_age_min=None),
                           limits=ScanLimits(max_seconds=0.0, concurrency=1))
        eintrag = entry_of(koerper, "AAPL")
        self.assertEqual(eintrag["status"], STATUS_NOT_CHECKED)
        for sid in STRATEGY_IDS:
            self.assertEqual(eintrag["strategies"][sid]["checked_freshness"], "unchecked")

    def test_the_deadline_leaves_the_rest_explicitly_unchecked(self):
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]
        chains = {r["symbol"]: call_pair(r["symbol"], EXP_A) for r in rows}
        koerper = run_scan(chains, rows,
                           limits=ScanLimits(max_seconds=0.0, concurrency=1))
        self.assertEqual(koerper["coverage"]["not_checked"], 2)
        self.assertFalse(koerper["coverage"]["complete"])
        self.assertTrue(koerper["coverage"]["truncated"])
        self.assertEqual(koerper["coverage"]["truncation_reasons"][0]["code"],
                         LIMIT_DEADLINE)
        for eintrag in koerper["entries"]:
            self.assertEqual(eintrag["status"], STATUS_NOT_CHECKED)
            self.assertEqual(eintrag["exclusion"]["code"], LIMIT_DEADLINE)


class TestTheBestRowIsTheDocumentedSort(unittest.TestCase):
    def test_best_is_the_first_row_of_the_page_sort(self):
        chains = {"AAPL": call_pair("AAPL", EXP_A) + call_pair(
            "AAPL", EXP_B, long_bid=1.40, long_ask=1.50,
            short_bid=0.40, short_ask=0.50)}
        koerper = run_scan(chains, [calendar_row("AAPL", 30)])
        best = entry_of(koerper, "AAPL")["strategies"][BULL_CALL_DEBIT]["best"]
        self.assertEqual(koerper["best_sort"], "rendite_desc")
        self.assertEqual(best["dte"], 44)

        quotes = FakeChainAdapter(chains).get_options("AAPL")
        kept, _dropped = vertical_spreads(quotes, _LOOSE, strategy=BULL_CALL_DEBIT,
                                          now_fn=CLOCK)
        erwartet = sorted(kept, key=_vertical_sort_key("rendite_desc"))[0]
        self.assertEqual(best["symbol"], erwartet.symbol)

    def test_the_scan_key_and_the_page_key_order_identically(self):
        chains = {
            "AAPL": call_pair("AAPL", EXP_A) + call_pair("AAPL", EXP_B),
            "MSFT": call_pair("MSFT", EXP_A, long_bid=1.00, long_ask=1.10,
                              short_bid=0.40, short_ask=0.50),
            "NVDA": call_pair("NVDA", EXP_B, long_bid=2.60, long_ask=2.75,
                              short_bid=0.20, short_ask=0.30),
        }
        alle = []
        for symbol, kette in chains.items():
            kept, _d = vertical_spreads(FakeChainAdapter({symbol: kette}).get_options(symbol),
                                        _LOOSE, strategy=BULL_CALL_DEBIT, now_fn=CLOCK)
            alle.extend(kept)
        self.assertGreater(len(alle), 2)
        seite = [pair.symbol for pair in sorted(alle, key=_vertical_sort_key("rendite_desc"))]
        scan = [pair.symbol for pair in sorted(alle, key=best_sort_key)]
        self.assertEqual(scan, seite)


class TestTheAgePassRunsOncePerExpiry(unittest.TestCase):
    """Review finding on PR #74: the freshness split was recomputed per strategy."""

    def test_fewer_age_passes_than_strategies_times_expiries(self):
        # Four strategies over two checked expiries. Before this fix that was
        # 1 chain pass + 4 x 2 strategy passes = 9; now it is 1 + 2 = 3.
        from paperbroker import earnings_scan as modul

        kette = call_pair("AAPL", EXP_A) + call_pair("AAPL", EXP_B)
        aufrufe = []
        echt = modul._freshness

        def zaehlend(quotes, criteria, now_fn):
            aufrufe.append(len(quotes))
            return echt(quotes, criteria, now_fn)

        modul._freshness = zaehlend
        try:
            koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 30)])
        finally:
            modul._freshness = echt

        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(len(aapl["checked_expirations"]), 2)
        self.assertEqual(len(koerper["strategies"]), 4)
        self.assertEqual(len(aufrufe), 3, f"age passes: {aufrufe}")

    def test_the_fold_equals_one_pass_over_the_union(self):
        """The shortcut is only allowed if it answers what the union answers."""
        from paperbroker.earnings_scan import _combine_freshness, _freshness

        alt = "2026-10-07 09:00:00"
        faelle = {
            "zwei frische": [call_pair("AAPL", EXP_A), call_pair("AAPL", EXP_B)],
            "eine alt, eine frisch": [call_pair("AAPL", EXP_A),
                                       call_pair("AAPL", EXP_B, quote_timestamp=alt)],
            "beide alt": [call_pair("AAPL", EXP_A, quote_timestamp=alt),
                          call_pair("AAPL", EXP_B, quote_timestamp=alt)],
            "halbe Expiry alt": [[quote("AAPL261030C00250000", 2.40, 2.50),
                                  quote("AAPL261030C00252500", 0.50, 0.55,
                                        quote_timestamp=alt)],
                                 call_pair("AAPL", EXP_B)],
        }
        for name, expiries in faelle.items():
            teile = [_freshness(e, _LOOSE, CLOCK) for e in expiries]
            vereinigt = _freshness([q for e in expiries for q in e], _LOOSE, CLOCK)
            self.assertEqual(_combine_freshness(teile, _LOOSE), vereinigt, name)

    def test_an_unreadable_expiry_contributes_no_evidence(self):
        # The fold must answer like one pass over the union: an "unknown" part
        # is no evidence, not a fresh-leaning one (review finding on PR #74).
        from paperbroker.earnings_scan import _combine_freshness

        self.assertEqual(_combine_freshness(["unknown"], _LOOSE), "unknown")
        self.assertEqual(_combine_freshness(["unknown", "stale"], _LOOSE), "stale")
        self.assertEqual(_combine_freshness(["unknown", "fresh"], _LOOSE), "fresh")
        self.assertEqual(_combine_freshness(["unknown", "partial"], _LOOSE), "partial")

    def test_each_quote_is_keyed_exactly_once(self):
        # Review finding on PR #74: the bucket comprehension parsed every quote
        # once per checked expiry. One pass for the ticker is the contract.
        from paperbroker import earnings_scan as modul

        kette = call_pair("AAPL", EXP_A) + call_pair("AAPL", EXP_B)
        aufrufe = []
        echt = modul._expiry_key

        def zaehlend(quote):
            aufrufe.append(1)
            return echt(quote)

        modul._expiry_key = zaehlend
        try:
            koerper = run_scan({"AAPL": kette}, [calendar_row("AAPL", 30)])
        finally:
            modul._expiry_key = echt

        self.assertEqual(len(entry_of(koerper, "AAPL")["checked_expirations"]), 2)
        self.assertEqual(len(aufrufe), len(kette), f"key passes: {len(aufrufe)} for {len(kette)} quotes")

    def test_list_and_buckets_use_the_same_normalised_key(self):
        # A trailing space in the raw field must not empty the expiry bucket and
        # make a listed expiry look like one that was never read. Unit-level on
        # purpose: the padded string breaks arrow's date parser further down, so
        # feeding it through the screener would test arrow, not this rule.
        from paperbroker.earnings_scan import _expiry_key, _listed_expirations

        quote_a = quote("AAPL261030C00250000", 2.40, 2.50)
        quote_a.asset.expiration_date = quote_a.asset.expiration_date + " "
        self.assertEqual(_expiry_key(quote_a), EXP_A)
        self.assertEqual([d.isoformat() for d in _listed_expirations([quote_a])], [EXP_A])
        # An OCC symbol without an expiry is not an expiry - it is no key at all.
        ohne = quote("AAPL261030C00250000", 2.40, 2.50)
        ohne.asset.expiration_date = "not-a-date"
        self.assertIsNone(_expiry_key(ohne))

    def test_an_uncheckable_expiry_costs_one_expiry_not_the_scan(self):
        # Found by CI: the maturity arithmetic can raise on one expiry. That must
        # cost that one expiry (and be named), never the whole scan - the other
        # titles must not depend on it. The failure is injected on purpose: it
        # must not hang on what arrow happens to accept in a given version.
        from paperbroker import earnings_scan as modul

        echt = modul.vertical_spreads

        def kaputt(chain, criteria, *, strategy=None, now_fn=None, **kw):
            if chain and chain[0].asset.underlying.symbol == "AAPL":
                raise ValueError("unpriceable expiry")
            return echt(chain, criteria, strategy=strategy, now_fn=now_fn, **kw)

        modul.vertical_spreads = kaputt
        try:
            koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A),
                                "MSFT": call_pair("MSFT", EXP_A)},
                               [calendar_row("AAPL", 30), calendar_row("MSFT", 20)])
        finally:
            modul.vertical_spreads = echt
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["status"], STATUS_INCOMPLETE_DATA)
        self.assertIn("expiration_not_checkable", aapl["quality_flags"])
        self.assertEqual(aapl["skipped_expirations"][0]["reason"], "expiration_not_checkable")
        self.assertEqual(entry_of(koerper, "MSFT")["status"], STATUS_HITS)

    def test_without_the_age_guard_nothing_is_called_stale(self):
        from paperbroker.earnings_scan import _combine_freshness

        ohne = ScreenerCriteria(max_quote_age_min=None)
        self.assertEqual(_combine_freshness(["stale", "stale"], ohne), "unchecked")


class TestTheScannedDaySuppliesTheRecord(unittest.TestCase):
    """The row of the scanned day is the earnings record - no lookup needed.

    Measured before this: GET /api/v1/earnings-scan?day=2026-11-12 answered
    ``window_unknown`` for all 40 titles, because Nasdaq has no *upcoming* date
    for a title reporting TODAY. The row itself already names the day.
    """

    def test_a_calendar_row_yields_the_earnings_record(self):
        record = earnings_from_calendar_row(calendar_row("AAPL", 30), SCAN_DAY, "live")
        self.assertEqual(record["next_earnings_date"], SCAN_DAY)
        self.assertEqual(record["release_session"], SESSION_AFTER)
        self.assertEqual(record["date_confirmation"], CONFIRMATION_CONFIRMED)
        self.assertEqual(record["source"], SOURCE_CALENDAR_ROW)
        self.assertEqual(record["status"], STATUS_FRESH)
        # The row carries no timestamp: the record says so instead of borrowing
        # the scan's clock, which would look like a checked proof.
        self.assertIsNone(record["source_checked_at_utc"])
        self.assertIn(FLAG_CALENDAR_ROW_SOURCE, record["quality_flags"])
        self.assertIn("calendar_row_without_timestamp", record["quality_flags"])
        # Same contract as every other record - no key gained, none lost.
        self.assertEqual(set(record), set(empty_earnings("AAPL")))

    def test_a_stale_listing_does_not_prove_a_fresh_date(self):
        # Kody finding on PR #80: the row settles WHICH day a symbol reports
        # on, but only a LIVE listing proves that answer is still today's
        # truth. A stale cache day used to produce a fresh, confirmed record -
        # and the fail-closed window gate then let a window through on it.
        record = earnings_from_calendar_row(calendar_row("AAPL", 30), SCAN_DAY, "stale")
        self.assertEqual(record["next_earnings_date"], SCAN_DAY)
        self.assertEqual(record["status"], STATUS_STALE)
        self.assertEqual(record["date_confirmation"], CONFIRMATION_UNKNOWN)
        self.assertIn(FLAG_CALENDAR_ROW_STALE_DAY, record["quality_flags"])
        # The date, the session and the source are still the row's statement:
        # only the proof weakens, nothing is rewritten.
        self.assertEqual(record["release_session"], SESSION_AFTER)
        self.assertEqual(record["source"], SOURCE_CALENDAR_ROW)
        self.assertIn(FLAG_CALENDAR_ROW_SOURCE, record["quality_flags"])
        self.assertEqual(set(record), set(empty_earnings("AAPL")))

    def test_a_stale_row_cannot_authorise_a_window(self):
        rows = [calendar_row("AAPL", 30)]
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           day_state="stale")
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["data"]["earnings_status"], STATUS_STALE)
        # The gate blocks instead of authorising on an old entry - and it
        # names both reasons, so the verdict is readable from the answer. A
        # stale record with an unknown confirmation is UNDECIDABLE, which the
        # shared gate reports as ``unknown`` rather than as a clean "blocked".
        self.assertFalse(aapl["window"]["eligible"])
        self.assertEqual(aapl["window"]["status"], "unknown")
        # ``selected_expiration`` only NAMES the first listed expiry after the
        # date; it authorises nothing on its own - the gate says ineligible and
        # the entry is never scanned.
        codes = [g["code"] for g in aapl["window"]["reasons"]]
        self.assertIn("earnings_data_stale", codes)
        self.assertIn("date_confirmation_unknown", codes)
        self.assertEqual(aapl["status"], STATUS_WINDOW_UNKNOWN)
        self.assertNotEqual(aapl["status"], STATUS_HITS)
        self.assertEqual(aapl["checked_expirations"], [])
        self.assertEqual(aapl["strategies"], {})

    def test_a_live_row_still_authorises_the_window(self):
        rows = [calendar_row("AAPL", 30)]
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           day_state="live")
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(aapl["data"]["earnings_status"], STATUS_FRESH)
        self.assertTrue(aapl["window"]["eligible"])
        self.assertEqual(aapl["window"]["status"], "ok")
        self.assertEqual(aapl["window"]["planned_last_exit_date"], "2026-10-19")
        self.assertEqual(aapl["window"]["selected_expiration"], EXP_A)
        self.assertEqual(aapl["status"], STATUS_HITS)
        codes = [g["code"] for g in aapl["window"]["reasons"]]
        self.assertNotIn("earnings_data_stale", codes)
        self.assertNotIn("date_confirmation_unknown", codes)

    def test_a_session_the_row_does_not_state_is_not_guessed(self):
        for wert in (SESSION_UNKNOWN, None, "midday"):
            record = earnings_from_calendar_row(
                dict(calendar_row("AAPL", 30), session=wert), SCAN_DAY, "live")
            self.assertEqual(record["release_session"], SESSION_UNKNOWN)
            self.assertIn("release_session_unknown", record["quality_flags"])

    def test_a_day_that_is_not_a_day_yields_no_record(self):
        row = calendar_row("AAPL", 30)
        for tag in (None, "", "not-a-day", 20261020):
            self.assertIsNone(earnings_from_calendar_row(row, tag, "live"))

    def test_the_scanned_day_pays_no_earnings_lookup(self):
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]
        gerufen = []

        def gezaehlt(symbol):
            gerufen.append(symbol)
            return FakeEarningsAdapter(rows).get_earnings(symbol)

        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A),
                            "MSFT": call_pair("MSFT", EXP_A)}, rows,
                           fetch_earnings=gezaehlt)
        self.assertEqual(gerufen, [])
        self.assertEqual(koerper["coverage"]["source_calls"]["earnings"], 0)
        for eintrag in koerper["entries"]:
            self.assertEqual(eintrag["data"]["earnings_source"], SOURCE_CALENDAR_ROW)
            self.assertEqual(eintrag["data"]["earnings_date"], SCAN_DAY)
            self.assertEqual(eintrag["data"]["earnings_status"], STATUS_FRESH)

    def test_without_a_day_the_source_is_asked_once_per_ticker(self):
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]
        gerufen = []

        def gezaehlt(symbol):
            gerufen.append(symbol)
            return FakeEarningsAdapter(rows).get_earnings(symbol)

        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A),
                            "MSFT": call_pair("MSFT", EXP_A)}, rows,
                           fetch_earnings=gezaehlt, day=None)
        self.assertEqual(sorted(gerufen), ["AAPL", "MSFT"])
        self.assertEqual(koerper["coverage"]["source_calls"]["earnings"], 2)
        for eintrag in koerper["entries"]:
            self.assertEqual(eintrag["data"]["earnings_source"], "nasdaq_earnings_date")
            self.assertEqual(eintrag["data"]["earnings_date"], EARNINGS_DATE)

    def test_a_failing_lookup_without_a_day_does_not_stop_the_scan(self):
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]
        adapter = FakeEarningsAdapter(rows, failures={"AAPL": RuntimeError("boom")})
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A),
                            "MSFT": call_pair("MSFT", EXP_A)}, rows,
                           fetch_earnings=adapter.get_earnings, day=None)
        self.assertEqual(entry_of(koerper, "AAPL")["status"], STATUS_FETCH_FAILED)
        self.assertEqual(entry_of(koerper, "AAPL")["exclusion"]["code"],
                         "earnings_fetch_failed")
        self.assertEqual(entry_of(koerper, "MSFT")["status"], STATUS_HITS)
        self.assertEqual(koerper["coverage"]["processed"], 1)

    def test_the_running_day_is_named_as_the_report_today(self):
        # The rule, not a bug: the planned exit day is the last trading day
        # STRICTLY before the report, so a title reporting today can no longer
        # be entered - and that is a verdict, not "we do not know". The cause is
        # named ("report today"), the past exit day is its consequence - and the
        # chain that cannot change the answer is not requested at all.
        rows = [calendar_row("AAPL", 30)]
        adapter = FakeChainAdapter({"AAPL": call_pair("AAPL", EXP_A)})
        koerper = run_scan({"AAPL": call_pair("AAPL", EXP_A)}, rows,
                           adapter=adapter, day=TODAY.isoformat())
        aapl = entry_of(koerper, "AAPL")
        self.assertEqual(adapter.calls, [])
        self.assertEqual(aapl["status"], STATUS_WINDOW_BLOCKED)
        self.assertEqual(aapl["window"]["status"], "blocked")
        self.assertEqual(aapl["window"]["planned_last_exit_date"], "2026-10-06")
        self.assertEqual(aapl["window"]["remaining_trading_days"], 0)
        self.assertEqual([g["code"] for g in aapl["window"]["reasons"]],
                         ["earnings_today"])
        self.assertIn("Bericht ist heute", aapl["exclusion"]["message"])
        self.assertEqual(aapl["exclusion"]["code"], "earnings_window_blocked")

    def test_no_row_of_a_scanned_day_is_window_unknown(self):
        chains = {
            "AAPL": call_pair("AAPL", EXP_A) + call_pair("AAPL", EXP_B),
            "MSFT": call_pair("MSFT", EXP_A),
            "NVDA": put_pair("NVDA", EXP_A),
        }
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20),
                calendar_row("NVDA", 10)]
        koerper = run_scan(chains, rows)
        self.assertEqual(koerper["counts"][STATUS_WINDOW_UNKNOWN], 0)
        self.assertEqual(sum(koerper["counts"].values()),
                         koerper["coverage"]["processed"])
        for eintrag in koerper["entries"]:
            self.assertNotEqual(eintrag["status"], STATUS_WINDOW_UNKNOWN)


class TestAnUnprovenRecordCostsNoChain(unittest.TestCase):
    """Kody finding on PR #80: the chain is the expensive call of a scan, and a
    record that can never authorise a window does not need it.

    Measured before this: every row of a stale calendar day paid one chain
    request, up to ``max_symbols`` of them, for a verdict the record alone
    already fixed.
    """

    def _stale_scan(self):
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]
        chains = {
            "AAPL": call_pair("AAPL", EXP_A),
            "MSFT": call_pair("MSFT", EXP_A),
        }
        adapter = FakeChainAdapter(chains)
        return run_scan(chains, rows, day_state="stale", adapter=adapter), adapter

    def test_an_unproven_record_costs_no_chain_request(self):
        koerper, adapter = self._stale_scan()
        # Not one chain was asked for: the record decided on its own.
        self.assertEqual(adapter.calls, [])
        self.assertEqual(koerper["coverage"]["source_calls"]["chains"], 0)
        for symbol in ("AAPL", "MSFT"):
            eintrag = entry_of(koerper, symbol)
            self.assertEqual(eintrag["data"]["earnings_status"], STATUS_STALE)
            # The omission is visible and deliberate, not a missing chain.
            self.assertIs(eintrag["data"]["chain_requested"], False)
            self.assertFalse(eintrag["window"]["eligible"])
            self.assertIn(eintrag["status"],
                          (STATUS_WINDOW_UNKNOWN, STATUS_WINDOW_BLOCKED))
            self.assertNotEqual(eintrag["status"], STATUS_HITS)
            # Nothing about a chain is claimed: no contracts, no stamp, no scan.
            self.assertEqual(eintrag["data"]["chain_contracts"], 0)
            self.assertIsNone(eintrag["data"]["chain_quote_timestamp_raw"])
            self.assertEqual(eintrag["checked_expirations"], [])
            self.assertEqual(eintrag["skipped_expirations"], [])
            self.assertEqual(eintrag["strategies"], {})

    def test_an_unproven_record_keeps_the_counts_adding_up(self):
        koerper, _adapter = self._stale_scan()
        self.assertEqual(sum(koerper["counts"].values()),
                         koerper["coverage"]["processed"])
        self.assertEqual(koerper["coverage"]["processed"], 2)
        # The same counter as before: an undecidable window is a verdict about
        # the trade, never "not checked" and never a silent gap.
        self.assertEqual(koerper["counts"][STATUS_WINDOW_UNKNOWN], 2)
        self.assertEqual(koerper["counts"][STATUS_NOT_CHECKED], 0)
        for eintrag in koerper["entries"]:
            self.assertIsNotNone(eintrag["data"])

    def test_a_fresh_record_still_fetches_the_chain(self):
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]
        chains = {
            "AAPL": call_pair("AAPL", EXP_A),
            "MSFT": call_pair("MSFT", EXP_A),
        }
        adapter = FakeChainAdapter(chains)
        koerper = run_scan(chains, rows, adapter=adapter)
        # One request per title - the fresh path is untouched.
        self.assertEqual(sorted(symbol for symbol, _exp in adapter.calls),
                         ["AAPL", "MSFT"])
        self.assertEqual(koerper["coverage"]["source_calls"]["chains"], 2)
        for symbol in ("AAPL", "MSFT"):
            eintrag = entry_of(koerper, symbol)
            self.assertIs(eintrag["data"]["chain_requested"], True)
            self.assertTrue(eintrag["window"]["eligible"])
            self.assertEqual(eintrag["status"], STATUS_HITS)
            self.assertGreater(eintrag["data"]["chain_contracts"], 0)

    def test_a_missing_confirmation_costs_no_chain_request(self):
        # Kody's third finding: a FRESH record that simply does not state its
        # confirmation passed the record-only precheck, paid a chain request and
        # was blocked by the gate only afterwards. ``None`` and ``""`` must be
        # read as "unknown" here, exactly as the gate reads them.
        rows = [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]
        chains = {
            "AAPL": call_pair("AAPL", EXP_A),
            "MSFT": call_pair("MSFT", EXP_A),
        }
        for fehlend in (None, ""):
            with self.subTest(date_confirmation=fehlend):
                adapter = FakeChainAdapter(chains)

                def antwort(symbol, _fehlend=fehlend):
                    koerper = FakeEarningsAdapter(rows).get_earnings(symbol)
                    koerper["earnings"]["date_confirmation"] = _fehlend
                    return koerper

                koerper = run_scan(chains, rows, fetch_earnings=antwort,
                                   adapter=adapter, day=None)
                # The record alone already refuses: not one chain was asked.
                self.assertEqual(adapter.calls, [])
                self.assertEqual(koerper["coverage"]["source_calls"]["chains"], 0)
                for symbol in ("AAPL", "MSFT"):
                    eintrag = entry_of(koerper, symbol)
                    self.assertEqual(eintrag["data"]["earnings_status"], STATUS_FRESH)
                    self.assertIs(eintrag["data"]["chain_requested"], False)
                    self.assertFalse(eintrag["window"]["eligible"])
                    self.assertIn(eintrag["status"],
                                  (STATUS_WINDOW_UNKNOWN, STATUS_WINDOW_BLOCKED))


class TestTheRoute(unittest.TestCase):
    def _rows(self):
        return [calendar_row("AAPL", 30), calendar_row("MSFT", 20)]

    def _client(self, *, rows=None, chains=None, earnings=None):
        rows = self._rows() if rows is None else rows
        chains = chains if chains is not None else {
            "AAPL": call_pair("AAPL", EXP_A),
            "MSFT": call_pair("MSFT", EXP_A),
        }
        return create_app(
            quote_adapter=FakeChainAdapter(chains),
            earnings_adapter=earnings or FakeEarningsAdapter(rows),
            now_fn=CLOCK,
        ).test_client()

    def test_the_age_guard_can_be_switched_off_like_on_the_single_view(self):
        # Found by the live probe: the criteria parser honours
        # `ignore_quote_age`, so the scan must accept the parameter too -
        # otherwise a weekend scan could never be run at all.
        antwort = self._client().get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}&ignore_quote_age=1")
        self.assertEqual(antwort.status_code, 200)
        self.assertIsNone(antwort.get_json()["criteria"]["max_quote_age_min"])

    def test_the_scan_answers_its_contract(self):
        antwort = self._client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}")
        self.assertEqual(antwort.status_code, 200)
        koerper = antwort.get_json()
        self.assertEqual(koerper["schema_version"], "1.0")
        self.assertEqual(koerper["day"], SCAN_DAY)
        self.assertEqual(koerper["day_state"], "live")
        self.assertEqual(koerper["strategies"], list(STRATEGY_IDS))
        self.assertEqual(koerper["coverage"]["day_rows"], 2)
        self.assertTrue(koerper["coverage"]["truncated"] is False)
        self.assertEqual(koerper["counts"][STATUS_HITS], 2)

    def test_several_strategies_can_be_selected(self):
        antwort = self._client().get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}"
            f"&strategy={BULL_CALL_DEBIT}&strategy={BEAR_PUT_DEBIT}")
        self.assertEqual(antwort.get_json()["strategies"],
                         [BULL_CALL_DEBIT, BEAR_PUT_DEBIT])

    def test_a_comma_list_works_too(self):
        antwort = self._client().get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}&strategy={BULL_CALL_DEBIT},{BEAR_PUT_DEBIT}")
        self.assertEqual(antwort.get_json()["strategies"],
                         [BULL_CALL_DEBIT, BEAR_PUT_DEBIT])

    def test_the_legacy_alias_still_names_one_strategy(self):
        koerper = self._client().get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}&option_type=put").get_json()
        self.assertEqual(koerper["strategies"], [BEAR_PUT_DEBIT])

    def test_the_alias_cannot_be_combined_with_several_strategies(self):
        antwort = self._client().get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}&strategy={BULL_CALL_DEBIT}"
            f"&strategy={BEAR_PUT_DEBIT}&option_type=put")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"], "conflicting strategy and option_type")

    def test_an_unknown_strategy_is_refused(self):
        antwort = self._client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}&strategy=iron_condor")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"], "unknown strategy")

    def test_the_day_is_required_and_validated(self):
        self.assertEqual(self._client().get("/api/v1/earnings-scan").status_code, 400)
        self.assertEqual(
            self._client().get("/api/v1/earnings-scan?day=2026-02-31")
            .get_json()["error"]["code"], "invalid_day")
        self.assertEqual(
            self._client().get("/api/v1/earnings-scan?day=07.10.2026")
            .get_json()["error"]["code"], "invalid_day")

    def test_unknown_parameters_are_named(self):
        antwort = self._client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}&ticker=AAPL")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "unknown_parameter")

    def test_a_duplicate_day_is_refused(self):
        antwort = self._client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}&day={SCAN_DAY}")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "duplicate_parameter")

    def test_the_budget_is_bounded(self):
        self.assertEqual(
            self._client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}&max_symbols=9999")
            .get_json()["error"]["code"], "invalid_limit")
        self.assertEqual(
            self._client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}&concurrency=0")
            .get_json()["error"]["code"], "invalid_limit")

    def test_a_criteria_error_is_reported(self):
        antwort = self._client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}&dte_min=50&dte_max=10")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "invalid_criteria")

    def test_a_failed_day_is_a_service_error_not_an_empty_day(self):
        rows = self._rows()
        earnings = FakeEarningsAdapter(rows, day_state="unavailable")
        antwort = self._client(rows=rows, earnings=earnings).get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}")
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(antwort.get_json()["error"]["code"], "calendar_unavailable")

    def test_a_blocked_source_is_named_in_the_503_instead_of_did_not_answer(self):
        # The source answered 403 - so "did not answer" is a false sentence.
        # The route's own code stays ``calendar_unavailable`` (nothing to scan),
        # the upstream code and status travel beside it.
        class _Blocked(FakeEarningsAdapter):
            def get_calendar_days(self, days):
                antwort = super().get_calendar_days(days)
                for eintrag in antwort["days"].values():
                    eintrag.update(
                        state="unavailable", rows=[], payload_ok=False,
                        live_query_failed=True,
                        error={"code": "source_blocked", "http_status": 403,
                               "message": "the source blocked this client with HTTP 403"},
                    )
                antwort["state"] = "unavailable"
                return antwort

        antwort = self._client(earnings=_Blocked(self._rows())).get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}")
        self.assertEqual(antwort.status_code, 503)
        fehler = antwort.get_json()["error"]
        self.assertEqual(fehler["code"], "calendar_unavailable")
        self.assertEqual(fehler["source_code"], "source_blocked")
        self.assertEqual(fehler["http_status"], 403)
        self.assertIn("HTTP 403", fehler["message"])
        self.assertIn("source_blocked", fehler["message"])
        self.assertNotIn("did not answer", fehler["message"])

    def test_a_day_without_an_error_keeps_the_old_sentence_and_no_extra_fields(self):
        # Nothing to report about the source: the answer stays exactly as
        # before, without invented upstream fields.
        antwort = self._client(earnings=FakeEarningsAdapter(self._rows(), day_state="unavailable")).get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}")
        fehler = antwort.get_json()["error"]
        self.assertEqual(fehler["code"], "calendar_unavailable")
        self.assertNotIn("source_code", fehler)
        self.assertNotIn("http_status", fehler)

    def test_without_a_source_the_scan_says_so(self):
        app = create_app(quote_adapter=FakeChainAdapter({}), now_fn=CLOCK)
        antwort = app.test_client().get(f"/api/v1/earnings-scan?day={SCAN_DAY}")
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(antwort.get_json()["error"]["code"],
                         "earnings_source_not_configured")

    def test_the_start_page_never_starts_a_scan(self):
        rows = self._rows()
        earnings = FakeEarningsAdapter(rows)
        chains = FakeChainAdapter({"AAPL": call_pair("AAPL", EXP_A)})
        client = create_app(quote_adapter=chains, earnings_adapter=earnings,
                            now_fn=CLOCK).test_client()
        client.get("/?no_calendar")
        client.get("/")
        # Neither a chain nor a per-ticker earnings lookup was triggered: the
        # expensive path runs on an explicit request only.
        self.assertEqual(chains.calls, [])
        self.assertEqual(earnings.earnings_calls, [])

    def test_a_missing_day_entry_is_not_an_empty_day(self):
        # A live answer that simply forgets the requested day proves nothing
        # about that day: the scan must refuse instead of reporting "0 rows".
        class _Forgetful(FakeEarningsAdapter):
            def get_calendar_days(self, days):
                antwort = super().get_calendar_days(days)
                antwort["days"] = {}
                return antwort

        earnings = _Forgetful(self._rows())
        antwort = self._client(earnings=earnings).get(f"/api/v1/earnings-scan?day={SCAN_DAY}")
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(antwort.get_json()["error"]["code"], "calendar_unavailable")

    def test_a_stale_day_is_served_but_labelled(self):
        rows = self._rows()
        earnings = FakeEarningsAdapter(rows, day_state="stale")
        koerper = self._client(rows=rows, earnings=earnings).get(
            f"/api/v1/earnings-scan?day={SCAN_DAY}").get_json()
        self.assertEqual(koerper["day_state"], "stale")
        self.assertEqual(koerper["coverage"]["day_rows"], 2)
        # The header and the rows agree: a stale day may not produce a fresh
        # proof, so no row reaches a window and nothing is scanned for hits.
        self.assertEqual(koerper["counts"][STATUS_HITS], 0)
        for eintrag in koerper["entries"]:
            self.assertEqual(eintrag["data"]["earnings_status"], STATUS_STALE)
            self.assertFalse(eintrag["window"]["eligible"])
            self.assertEqual(eintrag["status"], STATUS_WINDOW_UNKNOWN)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
