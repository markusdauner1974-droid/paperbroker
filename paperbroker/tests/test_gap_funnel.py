"""Offline tests for the gap -> debit-spread funnel (GET /api/v1/gap-spreads).

No test touches the network. The chain comes from ``OptionQuote`` objects built
from real OCC symbols, the report dates from a dict, and the endpoint runs
against fakes for all four sources (calendar, daily closes, chain, Finnhub).
The screener's own arithmetic is not re-tested here - what is pinned is the
FUNNEL: which expiry, which pair, which label, which reason.

Pinned, in the order of the requirement:

* an unsorted Finnhub answer yields the EARLIEST date at or after today
  (``test_next_earnings.py`` owns the source; here the date arrives as a value),
* the expiry is the first LISTED series strictly after the report - with the
  measured fixture 2026-11-20 / 2026-12-18 / 2027-03-19 and a report on
  2027-02-01 that is 2027-03-19, and a series ON the report day does not count,
* the pair is the best reward/risk of the whole expiry: 1:1.8 loses against
  1:2.3, a long-leg delta of 15 is out, a net debit of 0.40 USD is out,
* open interest and bid-ask decide ``thin`` / ``full``, and 100 is the candidate
  floor while 500 + a 10 % spread is the ``full`` floor,
* the counters chain the stages and every excluded title names its reason,
* the endpoint answers the agreed JSON contract, sorted by |gap_pct| descending,
* a title without a report date costs NO chain request,
* the funnel's own notes (the pair search's ceiling) reach the HTTP answer,
  after the general ones and each sentence only once.
"""

import json
import math
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import arrow

from paperbroker import gap_funnel as gap_funnel_mod
from paperbroker import screener as screener_mod
from paperbroker.assets import asset_factory
from paperbroker.gap_funnel import (
    DEFAULT_MIN_OI,
    DEFAULT_RR_MIN,
    LIQUIDITY_FULL,
    LIQUIDITY_THIN,
    REASON_NO_EXPIRY,
    REASON_NO_NEXT_EARNINGS,
    REASON_NO_SPREAD,
    REASON_NOT_OPTIONABLE,
    best_spread,
    bid_ask_pct,
    criteria_for,
    expirations_in,
    liquidity_of,
    pick_expiration,
    run_funnel,
    slice_chain,
)
from paperbroker.gap_scan import GapBar
from paperbroker.next_earnings import CODE_NO_API_KEY, NextEarningsError
from paperbroker.quotes import OptionQuote
from paperbroker.screener import BULL_CALL_DEBIT, MAX_PAIRS_PER_SECTION, VerticalPair
from paperbroker.server import create_app

NOW = lambda: arrow.get("2026-11-12T21:00:00Z")  # noqa: E731
CLOCK = lambda: arrow.get("2026-11-14T21:00:00Z")  # noqa: E731
DAY = "2026-11-12"
PREVIOUS = "2026-11-11"
REACTION = date(2026, 11, 12)
BEFORE = date(2026, 11, 11)

#: The measured expiry ladder of the requirement.
LADDER = ("2026-11-20", "2026-12-18", "2027-03-19")


# --- fixtures -------------------------------------------------------------- #
def occ(symbol, expiry, option_type, strike):
    """A real OCC symbol for one contract (the asset factory parses it back)."""
    year, month, day = str(expiry).split("-")
    kind = "C" if option_type == "call" else "P"
    return f"{symbol}{year[2:]}{month}{day}{kind}{int(round(strike * 1000)):08d}"


def leg(symbol, expiry, option_type, strike, bid, ask, oi=1000, delta=50.0,
        iv=30.0, volume=100, bid_size=50, ask_size=50):
    """One chain row as the CBOE adapter delivers it (greeks from the feed)."""
    return OptionQuote(
        quote_date="2026-11-12 20:00:00",
        asset=asset_factory(occ(symbol, expiry, option_type, strike)),
        price=(bid + ask) / 2,
        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
        delta=delta, iv=iv, gamma=1.0, vega=1.0, theta=-1.0, rho=0.1,
        underlying_price=100.0, open_interest=oi, volume=volume,
        greeks_source="adapter",
    )


def call(symbol, expiry, strike, bid, ask, **kwargs):
    return leg(symbol, expiry, "call", strike, bid, ask, **kwargs)


def put(symbol, expiry, strike, bid, ask, **kwargs):
    return leg(symbol, expiry, "put", strike, bid, ask, **kwargs)


def ladder_chain(symbol="AAA", expiry="2027-03-19"):
    """Four call pairs of ONE expiry - the R/R fixture of the requirement.

    ``net_debit = long.ask - short.bid`` (buy at ask, sell at bid), so with a
    bought 100 call at 4.00 all four pairs share the same risk and differ in
    what they can make:

    =============  ============  =========  ======  =====================
    short strike   net debit     width      R/R     fate
    =============  ============  =========  ======  =====================
    102.80         1.00          2.80       1.8     loses against 2.3
    103.30         1.00          3.30       2.3     the winner
    105.00         1.00          5.00       4.0     long delta 15 - out
    101.32         0.40          1.32       2.3     debit < 0.50 - out
    =============  ============  =========  ======  =====================
    """
    return [
        call(symbol, expiry, 100.0, 3.90, 4.00, delta=55.0, oi=1200),
        call(symbol, expiry, 102.80, 3.00, 3.10, delta=35.0, oi=900),
        call(symbol, expiry, 103.30, 3.00, 3.10, delta=30.0, oi=800),
        # The 4.0 pair exists to prove the DELTA hurdle is real: without it the
        # best ratio of the chain would be chosen.
        call(symbol, expiry, 105.00, 3.55, 3.65, delta=15.0, oi=700),
        # The 2.3-with-40-cents pair proves the DEBIT hurdle is real.
        call(symbol, expiry, 101.32, 3.60, 3.70, delta=45.0, oi=700),
    ]


def ratio_star_ladder(symbol="AAA", expiry="2027-03-19", center_delta=15.0):
    """Ten calls of ONE expiry: 45 combinations, 10 valid pairs, 3 above 2:1.

    The 100 call (0.45 / 0.50) is the only sane long leg: its six 0.02-spaced
    partners (0.10 / 2.50) and the three wide ones at 102.72 / 102.82 / 102.92
    all risk 0.40 USD, so the six near pairs stay at 1.50 .. 1.75 and the three
    wide ones reach 5.8 .. 6.3 - exactly three clear the 2:1 floor. The cheap
    ask on 102.72 adds ONE more valid pair (102.72 / 102.92, 0.18 debit, a ratio
    of 0.11) so the section counts ten; every partner-partner pair is incoherent
    (debit >= width) and stays out. ``center_delta`` decides which hurdle fires:
    15 sends the three 2:1 pairs to the DELTA branch, 55 lets them through to
    the debit floor - the pair count stays 10 either way.
    """
    chain = [call(symbol, expiry, 100.0, 0.45, 0.50, delta=center_delta, oi=1200)]
    for index in range(6):
        chain.append(call(symbol, expiry, 101.0 + index * 0.02, 0.10, 2.50,
                          delta=10.0, oi=900))
    chain.append(call(symbol, expiry, 102.72, 0.10, 0.28, delta=10.0, oi=900))
    chain.append(call(symbol, expiry, 102.82, 0.10, 2.50, delta=10.0, oi=900))
    chain.append(call(symbol, expiry, 102.92, 0.10, 2.50, delta=10.0, oi=900))
    return chain


# --- expiry ---------------------------------------------------------------- #
class ExpiryTests(unittest.TestCase):
    def test_first_listed_series_after_the_report(self):
        self.assertEqual(pick_expiration(LADDER, date(2027, 2, 1)), date(2027, 3, 19))

    def test_a_series_on_the_report_day_does_not_count(self):
        self.assertEqual(pick_expiration(LADDER, date(2026, 11, 20)), date(2026, 12, 18))

    def test_no_series_after_the_report_is_none(self):
        self.assertIsNone(pick_expiration(LADDER, date(2027, 3, 19)))
        self.assertIsNone(pick_expiration([], date(2027, 2, 1)))

    def test_iso_strings_are_accepted(self):
        self.assertEqual(pick_expiration(LADDER, "2027-02-01"), date(2027, 3, 19))

    def test_the_chain_is_read_and_sliced_once(self):
        chain = ladder_chain() + [call("AAA", "2026-12-18", 100.0, 5.0, 5.1)]
        self.assertEqual(expirations_in(chain), [date(2026, 12, 18), date(2027, 3, 19)])
        self.assertEqual(len(slice_chain(chain, "2027-03-19")), len(ladder_chain()))
        self.assertEqual(slice_chain(chain, "2030-01-01"), [])


class CriteriaTests(unittest.TestCase):
    def test_no_dte_window_no_age_filter_and_the_oi_floor(self):
        criteria = criteria_for(ladder_chain())
        self.assertIsNone(criteria.dte_min)
        self.assertIsNone(criteria.dte_max)
        self.assertIsNone(criteria.max_quote_age_min)
        self.assertEqual(criteria.min_oi, DEFAULT_MIN_OI)
        self.assertEqual(criteria.data_mode, "last_available")
        self.assertEqual(criteria.pair_mode, "within_width")
        # The WHOLE strike span: 100 .. 105 is "every combination" of this expiry.
        self.assertEqual(criteria.pair_max_width, 5.0)
        criteria.validate()

    def test_one_strike_has_no_pair_width(self):
        self.assertIsNone(criteria_for([call("AAA", "2027-03-19", 100.0, 3.9, 4.0)])
                          .pair_max_width)


# --- the pair -------------------------------------------------------------- #
class SpreadTests(unittest.TestCase):
    def test_the_best_ratio_wins_and_the_hurdles_hold(self):
        pair, criteria, note, coverage = best_spread(ladder_chain(), BULL_CALL_DEBIT,
                                                     now_fn=NOW)
        self.assertIsNone(note)
        self.assertIsNotNone(pair)
        self.assertEqual((pair.long.asset.strike, pair.short.asset.strike), (100.0, 103.30))
        self.assertEqual(pair.net_debit, 1.00)
        self.assertEqual(pair.net_return_pct(criteria), 230.0)
        self.assertAlmostEqual(pair.net_return_pct(criteria) / 100.0, 2.3, places=4)

    def test_a_one_to_one_point_eight_loses_against_one_to_two_point_three(self):
        section = [quote for quote in ladder_chain()
                   if quote.asset.strike in (100.0, 102.80, 103.30)]
        pair, criteria, _note, _coverage = best_spread(section, BULL_CALL_DEBIT, now_fn=NOW)
        self.assertEqual(pair.short.asset.strike, 103.30)
        self.assertGreater(pair.net_return_pct(criteria), 200.0)
        # The 1:1.8 pair really is in the section and really is weaker.
        weaker = VerticalPair(long=section[0], short=section[1], strategy=BULL_CALL_DEBIT)
        self.assertEqual(weaker.net_return_pct(criteria), 180.0)

    def test_a_long_delta_of_fifteen_is_out(self):
        section = [call("AAA", "2027-03-19", 100.0, 3.90, 4.00, delta=15.0),
                   call("AAA", "2027-03-19", 105.00, 3.55, 3.65, delta=10.0)]
        pair, _criteria, note, _coverage = best_spread(section, BULL_CALL_DEBIT, now_fn=NOW)
        self.assertIsNone(pair)
        self.assertIn("Delta", note)

    def test_a_debit_of_forty_cents_is_out(self):
        section = [call("AAA", "2027-03-19", 100.0, 3.90, 4.00, delta=55.0),
                   call("AAA", "2027-03-19", 101.32, 3.60, 3.70, delta=45.0)]
        pair, _criteria, note, _coverage = best_spread(section, BULL_CALL_DEBIT, now_fn=NOW)
        self.assertIsNone(pair)
        self.assertIn("Debit", note)

    def test_a_section_without_a_two_point_zero_ratio_is_named(self):
        section = [call("AAA", "2027-03-19", 100.0, 3.90, 4.00, delta=55.0),
                   call("AAA", "2027-03-19", 102.80, 3.00, 3.10, delta=35.0)]
        pair, _criteria, note, _coverage = best_spread(section, BULL_CALL_DEBIT, now_fn=NOW,
                                                       rr_min=DEFAULT_RR_MIN)
        self.assertIsNone(pair)
        self.assertIn("R/R", note)

    def test_a_single_strike_is_a_named_dead_end(self):
        pair, _criteria, note, _coverage = best_spread(
            [call("AAA", "2027-03-19", 100.0, 3.9, 4.0)], BULL_CALL_DEBIT, now_fn=NOW)
        self.assertIsNone(pair)
        self.assertIn("Strike-Paar", note)

    def test_the_exclusion_counts_valid_pairs_not_the_stage_count(self):
        # 45 combinations, 10 valid pairs, 3 of them above 2:1 - and every long
        # leg of those three is a 15-delta, so the DELTA hurdle is the one that
        # fired. "3 gültige Paare" would pass the stage size off as the pair
        # count: the label keeps the 10, the stage number stands in the sentence.
        pair, _criteria, note, _coverage = best_spread(
            ratio_star_ladder(), BULL_CALL_DEBIT, now_fn=NOW)
        self.assertIsNone(pair)
        self.assertIn("(3 von 10)", note)
        self.assertIn("(10 gültige Paare von 45 aufgezählten Paaren)", note)
        self.assertNotIn("3 gültige Paare", note)
        self.assertIn("Delta", note)

    def test_the_debit_branch_counts_valid_pairs_too(self):
        # Same section with a 55-delta long leg: the three 2:1 pairs clear the
        # delta as well and die on the 0.50 USD debit floor. The label is still
        # the pair count (10); the three are the stage, not the section.
        pair, _criteria, note, _coverage = best_spread(
            ratio_star_ladder(center_delta=55.0), BULL_CALL_DEBIT, now_fn=NOW)
        self.assertIsNone(pair)
        self.assertIn("(3 von 10)", note)
        self.assertIn("(10 gültige Paare von 45 aufgezählten Paaren)", note)
        self.assertNotIn("3 gültige Paare", note)
        self.assertIn("Netto-Debit", note)


# --- how much of the expiry was searched ----------------------------------- #
def wide_ladder(symbol="AAA", expiry="2027-03-19", strikes=10, start=100.0):
    """``strikes`` calls of ONE expiry: 100 costs 4.00, every other bids 3.00.

    ``net_debit`` is 1.00 for every pair and the width grows with the strike
    distance, so the ratio grows with the width and the LAST strike of the
    ladder is the best pair of the full search - which is exactly what a capped
    enumeration never gets to build.
    """
    chain = [call(symbol, expiry, start, 3.90, 4.00, delta=55.0, oi=1200)]
    for index in range(1, strikes):
        chain.append(call(symbol, expiry, start + index, 3.00, 3.10, delta=30.0, oi=900))
    return chain


def flat_ladder(symbol="AAA", expiry="2027-03-19", strikes=10, start=100.0, step=0.25):
    """``strikes`` calls of ONE expiry whose best pair stays below 2:1.

    Every leg bids 3.00 and asks 4.00, so each pair risks the same 1.00 USD. At
    25-cent spacing the widest pair of ten strikes is 2.25 wide - a ratio of
    1.25 - and no pair narrower than the debit is a pair at all, so nothing in
    this section can clear the funnel's 2:1 floor. What the section CAN do is be
    cut: ten strikes are 45 combinations.
    """
    return [call(symbol, expiry, start + index * step, 3.00, 4.00, delta=30.0, oi=900)
            for index in range(strikes)]


def star_ladder(symbol="AAA", expiry="2027-03-19"):
    """Ten calls of ONE expiry: 45 combinations, of which 5 are valid pairs.

    The 100 call (0.95 / 1.00) is the only sane long leg; five partners at
    101.00 .. 101.08 bid 0.10, so every (100, partner) pair risks 0.90 USD on a
    width of 1.00 .. 1.08 - a valid pair whose R/R stays below 2.0. The
    partners' own asks (2.50) and the four quotes at 3.00 / 3.00 are incoherent
    as pairs (debit >= width or <= 0), so the screener's validity rules keep
    exactly those five. The count is the point: 45 enumerated, 5 valid.
    """
    chain = [call(symbol, expiry, 100.0, 0.95, 1.00, delta=55.0, oi=1200)]
    for index in range(5):
        chain.append(call(symbol, expiry, 101.0 + index * 0.02, 0.10, 2.50,
                          delta=30.0, oi=900))
    for strike in (106.0, 107.0, 108.0, 109.0):
        chain.append(call(symbol, expiry, strike, 3.00, 3.00, delta=30.0, oi=900))
    return chain


def run_one(chain, symbol="AAA", **kwargs):
    """One title through the funnel: report date and chain answered from memory."""
    return run_funnel([candidate(symbol)],
                      fetch_next_earnings=lambda _symbol: date(2027, 2, 1),
                      fetch_chain=lambda _symbol: list(chain),
                      day=REACTION, now_fn=NOW, **kwargs)


class PairSearchCoverageTests(unittest.TestCase):
    """The 50 000-pair ceiling may cut the search - the answer must say so."""

    def test_the_row_says_how_many_pairs_were_possible(self):
        chain = ladder_chain()
        row = run_one(chain).results[0]
        strikes = {quote.asset.strike for quote in chain}
        moeglich = len(strikes) * (len(strikes) - 1) // 2
        self.assertEqual(len(strikes), 5)
        # 5 strikes -> 10 combinations, all of them enumerated and all of them
        # counted: the row's numbers are the chain's own arithmetic.
        self.assertEqual(moeglich, 10)
        self.assertEqual(row["pairs_possible"], moeglich)
        self.assertEqual(row["pairs_possible_measured"], moeglich)
        self.assertTrue(row["pairs_possible_exact"])
        self.assertEqual(row["pairs_enumerated"], moeglich)
        self.assertEqual(row["pairs_limit"], MAX_PAIRS_PER_SECTION)
        self.assertFalse(row["pairs_capped"])

    def test_the_possible_pairs_are_the_exact_combination_count(self):
        # Ten strikes are 45 combinations - whether or not a ceiling lets the
        # enumeration get that far. The number is the chain's arithmetic.
        chain = wide_ladder(strikes=10)
        offen = run_one(chain).results[0]
        self.assertEqual(offen["pairs_possible"], 45)
        self.assertEqual(offen["pairs_enumerated"], 45)
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", 5):
            gedeckelt = run_one(chain).results[0]
        self.assertEqual(gedeckelt["pairs_possible"], 45)
        self.assertEqual(gedeckelt["pairs_enumerated"], 5)

    def test_a_capped_search_says_its_possible_number_is_only_measured(self):
        # The screener stops COUNTING a cut search, so its own number is a
        # lower bound. The row says so instead of printing "5 von 45".
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", 5):
            row = run_one(wide_ladder(strikes=10)).results[0]
        self.assertEqual(row["pairs_possible"], 45)            # the section's truth
        self.assertEqual(row["pairs_possible_measured"], 5)    # what was measured
        self.assertFalse(row["pairs_possible_exact"])
        self.assertEqual(row["pairs_enumerated"], 5)
        self.assertEqual(row["pairs_limit"], 5)
        self.assertTrue(row["pairs_capped"])

    def test_a_capped_search_is_named_in_the_answer(self):
        chain = wide_ladder(strikes=10)
        # The ceiling is a module constant, patched here to a size this chain
        # reaches: 10 strikes are 45 combinations, 5 are built.
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", 5):
            run = run_one(chain)
        self.assertEqual(len(run.results), 1)
        row = run.results[0]
        self.assertTrue(row["pairs_capped"])
        self.assertEqual(row["pairs_limit"], 5)
        self.assertEqual(row["pairs_enumerated"], 5)
        # The shown pair is the best one BUILT, not the best one that exists:
        # the 105 short is the widest pair of the first five, the 109 (a ratio
        # of 8.0) was never enumerated.
        self.assertEqual(row["short_strike"], 105.0)
        self.assertEqual(len(run.notes), 1)
        self.assertIn("Deckel", run.notes[0])
        self.assertIn("(5)", run.notes[0])
        self.assertIn("GEFUNDENE", run.notes[0])

    def test_a_complete_search_is_not_called_capped(self):
        run = run_one(ladder_chain())
        row = run.results[0]
        self.assertFalse(row["pairs_capped"])
        self.assertEqual(row["pairs_possible"], row["pairs_enumerated"])
        # No ceiling was hit, so nothing may talk about one.
        self.assertEqual(run.notes, [])
        self.assertFalse(any("Deckel" in note for note in run.notes))

    def test_an_excluded_title_names_the_cap(self):
        # The section can be cut even when it produces no winner: the reason has
        # to describe the prefix that was built, not the expiry - and it has to
        # name both the ceiling and the section's exact size, or the cut stays
        # invisible in the very message that reports it.
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", 5):
            run = run_one(flat_ladder(strikes=10))
        self.assertEqual(run.results, [])
        eintrag = run.excluded[0]
        self.assertEqual(eintrag.reason, REASON_NO_SPREAD)
        self.assertIn("5 aufgezählten Paaren", eintrag.message)
        self.assertIn("Suche am Deckel von 5 - 45 möglich", eintrag.message)
        self.assertTrue(eintrag.pairs_capped)
        self.assertEqual(eintrag.pairs_possible, 45)
        self.assertEqual(eintrag.pairs_enumerated, 5)
        # The JSON of the exclusion carries the same facts - additively.
        self.assertTrue(eintrag.as_dict()["pairs_capped"])
        self.assertEqual(eintrag.as_dict()["pairs_possible"], 45)
        # A capped exclusion is still a capped title, so the run names it - and
        # it does not invent a ceiling number it cannot read from a row.
        self.assertEqual(len(run.notes), 1)
        self.assertIn("Bei 1 Titel", run.notes[0])
        self.assertIn("Deckel", run.notes[0])
        self.assertNotIn("50 000", run.notes[0])

    def test_an_uncapped_exclusion_names_valid_and_enumerated_pairs(self):
        # No ceiling here: the section enumerates all 45 combinations, the
        # screener's validity rules keep 5 of them. "5 Paare geprüft" beside
        # pairs_enumerated 45 read as if the search had only SEEN five pairs;
        # the sentence now names both numbers.
        run = run_one(star_ladder())
        self.assertEqual(run.results, [])
        eintrag = run.excluded[0]
        self.assertFalse(eintrag.pairs_capped)
        self.assertEqual(eintrag.pairs_enumerated, 45)
        self.assertEqual(eintrag.pairs_possible, 45)
        self.assertIn("5 gültige Paare von 45 aufgezählten Paaren", eintrag.message)
        self.assertNotIn("Paare geprüft", eintrag.message)

    def test_a_capped_exclusion_names_the_cap_and_the_possible_count(self):
        # The ceiling (5) cuts an enumeration of 45 combinations: the sentence
        # names the cap AND the section's exact size, so the cut is visible
        # instead of hiding behind the number that was actually looked at.
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", 5):
            run = run_one(star_ladder())
        self.assertEqual(run.results, [])
        eintrag = run.excluded[0]
        self.assertTrue(eintrag.pairs_capped)
        self.assertEqual(eintrag.pairs_enumerated, 5)
        self.assertEqual(eintrag.pairs_possible, 45)
        self.assertIn("5 gültige Paare von 5 aufgezählten Paaren", eintrag.message)
        self.assertIn("Suche am Deckel von 5 - 45 möglich", eintrag.message)
        self.assertNotIn("Paare geprüft", eintrag.message)

    def test_one_valid_pair_is_worded_in_the_singular(self):
        # One counted pair is not "1 gültige Paare": the count stays one, the
        # adjective and the noun follow it - in the valid count, in the
        # enumerated count and in the possible count alike.
        einer = gap_funnel_mod._capped_counts(
            "kein Paar mit R/R >= 2.0", 1, {"capped": False, "enumerated": 2},
            ladder_chain(), BULL_CALL_DEBIT)
        self.assertEqual(einer, "kein Paar mit R/R >= 2.0 "
                                "(1 gültiges Paar von 2 aufgezählten Paaren)")
        # One enumerated pair as well: "von 1 aufgezählten Paar", not "Paaren".
        aufgezaehlt = gap_funnel_mod._capped_counts(
            "kein Paar mit R/R >= 2.0", 1,
            {"capped": True, "enumerated": 1, "limit": 1},
            ladder_chain(), BULL_CALL_DEBIT)
        self.assertIn("(1 gültiges Paar von 1 aufgezählten Paar, ", aufgezaehlt)
        # Two strikes are exactly one combination: "1 mögliches Paar".
        zwei = [call("AAA", "2027-03-19", 100.0, 3.90, 4.00, oi=1200),
                call("AAA", "2027-03-19", 103.3, 3.00, 3.10, oi=900)]
        moeglich = gap_funnel_mod._capped_counts(
            "kein Paar mit R/R >= 2.0", 1,
            {"capped": True, "enumerated": 1, "limit": 1}, zwei, BULL_CALL_DEBIT)
        self.assertIn("Suche am Deckel von 1 - 1 mögliches Paar)", moeglich)

    def test_a_message_without_a_run_or_a_section_invents_no_number(self):
        # No coverage means no run: there is no enumerated count to print, so
        # the sentence stays at "N gültige Paare" instead of inventing one.
        ohne_lauf = gap_funnel_mod._capped_counts(
            "kein Paar mit R/R >= 2.0", 5, None, ladder_chain(), BULL_CALL_DEBIT)
        self.assertEqual(ohne_lauf, "kein Paar mit R/R >= 2.0 (5 gültige Paare)")
        # A cut search without a readable ceiling or section stays "?" - a
        # guessed number would be worse than the question mark.
        ohne_deckel = gap_funnel_mod._capped_counts(
            "kein Paar mit R/R >= 2.0", 5,
            {"capped": True, "enumerated": 5, "limit": None},
            [call("AAA", "2027-03-19", 100.0, 3.9, 4.0)], BULL_CALL_DEBIT)
        self.assertEqual(
            ohne_deckel,
            "kein Paar mit R/R >= 2.0 (5 gültige Paare von 5 aufgezählten Paaren, "
            "Suche am Deckel von ? - ? möglich)")

    def test_the_cap_note_counts_rows_and_exclusions(self):
        # One title shows a pair found under the ceiling, one is excluded by it:
        # the sentence has to name both, or the exclusion stays invisible.
        chains = {"AAA": wide_ladder("AAA", strikes=10),
                  "BBB": flat_ladder("BBB", strikes=10)}
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", 5):
            run = run_funnel(
                [candidate("AAA"), candidate("BBB")],
                fetch_next_earnings=lambda _symbol: date(2027, 2, 1),
                fetch_chain=lambda symbol: list(chains[symbol]),
                day=REACTION, now_fn=NOW)
        self.assertEqual([row["symbol"] for row in run.results], ["AAA"])
        self.assertEqual([entry.symbol for entry in run.excluded], ["BBB"])
        self.assertTrue(run.excluded[0].pairs_capped)
        self.assertEqual(len(run.notes), 1)
        self.assertIn("Bei 2 Titeln", run.notes[0])
        self.assertIn("(5)", run.notes[0])
        self.assertIn("GEFUNDENE", run.notes[0])

    def test_the_wide_search_stays_unbounded(self):
        # 100 .. 300 is a 200-point span. A width rule of our own (say "max 25
        # points") would quietly drop the only pair this chain has - the agreed
        # rule is "the best R/R of the WHOLE expiry", so the span IS the width.
        chain = [call("AAA", "2027-03-19", 100.0, 3.90, 4.00, delta=55.0, oi=1200),
                 call("AAA", "2027-03-19", 300.0, 0.10, 0.20, delta=25.0, oi=900)]
        criteria = criteria_for(chain)
        self.assertEqual(criteria.pair_max_width, 200.0)      # max - min, nothing else
        self.assertEqual(criteria.pair_max_width, max(q.asset.strike for q in chain)
                         - min(q.asset.strike for q in chain))
        pair, _criteria, note, coverage = best_spread(chain, BULL_CALL_DEBIT, now_fn=NOW)
        self.assertIsNone(note)
        self.assertIsNotNone(pair)
        self.assertEqual(pair.strike_width, 200.0)
        self.assertFalse(coverage["capped"])


# --- liquidity ------------------------------------------------------------- #
class LiquidityTests(unittest.TestCase):
    def _pair(self, long_leg, short_leg):
        return VerticalPair(long=long_leg, short=short_leg, strategy=BULL_CALL_DEBIT)

    def test_deep_and_tight_is_full(self):
        pair = self._pair(call("AAA", "2027-03-19", 100.0, 3.90, 4.00, oi=600),
                          call("AAA", "2027-03-19", 103.3, 3.00, 3.10, oi=500))
        self.assertEqual(liquidity_of(pair), LIQUIDITY_FULL)

    def test_open_interest_below_five_hundred_is_thin(self):
        pair = self._pair(call("AAA", "2027-03-19", 100.0, 3.90, 4.00, oi=499),
                          call("AAA", "2027-03-19", 103.3, 3.00, 3.10, oi=5000))
        self.assertEqual(liquidity_of(pair), LIQUIDITY_THIN)

    def test_a_spread_above_ten_percent_is_thin(self):
        # mid 3.00, 0.40 wide -> 13.3 %: too wide for "full", still a candidate.
        pair = self._pair(call("AAA", "2027-03-19", 100.0, 2.80, 3.20, oi=5000),
                          call("AAA", "2027-03-19", 103.3, 3.00, 3.10, oi=5000))
        self.assertGreater(bid_ask_pct(pair.long), 10.0)
        self.assertEqual(liquidity_of(pair), LIQUIDITY_THIN)

    def test_ten_percent_is_still_full(self):
        # mid 1.00, 0.10 wide -> exactly 10 %: the bound is inclusive.
        pair = self._pair(call("AAA", "2027-03-19", 100.0, 0.95, 1.05, oi=500),
                          call("AAA", "2027-03-19", 103.3, 0.95, 1.05, oi=500))
        self.assertEqual(round(bid_ask_pct(pair.long), 6), 10.0)
        self.assertEqual(liquidity_of(pair), LIQUIDITY_FULL)
        # 10.5 % is beyond it - the bound is inclusive, not a suggestion.
        wider = self._pair(call("AAA", "2027-03-19", 100.0, 0.94, 1.05, oi=500),
                           call("AAA", "2027-03-19", 103.3, 0.95, 1.05, oi=500))
        self.assertEqual(liquidity_of(wider), LIQUIDITY_THIN)

    def test_an_unmeasurable_spread_is_never_full(self):
        pair = self._pair(call("AAA", "2027-03-19", 100.0, 0.0, 4.00, oi=5000),
                          call("AAA", "2027-03-19", 103.3, 3.00, 3.10, oi=5000))
        self.assertIsNone(bid_ask_pct(pair.long))
        self.assertEqual(liquidity_of(pair), LIQUIDITY_THIN)


# --- the run --------------------------------------------------------------- #
class _ChainError(Exception):
    """Stand-in for ``CboeChainError``: only ``reason`` is read."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def candidate(symbol, gap_pct=10.0, direction="up", name=None, close=100.0):
    return {
        "symbol": symbol,
        "name": name or f"{symbol} Inc.",
        "gap_pct": gap_pct,
        "direction": direction,
        "strategy": "bull_call_debit" if direction == "up" else "bear_put_debit",
        "close": close,
        "turnover_usd": close * 2_000_000.0,
        "earnings_date": PREVIOUS,
    }


class FunnelRunTests(unittest.TestCase):
    @staticmethod
    def _raiser(table):
        """A source fake: a stored exception is RAISED, a stored value answered."""
        def fetch(symbol):
            value = table[symbol]
            if isinstance(value, Exception):
                raise value
            return value
        return fetch

    def _run(self, candidates, dates, chains, **kwargs):
        return run_funnel(
            candidates,
            fetch_next_earnings=self._raiser(dates),
            fetch_chain=self._raiser(chains),
            day=REACTION,
            now_fn=NOW,
            **kwargs,
        )

    def test_every_title_is_a_row_or_a_reason(self):
        dates = {"AAA": date(2027, 2, 1), "DDD": None, "EEE": date(2027, 2, 1),
                 "FFF": date(2027, 2, 1), "GGG": date(2027, 2, 1),
                 "HHH": NextEarningsError(CODE_NO_API_KEY, "no key")}
        chains = {
            "AAA": ladder_chain(),
            # EEE: the only listed series lies BEFORE the report.
            "EEE": [call("EEE", "2026-12-18", 100.0, 3.9, 4.0)],
            "FFF": _ChainError("not_listed"),
            "GGG": _ChainError("throttled"),
        }
        run = self._run([candidate(symbol) for symbol in
                          ("AAA", "DDD", "EEE", "FFF", "GGG", "HHH")], dates, chains)

        self.assertEqual([row["symbol"] for row in run.results], ["AAA"])
        self.assertEqual(run.counts["with_next_earnings"], 4)   # AAA, EEE, FFF, GGG
        self.assertEqual(run.counts["with_chain"], 2)           # AAA, EEE
        self.assertEqual(run.counts["with_expiry_after_earnings"], 1)
        self.assertEqual(run.counts["with_spread_ge_rr"], 1)
        self.assertEqual(run.counts["shown"], 1)

        reasons = {entry.symbol: entry.reason for entry in run.excluded}
        self.assertEqual(reasons, {
            "DDD": REASON_NO_NEXT_EARNINGS,
            "EEE": REASON_NO_EXPIRY,
            "FFF": REASON_NOT_OPTIONABLE,
            "HHH": REASON_NO_NEXT_EARNINGS,
        })
        failures = {entry.symbol: entry.code for entry in run.failures}
        self.assertEqual(failures, {"GGG": "chain_fetch_failed"})
        # Every candidate is accounted for exactly once.
        self.assertEqual(len(run.results) + len(run.excluded) + len(run.failures), 6)

    def test_a_missing_key_is_a_setup_fact_not_a_failure(self):
        run = self._run([candidate("AAA")],
                        {"AAA": NextEarningsError(CODE_NO_API_KEY, "FINNHUB_API_KEY is not set")},
                        {})
        self.assertEqual(run.failures, [])
        self.assertEqual(run.excluded[0].reason, REASON_NO_NEXT_EARNINGS)
        self.assertIn("FINNHUB_API_KEY", run.excluded[0].message)

    def test_a_thin_pair_is_still_a_row(self):
        chain = [
            call("AAA", "2027-03-19", 100.0, 3.90, 4.00, oi=150, delta=55.0),
            call("AAA", "2027-03-19", 103.30, 3.00, 3.10, oi=150, delta=30.0),
        ]
        run = self._run([candidate("AAA")], {"AAA": date(2027, 2, 1)}, {"AAA": chain})
        self.assertEqual(len(run.results), 1)
        self.assertEqual(run.results[0]["liquidity"], LIQUIDITY_THIN)

    def test_a_pair_below_the_open_interest_floor_is_excluded(self):
        chain = [
            call("AAA", "2027-03-19", 100.0, 3.90, 4.00, oi=99, delta=55.0),
            call("AAA", "2027-03-19", 103.30, 3.00, 3.10, oi=99, delta=30.0),
        ]
        run = self._run([candidate("AAA")], {"AAA": date(2027, 2, 1)}, {"AAA": chain})
        self.assertEqual(run.results, [])
        self.assertEqual(run.excluded[0].reason, REASON_NO_SPREAD)

    def test_an_expired_deadline_is_named_not_dropped(self):
        def slow(_symbol):
            time.sleep(0.3)
            return date(2027, 2, 1)

        run = run_funnel([candidate("AAA"), candidate("BBB")],
                         fetch_next_earnings=slow,
                         fetch_chain=lambda symbol: ladder_chain(symbol),
                         day=REACTION, now_fn=NOW, deadline_seconds=0.05)
        self.assertEqual({entry.code for entry in run.failures}, {"deadline_exceeded"})
        self.assertEqual(len(run.failures), 2)
        self.assertEqual(run.results, [])

    def test_unfinished_work_is_cancelled_at_the_deadline(self):
        started = []
        gate = threading.Event()

        def blocking_chain(symbol):
            started.append(symbol)
            # The request outlives the deadline; it is released by the test, not
            # by the run - so anything that starts after the answer was built is
            # work the run should never have queued.
            gate.wait(5.0)
            return ladder_chain(symbol)

        symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        try:
            run = run_funnel([candidate(symbol) for symbol in symbols],
                             fetch_next_earnings=lambda symbol: date(2027, 2, 1),
                             fetch_chain=blocking_chain,
                             day=REACTION, now_fn=NOW,
                             max_workers=2, deadline_seconds=0.1)
            # Two workers, both stuck in their chain: nothing else CAN start
            # before the deadline, so exactly two requests are in flight.
            self.assertEqual(len(started), 2)
            self.assertEqual({entry.code for entry in run.failures}, {"deadline_exceeded"})
            self.assertEqual(len(run.failures), 6)
            self.assertEqual(run.results, [])
            # Releasing the in-flight requests must not be answered by four more
            # chains: the queued titles were cancelled, not left in the queue.
            gate.set()
            time.sleep(0.3)
            self.assertEqual(len(started), 2)
        finally:
            gate.set()

    def test_an_empty_candidate_list_is_an_empty_run(self):
        run = run_funnel([], fetch_next_earnings=lambda symbol: None,
                         fetch_chain=lambda symbol: [], day=REACTION, now_fn=NOW)
        self.assertEqual((run.results, run.excluded, run.failures), ([], [], []))
        self.assertEqual(run.counts["shown"], 0)


# --- reachability: the distance cap and the measured frequency ------------- #
def far_call_chain(symbol="AAA", expiry="2027-03-19"):
    """A 2.3:1 call spread whose break-even sits 51 % above a 60 USD close."""
    return [call(symbol, expiry, 90.0, 3.90, 4.00, delta=55.0, oi=1200),
            call(symbol, expiry, 93.30, 3.00, 3.10, delta=30.0, oi=900)]


def cheap_call_chain(symbol="AAA", expiry="2027-03-19"):
    """A 2.3:1 call spread 1 % away from the close: break-even 101 on 100."""
    return [call(symbol, expiry, 100.0, 3.90, 4.00, delta=55.0, oi=1200),
            call(symbol, expiry, 103.30, 3.00, 3.10, delta=30.0, oi=900)]


def far_put_chain(symbol="AAA", expiry="2027-03-19"):
    """A 2.3:1 put spread whose break-even sits 21 % BELOW a 100 USD close."""
    return [put(symbol, expiry, 80.0, 3.90, 4.00, delta=-55.0, oi=1200),
            put(symbol, expiry, 76.70, 3.00, 3.10, delta=-30.0, oi=900)]


class ReachabilityFunnelTests(unittest.TestCase):
    """The upper bound on the DISTANCE, and the history behind every row."""

    @staticmethod
    def _run(candidates, chains, *, history=None, dates=None, **kwargs):
        for symbol in chains:
            dates = dates or {}
            dates.setdefault(symbol, date(2027, 2, 1))
        return run_funnel(
            candidates,
            fetch_next_earnings=lambda symbol: dates[symbol],
            fetch_chain=lambda symbol: chains[symbol],
            fetch_daily=history,
            day=REACTION, now_fn=NOW, **kwargs)

    def test_a_move_above_the_bound_is_excluded_with_both_numbers(self):
        run = self._run([candidate("AAA", close=60.0)], {"AAA": far_call_chain()})
        self.assertEqual(run.results, [])
        grund = run.excluded[0]
        self.assertEqual(grund.reason, gap_funnel_mod.REASON_MOVE_TOO_FAR)
        # (91.00 / 60.00 - 1) * 100 = 51.67 % - the number, not a label.
        self.assertEqual(grund.required_move_pct, 51.67)
        self.assertEqual(grund.max_required_move_pct, 20.0)
        self.assertEqual(grund.as_dict()["required_move_pct"], 51.67)
        self.assertIn("51,67 % steigen", grund.message)
        self.assertIn("zugelassen sind 20 %", grund.message)

    def test_the_bound_holds_at_equality_and_only_break_above_it(self):
        # break-even 101 on a close of 100 is exactly 1.00 %.
        genau = self._run([candidate("AAA", close=100.0)], {"AAA": cheap_call_chain()},
                          max_required_move_pct=1.0)
        self.assertEqual(len(genau.results), 1)
        self.assertEqual(genau.results[0]["required_move_pct"], 1.0)

        knapp = self._run([candidate("AAA", close=100.0)], {"AAA": cheap_call_chain()},
                          max_required_move_pct=0.99)
        self.assertEqual(knapp.results, [])
        self.assertEqual(knapp.excluded[0].reason, gap_funnel_mod.REASON_MOVE_TOO_FAR)

    def test_the_default_bound_is_twenty_percent(self):
        self.assertEqual(gap_funnel_mod.DEFAULT_MAX_REQUIRED_MOVE_PCT, 20.0)
        run = self._run([candidate("AAA", close=60.0)], {"AAA": far_call_chain()})
        self.assertEqual(run.excluded[0].max_required_move_pct, 20.0)

    def test_the_bound_reads_the_absolute_move_of_a_put_too(self):
        # A put spread breaks even BELOW the close: 79 on 100 is -21 %.
        run = self._run([candidate("AAA", gap_pct=-9.0, direction="down", close=100.0)],
                        {"AAA": far_put_chain()})
        self.assertEqual(run.results, [])
        grund = run.excluded[0]
        self.assertEqual(grund.reason, gap_funnel_mod.REASON_MOVE_TOO_FAR)
        self.assertEqual(grund.required_move_pct, -21.0)
        self.assertIn("21 % fallen", grund.message)

    def test_a_ratio_of_one_to_twenty_stays_a_row(self):
        # No ratio ceiling: a 40-wide pair for a 1.00 debit is 1:39, and it stays
        # as long as the move is reachable.
        chain = [call("AAA", "2027-03-19", 100.0, 3.90, 4.00, delta=55.0, oi=1200),
                 call("AAA", "2027-03-19", 140.0, 3.00, 3.10, delta=30.0, oi=900)]
        run = self._run([candidate("AAA", close=100.0)], {"AAA": chain})
        self.assertEqual(len(run.results), 1)
        self.assertGreater(run.results[0]["rr"], 20.0)
        self.assertEqual(run.results[0]["required_move_pct"], 1.0)

    def test_the_row_carries_the_distance_and_the_direction(self):
        run = self._run([candidate("AAA", close=100.0)], {"AAA": cheap_call_chain()})
        row = run.results[0]
        self.assertEqual(row["required_move_pct"], 1.0)
        self.assertEqual(row["required_move_direction"], "up")

    def test_the_counters_name_the_rows_that_passed_the_move(self):
        run = self._run([candidate("AAA", close=100.0), candidate("BBB", close=60.0)],
                        {"AAA": cheap_call_chain("AAA"), "BBB": far_call_chain("BBB")})
        self.assertEqual(run.counts["with_spread_ge_rr"], 2)
        self.assertEqual(run.counts["with_reachable_move"], 1)
        self.assertEqual(run.counts["shown"], 1)

    def test_only_rows_with_a_spread_cost_a_history_request(self):
        gefragt = []

        def history(symbol, fromdate, limit):
            gefragt.append((symbol, fromdate, limit))
            return _series(100.0)

        run = self._run([candidate("AAA", close=100.0), candidate("BBB", close=60.0)],
                        {"AAA": cheap_call_chain("AAA"), "BBB": far_call_chain("BBB")},
                        history=history)
        self.assertEqual([symbol for symbol, _f, _l in gefragt], ["AAA"])
        # About two years of sessions, opened early enough for the count to
        # close the window - one request per row, not per candidate.
        symbol, fromdate, limit = gefragt[0]
        self.assertEqual(limit, 520)
        self.assertLess(fromdate, REACTION - timedelta(days=730))
        self.assertEqual(run.results[0]["history_state"], "ok")

    def test_a_failing_history_keeps_the_row_and_names_the_source(self):
        def kaputt(_symbol, _fromdate, _limit):
            raise RuntimeError("source down")

        run = self._run([candidate("AAA", close=100.0)], {"AAA": cheap_call_chain()},
                        history=kaputt)
        self.assertEqual(len(run.results), 1)
        row = run.results[0]
        self.assertEqual(row["history_state"], "source_failed")
        self.assertIsNone(row["hit_rate_pct"])
        self.assertEqual(row["windows_measured"], 0)
        # A failing history is not an exclusion and not a failure of the title.
        self.assertEqual(run.excluded, [])
        self.assertEqual(run.failures, [])

    def test_a_run_without_a_history_source_says_so_per_row(self):
        run = self._run([candidate("AAA", close=100.0)], {"AAA": cheap_call_chain()})
        self.assertEqual(run.results[0]["history_state"], "source_failed")
        self.assertIsNone(run.results[0]["hit_rate_pct"])

    def test_a_short_history_is_too_short_not_zero(self):
        run = self._run([candidate("AAA", close=100.0)], {"AAA": cheap_call_chain()},
                        history=lambda _symbol, _fromdate, _limit: _series(100.0, count=40))
        row = run.results[0]
        self.assertEqual(row["history_state"], "too_short")
        self.assertIsNone(row["hit_rate_pct"])
        self.assertEqual(row["windows_measured"], 0)

    def test_the_measured_row_carries_the_frequency(self):
        # A wave of 20 % around 100 with a required move of 1 %: about half of
        # the windows of 127 sessions end above their start, and the count is
        # the series minus the window.
        run = self._run([candidate("AAA", close=100.0)], {"AAA": cheap_call_chain()},
                        history=lambda _symbol, _fromdate, _limit: _series(100.0, wave=20.0))
        row = run.results[0]
        self.assertEqual(row["history_state"], "ok")
        self.assertEqual(row["windows_measured"], 520 - row["dte"])
        self.assertGreater(row["hit_rate_pct"], 0.0)
        self.assertLessEqual(row["hit_rate_pct"], 100.0)


class ResultSortTests(unittest.TestCase):
    """The order of the answer: the measured frequency decides."""

    @staticmethod
    def _row(symbol, rate, move, gap):
        return {"symbol": symbol, "hit_rate_pct": rate, "required_move_pct": move,
                "gap_pct": gap}

    def test_the_higher_hit_rate_comes_first(self):
        rows = [self._row("AAA", 10.0, 5.0, 20.0), self._row("BBB", 40.0, 5.0, 3.0)]
        rows.sort(key=gap_funnel_mod.result_sort_key)
        self.assertEqual([row["symbol"] for row in rows], ["BBB", "AAA"])

    def test_rows_without_a_rate_stand_behind_every_measured_one(self):
        rows = [self._row("AAA", None, 1.0, 30.0), self._row("BBB", 3.0, 15.0, 2.0)]
        rows.sort(key=gap_funnel_mod.result_sort_key)
        self.assertEqual([row["symbol"] for row in rows], ["BBB", "AAA"])

    def test_a_tie_goes_to_the_shorter_move(self):
        rows = [self._row("AAA", 20.0, 14.0, 30.0), self._row("BBB", 20.0, 4.0, 2.0)]
        rows.sort(key=gap_funnel_mod.result_sort_key)
        self.assertEqual([row["symbol"] for row in rows], ["BBB", "AAA"])

    def test_the_unmeasured_rows_follow_the_same_rule_among_themselves(self):
        rows = [self._row("AAA", None, 12.0, 30.0), self._row("BBB", None, 3.0, 2.0)]
        rows.sort(key=gap_funnel_mod.result_sort_key)
        self.assertEqual([row["symbol"] for row in rows], ["BBB", "AAA"])

    def test_a_full_tie_falls_back_to_the_bigger_gap(self):
        rows = [self._row("AAA", 20.0, 4.0, 2.0), self._row("BBB", 20.0, 4.0, 30.0)]
        rows.sort(key=gap_funnel_mod.result_sort_key)
        self.assertEqual([row["symbol"] for row in rows], ["BBB", "AAA"])

    def test_the_order_is_total(self):
        rows = [self._row("AAA", 20.0, 4.0, 2.0), self._row("BBB", 20.0, 4.0, 2.0)]
        rows.sort(key=gap_funnel_mod.result_sort_key)
        self.assertEqual([row["symbol"] for row in rows], ["AAA", "BBB"])


# --- the endpoint ---------------------------------------------------------- #
def calendar_row(symbol, session=None, name=None):
    return {"symbol": symbol, "name": name or f"{symbol} Inc.", "session": session,
            "eps_forecast": "$1.10", "market_cap": 1000}


class _FakeCalendar:
    def __init__(self, rows_by_day):
        self.rows_by_day = rows_by_day
        self.calls = 0

    def get_calendar_days(self, days):
        self.calls += 1
        return {
            "state": "live",
            "days": {tag: {"date": tag, "state": "live",
                           "rows": self.rows_by_day.get(tag, [])}
                     for tag in days.values()},
        }


class _History:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def fetch_daily(self, symbol, fromdate, limit=None):
        self.calls.append(symbol)
        return list(self.bars.get(symbol, []))


class _Chains:
    def __init__(self, chains):
        self.chains = chains
        self.calls = []

    def get_options(self, underlying_asset=None, expiration_date=None):
        self.calls.append((underlying_asset, expiration_date))
        value = self.chains.get(underlying_asset, [])
        if isinstance(value, Exception):
            raise value
        return list(value)


class _NextEarnings:
    def __init__(self, dates):
        self.dates = dates
        self.calls = []

    def get_next_earnings(self, symbol):
        self.calls.append(symbol)
        value = self.dates.get(symbol)
        if isinstance(value, Exception):
            raise value
        return value


def _bars(close, previous_close=100.0, volume=2_000_000.0):
    return [
        GapBar(session_date=REACTION, open=close, close=close, volume=volume),
        GapBar(session_date=BEFORE, open=previous_close, close=previous_close,
               volume=volume),
    ]


def _series(last_close, previous_close=100.0, count=520, wave=0.0, step=0.0,
            volume=2_000_000.0):
    """``count`` daily bars, NEWEST first, ending at the reaction day.

    The first two bars are the gap of the fixture (reaction close against the
    close right before it); the older ones swing around ``previous_close`` -
    ``wave`` in percent of the price, ``step`` as a drift per session. Written
    as a sine so the series is deterministic: no seed, no wall clock, and the
    very same bars for every run.
    """
    bars = []
    for index in range(count):
        close = previous_close * (1.0 + wave / 100.0 * math.sin(index / 7.0)
                                  - step / 100.0 * index)
        if index == 0:
            close = last_close
        elif index == 1:
            close = previous_close
        bars.append(GapBar(session_date=REACTION - timedelta(days=index),
                           open=close, close=close, volume=volume))
    return bars


def _client(calendar=None, history=None, chains=None, dates=None):
    return create_app(
        quote_adapter=chains or _Chains({}),
        earnings_adapter=calendar or _FakeCalendar({}),
        gap_adapter=history or _History({}),
        next_earnings_adapter=dates or _NextEarnings({}),
        now_fn=CLOCK,
    ).test_client()


def _fixture():
    """AAA +10 % (up), BBB -8 % (down), CCC +3 % without a report date."""
    calendar = _FakeCalendar({
        PREVIOUS: [calendar_row("AAA", session="after_market", name="Alpha Corp"),
                   calendar_row("BBB", session=None),
                   calendar_row("CCC", session="before_market")],
        DAY: [calendar_row("DDD", session="after_market")],   # no gap yet
    })
    # Two years of sessions for every candidate: the reachability numbers of
    # this answer are measured, not ``too_short``. AAA swings three times as
    # hard as BBB, so the two hit rates differ and the order is checkable.
    history = _History({"AAA": _series(110.0, wave=20.0),
                        "BBB": _series(92.0, wave=8.0),
                        "CCC": _series(103.0, wave=12.0)})
    chains = _Chains({
        "AAA": ladder_chain("AAA"),
        "BBB": [put("BBB", "2027-04-16", 100.0, 3.90, 4.00, delta=-60.0, oi=1200),
                put("BBB", "2027-04-16", 96.70, 3.00, 3.10, delta=-40.0, oi=900)],
        "CCC": ladder_chain("CCC"),          # must never be asked
    })
    dates = _NextEarnings({"AAA": date(2027, 2, 1), "BBB": date(2027, 3, 1), "CCC": None})
    return calendar, history, chains, dates


CONTRACT_KEYS = {"schema_version", "day", "day_state", "threshold_pct", "min_turnover_usd",
                 "rr_min", "min_oi", "max_required_move_pct", "counts", "results",
                 "excluded", "failures", "notes"}
COUNT_KEYS = ("calendar_rows", "gaps_computed", "over_threshold", "turnover_passed",
              "price_passed", "candidates", "with_next_earnings",
              "with_expiry_after_earnings", "with_chain", "with_spread_ge_rr",
              "with_reachable_move", "shown")
ROW_KEYS = {"symbol", "name", "gap_pct", "direction", "strategy", "strategy_label", "close",
            "turnover_usd", "earnings_date", "next_earnings_date", "expiration", "dte",
            "long_strike", "short_strike", "long_delta", "short_delta", "net_debit",
            "max_loss", "max_profit", "rr", "net_return_pct", "liquidity", "open_interest",
            "bid_ask_pct", "position_debit", "pairs_possible", "pairs_possible_measured",
            "pairs_possible_exact", "pairs_enumerated", "pairs_limit", "pairs_capped",
            "required_move_pct", "required_move_direction", "volatility_pct",
            "daily_move_pct", "hit_rate_pct", "windows_measured", "history_state"}


class EndpointTests(unittest.TestCase):
    def _get(self, query=f"?day={DAY}", fixture=None):
        calendar, history, chains, dates = fixture or _fixture()
        antwort = _client(calendar, history, chains, dates).get(f"/api/v1/gap-spreads{query}")
        return antwort, antwort.get_json(), chains, dates

    def test_the_json_contract(self):
        antwort, body, _chains, _dates = self._get()
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(set(body), CONTRACT_KEYS)
        self.assertEqual(body["schema_version"], "1.0")
        self.assertEqual(body["day"], DAY)
        self.assertEqual(body["day_state"], "live")
        self.assertEqual(body["threshold_pct"], 2.0)
        self.assertEqual(body["min_turnover_usd"], 5_000_000.0)
        self.assertEqual(body["rr_min"], 2.0)
        self.assertEqual(body["min_oi"], 100)
        self.assertEqual(set(body["counts"]), set(COUNT_KEYS))
        self.assertIsInstance(body["notes"], list)
        self.assertTrue(all(isinstance(note, str) for note in body["notes"]))
        for row in body["results"]:
            self.assertEqual(set(row), ROW_KEYS)

    def test_results_are_sorted_by_the_hit_rate_not_by_the_gap(self):
        # The gap order would put AAA (|10 %|) first; the ORDER is the measured
        # frequency, and BBB reaches its required move more often.
        _antwort, body, _chains, _dates = self._get()
        self.assertEqual([row["symbol"] for row in body["results"]], ["BBB", "AAA"])
        self.assertGreater(body["results"][0]["hit_rate_pct"],
                           body["results"][1]["hit_rate_pct"])
        self.assertGreater(abs(body["results"][1]["gap_pct"]),
                           abs(body["results"][0]["gap_pct"]))
        rates = [row["hit_rate_pct"] for row in body["results"]]
        self.assertEqual(sorted(rates, reverse=True), rates)

    def test_the_row_carries_the_agreed_numbers(self):
        _antwort, body, _chains, _dates = self._get()
        aaa = next(row for row in body["results"] if row["symbol"] == "AAA")
        self.assertEqual(aaa["name"], "Alpha Corp")
        self.assertEqual(aaa["direction"], "up")
        self.assertEqual(aaa["strategy"], "bull_call_debit")
        self.assertEqual(aaa["strategy_label"], "Bull Call Spread (Debit)")
        self.assertEqual(aaa["earnings_date"], PREVIOUS)
        self.assertEqual(aaa["next_earnings_date"], "2027-02-01")
        self.assertEqual(aaa["expiration"], "2027-03-19")
        self.assertEqual(aaa["dte"], (date(2027, 3, 19) - REACTION).days)
        self.assertEqual((aaa["long_strike"], aaa["short_strike"]), (100.0, 103.3))
        self.assertEqual(aaa["long_delta"], 55.0)
        self.assertEqual(aaa["short_delta"], 30.0)
        self.assertEqual(aaa["net_debit"], 1.0)
        self.assertEqual(aaa["max_profit"], 2.3)
        self.assertEqual(aaa["max_loss"], 1.0)
        self.assertEqual(aaa["rr"], 2.3)
        self.assertEqual(aaa["net_return_pct"], 230.0)
        self.assertEqual(aaa["liquidity"], LIQUIDITY_FULL)
        self.assertEqual(aaa["open_interest"], [1200, 800])
        self.assertEqual(len(aaa["bid_ask_pct"]), 2)
        self.assertEqual(aaa["position_debit"], 100.0)     # 1.00 x 100 x 1 + 0 fees

        bbb = next(row for row in body["results"] if row["symbol"] == "BBB")
        self.assertEqual(bbb["direction"], "down")
        self.assertEqual(bbb["strategy"], "bear_put_debit")
        self.assertEqual(bbb["expiration"], "2027-04-16")
        self.assertEqual(bbb["long_strike"], 100.0)        # the bought put is higher
        self.assertEqual(bbb["short_strike"], 96.7)
        self.assertLess(bbb["long_delta"], 0)              # puts carry a negative delta

    def test_the_counters_describe_the_whole_day(self):
        _antwort, body, chains, dates = self._get()
        counts = body["counts"]
        self.assertEqual(counts["calendar_rows"], 3)       # AAA, BBB, CCC
        self.assertEqual(counts["gaps_computed"], 3)
        self.assertEqual(counts["over_threshold"], 3)
        self.assertEqual(counts["turnover_passed"], 3)
        self.assertEqual(counts["price_passed"], 3)
        self.assertEqual(counts["candidates"], 3)
        self.assertEqual(counts["with_next_earnings"], 2)  # CCC has no date
        self.assertEqual(counts["with_chain"], 2)
        self.assertEqual(counts["with_expiry_after_earnings"], 2)
        self.assertEqual(counts["with_spread_ge_rr"], 2)
        self.assertEqual(counts["shown"], 2)

    def test_an_excluded_title_names_its_reason_and_costs_no_chain_request(self):
        _antwort, body, chains, dates = self._get()
        self.assertEqual([entry["symbol"] for entry in body["excluded"]], ["CCC"])
        self.assertEqual(body["excluded"][0]["reason"], REASON_NO_NEXT_EARNINGS)
        self.assertIn("Berichtstermin", body["excluded"][0]["message"])
        self.assertEqual(body["failures"], [])
        # The report date is asked FIRST: a title without one never costs a chain.
        self.assertIn("CCC", dates.calls)
        self.assertNotIn("CCC", [symbol for symbol, _expiry in chains.calls])
        # ONE chain request per symbol, for the whole chain (no expiry).
        self.assertEqual(sorted(symbol for symbol, _expiry in chains.calls), ["AAA", "BBB"])
        self.assertTrue(all(expiry is None for _symbol, expiry in chains.calls))

    def test_the_answer_names_the_move_bound_it_applied(self):
        _antwort, body, _chains, _dates = self._get()
        self.assertEqual(body["max_required_move_pct"], 20.0)

    def test_a_tighter_move_bound_empties_the_list_and_names_the_reason(self):
        # A bound of 1 % is below both required moves (7.61 % and 8.18 %): no
        # row, two exclusions with their numbers - and no history request.
        _antwort, body, _chains, _dates = self._get(f"?day={DAY}&max_required_move_pct=1")
        self.assertEqual(body["max_required_move_pct"], 1.0)
        self.assertEqual(body["results"], [])
        self.assertEqual(body["counts"]["with_reachable_move"], 0)
        self.assertEqual(body["counts"]["with_spread_ge_rr"], 2)
        gruende = {entry["symbol"]: entry for entry in body["excluded"]
                   if entry["reason"] == gap_funnel_mod.REASON_MOVE_TOO_FAR}
        self.assertEqual(set(gruende), {"AAA", "BBB"})
        for eintrag in gruende.values():
            self.assertEqual(eintrag["reason"], gap_funnel_mod.REASON_MOVE_TOO_FAR)
            self.assertEqual(eintrag["max_required_move_pct"], 1.0)
            self.assertIsNotNone(eintrag["required_move_pct"])

    def test_a_move_bound_above_the_rows_keeps_them(self):
        _antwort, body, _chains, _dates = self._get(f"?day={DAY}&max_required_move_pct=30")
        self.assertEqual(body["max_required_move_pct"], 30.0)
        self.assertEqual([row["symbol"] for row in body["results"]], ["BBB", "AAA"])
        self.assertEqual(body["counts"]["with_reachable_move"], 2)

    def test_an_unusable_move_bound_is_a_400(self):
        for wert in ("0", "-5", "101", "abc", "nan", "inf"):
            with self.subTest(wert=wert):
                antwort, body, _chains, _dates = self._get(
                    f"?day={DAY}&max_required_move_pct={wert}")
                self.assertEqual(antwort.status_code, 400)
                self.assertEqual(body["error"], "invalid_parameter")
                self.assertIn("max_required_move_pct", body["message"])

    def test_the_reachability_fields_carry_two_decimals_or_none(self):
        _antwort, body, _chains, _dates = self._get()
        for row in body["results"]:
            for name in ("volatility_pct", "daily_move_pct", "hit_rate_pct",
                         "required_move_pct"):
                wert = row[name]
                self.assertIsNotNone(wert, name)
                self.assertEqual(round(wert, 2), wert, name)
            self.assertEqual(row["history_state"], "ok")
            self.assertIsInstance(row["windows_measured"], int)
            self.assertGreater(row["windows_measured"], 0)
            self.assertIn(row["required_move_direction"], ("up", "down"))

    def test_a_short_history_is_null_and_not_zero(self):
        # 40 sessions cannot answer a question about a 155-session window: the
        # row keeps nulls and a named state - a 0 % would read as "measured,
        # never happened".
        calendar, history, chains, dates = _fixture()
        history.bars["BBB"] = _series(92.0, count=40)
        antwort, body, _chains, _dates = self._get(
            fixture=(calendar, history, chains, dates))
        self.assertEqual(antwort.status_code, 200)
        bbb = next(row for row in body["results"] if row["symbol"] == "BBB")
        self.assertEqual(bbb["history_state"], "too_short")
        self.assertIsNone(bbb["hit_rate_pct"])
        self.assertIsNone(bbb["volatility_pct"])
        self.assertIsNone(bbb["daily_move_pct"])
        self.assertEqual(bbb["windows_measured"], 0)
        # A row without a measurement stands behind a measured one.
        self.assertEqual(body["results"][0]["symbol"], "AAA")

    def test_a_broken_chain_is_a_failure_not_a_row(self):
        calendar, history, chains, dates = _fixture()
        chains.chains["AAA"] = _ChainError("throttled")
        antwort, body, _chains, _dates = self._get(fixture=(calendar, history, chains, dates))
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual([entry["symbol"] for entry in body["failures"]], ["AAA"])
        self.assertEqual(body["failures"][0]["code"], "chain_fetch_failed")
        self.assertIn("throttled", body["failures"][0]["message"])
        self.assertEqual([row["symbol"] for row in body["results"]], ["BBB"])
        self.assertEqual(body["counts"]["with_chain"], 1)

    def test_an_unlisted_chain_is_not_optionable(self):
        calendar, history, chains, dates = _fixture()
        chains.chains["AAA"] = _ChainError("not_listed")
        _antwort, body, _chains, _dates = self._get(
            fixture=(calendar, history, chains, dates))
        self.assertEqual(body["excluded"][0]["symbol"], "AAA")
        self.assertEqual(body["excluded"][0]["reason"], REASON_NOT_OPTIONABLE)

    def test_a_day_without_a_report_date_is_a_200_with_counts(self):
        calendar = _FakeCalendar({PREVIOUS: [calendar_row("AAA")], DAY: []})
        history = _History({"AAA": _bars(110.0)})
        fixture = (calendar, history, _Chains({"AAA": ladder_chain("AAA")}),
                   _NextEarnings({"AAA": None}))
        antwort, body, _chains, _dates = self._get(fixture=fixture)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(body["results"], [])
        self.assertEqual(body["counts"]["with_next_earnings"], 0)
        self.assertEqual(body["excluded"][0]["reason"], REASON_NO_NEXT_EARNINGS)

    def test_a_bad_day_is_still_a_400(self):
        antwort, body, _chains, _dates = self._get("?day=2026-11-14")  # Saturday
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(body["error"], "invalid_day")
        antwort, body, _chains, _dates = self._get("?day=nonsense")
        self.assertEqual(antwort.status_code, 400)

    def test_an_unknown_parameter_is_refused(self):
        antwort, body, _chains, _dates = self._get(f"?day={DAY}&nonsense=1")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(body["error"], "unknown_parameter")

    def test_a_capped_pair_search_is_named_in_the_http_answer(self):
        # Ten strikes are 45 combinations; a ceiling of 5 cuts the enumeration,
        # so every R/R of such a title is the best pair FOUND, not the best.
        # The module knows that - the ANSWER has to say it.
        calendar, history, chains, dates = _fixture()
        chains.chains["AAA"] = wide_ladder("AAA", strikes=10)
        _a, offen, _c, _d = self._get(fixture=(calendar, history, chains, dates))
        self.assertFalse(any("Deckel" in note for note in offen["notes"]))
        with mock.patch.object(screener_mod, "MAX_PAIRS_PER_SECTION", 5):
            antwort, body, _chains, _dates = self._get(
                fixture=(calendar, history, chains, dates))
        self.assertEqual(antwort.status_code, 200)
        aaa = next(row for row in body["results"] if row["symbol"] == "AAA")
        self.assertTrue(aaa["pairs_capped"])
        self.assertEqual(aaa["pairs_limit"], 5)
        deckel = [note for note in body["notes"] if "Deckel" in note]
        self.assertEqual(len(deckel), 1)
        # The sentence names the ceiling the rows really ran against (5, not
        # the module's 50 000) and the number of titles affected.
        self.assertIn("(5)", deckel[0])
        self.assertIn("Bei 1 Titel", deckel[0])
        self.assertIn("GEFUNDENE", deckel[0])
        # The run's sentence comes AFTER the general block, never instead of it.
        self.assertEqual(body["notes"][-1], deckel[0])
        self.assertEqual(body["notes"][:len(offen["notes"])], offen["notes"])

    def test_the_notes_carry_no_duplicates(self):
        # The general block and the run CAN name the same thing (here forced by
        # handing the run a sentence the block already carries): it still stands
        # in the answer once, in the place the general note has.
        calendar, history, chains, dates = _fixture()
        chains.chains["AAA"] = wide_ladder("AAA", strikes=10)
        _a, body, _c, _d = self._get(fixture=(calendar, history, chains, dates))
        allgemein = body["notes"][0]
        with mock.patch.object(gap_funnel_mod, "cap_note",
                               lambda _rows, _excluded=None: allgemein):
            _a, doppelt, _c, _d = self._get(fixture=(calendar, history, chains, dates))
        self.assertEqual(doppelt["notes"].count(allgemein), 1)
        self.assertEqual(doppelt["notes"], body["notes"])
        self.assertEqual(len(doppelt["notes"]), len(set(doppelt["notes"])))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
