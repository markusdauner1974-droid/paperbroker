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
import sqlite3
import sys
import threading
import time
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
<tr><th>Operating Income</th><td>3,600</td><td>3,000</td><td>2,850</td><td>2,550</td>
    <td>2,700</td><td>2,400</td><td>2,250</td><td>2,250</td><td>-</td></tr>
</table>
"""

HALF_YEARS = """
<table>
<tr><th>Fiscal Quarter</th><th>H1 2026</th><th>H2 2025</th><th>H1 2025</th><th>H2 2024</th></tr>
<tr><th>Period Ending</th><td>Jun 30, 2026</td><td>Dec 31, 2025</td>
    <td>Jun 30, 2025</td><td>Dec 31, 2024</td></tr>
<tr><th>Revenue</th><td>24,000</td><td>22,000</td><td>22,000</td><td>20,000</td></tr>
<tr><th>EPS (Diluted)</th><td>2.20</td><td>2.00</td><td>2.00</td><td>1.80</td></tr>
<tr><th>Operating Income</th><td>5,200</td><td>4,800</td><td>4,400</td><td>4,000</td></tr>
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

    def test_a_header_that_does_not_line_up_is_refused(self):
        # A colspan in the header shifts every figure against its date without raising.
        shifted = TABLE.replace("<th>Fiscal Quarter</th>", "<th>Fiscal Quarter</th><th>Extra</th>", 1)
        self.assertEqual(stockanalysis.parse(shifted), [])

    def test_an_extra_note_column_is_refused(self):
        noted = TABLE.replace("<td>1.80</td>", "<td>1.80</td><td>Hinweis</td>", 1)
        self.assertEqual(stockanalysis.parse(noted), [])


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

    def test_a_share_class_is_read_from_the_us_listing(self):
        # BRK.B is not a Frankfurt listing; the letter after the dot is a share class.
        self.assertEqual(stockanalysis.url_for("BRK.B"),
                         "https://stockanalysis.com/stocks/brk.b/financials/income-statement/?p=quarterly")


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

    def test_the_operating_income_is_read_with_the_other_figures(self):
        periods = stockanalysis.parse(TABLE)
        self.assertEqual(periods[0]["operating_income"], 3600.0)
        self.assertIsNone(periods[-1]["operating_income"])

    def test_the_twelve_month_sums_are_whole_currency_units(self):
        """The site publishes millions; a balance sheet figure is divided in whole units."""
        growth = stockanalysis.twelve_month_growth(stockanalysis.parse(TABLE))
        self.assertEqual(growth["revenue_total"], 40_000 * stockanalysis.MILLIONS)
        self.assertEqual(growth["operating_income_total"], (3600 + 3000 + 2850 + 2550)
                         * stockanalysis.MILLIONS)

    def test_the_sums_survive_a_missing_comparison_window(self):
        """Four quarters carry no growth, but the twelve month total is still there."""
        growth = stockanalysis.twelve_month_growth(stockanalysis.parse(TABLE)[:4])
        self.assertIsNone(growth["revenue"])
        self.assertEqual(growth["revenue_total"], 40_000 * stockanalysis.MILLIONS)

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


class BudgetTests(unittest.TestCase):
    """No request waits on the second source: the body is capped, the call is clocked, the rest runs later."""

    def setUp(self):
        self.original = stockanalysis.urllib.request.urlopen
        self.calls = []

    def tearDown(self):
        stockanalysis.urllib.request.urlopen = self.original

    def stub(self, body: bytes):
        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return body[:size] if size and size > 0 else body

        def opener(request, timeout=None):
            self.calls.append(timeout)
            return Answer()

        stockanalysis.urllib.request.urlopen = opener

    def test_a_body_over_the_cap_is_refused(self):
        self.stub(b"x" * (stockanalysis.MAX_BYTES + 10))
        with self.assertRaises(stockanalysis.SourceUnavailable):
            stockanalysis.fetch("SAP.DE")

    def test_no_budget_means_no_call_at_all(self):
        self.stub(b"egal")
        with self.assertRaises(stockanalysis.SourceUnavailable):
            stockanalysis.fetch("SAP.DE", budget_s=0)
        self.assertEqual(self.calls, [])

    def test_the_socket_timeout_carries_the_budget(self):
        self.stub(TABLE.encode())
        stockanalysis.fetch("SAP.DE", budget_s=2.5)
        self.assertEqual(self.calls, [2.5])


class BackgroundTests(unittest.TestCase):
    def test_a_slow_source_is_loaded_afterwards_and_asked_once(self):
        import tempfile

        import store

        path = Path(tempfile.mkdtemp()) / "t.db"
        gate = threading.Event()
        started = []

        def slow_fetch(symbol, budget_s=None):
            started.append(symbol)
            gate.wait(5)
            return stockanalysis.parse(TABLE)

        original = stockanalysis.fetch
        stockanalysis.fetch = slow_fetch
        try:
            first = stockanalysis.refresh_in_background("SAP.DE", store, path)
            second = stockanalysis.refresh_in_background("SAP.DE", store, path)
            self.assertTrue(first)
            self.assertFalse(second, "ein zweiter Anlauf für denselben Ticker darf nicht starten")
            gate.set()
            # Give the writer the file first: the poll itself opens the store, and a tight loop of
            # readers starves the one write we are waiting for.
            stored = None
            for _ in range(20):
                time.sleep(0.25)
                try:
                    with store.connect(path) as conn:
                        stored = store.read_quarterly_source(conn, "SAP.DE")
                except sqlite3.OperationalError:
                    continue
                if stored:
                    break
            self.assertTrue(stored, "der Hintergrundlauf muss die Reihe ablegen")
            self.assertEqual(started, ["SAP.DE"])
        finally:
            stockanalysis.fetch = original

    def test_a_series_that_cannot_be_fetched_names_the_reason(self):
        import tempfile

        import store

        path = Path(tempfile.mkdtemp()) / "t.db"
        original = stockanalysis.fetch

        def never(symbol, budget_s=None):
            raise stockanalysis.SourceUnavailable("kein Netz")

        stockanalysis.fetch = never
        try:
            answer = stockanalysis.growth("SAP.DE", store_module=store, path=path)
        finally:
            stockanalysis.fetch = original
        self.assertIsNone(answer["revenue"])
        self.assertIn("Hintergrund", answer["reason"])


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
            self.assertEqual(back[0]["operating_income"], 3600.0)

    def test_a_store_written_before_the_operating_income_gains_the_column(self):
        import sqlite3
        import tempfile

        import store

        path = Path(tempfile.mkdtemp()) / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute("""CREATE TABLE quarterly_source (symbol TEXT, period_end TEXT, label TEXT,
                            revenue REAL, eps REAL, fetched_at TEXT NOT NULL,
                            PRIMARY KEY (symbol, period_end))""")
            conn.execute("INSERT INTO quarterly_source VALUES ('SAP.DE','2026-09-30','Q3 2026',10270,1.94,'2026-11-23T00:00:00Z')")
        with store.connect(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(quarterly_source)")}
            self.assertIn("operating_income", columns, "die Spalte muss beim Öffnen ergänzt werden")
            kept = store.read_quarterly_source(conn, "SAP.DE")
            self.assertEqual(kept[0]["revenue"], 10270.0, "die vorhandenen Zeilen bleiben")
            self.assertIsNone(kept[0]["operating_income"])


class StaleSeriesTests(unittest.TestCase):
    def test_a_series_without_operating_income_is_read_again(self):
        """A week-old row set from before the column existed must not block the new figures."""
        import tempfile

        import store

        path = Path(tempfile.mkdtemp()) / "t.db"
        old_rows = [{"period_end": row["period_end"], "label": row["label"],
                     "revenue": row["revenue"], "eps": row["eps"], "operating_income": None}
                    for row in stockanalysis.parse(TABLE)]
        store.write_quarterly_source("SAP.DE", old_rows, path)
        original = stockanalysis.fetch
        calls = []

        def fake_fetch(symbol, budget_s=None):
            calls.append(symbol)
            return stockanalysis.parse(TABLE)

        stockanalysis.fetch = fake_fetch
        try:
            fresh = stockanalysis.series("SAP.DE", store, path)
        finally:
            stockanalysis.fetch = original
        self.assertEqual(calls, ["SAP.DE"], "die veraltete Reihe muss neu geholt werden")
        self.assertIsNotNone(fresh[0]["operating_income"])


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

    def test_the_second_source_never_fails_the_page(self):
        original = self.app.stockanalysis.growth

        def broken(*args, **kwargs):
            raise RuntimeError("kaputt")

        self.app.stockanalysis.growth = broken
        try:
            answer = self.app.long_quarterly_growth("SAP.DE", {"quarterly": {}})
        finally:
            self.app.stockanalysis.growth = original
        self.assertIsNone(answer["revenue"])
        self.assertEqual(answer["reason"], "Quartalsreihe nicht abrufbar")


if __name__ == "__main__":
    unittest.main()
