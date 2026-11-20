"""Tests for the balance sheet part of the SEC import.

Two rules are pinned here: the short- and long-term debt positions must arrive
without being counted twice when the filing also states a total, and the invested
capital for the ROIC only exists when all three inputs are reported.
"""

import importlib
import sys
import unittest
from datetime import date
from pathlib import Path

AKT_DIR = Path(__file__).resolve().parent
if str(AKT_DIR) not in sys.path:
    sys.path.insert(0, str(AKT_DIR))
sec = importlib.import_module("sec_statements")

CUTOFF = date(2026, 6, 30)
FY = ("2025-01-01", "2025-12-31")


def fact(value, end, start=None, form="10-K", filed="2026-02-01"):
    item = {"end": end, "val": value, "form": form, "filed": filed}
    if start:
        item["start"] = start
    return item


def companyfacts(**concepts):
    return {"facts": {"us-gaap": {name: {"units": {"USD": rows}} for name, rows in concepts.items()}}}


def year(value, end=FY[1], start=FY[0]):
    return [fact(value, end, start)]


def quarterly(values):
    ends = [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30")]
    return [fact(value, end, start) for value, (start, end) in zip(values, ends[:len(values)], strict=True)]


def instant(value, end=FY[1]):
    return [fact(value, end)]


def statements(**concepts):
    base = {"Revenues": year(800_000), "NetIncomeLoss": year(50_000)}
    base.update(concepts)
    return sec.parse_statements(companyfacts(**base), CUTOFF)


class BalanceDebtTest(unittest.TestCase):
    def test_a_stated_total_is_used_and_the_parts_are_not_added_again(self):
        result = statements(
            DebtAndCapitalLeaseObligations=instant(120_000),
            LongTermDebtAndCapitalLeaseObligationsNoncurrent=instant(80_000),
            LongTermDebtAndCapitalLeaseObligationsCurrent=instant(15_000),
            CommercialPaper=instant(5_000))
        row = result["balance"]["2025-12-31"]
        self.assertEqual(row["totaldebt"], 120_000)
        self.assertEqual(row["longtermdebt"], 80_000)
        self.assertEqual(row["currentlongtermdebt"], 15_000)
        self.assertEqual(row["shorttermborrowings"], 5_000)

    def test_without_a_total_the_parts_are_added_once(self):
        result = statements(
            LongTermDebtAndCapitalLeaseObligationsNoncurrent=instant(80_000),
            LongTermDebtAndCapitalLeaseObligationsCurrent=instant(15_000),
            CommercialPaper=instant(5_000))
        self.assertEqual(result["balance"]["2025-12-31"]["totaldebt"], 100_000)

    def test_a_current_part_alone_is_not_declared_as_the_total(self):
        """Long-term debt without its current part is not a total: better open than wrong."""
        result = statements(LongTermDebtAndCapitalLeaseObligationsNoncurrent=instant(80_000))
        row = result["balance"]["2025-12-31"]
        self.assertEqual(row["longtermdebt"], 80_000)
        self.assertNotIn("totaldebt", row)

    def test_the_older_long_term_tag_is_read_as_well(self):
        result = statements(LongTermDebtNoncurrent=instant(70_000),
                            LongTermDebtCurrent=instant(10_000))
        self.assertEqual(result["balance"]["2025-12-31"]["totaldebt"], 80_000)


class InvestedCapitalTest(unittest.TestCase):
    def test_invested_capital_is_equity_plus_debt_less_cash(self):
        result = statements(
            DebtAndCapitalLeaseObligations=instant(120_000),
            StockholdersEquity=instant(500_000),
            CashAndCashEquivalentsAtCarryingValue=instant(60_000))
        self.assertEqual(result["balance"]["2025-12-31"]["investedcapital"], 560_000)

    def test_without_cash_there_is_no_invested_capital(self):
        result = statements(DebtAndCapitalLeaseObligations=instant(120_000),
                            StockholdersEquity=instant(500_000))
        self.assertNotIn("investedcapital", result["balance"]["2025-12-31"])

    def test_without_debt_there_is_no_invested_capital(self):
        result = statements(StockholdersEquity=instant(500_000),
                            CashAndCashEquivalentsAtCarryingValue=instant(60_000))
        self.assertNotIn("investedcapital", result["balance"]["2025-12-31"])


class QuarterlyDerivationTest(unittest.TestCase):
    def test_the_fourth_quarter_of_revenue_is_the_year_less_three_quarters(self):
        result = statements(Revenues=year(800_000) + quarterly([200_000, 210_000, 215_000]))
        self.assertEqual(result["quarterly"]["2025-12-31"]["totalrevenue"], 175_000)

    def test_a_missing_quarter_keeps_the_fourth_quarter_open(self):
        result = statements(Revenues=year(800_000) + quarterly([200_000, 210_000])[:2])
        self.assertNotIn("2025-12-31", result["quarterly"])

    def test_earnings_per_share_is_never_derived_from_a_remainder(self):
        result = statements(Revenues=year(800_000) + quarterly([200_000, 210_000, 215_000]),
                            EarningsPerShareDiluted=[fact(0.9, "2025-06-30", "2025-04-01",
                                                          form="10-Q", filed="2025-08-01")])
        self.assertIn("2025-12-31", result["quarterly"])
        self.assertNotIn("dilutedeps", result["quarterly"]["2025-12-31"])


class AdditiveOnlyTest(unittest.TestCase):
    def test_operating_income_is_derived_like_revenue(self):
        result = statements(Revenues=year(800_000) + quarterly([200_000, 210_000, 215_000]),
                            OperatingIncomeLoss=year(120_000) + quarterly([30_000, 32_000, 33_000]))
        self.assertEqual(result["quarterly"]["2025-12-31"]["operatingincome"], 25_000)


if __name__ == "__main__":
    unittest.main()
