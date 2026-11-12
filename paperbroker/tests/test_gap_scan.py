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
import unittest
from dataclasses import FrozenInstanceError, dataclass
from datetime import date

from paperbroker.gap_scan import (
    STRATEGY_BEAR_PUT_DEBIT,
    STRATEGY_BULL_CALL_DEBIT,
    GapBar,
    GapSourceError,
    NasdaqHistoryAdapter,
    classify_gap,
    compute_gap,
    parse_nasdaq_history,
    select_candidates,
    strategy_for_direction,
)

REACTION = date(2026, 11, 11)


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


if __name__ == "__main__":
    unittest.main()
