"""The three net sort orders, their tie rules and their missing values.

The page may rank a spread by its payoff - that is a statement about the
position at expiry, not about how likely it is. These tests pin the order, the
place of a row without a number, and the wording of the labels.
"""

import unittest

from paperbroker.server import SORT_LABELS, SORT_MODES, _vertical_sort_key


class _Leg:
    def __init__(self, strike, underlying_price=250.0):
        self.asset = type("A", (), {"strike": strike})()
        self.underlying_price = underlying_price


class _Pair:
    """Only what the sort key reads - the numbers are handed in."""

    def __init__(self, symbol, strike, *, net_loss, net_profit, ratio,
                 max_profit=2.3, max_loss=2.7, breakeven=242.7, spot=250.0):
        self.symbol = symbol
        self.long = _Leg(strike, spot)
        self.short = _Leg(strike + 5)
        self.max_profit = max_profit
        self.max_loss = max_loss
        self.breakeven = breakeven
        self._loss = net_loss
        self._profit = net_profit
        self._ratio = ratio
        self.strike_width = 5.0

        self.seen_criteria = []

    def net_max_loss(self, criteria=None):
        self.seen_criteria.append(criteria)
        return self._loss

    def net_max_profit(self, criteria=None):
        self.seen_criteria.append(criteria)
        return self._profit

    def net_return_pct(self, criteria=None):
        self.seen_criteria.append(criteria)
        return self._ratio


def order(mode, pairs):
    return [p.symbol for p in sorted(pairs, key=_vertical_sort_key(mode))]


class TestTheNetOrders(unittest.TestCase):

    def _paare(self):
        # A: smallest risk, small profit; B: big profit, big risk; C: middle.
        # The GROSS numbers differ as well, so the default order really ranks
        # something instead of landing in the tie-break.
        return [
            _Pair("C", 260.0, net_loss=300.0, net_profit=120.0, ratio=40.0,
                  max_profit=1.2, max_loss=3.0, breakeven=261.2),
            _Pair("A", 240.0, net_loss=100.0, net_profit=50.0, ratio=50.0,
                  max_profit=0.5, max_loss=1.0, breakeven=240.5),
            _Pair("B", 250.0, net_loss=500.0, net_profit=400.0, ratio=80.0,
                  max_profit=4.0, max_loss=5.0, breakeven=254.0),
        ]

    def test_max_loss_ascending_puts_the_smallest_risk_first(self):
        self.assertEqual(["A", "C", "B"], order("max_loss_asc", self._paare()))

    def test_net_profit_descending_puts_the_biggest_potential_first(self):
        self.assertEqual(["B", "C", "A"], order("net_profit_desc", self._paare()))

    def test_net_ratio_descending_puts_the_best_payoff_ratio_first(self):
        self.assertEqual(["B", "A", "C"], order("net_ratio_desc", self._paare()))

    def test_the_default_order_is_unchanged(self):
        # gross return desc (B 0.80 > A 0.50 > C 0.40): the historical default
        self.assertEqual(["B", "A", "C"], order("rendite_desc", self._paare()))


class TestMissingValuesSortLast(unittest.TestCase):

    def _paare(self):
        return [
            _Pair("ohne", 240.0, net_loss=None, net_profit=None, ratio=None),
            _Pair("klein", 245.0, net_loss=100.0, net_profit=20.0, ratio=20.0),
            _Pair("gross", 250.0, net_loss=400.0, net_profit=300.0, ratio=75.0),
        ]

    def test_a_row_without_a_number_never_looks_like_the_best_or_the_worst(self):
        for mode in ("max_loss_asc", "net_profit_desc", "net_ratio_desc"):
            with self.subTest(mode=mode):
                self.assertEqual("ohne", order(mode, self._paare())[-1])

    def test_two_rows_without_a_number_keep_a_deterministic_order(self):
        paare = self._paare() + [_Pair("auch-ohne", 230.0, net_loss=None,
                                       net_profit=None, ratio=None)]
        # ties among unknowns resolve by symbol, so the list is reproducible
        self.assertEqual(["auch-ohne", "ohne"],
                         order("net_ratio_desc", paare)[-2:])


class TestTiesAreDeterministic(unittest.TestCase):

    def test_equal_numbers_fall_back_to_symbol_then_strike(self):
        paare = [
            _Pair("MSFT", 250.0, net_loss=100.0, net_profit=50.0, ratio=50.0),
            _Pair("AAPL", 260.0, net_loss=100.0, net_profit=50.0, ratio=50.0),
            _Pair("AAPL", 240.0, net_loss=100.0, net_profit=50.0, ratio=50.0),
        ]
        for mode in ("max_loss_asc", "net_profit_desc", "net_ratio_desc"):
            with self.subTest(mode=mode):
                # same symbol -> the lower strike first; then the symbol
                self.assertEqual([("AAPL", 240.0), ("AAPL", 260.0), ("MSFT", 250.0)],
                                 [(p.symbol, p.long.asset.strike)
                                  for p in sorted(paare, key=_vertical_sort_key(mode))])

    def test_the_same_input_produces_the_same_list_twice(self):
        paare = [_Pair("X", 250.0, net_loss=100.0, net_profit=50.0, ratio=50.0),
                 _Pair("Y", 250.0, net_loss=100.0, net_profit=50.0, ratio=50.0),
                 _Pair("Z", 250.0, net_loss=100.0, net_profit=50.0, ratio=50.0)]
        for mode in SORT_MODES:
            with self.subTest(mode=mode):
                einmal = order(mode, paare)
                zweimal = order(mode, paare)
                self.assertEqual(einmal, zweimal)          # same input, same list
                if mode != "none":
                    # a real order does not depend on the input order at all
                    self.assertEqual(einmal, order(mode, list(reversed(paare))))

    def test_the_chain_order_mode_keeps_what_it_was_given(self):
        paare = [_Pair("Z", 250.0, net_loss=100.0, net_profit=50.0, ratio=50.0),
                 _Pair("A", 250.0, net_loss=100.0, net_profit=50.0, ratio=50.0)]
        self.assertEqual(["Z", "A"], order("none", paare))


class TestTheKeyReadsTheCriteriaItWasGiven(unittest.TestCase):
    """Regression (live chain, 2026-11-11): three orders produced the same rows.

    Cause: the net orders called ``net_max_profit()`` without the criteria, which
    returns None (no size, no fee) - every key became "no value", the sort fell
    through to the symbol tie-break and all three modes printed the same list.
    """

    def test_the_net_orders_pass_the_criteria_through(self):
        kennung = object()
        for mode in ("max_loss_asc", "net_profit_desc", "net_ratio_desc"):
            with self.subTest(mode=mode):
                paar = _Pair("A", 240.0, net_loss=100.0, net_profit=50.0, ratio=50.0)
                _vertical_sort_key(mode, kennung)(paar)
                self.assertIn(kennung, paar.seen_criteria)

    def test_the_three_orders_really_sort_differently(self):
        # the same fixture, three questions - three different first rows
        paare = TestTheNetOrders()._paare()
        self.assertNotEqual(order("net_profit_desc", paare),
                            order("net_ratio_desc", paare))
        self.assertNotEqual(order("max_loss_asc", paare),
                            order("net_profit_desc", paare))


class TestTheLabelsDoNotPromiseAnything(unittest.TestCase):

    def test_no_label_claims_a_probability_or_an_expected_return(self):
        verboten = ("wahrscheinlichkeit", "erwartete rendite", "erwarteter gewinn",
                    "prognose", "trefferquote")
        for mode, label in SORT_LABELS.items():
            with self.subTest(mode=mode):
                for wort in verboten:
                    self.assertNotIn(wort, label.lower())

    def test_the_three_new_orders_are_offered_by_the_page_helper(self):
        for mode in ("max_loss_asc", "net_profit_desc", "net_ratio_desc"):
            self.assertIn(mode, SORT_MODES)
            self.assertIn(mode, SORT_LABELS)


if __name__ == "__main__":
    unittest.main()
