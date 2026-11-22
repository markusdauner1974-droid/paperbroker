"""The three-year risk of a series, computed from the closes the page already holds."""

import datetime
import math
import unittest

import app


def series(values, start="2023-11-24"):
    """Daily closes, oldest first, on trading days only - one row per weekday.

    Weekends are skipped on purpose: a series that carried a row for every calendar day would cover
    three years after 756 rows, while a real one needs about 1050 days for them.
    """
    day = datetime.date.fromisoformat(start)
    rows = []
    for value in values:
        while day.weekday() > 4:
            day += datetime.timedelta(days=1)
        rows.append([day.isoformat(), value, value, 0.0])
        day += datetime.timedelta(days=1)
    return rows


class PriceRiskTests(unittest.TestCase):
    def test_a_flat_series_has_no_volatility_and_no_drawdown(self):
        risk = app.price_risk(series([100.0] * 800)) or {}
        self.assertAlmostEqual(0.0, risk["annualized_volatility"], places=6)
        self.assertAlmostEqual(0.0, risk["max_drawdown"]["value"], places=6)
        self.assertAlmostEqual(0.0, risk["annualized_return"], places=6)
        self.assertEqual("eigene Kurse", risk["source"])

    def test_the_window_is_the_three_years_the_label_claims(self):
        risk = app.price_risk(series([100.0] * 1000)) or {}
        self.assertLessEqual(risk["returns"], app.RISK_WINDOW_DAYS - 1)
        self.assertTrue(app.spans_the_years(risk), "the window must cover the years it claims")

    def test_a_known_drawdown_is_reported_with_its_marks(self):
        # The drop sits at the end of the series: the window keeps the newest 756 rows, so a drop at
        # the beginning would fall out of it - exactly as it should, it is not in the window.
        rows = series([100.0] * 800)
        rows[-3][1], rows[-2][1], rows[-1][1] = 120.0, 90.0, 100.0
        risk = app.price_risk(rows) or {}
        self.assertAlmostEqual(-0.25, risk["max_drawdown"]["value"], places=6)
        self.assertEqual(rows[-3][0], risk["max_drawdown"]["peak"], "the high the drop started from")
        self.assertEqual(rows[-2][0], risk["max_drawdown"]["trough"])

    def test_volatility_matches_the_doubled_step_of_a_zigzag(self):
        values = []
        price = 100.0
        for index in range(800):
            price *= 1.01 if index % 2 else 0.99
            values.append(price)
        risk = app.price_risk(series(values)) or {}
        expected = math.log(1.01 / 0.99) / 2 * math.sqrt(252)
        self.assertAlmostEqual(expected, risk["annualized_volatility"], places=3)

    def test_many_closes_in_a_short_time_are_not_three_years(self):
        """Kody: a paper listed a few months ago collected 62 closes and got a three-year label."""
        rows = series([100.0 + index for index in range(62)], start="2026-06-01")
        self.assertIsNone(app.price_risk(rows), "a few months are not three years")

    def test_a_series_of_three_years_passes(self):
        self.assertIsNotNone(app.price_risk(series([100.0] * 800, start="2023-11-24")))

    def test_the_secondary_source_must_span_them_too(self):
        """Its answer carries years: 3 while the window it reports covers one."""
        one_year = {"start": "2025-11-21", "end": "2026-11-20", "annualized_volatility": 0.45}
        three_years = {"start": "2023-11-21", "end": "2026-11-20", "annualized_volatility": 0.45}
        self.assertFalse(app.spans_the_years(one_year))
        self.assertTrue(app.spans_the_years(three_years))
        self.assertFalse(app.spans_the_years({}), "no window, no claim")
        self.assertFalse(app.spans_the_years({"start": None, "end": None}))

    def test_a_short_series_says_nothing(self):
        """Three months are the floor - below that a volatility is a guess, not a figure."""
        self.assertIsNone(app.price_risk(series([100.0] * 30)))
        self.assertIsNone(app.price_risk([]))
        self.assertIsNone(app.price_risk(None))

    def test_zero_and_missing_prices_are_skipped_not_divided(self):
        rows = series([100.0] * 800)
        rows[50][1] = 0
        rows[51][1] = None
        risk = app.price_risk(rows) or {}
        self.assertTrue(math.isfinite(risk["annualized_volatility"]))

    def test_the_source_is_named_so_the_page_can_label_it(self):
        self.assertEqual("eigene Kurse", (app.price_risk(series([100.0] * 800)) or {})["source"])
