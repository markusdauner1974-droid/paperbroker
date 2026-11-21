"""The data state is named, not papered over.

A score built on four fiscal years is a different score from one built on six, and a TTM figure
needs eight consecutive quarters. The page shows which block is short, where it came from and what
stops it from being longer - so a missing value reads as a missing value and not as a company
without figures.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402

QUARTER_DAYS = ["2026-07-31", "2026-04-30", "2026-01-31", "2025-10-31", "2025-07-31",
                "2025-04-30", "2025-01-31", "2024-10-31", "2024-07-31", "2024-04-30"]


def income(years, empty=()):
    rows = {f"{2026 - step}-01-31": {"totalrevenue": 100.0, "netincome": 5.0}
            for step in range(years)}
    for day in empty:
        rows[day] = {}
    return rows


def quarter_rows(count, revenue=True):
    return {day: ({"totalrevenue": 50.0} if revenue else {}) for day in QUARTER_DAYS[:count]}


def statements(years=4, quarters=8, revenue=True, empty=()):
    return {"income": income(years, empty), "balance": {}, "cash": {},
            "quarterly": quarter_rows(quarters, revenue)}


class DataGapTest(unittest.TestCase):
    def test_a_missing_key_is_named_as_the_reason(self):
        gaps = app.statement_gaps(statements(4), None, False, filings_key=False)
        self.assertEqual(1, len(gaps))
        self.assertEqual("Jahresabschlüsse", gaps[0]["block"])
        self.assertEqual((4, app.SEC_YEARS), (gaps[0]["have"], gaps[0]["need"]))
        self.assertIn("AKTIENANALYSE_FILINGS_KEY", gaps[0]["reason"])

    def test_with_a_key_the_reason_moves_to_the_report(self):
        gaps = app.statement_gaps(statements(4), None, False, filings_key=True)
        self.assertIn("Originalbericht", gaps[0]["reason"])

    def test_a_complete_series_raises_no_hint(self):
        self.assertEqual([], app.statement_gaps(statements(6), None, False, True))

    def test_the_source_is_reported_as_it_is(self):
        for source, expected in (("SEC EDGAR", "SEC EDGAR"), (None, "Yahoo Finance")):
            with self.subTest(source=source):
                gaps = app.statement_gaps(statements(4), source, False, True)
                self.assertEqual(expected, gaps[0]["source"])
        gaps = app.statement_gaps(statements(4), None, True, True)
        self.assertIn("XBRL", gaps[0]["source"])

    def test_the_quarter_block_needs_eight_not_four(self):
        """The page builds its TTM growth from eight consecutive quarters."""
        gaps = app.statement_gaps(statements(6, quarters=6), None, False, True)
        self.assertEqual(["Quartale"], [gap["block"] for gap in gaps])
        self.assertEqual((6, 8), (gaps[0]["have"], gaps[0]["need"]))
        self.assertIn("acht", gaps[0]["reason"])

    def test_quarters_without_revenue_do_not_count(self):
        gaps = app.statement_gaps(statements(6, quarters=8, revenue=False), None, False, True)
        self.assertEqual(["Quartale"], [gap["block"] for gap in gaps])
        self.assertEqual(0, gaps[0]["have"])

    def test_a_hole_in_the_quarter_series_stops_the_run(self):
        rows = quarter_rows(8)
        del rows["2025-10-31"]  # 2026-01-31 to 2025-07-31 is 184 days, not a quarter
        gaps = app.statement_gaps({**statements(6), "quarterly": rows}, None, False, True)
        self.assertEqual(["Quartale"], [gap["block"] for gap in gaps])
        self.assertEqual(3, gaps[0]["have"])

    def test_an_empty_fiscal_year_is_named_with_its_date(self):
        gaps = app.statement_gaps(statements(6, empty=("2019-01-31",)), None, False, True)
        self.assertEqual(["Geschäftsjahre ohne Umsatz"], [gap["block"] for gap in gaps])
        self.assertIn("2019-01-31", gaps[0]["reason"])


class ConsecutiveQuartersTest(unittest.TestCase):
    def test_all_eight_count(self):
        self.assertEqual(8, app.consecutive_quarters(quarter_rows(8)))

    def test_an_unreadable_date_ends_the_run_instead_of_raising(self):
        """The count may land anywhere below the window; what matters is that it does not raise."""
        rows = quarter_rows(8)
        rows["2025/07/31"] = rows.pop("2025-07-31")
        self.assertLess(app.consecutive_quarters(rows), app.QUARTER_WINDOW)


if __name__ == "__main__":
    unittest.main()
