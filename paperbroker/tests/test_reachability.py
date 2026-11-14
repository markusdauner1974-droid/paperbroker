"""Offline tests for the reachability numbers (``paperbroker/reachability.py``).

The funnel excludes a spread whose break-even is too far away and shows how
often the title covered that distance before. Both numbers are measurements,
and this file pins them against values computed BY HAND - not against a second
call of the same function:

* ``required_move_pct`` = ``(breakeven / close - 1) * 100``, with the sign as
  the direction (a call needs a rise, a put a fall),
* ``volatility_pct`` = ``pstdev(log returns) * sqrt(252) * 100`` and
  ``daily_move_pct`` = ``mean(|log return|) * 100`` on a series whose two
  returns are ``+ln(1.1)`` and ``-ln(1.1)`` - a population deviation of exactly
  ``ln(1.1)``,
* ``hit_rate_pct`` on a series where the windows are countable by hand: ten
  windows, exactly three of them reached the move, so 30 %,
* the direction matters: a required FALL is only hit by falling windows,
* too short a history is ``too_short`` with ``None`` numbers and no exception,
* a failing source is ``source_failed`` with ``None`` numbers,
* the bars arrive newest first and are read oldest first.

No test touches a network and no test needs a route: the module is pure
arithmetic over ``GapBar`` objects.
"""

import math
import unittest
from datetime import date, datetime, timedelta

from paperbroker import reachability
from paperbroker.gap_scan import GapBar

#: ln(1.1): the one return of the volatility fixture, twice with both signs.
LN_11 = math.log(1.1)


def bar(close, day=date(2026, 11, 11)):
    """One session with a close - open and volume are not read here."""
    return GapBar(session_date=day, open=close, close=close, volume=1_000_000.0)


def series(moves, start=100.0):
    """The closes of ``moves`` applied one after the other (chronological)."""
    closes = [start]
    for move in moves:
        closes.append(closes[-1] * (1.0 + move))
    return closes


def bars_newest_first(closes):
    """``GapBar`` list as the source delivers it: the LAST close first."""
    day = date(2026, 11, 11)
    out = []
    for index, close in enumerate(reversed(closes)):
        out.append(bar(close, date.fromordinal(day.toordinal() - index)))
    return out


class RequiredMoveTests(unittest.TestCase):
    def test_a_call_has_to_rise_to_its_break_even(self):
        # break-even 110 against a close of 100: exactly +10 %.
        self.assertAlmostEqual(reachability.required_move_pct(110.0, 100.0), 10.0, places=9)

    def test_a_put_has_to_fall_to_its_break_even(self):
        self.assertAlmostEqual(reachability.required_move_pct(90.0, 100.0), -10.0, places=9)

    def test_the_measured_land_row_of_the_requirement(self):
        # Measured on 2026-11-11: LAND break-even 8.10 on a close of 7.36.
        # (8.10 / 7.36 - 1) * 100 = 10.0543... % - a rise of about a tenth.
        self.assertAlmostEqual(reachability.required_move_pct(8.10, 7.36), 10.0543,
                               places=4)

    def test_the_measured_crwv_row_of_the_requirement(self):
        # CRWV: 98.00 break-even against 60.77 - the 61 % that made the R/R of
        # 1:34 useless as a filter.
        self.assertAlmostEqual(reachability.required_move_pct(98.00, 60.77), 61.2638,
                               places=4)

    def test_unusable_numbers_have_no_distance(self):
        self.assertIsNone(reachability.required_move_pct(None, 100.0))
        self.assertIsNone(reachability.required_move_pct(110.0, None))
        self.assertIsNone(reachability.required_move_pct(110.0, 0.0))
        self.assertIsNone(reachability.required_move_pct(110.0, -5.0))
        self.assertIsNone(reachability.required_move_pct(float("nan"), 100.0))

    def test_the_sign_names_the_direction(self):
        self.assertEqual(reachability.required_direction(10.05), "up")
        self.assertEqual(reachability.required_direction(-8.18), "down")
        self.assertIsNone(reachability.required_direction(0.0))
        self.assertIsNone(reachability.required_direction(None))


class VolatilityTests(unittest.TestCase):
    def test_two_opposite_returns_have_the_deviation_of_one(self):
        # 100 -> 110 -> 100: +ln(1.1) and -ln(1.1). The population deviation of
        # two symmetric values is the value itself, so the annualised number is
        # ln(1.1) * sqrt(252) * 100 = 151.29... %.
        closes = [100.0, 110.0, 100.0]
        self.assertAlmostEqual(reachability.volatility_pct(closes),
                               LN_11 * math.sqrt(252) * 100, places=9)
        self.assertAlmostEqual(reachability.daily_move_pct(closes), LN_11 * 100, places=9)

    def test_a_flat_series_has_no_spread_and_no_daily_move(self):
        self.assertAlmostEqual(reachability.volatility_pct([100.0] * 30), 0.0, places=12)
        self.assertAlmostEqual(reachability.daily_move_pct([100.0] * 30), 0.0, places=12)

    def test_a_single_close_has_neither(self):
        self.assertIsNone(reachability.volatility_pct([100.0]))
        self.assertIsNone(reachability.daily_move_pct([100.0]))

    def test_the_daily_move_ignores_the_sign(self):
        # +5 % and -5 % travel the same distance in LOG terms: |ln 1.05| and
        # |ln 0.95| differ in the seventh decimal, the mean is their average.
        closes = [100.0, 105.0, 99.75]
        self.assertAlmostEqual(reachability.daily_move_pct(closes),
                               (abs(math.log(1.05)) + abs(math.log(0.95))) / 2 * 100,
                               places=9)


class HitRateTests(unittest.TestCase):
    #: Ten windows of one session each: exactly three of them rise 10 %.
    MOVES = [0.10, -0.05, 0.10, -0.05, -0.05, 0.10, -0.05, -0.05, -0.05, -0.05]

    def test_three_of_ten_windows_are_thirty_percent(self):
        closes = series(self.MOVES)
        rate, windows = reachability.count_hits(closes, 10.0, 1)
        self.assertEqual(windows, 10)
        self.assertAlmostEqual(rate, 30.0, places=9)

    def test_a_falling_requirement_is_only_hit_by_falling_windows(self):
        # The same series for a spread that needs a FALL of 4 %: every -5 %
        # window hits, the three +10 % ones do not - seven windows.
        closes = series(self.MOVES)
        rate, windows = reachability.count_hits(closes, -4.0, 1)
        self.assertEqual(windows, 10)
        self.assertAlmostEqual(rate, 70.0, places=9)

    def test_the_bound_is_inclusive(self):
        # Exactly the required move IS a hit: "mindestens erreicht".
        closes = series([0.10, 0.10, 0.10])
        rate, windows = reachability.count_hits(closes, 10.0, 1)
        self.assertEqual(windows, 3)
        self.assertAlmostEqual(rate, 100.0, places=9)

    def test_the_window_length_is_the_number_of_sessions(self):
        # Five sessions apart on a series of 11 closes: 6 windows.
        closes = series([0.01] * 10)
        _rate, windows = reachability.count_hits(closes, 1.0, 5)
        self.assertEqual(windows, 6)

    def test_a_zero_requirement_hits_nothing_and_counts_the_windows(self):
        # No direction, nothing to hit - but the windows exist, and 0 % would
        # read as "never happened".
        rate, windows = reachability.count_hits(series(self.MOVES), 0.0, 1)
        self.assertIsNone(rate)
        self.assertEqual(windows, 10)

    def test_a_window_longer_than_the_series_has_no_answer(self):
        rate, windows = reachability.count_hits(series(self.MOVES), 10.0, 99)
        self.assertIsNone(rate)
        self.assertEqual(windows, 0)


class MeasureTests(unittest.TestCase):
    def test_the_bars_are_read_oldest_first(self):
        closes = series([0.10, -0.05, 0.10, -0.05, 0.10])
        bars = bars_newest_first(closes)
        self.assertEqual(reachability.closes_oldest_first(bars), closes)

    def test_a_measured_series_carries_all_four_numbers(self):
        bars = bars_newest_first(series([0.10, -0.05] * 40))   # 81 closes
        fields = reachability.measure(bars, 5.0, 20)
        self.assertEqual(fields["history_state"], "ok")
        self.assertIsNotNone(fields["volatility_pct"])
        self.assertIsNotNone(fields["daily_move_pct"])
        self.assertIsNotNone(fields["hit_rate_pct"])
        self.assertEqual(fields["windows_measured"], 81 - 20)

    def test_too_short_a_history_is_named_and_guesses_nothing(self):
        bars = bars_newest_first(series([0.01] * 40))          # 41 closes
        fields = reachability.measure(bars, 5.0, 20)
        # 41 < 20 + 30: the windows would be a statement about two months.
        self.assertEqual(fields["history_state"], "too_short")
        self.assertIsNone(fields["hit_rate_pct"])
        self.assertIsNone(fields["volatility_pct"])
        self.assertIsNone(fields["daily_move_pct"])
        self.assertEqual(fields["windows_measured"], 0)

    def test_a_failing_source_is_a_state_not_an_exception(self):
        fields = reachability.measure([], 5.0, 20, history_state="source_failed")
        self.assertEqual(fields["history_state"], "source_failed")
        self.assertIsNone(fields["hit_rate_pct"])
        self.assertEqual(fields["windows_measured"], 0)

    def test_no_bars_at_all_is_too_short(self):
        fields = reachability.measure([], 5.0, 20)
        self.assertEqual(fields["history_state"], "too_short")
        self.assertIsNone(fields["hit_rate_pct"])

    def test_a_missing_dte_cannot_be_measured(self):
        fields = reachability.measure(bars_newest_first(series([0.01] * 400)), 5.0, None)
        self.assertEqual(fields["history_state"], "too_short")
        self.assertIsNone(fields["hit_rate_pct"])

    def test_the_numbers_carry_two_decimals(self):
        fields = reachability.measure(bars_newest_first(series([0.03, -0.02] * 60)),
                                      5.0, 20)
        for name in ("volatility_pct", "daily_move_pct", "hit_rate_pct"):
            wert = fields[name]
            self.assertIsNotNone(wert)
            self.assertEqual(round(wert, 2), wert)


class LookbackTests(unittest.TestCase):
    def test_the_window_opens_wide_enough_for_two_years(self):
        start = reachability.fromdate_for(date(2026, 11, 11))
        self.assertEqual(start, date(2026, 11, 11)
                         - timedelta(days=reachability.HISTORY_LOOKBACK_DAYS))
        # 520 sessions span about 730 calendar days - the window must be wider.
        self.assertGreater((date(2026, 11, 11) - start).days, 730)

    def test_a_datetime_is_reduced_to_its_day(self):
        start = reachability.fromdate_for(datetime(2026, 11, 11, 21, 0))
        self.assertEqual(start, date(2024, 9, 2))

    def test_a_missing_day_has_no_window(self):
        self.assertIsNone(reachability.fromdate_for(None))
        self.assertIsNone(reachability.fromdate_for("2026-11-11"))


class HistoryStateTests(unittest.TestCase):
    def test_the_three_states_are_the_documented_ones(self):
        self.assertEqual(set(reachability.HISTORY_STATES),
                         {"ok", "too_short", "source_failed"})

    def test_an_unmeasured_row_carries_no_numbers(self):
        fields = reachability.unmeasured()
        self.assertEqual(fields["history_state"], "source_failed")
        self.assertEqual(fields["windows_measured"], 0)
        self.assertIsNone(fields["hit_rate_pct"])
        self.assertIsNone(fields["volatility_pct"])
        self.assertIsNone(fields["daily_move_pct"])

    def test_the_bars_asked_for_are_two_years(self):
        self.assertEqual(reachability.HISTORY_BARS, 520)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
