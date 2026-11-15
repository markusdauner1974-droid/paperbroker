"""Offline tests for the funnel's alternatives as NAMED CHARACTERS.

The list used to show a ladder of ratios beside the pick. What it shows now is
three readings of the same expiry, each with a name that says what it is FOR:

* ``nah am Geld`` - the bought strike closest to the close,
* ``am Widerstand`` - the bought strike closest to the next level above the
  price (52-week high, MA20/50/200),
* ``engster Spread`` - the smallest WORSE leg spread.

The row itself is the HEBEL and stands in none of them. Every alternative
clears the row's three hurdles AND the row's reachability cap, is deduplicated
over its strike pair and carries the frequency of its OWN distance, measured on
the same bars as the row.

The chain below is hand-computed (close 101, expiry 2027-03-19)::

    long/short   debit   width   R/R     fate
    103 / 112    0.61     9     13.75    the row (highest ratio)
    103 / 109    0.51     6     10.76    engster Spread - worse leg 58.8 %
    106 / 112    0.60     6      9.00    (long 106, level tie - bigger move)
    100 / 112    1.93    12      5.22
    106 / 109    0.50     3      5.00    am Widerstand - long strike AT 106
    100 / 109    1.83     9      3.92
    100 / 106    1.65     6      2.64    nah am Geld - |100 - 101| = 1
     97 / 112    4.64    15      2.23
    103 / 106    0.41     3      -       debit < 0.50 - out
    106 /130     -        -      -       (not in the section)

The three characters land on three different strike pairs, so the display order
is exactly the order of ``VARIANT_LABELS``.
"""

import unittest
from datetime import date, timedelta
from unittest import mock

import arrow

from paperbroker import gap_funnel as gf
from paperbroker import reachability
from paperbroker.gap_scan import GapBar
from paperbroker.screener import BULL_CALL_DEBIT, VerticalPair
from paperbroker.server import create_app
from paperbroker.tests.test_gap_funnel import DAY, NOW, _Chains, _FakeCalendar, _History, _NextEarnings, calendar_row, call, candidate

EXPIRY = "2027-03-19"
CLOSE = 101.0
REACTION = date(2026, 11, 12)

#: The fields of one variant, in the order of the agreed contract.
VARIANT_KEYS = {"label", "long_strike", "short_strike", "net_debit", "max_loss",
                "max_profit", "rr", "chance", "required_move_pct", "bid_ask_pct",
                "hit_rate_pct", "windows_measured", "liquidity", "open_interest"}


def character_chain():
    """The six calls of the fixture above - one expiry, close 101."""
    return [
        call("AAA", EXPIRY, 97.0, 4.60, 4.66, delta=68.0),
        call("AAA", EXPIRY, 100.0, 1.90, 1.95, delta=55.0),
        call("AAA", EXPIRY, 103.0, 0.60, 0.63, delta=40.0),
        # A wide two-sided market on 106: as a short leg it is crossed at 0.30,
        # as a bought leg it costs 0.62 - which is what makes 106/109 a pair at
        # all (0.50 debit) and keeps the level at 106 inside the chain.
        call("AAA", EXPIRY, 106.0, 0.30, 0.62, delta=28.0),
        call("AAA", EXPIRY, 109.0, 0.12, 0.22, delta=24.0),
        call("AAA", EXPIRY, 112.0, 0.02, 0.06, delta=5.0),
    ]


def row_pick(chain=None):
    """``(pair, criteria)`` of the funnel's own pick - the HEBEL of the fixture."""
    pair, criteria, _note, _coverage = gf.best_spread(
        chain if chain is not None else character_chain(), BULL_CALL_DEBIT, now_fn=NOW)
    return pair, criteria


def bars_from(closes_oldest_first):
    """Daily bars of a close series, NEWEST first as ``fetch_daily`` answers."""
    total = len(closes_oldest_first)
    return [GapBar(session_date=REACTION - timedelta(days=index),
                   open=close, close=close, volume=2_000_000.0)
            for index, close in enumerate(reversed(closes_oldest_first))] if total else []


def level_bars(high=106.0):
    """257 sessions around 99 with ONE spike, newest close 101.

    The spike sits at index 40: inside the 252-session high window, outside the
    200-session average. So the only level strictly above 101 is the high 106 -
    the three averages (99.1 / 99.04 / 99.01) stay below the price.
    """
    return bars_from([99.0] * 40 + [high] + [99.0] * 215 + [101.0])


def rising_bars(count=31, end=CLOSE, step=0.01):
    """``count`` sessions that each close 1 % above the one before, ending at ``end``."""
    closes = [end / ((1.0 + step) ** index) for index in range(count - 1, -1, -1)]
    return bars_from(closes)


def labels(variants):
    return [variant["label"] for variant in variants]


def strikes(variants):
    return [(variant["long_strike"], variant["short_strike"]) for variant in variants]


def by_label(variants, label):
    return next(variant for variant in variants if variant["label"] == label)


class NearMoneyCharacter(unittest.TestCase):
    """1.1 - the bought strike closest to the close."""

    def test_the_bought_strike_closest_to_the_close_wins(self):
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        nah = by_label(variants, gf.VARIANT_NEAR_MONEY)
        # |100 - 101| = 1 against |103 - 101| = 2 and |106 - 101| = 5.
        self.assertEqual((nah["long_strike"], nah["short_strike"]), (100.0, 106.0))

    def test_the_closest_strike_wins_even_against_the_better_ratio(self):
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        nah = by_label(variants, gf.VARIANT_NEAR_MONEY)
        # 100/109 carries 3.92 and 100/112 5.22 - the character is the NEAREST
        # strike, not the best ratio, so both stay behind 100/106 (2.64).
        self.assertEqual(nah["short_strike"], 106.0)
        self.assertLess(nah["rr"], 5.22)

    def test_a_tie_on_the_strike_goes_to_the_smaller_move(self):
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        nah = by_label(variants, gf.VARIANT_NEAR_MONEY)
        # All three long-100 pairs touch the close equally close: +0.64 %,
        # +0.82 % and +0.92 % - the shortest distance wins.
        self.assertEqual(nah["required_move_pct"], 0.64)


class ResistanceCharacter(unittest.TestCase):
    """1.2 - the bought strike closest to the next level above the price."""

    def test_the_nearest_level_above_the_close_decides(self):
        pair, _criteria = row_pick()
        levels = gf.resistance_levels(level_bars(), CLOSE)
        self.assertEqual(levels, [106.0])
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        widerstand = by_label(variants, gf.VARIANT_AT_RESISTANCE)
        self.assertEqual(widerstand["long_strike"], 106.0)
        # Both long-106 pairs sit ON the level; the tie goes to the smaller
        # required move (+5.45 % against +5.54 %), not to the better ratio.
        self.assertEqual((widerstand["long_strike"], widerstand["short_strike"]),
                         (106.0, 109.0))
        self.assertEqual(widerstand["required_move_pct"], 5.45)

    def test_only_levels_strictly_above_the_close_count(self):
        # Newest close 101, everything before it below: no level above the
        # price, so no resistance level at all.
        at_high = bars_from([99.0] * 40 + [101.0] + [99.0] * 215 + [CLOSE])
        self.assertEqual(gf.resistance_levels(at_high, CLOSE), [])
        self.assertEqual(gf.resistance_levels(level_bars(high=CLOSE), CLOSE), [])

    def test_a_title_at_its_high_has_no_resistance_character(self):
        pair, _criteria = row_pick()
        at_high = bars_from([99.0] * 40 + [101.0] + [99.0] * 215 + [CLOSE])
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=at_high, dte=127, now_fn=NOW)
        # The character is omitted - and NOT replaced by the second-best idea.
        self.assertNotIn(gf.VARIANT_AT_RESISTANCE, labels(variants))
        self.assertEqual(labels(variants),
                         [gf.VARIANT_NEAR_MONEY, gf.VARIANT_TIGHTEST_SPREAD])
        self.assertEqual(len(variants), 2)

    def test_a_short_history_has_no_levels_to_offer(self):
        # Two bars cannot carry a 20/50/200 average, and their high is the
        # close itself: nothing above, nothing invented.
        pair, _criteria = row_pick()
        self.assertEqual(gf.resistance_levels(bars_from([100.0, 101.0]), CLOSE), [])
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=bars_from([100.0, 101.0]),
                                        dte=127, now_fn=NOW)
        self.assertNotIn(gf.VARIANT_AT_RESISTANCE, labels(variants))


class TightestSpreadCharacter(unittest.TestCase):
    """1.3 - the smallest WORSE leg spread."""

    def test_the_smallest_worse_leg_wins(self):
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        eng = by_label(variants, gf.VARIANT_TIGHTEST_SPREAD)
        # 103/109: long (0.63 - 0.60) / 0.615 = 4.88 %, short
        # (0.22 - 0.12) / 0.17 = 58.82 % - the worse leg is the short one.
        self.assertEqual((eng["long_strike"], eng["short_strike"]), (103.0, 109.0))
        self.assertEqual(eng["bid_ask_pct"], 58.82)

    def test_the_wider_leg_decides_and_not_the_average(self):
        # 106/109 has the same 58.82 % short leg but a 69.57 % long leg
        # (0.62 / 0.30): averaging them would call it the tighter pair.
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        self.assertNotIn((106.0, 109.0),
                         strikes([by_label(variants, gf.VARIANT_TIGHTEST_SPREAD)]))
        self.assertEqual(by_label(variants, gf.VARIANT_AT_RESISTANCE)["bid_ask_pct"], 69.57)

    def test_a_leg_without_a_mid_cannot_win_the_character(self):
        # A one-sided quote has no mid, so it has no spread percentage either -
        # half a measurement is not a tight one.
        pair = VerticalPair(
            long=call("AAA", EXPIRY, 100.0, 0.50, 0.52, delta=55.0),
            short=call("AAA", EXPIRY, 103.0, 0.0, 0.10, delta=40.0),
            strategy=BULL_CALL_DEBIT,
        )
        self.assertIsNone(gf.bid_ask_pct(pair.short))
        self.assertIsNone(gf.worse_leg_spread(pair))
        self.assertAlmostEqual(gf.bid_ask_pct(pair.long), 3.9215, places=3)


class TheRowIsNeverOneOfItsOwnVariants(unittest.TestCase):
    def test_the_pick_is_the_hebel_and_not_in_the_list(self):
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        self.assertTrue(variants)
        self.assertNotIn((103.0, 112.0), strikes(variants))
        self.assertNotIn(gf.ROW_PICK_LABEL, labels(variants))

    def test_without_another_qualifying_pair_the_list_is_empty(self):
        # One call ladder with a single valid pair: the pick has no alternative.
        chain = [call("AAA", EXPIRY, 100.0, 3.90, 4.00, delta=55.0),
                 call("AAA", EXPIRY, 103.30, 3.00, 3.10, delta=30.0)]
        pair, _criteria = row_pick(chain)
        self.assertEqual(gf.variant_characters(chain, BULL_CALL_DEBIT, primary=pair,
                                               close=CLOSE, bars=level_bars(), dte=127,
                                               now_fn=NOW), [])

    def test_without_a_close_there_are_no_variants(self):
        pair, _criteria = row_pick()
        self.assertEqual(gf.variant_characters(character_chain(), BULL_CALL_DEBIT,
                                               primary=pair, close=None,
                                               bars=level_bars(), dte=127, now_fn=NOW), [])


class DedupeAndOrder(unittest.TestCase):
    def test_the_display_order_is_the_order_of_the_characters(self):
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                        close=CLOSE, bars=level_bars(), dte=127,
                                        now_fn=NOW)
        self.assertEqual(labels(variants), list(gf.VARIANT_LABELS))
        self.assertEqual(strikes(variants), [(100.0, 106.0), (106.0, 109.0), (103.0, 109.0)])

    def test_two_characters_on_one_pair_yield_one_variant(self):
        # 106/109 is both the pair nearest the level and the tightest (its long
        # leg is the tight one) - the FIRST character keeps it, the second is
        # dropped instead of being re-pointed.
        chain = [
            call("AAA", EXPIRY, 100.0, 1.90, 1.95, delta=55.0),
            call("AAA", EXPIRY, 106.0, 0.30, 0.62, delta=28.0),
            call("AAA", EXPIRY, 109.0, 0.12, 0.22, delta=24.0),
        ]
        pair, _criteria = row_pick(chain)
        variants = gf.variant_characters(chain, BULL_CALL_DEBIT, primary=pair, close=CLOSE,
                                         bars=level_bars(), dte=127, now_fn=NOW)
        pairs = strikes(variants)
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(labels(variants)[0], gf.VARIANT_NEAR_MONEY)


class TheHurdlesHold(unittest.TestCase):
    def test_every_variant_clears_the_row_hurdles_and_the_cap(self):
        chain = character_chain()
        pair, _criteria = row_pick(chain)
        quotes = {quote.asset.strike: quote for quote in chain}
        variants = gf.variant_characters(chain, BULL_CALL_DEBIT, primary=pair, close=CLOSE,
                                         bars=level_bars(), dte=127, now_fn=NOW)
        self.assertEqual(len(variants), 3)
        for variant in variants:
            self.assertEqual(set(variant), VARIANT_KEYS)
            self.assertGreaterEqual(variant["rr"], gf.DEFAULT_RR_MIN)
            self.assertGreaterEqual(variant["net_debit"], gf.DEFAULT_MIN_NET_DEBIT)
            self.assertGreaterEqual(abs(quotes[variant["long_strike"]].delta),
                                    gf.DEFAULT_MIN_LONG_DELTA)
            self.assertLessEqual(abs(variant["required_move_pct"]),
                                 gf.DEFAULT_MAX_REQUIRED_MOVE_PCT)

    def test_a_pair_beyond_the_move_cap_is_no_alternative(self):
        # long 125, short 135: a two-sided market and R/R 4.3, but its
        # break-even is 125.6 - 24 % away. The same cap that excludes a row
        # excludes its alternatives, so the far structure is never offered.
        chain = character_chain() + [
            call("AAA", EXPIRY, 125.0, 1.50, 2.00, delta=22.0),
            call("AAA", EXPIRY, 135.0, 0.10, 0.15, delta=6.0),
        ]
        pair, _criteria = row_pick(character_chain())
        variants = gf.variant_characters(chain, BULL_CALL_DEBIT, primary=pair, close=CLOSE,
                                         bars=level_bars(), dte=127, now_fn=NOW)
        self.assertTrue(variants)
        self.assertNotIn(125.0, [variant["long_strike"] for variant in variants])
        for variant in variants:
            self.assertLessEqual(abs(variant["required_move_pct"]),
                                 gf.DEFAULT_MAX_REQUIRED_MOVE_PCT)

    def test_a_strict_cap_leaves_only_structures_inside_it(self):
        pair, _criteria = row_pick()
        # Every candidate has to travel less than 1 %: the resistance pair
        # (+5.45 %) and the tightest (+2.49 %) are both outside.
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                         close=CLOSE, bars=level_bars(), dte=127,
                                         max_required_move_pct=1.0, now_fn=NOW)
        self.assertTrue(variants)
        for variant in variants:
            self.assertLessEqual(abs(variant["required_move_pct"]), 1.0)
        self.assertNotIn(5.45, [variant["required_move_pct"] for variant in variants])


class TheFrequencyComesFromTheRowsBars(unittest.TestCase):
    def test_the_hit_rate_is_measured_on_the_same_bars(self):
        pair, _criteria = row_pick()
        # Every session closes 1 % above the one before, and dte = 1 makes the
        # window ONE session: the 0.64 % distance was covered 30 times out of
        # 30, the 2.49 % and 5.45 % distances never.
        bars = rising_bars()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                         close=CLOSE, bars=bars, dte=1, now_fn=NOW)
        nah = by_label(variants, gf.VARIANT_NEAR_MONEY)
        self.assertEqual(nah["hit_rate_pct"], 100.0)
        self.assertEqual(nah["windows_measured"], 30)
        self.assertEqual(by_label(variants, gf.VARIANT_TIGHTEST_SPREAD)["hit_rate_pct"], 0.0)
        # This series ends ON its high, so there is no level and no third
        # character - every rate that IS shown still comes from the same bars.
        self.assertEqual(labels(variants), [gf.VARIANT_NEAR_MONEY,
                                            gf.VARIANT_TIGHTEST_SPREAD])

    def test_the_measure_is_the_one_the_row_uses(self):
        pair, _criteria = row_pick()
        bars = rising_bars()
        variant = by_label(
            gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                  close=CLOSE, bars=bars, dte=1, now_fn=NOW),
            gf.VARIANT_NEAR_MONEY)
        fields = reachability.measure(bars, variant["required_move_pct"], 1)
        self.assertEqual(variant["hit_rate_pct"], fields["hit_rate_pct"])
        self.assertEqual(variant["windows_measured"], fields["windows_measured"])

    def test_without_bars_both_numbers_stay_null(self):
        pair, _criteria = row_pick()
        variants = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                         close=CLOSE, bars=None, dte=127, now_fn=NOW)
        # Without bars the resistance level is unknown too, so two characters.
        for variant in variants:
            self.assertIsNone(variant["hit_rate_pct"])
            self.assertIsNone(variant["windows_measured"])


# --- the same characters through the run and the route ---------------------- #
def _calendar():
    return _FakeCalendar({
        DAY: [calendar_row("AAA", session="after_market", name="Alpha Corp")],
        "2026-11-11": [calendar_row("AAA", session="after_market", name="Alpha Corp")],
    })


def _history():
    """99 around 106, then the gap: 95 -> 101 on the reaction day."""
    return _History({"AAA": bars_from([99.0] * 40 + [106.0] + [99.0] * 214 + [95.0, 101.0])})


def _chains():
    return _Chains({"AAA": character_chain()})


def _dates():
    return _NextEarnings({"AAA": date(2027, 2, 1)})


def _client():
    return create_app(
        quote_adapter=_chains(),
        earnings_adapter=_calendar(),
        gap_adapter=_history(),
        next_earnings_adapter=_dates(),
        now_fn=lambda: arrow.get("2026-11-14T21:00:00Z"),
    ).test_client()


class TheRunCarriesTheCharacters(unittest.TestCase):
    def _row(self):
        antwort = _client().get("/api/v1/gap-spreads?day=" + DAY)
        self.assertEqual(antwort.status_code, 200)
        rows = antwort.get_json()["results"]
        self.assertEqual([row["symbol"] for row in rows], ["AAA"])
        return rows[0]

    def test_the_row_lists_three_named_alternatives(self):
        row = self._row()
        self.assertEqual(labels(row["variants"]), list(gf.VARIANT_LABELS))
        self.assertEqual((row["long_strike"], row["short_strike"]), (103.0, 112.0))
        self.assertNotIn((row["long_strike"], row["short_strike"]), strikes(row["variants"]))

    def test_the_variants_carry_the_row_numbers_and_a_measured_rate(self):
        row = self._row()
        for variant in row["variants"]:
            self.assertEqual(set(variant), VARIANT_KEYS)
            self.assertIn(variant["liquidity"], gf.LIQUIDITIES)
            self.assertEqual(len(variant["open_interest"]), 2)
            # The bars are the row's: a measured rate, not a null.
            self.assertIsNotNone(variant["hit_rate_pct"])
            self.assertGreater(variant["windows_measured"], 0)
            # ... and its chance is that rate times its own R/R.
            self.assertEqual(variant["chance"],
                             round(variant["hit_rate_pct"] / 100.0 * variant["rr"], 2))

    def test_the_row_keeps_its_documented_shape(self):
        row = self._row()
        self.assertIn("variants", row)
        self.assertIsInstance(row["variants"], list)


def _funnel(chain, *, close=CLOSE, bars=None, history=True):
    """One title (``AAA``) through the whole funnel, history source optional.

    ``close`` is set AFTER ``candidate`` (which multiplies it into a turnover):
    the row's close is what the alternatives are measured against, and a title
    without one must reach the funnel as ``None``.
    """
    kandidat = dict(candidate("AAA", close=CLOSE))
    kandidat["close"] = close
    return gf.run_funnel(
        [kandidat],
        fetch_next_earnings=lambda _symbol: date(2027, 2, 1),
        fetch_chain=lambda _symbol: list(chain),
        # A ``date``, not the route's string: the history window is derived from
        # it, and a string is no day (``reachability.fromdate_for``).
        day=date(2026, 11, 12), now_fn=NOW,
        fetch_daily=None if not history else _History(
            {"AAA": level_bars() if bars is None else bars}).fetch_daily,
    )


class OneEnumerationPerRow(unittest.TestCase):
    """A row that names alternatives still searches its section exactly ONCE.

    ``best_spread`` and ``qualifying_pairs`` both start at ``spread_candidates``,
    and that function builds EVERY strike pair of the section (up to
    ``MAX_PAIRS_PER_SECTION`` = 50 000). A row that ran it once to pick and once
    more to name its alternatives did that work twice, in the deadline-bounded
    worker - so the count is a property worth a test, not an implementation
    detail.
    """

    def test_one_candidate_with_variants_enumerates_its_pairs_once(self):
        calls = []
        original = gf.spread_candidates

        def counted(*args, **kwargs):
            calls.append(args[:2])
            return original(*args, **kwargs)

        with mock.patch.object(gf, "spread_candidates", counted):
            run = _funnel(character_chain())

        self.assertEqual(len(run.results), 1)
        row = run.results[0]
        # The candidate HAS alternatives, so both needs really occurred.
        self.assertEqual(labels(row["variants"]), list(gf.VARIANT_LABELS))
        self.assertEqual(len(calls), 1)

    def test_the_alternatives_are_the_ones_of_the_same_list(self):
        """Reusing the list cannot change WHAT is offered - only how often."""
        shared = _funnel(character_chain()).results[0]["variants"]
        pair, criteria, _note, _coverage = gf.best_spread(character_chain(),
                                                          BULL_CALL_DEBIT, now_fn=NOW)
        kept, criteria, _sample_drop, _coverage = gf._enumerate(
            character_chain(), BULL_CALL_DEBIT, now_fn=NOW)
        handed = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                       close=CLOSE, bars=level_bars(), dte=127,
                                       now_fn=NOW, kept=kept, criteria=criteria)
        searched = gf.variant_characters(character_chain(), BULL_CALL_DEBIT, primary=pair,
                                         close=CLOSE, bars=level_bars(), dte=127,
                                         now_fn=NOW, kept=None, criteria=None)
        self.assertEqual(handed, searched)
        self.assertEqual(labels(handed), labels(shared))


class NoLadderVersusEmptyLadder(unittest.TestCase):
    """``variants: null`` is "never named"; ``[]`` is "named, nothing fits".

    Both show no counter, so the difference is only visible in the contract -
    which is exactly why it has to be there: a row may not claim a check that
    never ran.
    """

    def test_without_a_history_source_the_row_says_null(self):
        run = _funnel(character_chain(), history=False)
        self.assertEqual(len(run.results), 1)
        self.assertIsNone(run.results[0]["variants"])

    def test_without_a_close_no_ladder_can_be_named(self):
        run = _funnel(character_chain(), close=None)
        self.assertEqual(len(run.results), 1)
        self.assertIsNone(run.results[0]["variants"])

    def test_a_built_ladder_without_another_qualifying_pair_is_empty(self):
        # One valid pair of two strikes: the pick has no alternative, but the
        # ladder WAS built from the row's own close and bars.
        chain = [call("AAA", EXPIRY, 100.0, 3.90, 4.00, delta=55.0),
                 call("AAA", EXPIRY, 103.30, 3.00, 3.10, delta=30.0)]
        run = _funnel(chain)
        self.assertEqual(len(run.results), 1)
        self.assertEqual(run.results[0]["variants"], [])

    def test_a_row_without_a_source_says_null_not_empty(self):
        # Even a row that arrives carrying ``[]`` must not keep it: without a
        # source no ladder was named, and "[]" would read as "checked".
        row = {"symbol": "AAA", "required_move_pct": 1.0, "dte": 127, "variants": []}
        gf.measure_reachability([row], fetch_daily=None, day=date(2026, 11, 12))
        self.assertIsNone(row["variants"])


if __name__ == "__main__":
    unittest.main()
