"""Offline tests for the CBOE symbol list (slice 10, plan A, B and D).

The list is the only cheap answer to "does this ticker have listed options?":
one 388 KB download instead of one option-chain query per title (25 in flight
were throttled with HTTP 429, measured). Nothing here touches the network - the
transport is a fake that records its calls, exactly like the calendar tests.

Fail-closed is the point of the parsing tests: a header without the symbol
column yields the EMPTY set, and the adapter treats an empty set as a failed
answer instead of "no ticker has options" (which would filter the whole page).
"""

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from paperbroker.adapters.earnings.NasdaqEarningsAdapter import (
    OPTIONABLE_SYMBOLS_URL,
    DayCalendarStore,
    EarningsCache,
    NasdaqEarningsAdapter,
    OptionableSymbolsStore,
)
from paperbroker.optionable_symbols import filter_optionable, parse_cboe_symbols

# The real feed carries a UTF-8 BOM and a header whose column names start with
# a space (" Stock Symbol"). The company column is quoted and contains a comma
# on purpose: the symbol column must be found by NAME, not by position.
CSV_SAMPLE = (
    "\ufeff Stock Symbol , Company Name , Option Type\n"
    " AAPL ,Apple Inc.,Equity\n"
    'SPY,"SPDR S&P 500, Trust",ETF\n'
    "MSFT,Microsoft Corp.,Equity\n"
    " aapl ,Apple Inc.,Equity\n"
    "\n"
    ",,,\n"
)


def _row(symbol, cap=1000):
    return {"symbol": symbol, "name": f"{symbol} Inc.", "market_cap": cap}


class _Clock:
    def __init__(self, start="2026-11-08T06:00:00+00:00"):
        self.jetzt = datetime.fromisoformat(start)

    def now(self):
        return self.jetzt

    def advance(self, **kwargs):
        self.jetzt = self.jetzt + timedelta(**kwargs)


CSV_BODY = CSV_SAMPLE.encode("utf-8")


class _SymbolSource:
    """A fake transport for the CBOE download: records URLs, never dials out."""

    def __init__(self, body=CSV_BODY, status=200, fail=False):
        self.body = body
        self.status = status
        self.fail = fail
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if self.fail:
            raise OSError("the source is down")
        return self.status, self.body


# ---------------------------------------------------------------------- #
# A. CSV parsing                                                          #
# ---------------------------------------------------------------------- #
class TestParseCboeSymbols(unittest.TestCase):
    def test_bom_header_spaces_and_quotes_yield_the_symbols(self):
        # A1: BOM, leading spaces in the header AND the values, a quoted field
        # containing a comma, a duplicate and empty lines.
        self.assertEqual(parse_cboe_symbols(CSV_SAMPLE), {"AAPL", "SPY", "MSFT"})

    def test_symbols_are_uppercased(self):
        # A2: the source writes " aapl " - the calendar answers in uppercase,
        # so the set is normalised once, here.
        self.assertIn("AAPL", parse_cboe_symbols(CSV_SAMPLE))
        self.assertNotIn("aapl", parse_cboe_symbols(CSV_SAMPLE))

    def test_a_missing_symbol_column_is_an_empty_set(self):
        # A3: fail closed - a broken header yields nothing, not everything.
        text = " Company Name , Option Type \nApple Inc.,Equity\n"
        self.assertEqual(parse_cboe_symbols(text), set())

    def test_a_header_only_file_is_empty(self):
        self.assertEqual(parse_cboe_symbols("\ufeff Stock Symbol \n"), set())

    def test_non_text_input_is_an_empty_set(self):
        for wert in (None, 42, b" Stock Symbol \nAAPL\n", [" Stock Symbol "]):
            with self.subTest(wert=wert):
                self.assertEqual(parse_cboe_symbols(wert), set())

    def test_empty_values_do_not_become_a_symbol(self):
        text = " Stock Symbol , Company Name \n ,Nobody\nAAPL,Apple\n"
        self.assertEqual(parse_cboe_symbols(text), {"AAPL"})


# ---------------------------------------------------------------------- #
# B. Filter                                                               #
# ---------------------------------------------------------------------- #
class TestFilterOptionable(unittest.TestCase):
    def test_known_symbols_are_kept_and_unknowns_removed(self):
        rows = [_row("AAPL"), _row("IX"), _row("SPY"), _row("UI")]
        kept, removed = filter_optionable(rows, {"AAPL", "SPY"})
        self.assertEqual([z["symbol"] for z in kept], ["AAPL", "SPY"])
        self.assertEqual([z["symbol"] for z in removed], ["IX", "UI"])

    def test_none_means_no_filtering_at_all(self):
        rows = [_row("AAPL"), _row("IX")]
        kept, removed = filter_optionable(rows, None)
        self.assertEqual([z["symbol"] for z in kept], ["AAPL", "IX"])
        self.assertEqual(removed, [])

    def test_a_lowercase_symbol_is_not_normalised_by_the_filter(self):
        # B3: the calendar never hands out lowercase symbols; a row that does
        # is broken and must not be rescued into the filter's vocabulary.
        kept, removed = filter_optionable([_row("aapl")], {"AAPL"})
        self.assertEqual(kept, [])
        self.assertEqual([z["symbol"] for z in removed], ["aapl"])

    def test_an_empty_set_removes_every_row(self):
        kept, removed = filter_optionable([_row("AAPL")], set())
        self.assertEqual(kept, [])
        self.assertEqual(len(removed), 1)


# ---------------------------------------------------------------------- #
# D. adapter: one download, then 24 h cache                               #
# ---------------------------------------------------------------------- #
def _adapter(tmpdir, source, clock=None):
    clock = clock or _Clock()
    return NasdaqEarningsAdapter(
        transport=source,
        now_fn=clock.now,
        cache=EarningsCache(path=os.path.join(tmpdir, "earnings.json")),
        calendar_store=DayCalendarStore(path=os.path.join(tmpdir, "calendar.json")),
        optionable_store=OptionableSymbolsStore(path=os.path.join(tmpdir, "optionable.json")),
    )


class TestOptionableSymbolsAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_download_serves_the_next_calls_for_24_hours(self):
        # D1/D2
        clock = _Clock()
        quelle = _SymbolSource()
        adapter = _adapter(self.tmpdir, quelle, clock=clock)

        erste = adapter.get_optionable_symbols()
        self.assertEqual(erste["state"], "live")
        self.assertEqual(set(erste["symbols"]), {"AAPL", "SPY", "MSFT"})
        self.assertEqual(erste["fetched_at_utc"], "2026-11-08T06:00:00Z")
        self.assertIsNone(erste["error"])
        self.assertEqual(len(quelle.calls), 1)
        self.assertEqual(quelle.calls[0][0], OPTIONABLE_SYMBOLS_URL)

        clock.advance(hours=23)
        zweite = adapter.get_optionable_symbols()
        self.assertEqual(zweite["state"], "cached")
        self.assertEqual(set(zweite["symbols"]), {"AAPL", "SPY", "MSFT"})
        self.assertEqual(zweite["fetched_at_utc"], erste["fetched_at_utc"])
        self.assertEqual(len(quelle.calls), 1)

    def test_after_the_ttl_the_source_is_asked_again(self):
        clock = _Clock()
        quelle = _SymbolSource()
        adapter = _adapter(self.tmpdir, quelle, clock=clock)
        adapter.get_optionable_symbols()

        clock.advance(hours=24, seconds=1)
        zweite = adapter.get_optionable_symbols()
        self.assertEqual(zweite["state"], "live")
        self.assertEqual(len(quelle.calls), 2)

    def test_a_transport_failure_without_a_cache_is_unavailable(self):
        # D3: no list, no filter - and nothing is invented.
        quelle = _SymbolSource(fail=True)
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_optionable_symbols()
        self.assertEqual(ergebnis["state"], "unavailable")
        self.assertIsNone(ergebnis["symbols"])
        self.assertEqual(ergebnis["error"]["code"], "source_unreachable")
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "optionable.json")))

    def test_a_failed_live_query_with_an_old_list_serves_it_as_stale(self):
        # D4
        clock = _Clock()
        quelle = _SymbolSource()
        adapter = _adapter(self.tmpdir, quelle, clock=clock)
        erste = adapter.get_optionable_symbols()

        clock.advance(hours=25)
        quelle.fail = True
        zweite = adapter.get_optionable_symbols()
        self.assertEqual(zweite["state"], "stale")
        self.assertEqual(set(zweite["symbols"]), {"AAPL", "SPY", "MSFT"})
        self.assertEqual(zweite["fetched_at_utc"], erste["fetched_at_utc"])
        self.assertEqual(zweite["error"]["code"], "source_unreachable")

    def test_a_broken_cache_file_is_a_miss_not_a_crash(self):
        # D5
        pfad = os.path.join(self.tmpdir, "optionable.json")
        with open(pfad, "w", encoding="utf-8") as fh:
            fh.write("{kaputt")
        quelle = _SymbolSource()
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_optionable_symbols()
        self.assertEqual(ergebnis["state"], "live")
        self.assertEqual(len(quelle.calls), 1)

    def test_http_429_is_unavailable_and_not_a_filter(self):
        # D6: the measured throttle of the per-ticker endpoint is the reason
        # this code only ever downloads the collective list; an HTTP error on
        # the list itself is still an outage, labelled as one.
        quelle = _SymbolSource(body=b"", status=429)
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_optionable_symbols()
        self.assertEqual(ergebnis["state"], "unavailable")
        self.assertIsNone(ergebnis["symbols"])
        self.assertEqual(ergebnis["error"]["code"], "http_error")
        self.assertEqual(ergebnis["error"]["http_status"], 429)

    def test_a_csv_without_the_symbol_column_is_unavailable_and_not_cached(self):
        # D7: an empty set would filter EVERY title; fail closed instead.
        quelle = _SymbolSource(body=b" Company Name \nApple Inc.\n")
        adapter = _adapter(self.tmpdir, quelle)
        ergebnis = adapter.get_optionable_symbols()
        self.assertEqual(ergebnis["state"], "unavailable")
        self.assertIsNone(ergebnis["symbols"])
        self.assertEqual(ergebnis["error"]["code"], "invalid_payload")
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "optionable.json")))


if __name__ == "__main__":
    unittest.main()
