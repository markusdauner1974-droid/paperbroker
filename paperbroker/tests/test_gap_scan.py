"""Offline tests for the gap source and the candidate selection (gap_scan.py).

Every fixture is local: a Nasdaq answer is a dict written here, and the
transport is a stub that records its calls. Nothing in this file touches the
network, and nothing here changes the clock.

What the tests are here to prove, in the order of the requirement:

* a close below the previous one is a NEGATIVE gap, with the source's own
  rounding (PBR, 11.11.2026: 18.28 -> 17.82 = -2.52 %),
* a missing reaction session answers ``None``, never 0 - "not measurable" must
  not look like "no move",
* the 2 % threshold keeps TDG's 1.59 % out and lets SE's -3.66 % through,
* the direction decides the spread,
* penny stocks leave through the turnover hurdle, not through taste,
* candidates are sorted by the size of the move, capped at 30, never padded,
* a rate-limited source is NAMED (429), not swallowed into an empty list,
* a dotted symbol costs no call at all,
* "$173.73" and "339,214,800" parse; a row without a close is dropped.
"""

import json
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, dataclass
from datetime import date, timedelta

from paperbroker.gap_scan import (
    DEFAULT_LIMIT,
    STRATEGY_BEAR_PUT_DEBIT,
    STRATEGY_BULL_CALL_DEBIT,
    BoundedCache,
    GapBar,
    GapSourceError,
    NasdaqHistoryAdapter,
    classify_gap,
    compute_gap,
    fetch_gaps,
    parse_nasdaq_history,
    reaction_day_rows,
    scan_gaps,
    select_candidates,
    strategy_for_direction,
)

REACTION = date(2026, 11, 11)
PREVIOUS = date(2026, 11, 10)
# The window the fetch asks for: 30 calendar days before the reaction session.
PREVIOUS_WINDOW = REACTION - timedelta(days=30)


def row(day, open_="$10.00", close="$10.00", volume="1,000,000"):
    """One ``tradesTable`` row in the source's own shape (strings, $ signs)."""
    return {
        "date": day,
        "open": open_,
        "high": close,
        "low": open_,
        "close": close,
        "volume": volume,
    }


def payload(*rows):
    """A Nasdaq history answer around the given rows, newest first."""
    return {
        "data": {"tradesTable": {"rows": list(rows), "totalrecords": len(rows)}},
        "status": {"rCode": 200, "bCode": 0},
    }


def body(*rows):
    return json.dumps(payload(*rows)).encode("utf-8")


class StubTransport:
    """A transport with the adapter's signature that records every call."""

    def __init__(self, status=200, answer=b"{}", error=None):
        self.status = status
        self.answer = answer
        self.error = error
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.status, self.answer


@dataclass
class MeasuredRow:
    """The shape ``select_candidates`` expects - ``symbol``, ``gap_pct``, ``close``, ``volume``."""

    symbol: str
    gap_pct: float
    close: float
    volume: float


class GapMeasurementTest(unittest.TestCase):
    """The gap of one session against the one before it."""

    def test_a_closing_gap_below_the_previous_close_is_negative(self):
        bars = parse_nasdaq_history(
            payload(
                row("11/11/2026", open_="$17.95", close="$17.82", volume="21,400,000"),
                row("11/10/2026", open_="$18.31", close="$18.28", volume="19,100,000"),
            )
        )

        gap = compute_gap(bars, REACTION)
        if gap is None:
            self.fail("a close below the previous one is a measured gap")

        # Measured values (PBR, 11.11.2026): the sign is the statement.
        self.assertEqual(gap.gap_pct, -2.52)
        self.assertEqual(gap.prev_close, 18.28)
        self.assertEqual(gap.close, 17.82)
        self.assertEqual(gap.volume, 21_400_000.0)
        self.assertEqual(gap.reaction_date, REACTION)
        self.assertEqual(classify_gap(gap.gap_pct, 2.0), "down")

    def test_a_missing_reaction_day_yields_no_gap(self):
        bars = parse_nasdaq_history(
            payload(
                row("11/10/2026", close="$18.28"),
                row("11/07/2026", close="$18.55"),
            )
        )

        # No bar for 11.11. at all: the honest answer is "not measured".
        self.assertIsNone(compute_gap(bars, REACTION))

    def test_the_reaction_day_needs_a_session_before_it(self):
        bars = parse_nasdaq_history(payload(row("11/11/2026", close="$17.82")))

        # The day is there, the reference close is not - still no gap.
        self.assertIsNone(compute_gap(bars, REACTION))

    def test_a_zero_previous_close_is_no_gap(self):
        bars = parse_nasdaq_history(
            payload(
                row("11/11/2026", close="$1.20"),
                row("11/10/2026", open_="$0.00", close="$0.00"),
            )
        )

        # A ratio over 0 would be an invented number, not a measurement.
        self.assertIsNone(compute_gap(bars, REACTION))

    def test_the_previous_session_is_the_nearest_one(self):
        bars = parse_nasdaq_history(
            payload(
                row("11/11/2026", close="$11.00"),
                row("11/10/2026", close="$10.00"),
                row("11/06/2026", close="$5.00"),
            )
        )

        gap = compute_gap(bars, REACTION)
        if gap is None:
            self.fail("the nearest earlier session is the reference close")

        # 10.00 -> 11.00 is +10 %; the 5.00 from a week ago must not be used.
        self.assertEqual(gap.prev_close, 10.0)
        self.assertEqual(gap.gap_pct, 10.0)

    def test_a_threshold_of_two_percent_leaves_small_moves_out(self):
        # Measured: TDG 11.11.2026 moved 1.59 %, SE -3.66 %.
        self.assertEqual(classify_gap(1.59, 2.0), "none")
        self.assertEqual(classify_gap(-1.59, 2.0), "none")
        self.assertEqual(classify_gap(0.0, 2.0), "none")
        self.assertEqual(classify_gap(-3.66, 2.0), "down")
        self.assertEqual(classify_gap(3.66, 2.0), "up")
        # Both edges are inclusive: exactly 2 % is a trade, not a near-miss.
        self.assertEqual(classify_gap(2.0, 2.0), "up")
        self.assertEqual(classify_gap(-2.0, 2.0), "down")

    def test_the_direction_becomes_the_spread(self):
        self.assertEqual(strategy_for_direction("up"), STRATEGY_BULL_CALL_DEBIT)
        self.assertEqual(strategy_for_direction("down"), STRATEGY_BEAR_PUT_DEBIT)
        self.assertIsNone(strategy_for_direction("none"))
        self.assertIsNone(strategy_for_direction("sideways"))
        self.assertEqual(STRATEGY_BULL_CALL_DEBIT, "bull_call_debit")
        self.assertEqual(STRATEGY_BEAR_PUT_DEBIT, "bear_put_debit")


class CandidateSelectionTest(unittest.TestCase):
    """Who survives the threshold, the turnover hurdle and the sort."""

    def test_the_turnover_hurdle_removes_penny_stocks(self):
        # The two largest gaps of the measured day - both unfillable.
        rows = [
            MeasuredRow("SLXN", 23.6, 0.24, 3_750_000),      # 0.9 M USD
            MeasuredRow("UCL", 21.7, 0.23, 1_000_000),       # 0.23 M USD
            MeasuredRow("AAPL", 6.2, 210.0, 55_000_000),
            MeasuredRow("SE", -3.66, 58.4, 2_000_000),
            MeasuredRow("XYZ", 2.4, 12.0, 900_000),
        ]

        candidates = select_candidates(rows)

        self.assertEqual([c.symbol for c in candidates], ["AAPL", "SE", "XYZ"])
        self.assertNotIn("SLXN", [c.symbol for c in candidates])
        self.assertNotIn("UCL", [c.symbol for c in candidates])

    def test_the_candidates_are_sorted_by_the_size_of_the_move(self):
        rows = [
            MeasuredRow("SMALL", 2.5, 50.0, 1_000_000),
            MeasuredRow("BIGDOWN", -9.0, 50.0, 1_000_000),
            MeasuredRow("BBB", 4.0, 50.0, 1_000_000),
            MeasuredRow("AAA", 4.0, 50.0, 1_000_000),
        ]

        candidates = select_candidates(rows)

        # abs(gap) descending; the tie (both 4.0 %) by symbol ascending.
        self.assertEqual([c.symbol for c in candidates], ["BIGDOWN", "AAA", "BBB", "SMALL"])
        self.assertEqual([abs(c.gap_pct) for c in candidates], [9.0, 4.0, 4.0, 2.5])
        # The sign decides the spread, not the size.
        self.assertEqual(candidates[0].direction, "down")
        self.assertEqual(candidates[0].strategy, STRATEGY_BEAR_PUT_DEBIT)
        self.assertEqual(candidates[1].direction, "up")
        self.assertEqual(candidates[1].strategy, STRATEGY_BULL_CALL_DEBIT)

    def test_at_most_thirty_candidates_survive(self):
        rows = [
            MeasuredRow(f"S{index:02d}", 3.0 + index * 0.1, 50.0, 1_000_000)
            for index in range(40)
        ]

        candidates = select_candidates(rows)

        self.assertEqual(len(candidates), 30)
        # The cut keeps the BIGGEST moves: S00-S09 (3.0-3.9 %) fall out.
        self.assertEqual({c.symbol for c in candidates}, {f"S{index:02d}" for index in range(10, 40)})
        self.assertEqual(candidates[0].symbol, "S39")

    def test_fewer_than_thirty_candidates_are_not_padded(self):
        rows = [
            MeasuredRow("AAPL", 6.2, 210.0, 55_000_000),
            MeasuredRow("SE", -3.66, 58.4, 2_000_000),
        ]

        candidates = select_candidates(rows)

        self.assertEqual(len(candidates), 2)
        self.assertEqual([c.symbol for c in candidates], ["AAPL", "SE"])

    def test_the_turnover_is_the_session_value(self):
        candidates = select_candidates([MeasuredRow("AAPL", 6.2, 210.0, 55_000_000)])

        self.assertEqual(candidates[0].turnover_usd, 210.0 * 55_000_000)
        self.assertEqual(candidates[0].close, 210.0)
        self.assertEqual(candidates[0].volume, 55_000_000.0)

    def test_a_move_under_the_threshold_is_no_candidate(self):
        # TDG, 11.11.2026: 1.59 % on a liquid title - still no gap trade.
        candidates = select_candidates([MeasuredRow("TDG", 1.59, 1200.0, 1_000_000)])

        self.assertEqual(candidates, [])

    def test_an_empty_day_has_no_candidates(self):
        self.assertEqual(select_candidates([]), [])
        self.assertEqual(select_candidates(None), [])


class NasdaqHistoryAdapterTest(unittest.TestCase):
    """The source access: what it names, what it refuses, what it remembers."""

    def test_a_rate_limited_source_is_named_not_swallowed(self):
        transport = StubTransport(status=429, answer=b"Too Many Requests")
        adapter = NasdaqHistoryAdapter(transport=transport)

        with self.assertRaises(GapSourceError) as caught:
            adapter.fetch_daily("AAPL", REACTION)

        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(caught.exception.status, 429)
        self.assertIn("429", caught.exception.message)
        # str() carries the code, so a log line is never a bare sentence.
        self.assertTrue(str(caught.exception).startswith("http_error: "))

    def test_a_symbol_with_a_dot_is_refused_without_a_call(self):
        transport = StubTransport(answer=body(row("11/11/2026", close="$17.82")))
        adapter = NasdaqHistoryAdapter(transport=transport)

        with self.assertRaises(GapSourceError) as caught:
            adapter.fetch_daily("PBR.A", REACTION)

        self.assertEqual(caught.exception.code, "unsupported_symbol")
        self.assertIsNone(caught.exception.status)
        # The whole point: no request was made for a title this source cannot serve.
        self.assertEqual(transport.calls, [])

        with self.assertRaises(GapSourceError) as empty:
            adapter.fetch_daily("", REACTION)
        self.assertEqual(empty.exception.code, "unsupported_symbol")
        self.assertEqual(transport.calls, [])

    def test_a_transport_failure_is_named_not_raised_raw(self):
        transport = StubTransport(error=ConnectionError("connection refused"))
        adapter = NasdaqHistoryAdapter(transport=transport)

        with self.assertRaises(GapSourceError) as caught:
            adapter.fetch_daily("AAPL", REACTION)

        self.assertEqual(caught.exception.code, "transport_error")

    def test_an_answer_without_rows_is_named_no_data(self):
        for answer in (body(), json.dumps({"data": {}}).encode("utf-8"), b""):
            with self.subTest(answer=answer):
                adapter = NasdaqHistoryAdapter(transport=StubTransport(answer=answer))
                with self.assertRaises(GapSourceError) as caught:
                    adapter.fetch_daily("AAPL", REACTION)
                self.assertEqual(caught.exception.code, "no_data")

    def test_an_answer_that_is_not_json_is_named(self):
        adapter = NasdaqHistoryAdapter(transport=StubTransport(answer=b"<html>Access Denied</html>"))

        with self.assertRaises(GapSourceError) as caught:
            adapter.fetch_daily("AAPL", REACTION)

        self.assertEqual(caught.exception.code, "invalid_payload")

    def test_the_url_carries_the_symbol_the_day_and_the_limit(self):
        transport = StubTransport(answer=body(row("11/11/2026", close="$17.82")))
        adapter = NasdaqHistoryAdapter(transport=transport)

        adapter.fetch_daily("aapl", date(2026, 10, 28), limit=6)

        call = transport.calls[0]
        self.assertIn("/api/quote/AAPL/historical", call["url"])
        self.assertIn("fromdate=2026-10-28", call["url"])
        self.assertIn("limit=6", call["url"])
        self.assertIn("assetclass=stocks", call["url"])
        self.assertIn("Mozilla/5.0", call["headers"]["User-Agent"])

    def test_without_a_cache_every_call_asks_again(self):
        transport = StubTransport(answer=body(row("11/11/2026", close="$17.82")))
        adapter = NasdaqHistoryAdapter(transport=transport)

        adapter.fetch_daily("AAPL", REACTION)
        adapter.fetch_daily("AAPL", REACTION)

        self.assertEqual(len(transport.calls), 2)

    def test_a_given_cache_answers_the_second_call(self):
        transport = StubTransport(answer=body(row("11/11/2026", close="$17.82")))
        cache = {}
        adapter = NasdaqHistoryAdapter(transport=transport, cache=cache)

        first = adapter.fetch_daily("AAPL", REACTION, limit=6)
        second = adapter.fetch_daily("AAPL", REACTION, limit=6)

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(list(cache), [("AAPL", "2026-11-11", 6)])
        # A different window is a different question and is asked again.
        adapter.fetch_daily("AAPL", REACTION, limit=10)
        self.assertEqual(len(transport.calls), 2)

    def test_the_bounded_cache_evicts_the_oldest_entry(self):
        # The endpoint shares one adapter across requests, so its cache needs a
        # ceiling. FIFO by insertion: the oldest key leaves first, no TTL.
        transport = StubTransport(answer=body(row("11/11/2026", close="$17.82")))
        cache = BoundedCache(max_entries=2)
        adapter = NasdaqHistoryAdapter(transport=transport, cache=cache)

        for symbol in ("AAA", "BBB", "CCC"):
            adapter.fetch_daily(symbol, REACTION)

        self.assertEqual(len(cache), 2)
        # The ceiling holds and it is the OLDEST entry that went, not any entry.
        self.assertEqual([key[0] for key in cache], ["BBB", "CCC"])
        self.assertNotIn(("AAA", "2026-11-11", DEFAULT_LIMIT), cache)

        # The evicted title is asked again, because nothing remembers it ...
        adapter.fetch_daily("AAA", REACTION)
        self.assertEqual(len(transport.calls), 4)
        # ... and its insertion displaces the next oldest one, not a random key.
        self.assertEqual([key[0] for key in cache], ["CCC", "AAA"])
        # The survivor still answers from memory.
        adapter.fetch_daily("CCC", REACTION)
        self.assertEqual(len(transport.calls), 4)
        # The displaced BBB is a miss: the order is the eviction order.
        adapter.fetch_daily("BBB", REACTION)
        self.assertEqual(len(transport.calls), 5)
        self.assertEqual([key[0] for key in cache], ["AAA", "BBB"])

    def test_the_error_carries_a_code_a_message_and_no_status_by_default(self):
        error = GapSourceError("no_data", "the source answered no usable history rows")

        self.assertEqual(error.code, "no_data")
        self.assertEqual(error.message, "the source answered no usable history rows")
        self.assertIsNone(error.status)
        self.assertEqual(str(error), "no_data: the source answered no usable history rows")


class NasdaqHistoryParsingTest(unittest.TestCase):
    """Turning the source's strings into bars - and dropping what cannot be one."""

    def test_numbers_are_parsed_out_of_dollar_strings(self):
        bars = parse_nasdaq_history(
            payload(
                row("11/11/2026", open_="$170.00", close="$173.73", volume="339,214,800"),
                row("11/10/2026", open_="$168.40", close="$169.02", volume="21,000,000"),
            )
        )

        self.assertEqual(len(bars), 2)
        # Answer order is kept: the newest row stays first.
        self.assertEqual(
            bars[0],
            GapBar(session_date=date(2026, 11, 11), open=170.0, close=173.73, volume=339_214_800.0),
        )
        self.assertEqual(bars[1].session_date, date(2026, 11, 10))
        self.assertEqual(bars[1].close, 169.02)

    def test_a_bar_without_a_close_is_dropped(self):
        bars = parse_nasdaq_history(
            payload(
                row("11/11/2026", close="$17.82"),
                row("11/10/2026", close="N/A"),
                {"date": "11/07/2026", "open": "$18.00", "high": "$18.00", "low": "$18.00", "volume": "1,000"},
                row("not-a-date", close="$19.00"),
            )
        )

        # Only the one complete row survives; nothing is filled with 0.
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].session_date, date(2026, 11, 11))
        self.assertEqual(bars[0].close, 17.82)

    def test_a_bar_without_a_volume_is_dropped(self):
        bars = parse_nasdaq_history(
            payload(
                row("11/11/2026", close="$17.82", volume=""),
                row("11/10/2026", close="$18.28", volume="0"),
            )
        )

        # A real 0 is a bar (halted session); an empty volume is not.
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].session_date, date(2026, 11, 10))
        self.assertEqual(bars[0].volume, 0.0)

    def test_a_missing_rows_list_is_an_empty_answer(self):
        for answer in ({"data": {"tradesTable": {"rows": None}}}, {"data": {}}, {}, None, []):
            with self.subTest(answer=answer):
                self.assertEqual(parse_nasdaq_history(answer), [])

    def test_a_bar_is_immutable(self):
        bar = GapBar(session_date=REACTION, open=1.0, close=2.0, volume=3.0)

        with self.assertRaises(FrozenInstanceError):
            bar.close = 9.9  # pyright: ignore[reportAttributeAccessIssue] - the point of the test


class StubHistoryAdapter:
    """An adapter with ``fetch_daily``'s signature - answers per symbol, no network.

    ``bars[symbol]`` are the bars it returns and ``errors[symbol]`` the
    ``GapSourceError`` it raises instead, so one test can break one title
    without a transport, a server or a monkeypatch.
    """

    def __init__(self, bars=None, errors=None):
        self.bars = bars or {}
        self.errors = errors or {}
        self.calls = []

    def fetch_daily(self, symbol, fromdate, limit=DEFAULT_LIMIT):
        self.calls.append({"symbol": symbol, "fromdate": fromdate, "limit": limit})
        error = self.errors.get(symbol)
        if error is not None:
            raise error
        return list(self.bars.get(symbol, []))


def calendar_row(symbol, session=None, name=None, market_cap=None):
    """One calendar row of ``earnings_day_calendar`` - keys, not classes."""
    return {
        "symbol": symbol,
        "name": name if name is not None else f"{symbol} Inc.",
        "session": session,
        "market_cap": market_cap,
    }


def bars_for(reaction, close=11.0, previous_close=10.0, volume=1_000_000.0):
    """Two bars: the reaction session and the session before it."""
    return [
        GapBar(session_date=reaction, open=close, close=close, volume=volume),
        GapBar(
            session_date=reaction - timedelta(days=1),
            open=previous_close,
            close=previous_close,
            volume=volume,
        ),
    ]


class ReactionDayRowsTest(unittest.TestCase):
    """Who of a reaction day is even asked for a gap."""

    def test_the_previous_day_joins_the_set_without_a_proven_session(self):
        previous = [
            calendar_row("AAA", session="before_market"),
            calendar_row("BBB", session="unknown"),
            calendar_row("CCC", session=None),
        ]
        day = [
            calendar_row("DDD", session="before_market"),
            calendar_row("EEE", session="before_market"),
            calendar_row("FFF", session="after_market"),
            calendar_row("GGG", session="after_market"),
        ]

        rows = reaction_day_rows(previous, day, previous_date=PREVIOUS, day_date=REACTION)

        # 3 + 2: the two after_market titles of the day have no gap yet.
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["symbol"] for row in rows], ["DDD", "EEE", "AAA", "BBB", "CCC"])
        # Nasdaq deletes past sessions: "unknown" is a doubt, not "after close".
        self.assertFalse(by_symbol(rows)["AAA"]["session_uncertain"])
        self.assertTrue(by_symbol(rows)["BBB"]["session_uncertain"])
        self.assertTrue(by_symbol(rows)["CCC"]["session_uncertain"])
        self.assertEqual(by_symbol(rows)["CCC"]["session"], "unknown")
        # The date travels per day, because the two halves come from two days.
        self.assertEqual(by_symbol(rows)["DDD"]["earnings_date"], "2026-11-11")
        self.assertEqual(by_symbol(rows)["AAA"]["earnings_date"], "2026-11-10")

    def test_only_the_pre_market_rows_of_the_reaction_day_qualify(self):
        day = [
            calendar_row("PRE1", session="before_market", name="Pre One", market_cap=1234),
            calendar_row("AFTER1", session="after_market"),
            calendar_row("PRE2", session="before_market"),
            calendar_row("AFTER2", session="after_market"),
        ]

        rows = reaction_day_rows([], day, previous_date=PREVIOUS, day_date=REACTION)

        self.assertEqual([row["symbol"] for row in rows], ["PRE1", "PRE2"])
        for row in rows:
            self.assertFalse(row["session_uncertain"])
            self.assertEqual(row["session"], "before_market")
            self.assertEqual(row["earnings_date"], "2026-11-11")
        # Every field the caller needs survives; nothing is invented.
        self.assertEqual(rows[0]["name"], "Pre One")
        self.assertEqual(rows[0]["market_cap"], 1234)
        self.assertIsNone(rows[1]["market_cap"])

    def test_a_symbol_reported_twice_appears_once_with_the_known_session(self):
        previous = [calendar_row("DUAL", session="unknown"), calendar_row("ONLY", session="unknown")]
        day = [calendar_row("DUAL", session="before_market")]

        rows = reaction_day_rows(previous, day, previous_date=PREVIOUS, day_date=REACTION)

        self.assertEqual([row["symbol"] for row in rows], ["DUAL", "ONLY"])
        # The reaction day wins: it is the row whose session is KNOWN.
        self.assertEqual(by_symbol(rows)["DUAL"]["session"], "before_market")
        self.assertFalse(by_symbol(rows)["DUAL"]["session_uncertain"])
        self.assertEqual(by_symbol(rows)["DUAL"]["earnings_date"], "2026-11-11")

    def test_a_row_without_a_symbol_is_dropped(self):
        rows = reaction_day_rows(
            [{"symbol": "", "session": "unknown"}, {"name": "no symbol", "session": "unknown"}],
            [calendar_row("AAA", session="before_market")],
            previous_date=PREVIOUS,
            day_date=REACTION,
        )

        self.assertEqual([row["symbol"] for row in rows], ["AAA"])


class BlockingHistoryAdapter:
    """An adapter that answers only when the test releases it.

    Used to prove the RUN's deadline: a fetch that is still inside
    ``fetch_daily`` when the deadline expires must be reported, not awaited.
    """

    def __init__(self, release, bars=None):
        self.release = release
        self.bars = bars or {}
        self.calls = []

    def fetch_daily(self, symbol, fromdate, limit=DEFAULT_LIMIT):
        self.calls.append(symbol)
        self.release.wait(10)
        return list(self.bars.get(symbol, []))


class FetchGapsTest(unittest.TestCase):
    """The parallel fetch: what it measures, what it names, what it never does."""

    def test_a_deadline_reports_the_unmeasured_symbols(self):
        release = threading.Event()
        adapter = BlockingHistoryAdapter(release, bars={"AAA": bars_for(REACTION)})

        try:
            result = fetch_gaps(["AAA", "BBB"], REACTION, adapter=adapter,
                                deadline_seconds=0.1)
        finally:
            release.set()

        # The run is bounded: nothing measured, everything NAMED - no exception,
        # no silently missing symbol, and no row invented for a title that was
        # never answered.
        self.assertEqual(result.rows, [])
        self.assertEqual(result.failures, {"AAA": "deadline_exceeded",
                                           "BBB": "deadline_exceeded"})
        # Both titles were still ASKED: the deadline ends the waiting, not the
        # measurement of the day.
        ende = time.monotonic() + 2.0
        while len(adapter.calls) < 2 and time.monotonic() < ende:
            time.sleep(0.01)
        self.assertEqual(sorted(adapter.calls), ["AAA", "BBB"])

    def test_a_failing_symbol_does_not_cost_the_others(self):
        adapter = StubHistoryAdapter(
            bars={"AAA": bars_for(REACTION), "CCC": bars_for(REACTION, close=9.0)},
            errors={"X": GapSourceError("http_error", "the source answered HTTP 429", 429)},
        )

        result = fetch_gaps(["AAA", "X", "CCC"], REACTION, adapter=adapter)

        self.assertEqual(result.failures, {"X": "http_error"})
        self.assertEqual([row["symbol"] for row in result.rows], ["AAA", "CCC"])
        # Input order, no sort: the selection sorts, this does not.
        self.assertEqual([row["gap_pct"] for row in result.rows], [10.0, -10.0])
        self.assertEqual(result.rows[0]["close"], 11.0)
        self.assertEqual(result.rows[0]["volume"], 1_000_000.0)
        # Every symbol was asked with the documented window and limit.
        self.assertEqual({call["limit"] for call in adapter.calls}, {10})
        self.assertEqual({call["fromdate"] for call in adapter.calls}, {PREVIOUS_WINDOW})

    def test_a_missing_reaction_bar_is_named(self):
        # Bars exist, but only from BEFORE the reaction session.
        stale = [GapBar(session_date=PREVIOUS, open=10.0, close=10.0, volume=1_000_000.0)]
        adapter = StubHistoryAdapter(bars={"LATE": stale, "OK": bars_for(REACTION)})

        result = fetch_gaps(["LATE", "OK"], REACTION, adapter=adapter)

        self.assertEqual(result.failures, {"LATE": "no_reaction_bar"})
        self.assertEqual([row["symbol"] for row in result.rows], ["OK"])

    def test_an_unexpected_adapter_failure_is_named_not_raised(self):
        adapter = StubHistoryAdapter(errors={"BOOM": RuntimeError("adapter broke")})

        result = fetch_gaps(["BOOM"], REACTION, adapter=adapter)

        self.assertEqual(result.failures, {"BOOM": "unexpected_error"})
        self.assertEqual(result.rows, [])

    def test_no_symbols_is_no_work_and_no_adapter(self):
        transport = StubTransport(answer=body(row("11/11/2026", close="$17.82")))
        adapter = NasdaqHistoryAdapter(transport=transport)

        result = fetch_gaps([], REACTION, adapter=adapter)

        self.assertEqual(result.rows, [])
        self.assertEqual(result.failures, {})
        self.assertEqual(transport.calls, [])


class ScanGapsTest(unittest.TestCase):
    """The whole day: candidate set, fetch, selection and the numbers in between."""

    def test_the_counts_add_up_to_the_rows_that_were_checked(self):
        day = [
            calendar_row("BIG1", session="before_market"),
            calendar_row("BIG2", session="before_market"),
            calendar_row("BIG3", session="before_market"),
            calendar_row("SMALL1", session="before_market"),
            calendar_row("SMALL2", session="before_market"),
        ]
        adapter = StubHistoryAdapter(
            bars={
                # 3 x above 2 % with turnover over 5 M USD ...
                "BIG1": bars_for(REACTION, close=11.0),                  # +10 %, 11 M
                "BIG2": bars_for(REACTION, close=9.0),                   # -10 %,  9 M
                "BIG3": bars_for(REACTION, close=10.5, volume=2_000_000),  # +5 %, 21 M
                # ... and 2 x under the threshold.
                "SMALL1": bars_for(REACTION, close=10.1),
                "SMALL2": bars_for(REACTION, close=9.95),
            }
        )

        result = scan_gaps([], day, REACTION, previous_date=PREVIOUS, adapter=adapter)

        self.assertEqual(result.counts["candidate_rows"], 5)
        self.assertEqual(result.counts["gaps_computed"], 5)
        self.assertEqual(result.counts["over_threshold"], 3)
        self.assertEqual(result.counts["turnover_passed"], 3)
        self.assertEqual(result.counts["selected"], 3)
        self.assertEqual(result.counts["failures"], 0)
        self.assertEqual(len(result.candidates), 3)
        # The numbers are consistent with the rows they describe.
        self.assertEqual(
            result.counts["candidate_rows"],
            result.counts["gaps_computed"] + result.counts["failures"],
        )
        self.assertEqual(result.counts["gaps_computed"], len(result.gap_rows))
        self.assertEqual(result.counts["candidate_rows"], len(result.candidate_rows))
        self.assertEqual([c.symbol for c in result.candidates], ["BIG1", "BIG2", "BIG3"])
        self.assertEqual(result.failures, {})

    def test_a_missing_bar_and_a_broken_symbol_are_counted_as_failures(self):
        day = [
            calendar_row("OK", session="before_market"),
            calendar_row("LATE", session="before_market"),
            calendar_row("BROKEN", session="before_market"),
        ]
        adapter = StubHistoryAdapter(
            bars={"OK": bars_for(REACTION), "LATE": [GapBar(session_date=PREVIOUS, open=1.0, close=1.0, volume=1.0)]},
            errors={"BROKEN": GapSourceError("no_data", "no rows")},
        )

        result = scan_gaps([], day, REACTION, previous_date=PREVIOUS, adapter=adapter)

        self.assertEqual(result.counts["candidate_rows"], 3)
        self.assertEqual(result.counts["gaps_computed"], 1)
        self.assertEqual(result.counts["failures"], 2)
        self.assertEqual(result.failures, {"LATE": "no_reaction_bar", "BROKEN": "no_data"})
        # The one good title survives a day where two others failed.
        self.assertEqual([c.symbol for c in result.candidates], ["OK"])

    def test_the_selection_caps_at_thirty_after_the_turnover_hurdle(self):
        day = [calendar_row(f"S{index:02d}", session="before_market") for index in range(40)]
        adapter = StubHistoryAdapter(
            bars={
                f"S{index:02d}": bars_for(REACTION, close=50.0 + index, volume=1_000_000)
                for index in range(40)
            }
        )

        result = scan_gaps([], day, REACTION, previous_date=PREVIOUS, adapter=adapter)

        # All 40 clear the threshold AND the 5 M turnover hurdle (50 M each) ...
        self.assertEqual(result.counts["candidate_rows"], 40)
        self.assertEqual(result.counts["over_threshold"], 40)
        self.assertEqual(result.counts["turnover_passed"], 40)
        # ... and exactly 30 survive the cap, the biggest moves first.
        self.assertEqual(result.counts["selected"], 30)
        self.assertEqual(len(result.candidates), 30)
        self.assertEqual(result.candidates[0].symbol, "S39")
        self.assertNotIn("S00", [c.symbol for c in result.candidates])

    def test_the_turnover_hurdle_is_not_a_threshold_only_test(self):
        # 10 % on 0.4 M USD: over the threshold, nowhere near tradable.
        day = [calendar_row("THIN", session="before_market")]
        adapter = StubHistoryAdapter(
            bars={"THIN": bars_for(REACTION, close=11.0, previous_close=10.0, volume=40_000)}
        )

        result = scan_gaps([], day, REACTION, previous_date=PREVIOUS, adapter=adapter)

        self.assertEqual(result.counts["over_threshold"], 1)
        # A title that fails the turnover hurdle did NOT pass it.
        self.assertEqual(result.counts["turnover_passed"], 0)
        self.assertEqual(result.counts["selected"], 0)
        self.assertEqual(result.candidates, [])

    def test_the_previous_day_reaches_the_scan_as_an_uncertain_candidate(self):
        previous = [calendar_row("OLD", session="unknown")]
        day = [calendar_row("NEW", session="before_market")]
        adapter = StubHistoryAdapter(
            bars={"OLD": bars_for(REACTION), "NEW": bars_for(REACTION, close=9.0)}
        )

        result = scan_gaps(previous, day, REACTION, previous_date=PREVIOUS, adapter=adapter)

        self.assertEqual([row["symbol"] for row in result.candidate_rows], ["NEW", "OLD"])
        self.assertEqual(by_symbol(result.candidate_rows)["OLD"]["earnings_date"], "2026-11-10")
        self.assertTrue(by_symbol(result.candidate_rows)["OLD"]["session_uncertain"])
        self.assertEqual(sorted(c.symbol for c in result.candidates), ["NEW", "OLD"])


def by_symbol(rows):
    return {row["symbol"]: row for row in rows}


if __name__ == "__main__":
    unittest.main()
