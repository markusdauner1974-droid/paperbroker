"""The three-year risk of a series, computed from the closes the page already holds."""

import datetime
import math
import unittest

import app


def series(values, start="2023-11-24"):
    """Daily closes, oldest first, on consecutive calendar days."""
    day = datetime.date.fromisoformat(start)
    rows = []
    for value in values:
        rows.append([day.isoformat(), value, value, 0.0])
        day += datetime.timedelta(days=1)
    return rows


class PriceRiskTests(unittest.TestCase):
    def test_a_flat_series_has_no_volatility_and_no_drawdown(self):
        risk = app.price_risk(series([100.0] * 200)) or {}
        self.assertAlmostEqual(0.0, risk["annualized_volatility"], places=6)
        self.assertAlmostEqual(0.0, risk["max_drawdown"]["value"], places=6)
        self.assertAlmostEqual(0.0, risk["annualized_return"], places=6)
        self.assertEqual("eigene Kurse", risk["source"])

    def test_the_window_is_the_three_years_the_label_claims(self):
        risk = app.price_risk(series([100.0] * 1000)) or {}
        self.assertLessEqual(risk["returns"], app.RISK_WINDOW_DAYS - 1)
        self.assertEqual(756, len(series([100.0] * 1000)[-app.RISK_WINDOW_DAYS:]))

    def test_a_known_drawdown_is_reported_with_its_marks(self):
        values = [100.0, 120.0, 90.0, 130.0] + [130.0] * 200
        risk = app.price_risk(series(values)) or {}
        # 90 out of a peak of 120 is a quarter gone
        self.assertAlmostEqual(-0.25, risk["max_drawdown"]["value"], places=6)
        self.assertEqual("2023-11-25", risk["max_drawdown"]["peak"], "the high the drop started from")
        self.assertEqual("2023-11-26", risk["max_drawdown"]["trough"])

    def test_volatility_matches_the_doubled_step_of_a_zigzag(self):
        values = []
        price = 100.0
        for index in range(200):
            price *= 1.01 if index % 2 else 0.99
            values.append(price)
        risk = app.price_risk(series(values)) or {}
        expected = math.log(1.01 / 0.99) / 2 * math.sqrt(252)
        self.assertAlmostEqual(expected, risk["annualized_volatility"], places=3)

    def test_a_short_series_says_nothing(self):
        """Three months are the floor - below that a volatility is a guess, not a figure."""
        self.assertIsNone(app.price_risk(series([100.0] * 30)))
        self.assertIsNone(app.price_risk([]))
        self.assertIsNone(app.price_risk(None))

    def test_zero_and_missing_prices_are_skipped_not_divided(self):
        rows = series([100.0] * 100)
        rows[50][1] = 0
        rows[51][1] = None
        risk = app.price_risk(rows) or {}
        self.assertTrue(math.isfinite(risk["annualized_volatility"]))

    def test_the_source_is_named_so_the_page_can_label_it(self):
        self.assertEqual("eigene Kurse", (app.price_risk(series([100.0] * 200)) or {})["source"])
