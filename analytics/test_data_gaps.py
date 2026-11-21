"""The data state is named, not papered over.

A score built on four fiscal years is a different score from one built on six. The page shows
which block is short, where the short block came from, and what stops it from being longer - so a
missing value reads as a missing value and not as a company without figures.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402


def income(years):
    return {f"{2026 - step}-01-31": {"totalrevenue": 100.0, "netincome": 5.0}
            for step in range(years)}


def statements(years=4, quarters=6, empty=()):
    rows = income(years)
    for day in empty:
        rows[day] = {}
    return {"income": rows, "balance": {}, "cash": {},
            "quarterly": {f"q{index}": {} for index in range(quarters)}}


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
        self.assertEqual([], app.statement_gaps(statements(6, quarters=8), None, False, True))

    def test_the_source_is_reported_as_it_is(self):
        for source, expected in (("SEC EDGAR", "SEC EDGAR"), (None, "Yahoo Finance")):
            with self.subTest(source=source):
                gaps = app.statement_gaps(statements(4), source, False, True)
                self.assertEqual(expected, gaps[0]["source"])
        gaps = app.statement_gaps(statements(4), None, True, True)
        self.assertIn("XBRL", gaps[0]["source"])

    def test_few_quarters_are_named_too(self):
        gaps = app.statement_gaps(statements(6, quarters=2), None, False, True)
        self.assertEqual(["Quartale"], [gap["block"] for gap in gaps])

    def test_an_empty_fiscal_year_is_named_with_its_date(self):
        gaps = app.statement_gaps(statements(6, quarters=8, empty=("2019-01-31",)),
                                  None, False, True)
        self.assertEqual(["Geschäftsjahre ohne Umsatz"], [gap["block"] for gap in gaps])
        self.assertIn("2019-01-31", gaps[0]["reason"])


if __name__ == "__main__":
    unittest.main()
