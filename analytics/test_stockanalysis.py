"""The second quarterly source: reading the table, and the twelve month comparison.

Yahoo hands over five or six quarters for a European paper and none at all for L'Oréal, so the page
could not put one twelve month window against the window before it. stockanalysis.com keeps twenty
quarters as a plain table.

Checked here:

* the table is read the way it is published - periods newest first, thousands separators, a dash for
  a missing figure
* four quarters are compared with the four before them, two half years with the two before them
* a series that cannot carry two full windows says so instead of comparing nine months with twelve
"""

import importlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stockanalysis  # noqa: E402

TABLE = """
<table>
<tr><th>Fiscal Quarter</th><th>Q3 2026</th><th>Q2 2026</th><th>Q1 2026</th><th>Q4 2025</th>
    <th>Q3 2025</th><th>Q2 2025</th><th>Q1 2025</th><th>Q4 2024</th><th>Q3 2024</th></tr>
<tr><th>Period Ending</th><td>Sep '26 Sep 30, 2026</td><td>Jun '26 Jun 30, 2026</td>
    <td>Mar '26 Mar 31, 2026</td><td>Dec '25 Dec 31, 2025</td><td>Sep '25 Sep 30, 2025</td>
    <td>Jun '25 Jun 30, 2025</td><td>Mar '25 Mar 31, 2025</td><td>Dec '24 Dec 31, 2024</td>
    <td>Sep '24 Sep 30, 2024</td></tr>
<tr><th>Revenue</th><td>12,000</td><td>10,000</td><td>9,500</td><td>8,500</td>
    <td>9,000</td><td>8,000</td><td>7,500</td><td>7,500</td><td>-</td></tr>
<tr><th>EPS (Diluted)</th><td>2.40</td><td>2.00</td><td>1.90</td><td>1.70</td>
    <td>1.80</td><td>1.60</td><td>1.50</td><td>1.50</td><td>1.40</td></tr>
</table>
"""

HALF_YEARS = """
<table>
<tr><th>Fiscal Quarter</th><th>H1 2026</th><th>H2 2025</th><th>H1 2025</th><th>H2 2024</th></tr>
<tr><th>Period Ending</th><td>Jun 30, 2026</td><td>Dec 31, 2025</td>
    <td>Jun 30, 2025</td><td>Dec 31, 2024</td></tr>
<tr><th>Revenue</th><td>24,000</td><td>22,000</td><td>22,000</td><td>20,000</td></tr>
<tr><th>EPS (Diluted)</th><td>2.20</td><td>2.00</td><td>2.00</td><td>1.80</td></tr>
</table>
"""


class ParsingTests(unittest.TestCase):
    def test_the_table_is_read_newest_first(self):
        periods = stockanalysis.parse(TABLE)
        self.assertEqual(len(periods), 9)
        self.assertEqual(periods[0]["period_end"], "2026-09-30")
        self.assertEqual(periods[-1]["period_end"], "2024-09-30")

    def test_thousands_separators_and_dashes(self):
        periods = stockanalysis.parse(TABLE)
        self.assertEqual(periods[0]["revenue"], 12000.0)
        self.assertIsNone(periods[-1]["revenue"])
        self.assertEqual(periods[0]["eps"], 2.40)

    def test_a_page_without_the_table_reads_as_nothing(self):
        self.assertEqual(stockanalysis.parse("<html><body>nichts</body></html>"), [])


class AddressTests(unittest.TestCase):
    def test_german_listings_use_the_xetra_path(self):
        self.assertEqual(stockanalysis.url_for("SAP.DE"),
                         "https://stockanalysis.com/quote/etr/SAP/financials/income-statement/?p=quarterly")

    def test_paris_uses_the_euronext_path(self):
        self.assertIn("/quote/epa/OR/", stockanalysis.url_for("OR.PA") or "")

    def test_a_us_ticker_uses_the_stocks_path(self):
        self.assertIn("/stocks/aapl/", stockanalysis.url_for("AAPL") or "")

    def test_an_unknown_exchange_is_refused(self):
        self.assertIsNone(stockanalysis.url_for("XYZ.QQ"))


class GrowthTests(unittest.TestCase):
    def test_four_quarters_against_the_four_before(self):
        growth = stockanalysis.twelve_month_growth(stockanalysis.parse(TABLE))
        # 12,000 + 10,000 + 9,500 + 8,500 = 40,000 against 9,000 + 8,000 + 7,500 + 7,500 = 32,000
        self.assertAlmostEqual(growth["revenue"], 40_000 / 32_000 - 1, places=6)
        self.assertEqual(growth["step_days"], 92)
        self.assertEqual(growth["periods"], 9)

    def test_an_earnings_figure_is_read_the_same_way(self):
        growth = stockanalysis.twelve_month_growth(stockanalysis.parse(TABLE))
        self.assertAlmostEqual(growth["eps"], (2.4 + 2.0 + 1.9 + 1.7) / (1.8 + 1.6 + 1.5 + 1.5) - 1,
                               places=6)

    def test_half_years_are_compared_two_against_two(self):
        growth = stockanalysis.twelve_month_growth(stockanalysis.parse(HALF_YEARS))
        self.assertEqual(growth["step_days"], 181)
        self.assertAlmostEqual(growth["revenue"], 46_000 / 42_000 - 1, places=6)
        self.assertAlmostEqual(growth["eps"], 4.2 / 3.8 - 1, places=6)

    def test_five_quarters_carry_no_twelve_month_comparison(self):
        periods = stockanalysis.parse(TABLE)[:5]
        growth = stockanalysis.twelve_month_growth(periods)
        self.assertIsNone(growth["revenue"])
        self.assertIsNone(growth["eps"])

    def test_a_hole_in_the_window_leaves_the_row_open(self):
        periods = stockanalysis.parse(TABLE)
        periods[3]["period_end"] = "2025-06-30"  # a quarter in the wrong place
        growth = stockanalysis.twelve_month_growth(periods)
        self.assertIsNone(growth["revenue"])

    def test_no_figure_means_no_growth(self):
        periods = stockanalysis.parse(TABLE)
        periods[1]["revenue"] = None
        self.assertIsNone(stockanalysis.twelve_month_growth(periods)["revenue"])


class StoreTests(unittest.TestCase):
    def test_the_series_survives_a_round_trip_and_ages_out(self):
        import tempfile

        import store

        path = Path(tempfile.mkdtemp()) / "t.db"
        periods = stockanalysis.parse(TABLE)
        store.write_quarterly_source("SAP.DE", periods, path)
        with store.connect(path) as conn:
            back = store.read_quarterly_source(conn, "SAP.DE")
            self.assertEqual([row["period_end"] for row in back],
                             [row["period_end"] for row in periods])
            self.assertIsNone(store.read_quarterly_source(conn, "SAP.DE", max_age_h=0))


class OwnDataWinsTests(unittest.TestCase):
    """The page computes the figure itself whenever it can; the second source only fills in."""

    @classmethod
    def setUpClass(cls):
        cls.app = importlib.import_module("app")

    def test_no_question_asked_when_eight_quarters_are_there(self):
        quarters = {}
        for year, months in ((2026, (3, 6, 9, 12)), (2025, (3, 6, 9, 12))):
            for month in months:
                quarters[f"{year}-{month:02d}-28"] = {"totalrevenue": 10.0}
        self.assertGreaterEqual(self.app.consecutive_quarters(quarters), self.app.QUARTER_WINDOW)
        self.assertIsNone(self.app.long_quarterly_growth("SAP.DE", {"quarterly": quarters}))


if __name__ == "__main__":
    unittest.main()
