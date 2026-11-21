"""The standardised export is read into the sections the page uses.

The fixture mirrors the real file: a cover that names the unit, one annual and one interim sheet,
a period-end row, a currency row and a source row. The figures are Einhell's own, so the derived
quarters can be compared with what the company published.
"""

import io
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import statement_export as export
import store
from openpyxl import Workbook

ANNUAL = {
    "2023-12-31": {"Total Revenues": 971.525, "Basic EPS": 8.0},
    "2025-12-31": {"Total Revenues": 1157.714},
}
INTERIM = [
    ("Q1 2024", "2024-03-31", {"Total Revenues": 269.5, "Basic EPS": 1.4}),
    ("H1 2024", "2024-06-30", {"Total Revenues": 576.173, "Basic EPS": 3.1}),
    ("9M 2024", "2024-09-30", {"Total Revenues": 839.345, "Basic EPS": 4.5}),
    ("Q1 2025", "2025-03-31", {"Total Revenues": 302.832, "Basic EPS": 1.8}),
    ("H1 2025", "2025-06-30", {"Total Revenues": 630.224, "Basic EPS": 3.9}),
    ("9M 2025", "2025-09-30", {"Total Revenues": 897.689, "Basic EPS": 5.3}),
]


def workbook(annual=None, interim=None, currency="EUR", units="Millions, except per-share figures and ratios",
             isin="DE000A40ESU3"):
    """A workbook shaped like the provider's export."""
    annual = ANNUAL if annual is None else annual
    interim = INTERIM if interim is None else interim
    book = Workbook()
    cover = book.active
    cover.title = "Cover"
    for row in (("FinancialFilings", ""), ("Einhell Germany AG", ""), ("Units", units),
                ("ISIN", isin), ("Currency note", currency)):
        cover.append(list(row))
    book.create_sheet("Income (Annual)")
    book.create_sheet("Income (Interim)")
    book.create_sheet("Balance (Annual)")
    book.create_sheet("Cash Flow (Annual)")

    def sheet_annual(name, columns):
        sheet = book[name]
        sheet.append(["Name", "", ""])
        sheet.append([])
        sheet.append([])
        # The provider labels an annual column "FY<year>"; the period end carries the date.
        sheet.append(["Line item", *[f"FY{day[:4]}" for day in columns]])
        sheet.append(["Period end", *[datetime.fromisoformat(day) for day in columns]])
        sheet.append(["Currency", *[currency for _ in columns]])
        items = []
        for column in columns:
            items.append(set(annual[column]))
        for item in sorted(set().union(*items)):
            sheet.append([item, *[annual[column].get(item) for column in columns]])
        sheet.append(["Source filing", *["annual-report.pdf" for _ in columns]])

    for name, columns in (("Income (Annual)", sorted(annual)), ("Balance (Annual)", sorted(annual)),
                          ("Cash Flow (Annual)", sorted(annual))):
        sheet_annual(name, columns)

    sheet = book["Income (Interim)"]
    sheet.append(["Name", "", ""])
    sheet.append([])
    sheet.append([])
    sheet.append(["Line item", *[label for label, _, _ in interim]])
    sheet.append(["Period end", *[datetime.fromisoformat(day) for _, day, _ in interim]])
    sheet.append(["Currency", *[currency for _ in interim]])
    for item in sorted({key for _, _, figures in interim for key in figures}):
        sheet.append([item, *[figures.get(item) for _, _, figures in interim]])
    sheet.append(["Source filing", *["interim-report.pdf" for _ in interim]])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = mock.patch.dict(export.os.environ,
                                   {"AKTIENANALYSE_DB": str(Path(self.directory.name) / "test.db"),
                                    "AKTIENANALYSE_FILINGS_KEY": "test-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(export.statement_export.cache_clear)

    def report(self, **kwargs):
        return export.read_export(workbook(**kwargs))

    def test_the_money_is_scaled_by_the_unit_the_cover_names(self):
        sections, gaps = export.sections_from_export(self.report(), "2026-11-20")
        self.assertEqual(1157.714e6, sections["income"]["2025-12-31"]["totalrevenue"])
        self.assertEqual(8.0, sections["income"]["2023-12-31"]["basiceps"], "per-share figures stay as published")
        self.assertEqual([], gaps)

    def test_the_quarters_are_the_difference_of_the_cumulative_columns(self):
        sections, _gaps = export.sections_from_export(self.report(), "2026-11-20")
        quarters = sections["quarterly"]
        self.assertAlmostEqual(306.673e6, quarters["2024-06-30"]["totalrevenue"], places=2)
        self.assertAlmostEqual(263.172e6, quarters["2024-09-30"]["totalrevenue"], places=2)
        self.assertAlmostEqual(327.392e6, quarters["2025-06-30"]["totalrevenue"], places=2)
        self.assertAlmostEqual(267.465e6, quarters["2025-09-30"]["totalrevenue"], places=2)

    def test_the_fourth_quarter_uses_the_published_year(self):
        """Q4 = year - 9M, and the year comes from the annual sheet when the interim has none."""
        sections, _gaps = export.sections_from_export(self.report(), "2026-11-20")
        self.assertAlmostEqual(260.025e6, sections["quarterly"]["2025-12-31"]["totalrevenue"], places=2)

    def test_a_quarter_follows_its_period_end_not_its_label(self):
        """A label that names the wrong year must not move the figure.

        Every fourth quarter of a widely used list is named one year late. The grouping therefore
        comes from the period ends, and the label only says whether a column is cumulative.
        """
        interim = [("FY 2026", "2025-12-31", {"Total Revenues": 1157.714}),
                   ("9M 2025", "2025-09-30", {"Total Revenues": 897.689}),
                   ("H1 2025", "2025-06-30", {"Total Revenues": 630.224}),
                   ("Q1 2025", "2025-03-31", {"Total Revenues": 302.832})]
        sections, _gaps = export.sections_from_export(self.report(interim=interim), "2026-11-20")
        self.assertIn("2025-12-31", sections["quarterly"], "stored under its period end")
        self.assertAlmostEqual(260.025e6, sections["quarterly"]["2025-12-31"]["totalrevenue"], places=2)
        self.assertEqual({"2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"},
                         set(sections["quarterly"]), "no quarter of 2026 may appear")

    def test_a_fiscal_year_that_does_not_end_in_december(self):
        """The quarters hang on the fiscal year of the report, not on the calendar year."""
        annual = {"2025-06-30": {"Total Revenues": 1200.0}}
        interim = [("Q1 2024", "2024-09-30", {"Total Revenues": 300.0}),
                   ("H1 2024", "2024-12-31", {"Total Revenues": 620.0}),
                   ("9M 2024", "2025-03-31", {"Total Revenues": 900.0}),
                   ("FY 2024", "2025-06-30", {"Total Revenues": 1200.0})]
        sections, gaps = export.sections_from_export(self.report(annual=annual, interim=interim), "2026-11-20")
        quarters = sections["quarterly"]
        self.assertAlmostEqual(320.0e6, quarters["2024-12-31"]["totalrevenue"], places=2)
        self.assertAlmostEqual(280.0e6, quarters["2025-03-31"]["totalrevenue"], places=2)
        self.assertAlmostEqual(300.0e6, quarters["2025-06-30"]["totalrevenue"], places=2)
        self.assertEqual([], gaps)

    def test_a_published_single_quarter_is_taken_as_it_is(self):
        interim = [("Q2 2024", "2024-06-30", {"Total Revenues": 306.673})]
        sections, _gaps = export.sections_from_export(self.report(annual={}, interim=interim), "2026-11-20")
        self.assertAlmostEqual(306.673e6, sections["quarterly"]["2024-06-30"]["totalrevenue"], places=2)

    def test_a_year_that_contradicts_the_annual_report_is_dropped(self):
        """The interim sheet names a year of its own; if it disagrees with the annual one, nothing
        of that year is stored. Q4 is built from that very number, so only a second, independent
        year figure can expose the contradiction - and here it does."""
        interim = [("Q1 2025", "2025-03-31", {"Total Revenues": 302.832}),
                   ("H1 2025", "2025-06-30", {"Total Revenues": 630.224}),
                   ("9M 2025", "2025-09-30", {"Total Revenues": 897.689}),
                   ("FY 2025", "2025-12-31", {"Total Revenues": 1100.0})]
        annual = {"2025-12-31": {"Total Revenues": 1157.714}}
        sections, gaps = export.sections_from_export(self.report(annual=annual, interim=interim), "2026-11-20")
        self.assertNotIn("2025-06-30", sections.get("quarterly") or {}, "the chain does not close")
        self.assertTrue(any(gap[1] == "totalrevenue" and "Jahr" in gap[2] for gap in gaps), gaps)

    def test_an_incomplete_chain_keeps_the_quarters_it_has(self):
        """No year column and no ninth month: Q2 is still the difference of two reported figures."""
        interim = [("Q1 2025", "2025-03-31", {"Total Revenues": 302.832}),
                   ("H1 2025", "2025-06-30", {"Total Revenues": 630.224})]
        sections, gaps = export.sections_from_export(self.report(annual={}, interim=interim), "2026-11-20")
        self.assertAlmostEqual(327.392e6, sections["quarterly"]["2025-06-30"]["totalrevenue"], places=2)
        self.assertEqual([], gaps)

    def test_the_cutoff_keeps_later_periods_out(self):
        sections, _gaps = export.sections_from_export(self.report(), "2024-12-31")
        self.assertNotIn("2025-12-31", sections["income"])
        self.assertNotIn("2025-06-30", sections.get("quarterly") or {})

    def test_without_the_key_nothing_is_read(self):
        with mock.patch.dict(export.os.environ, {"AKTIENANALYSE_FILINGS_KEY": ""}):
            self.assertEqual(({}, None), export.statement_export("EIN3.DE", "2026-11-20", "EUR", 0))

    def test_the_second_look_does_not_download_again(self):
        payload = workbook()
        with mock.patch.object(export, "company_id", return_value=134), \
                mock.patch.object(export, "download", return_value=payload) as geladen:
            first, currency = export.statement_export("EIN3.DE", "2026-11-20", "EUR", 10)
            second, _same = export.statement_export("EIN3.DE", "2026-11-20", "EUR", 11)
        self.assertEqual("EUR", currency)
        self.assertIn("2025-06-30", second["quarterly"])
        self.assertEqual(1, geladen.call_count, "the export is read once a week, not once a view")

    def test_a_foreign_currency_is_not_mixed_into_the_statements(self):
        with mock.patch.object(export, "company_id", return_value=134), \
                mock.patch.object(export, "download", return_value=workbook(currency="USD")):
            sections, currency = export.statement_export("EIN3.DE", "2026-11-20", "EUR", 12)
        self.assertEqual({}, sections)
        self.assertIsNone(currency)

    def test_only_ten_exports_a_day_are_taken(self):
        """The provider allows ten files a day; a page view must not spend them silently."""
        payload = workbook()
        self.assertEqual(0, store.budget_used())
        with mock.patch.object(export, "company_id", return_value=134), \
                mock.patch.object(export, "download", return_value=payload):
            for slot in range(export.EXPORT_PER_DAY):
                sections, _currency = export.statement_export(f"T{slot}.DE", "2026-11-20", "EUR", slot)
                self.assertTrue(sections, f"slot {slot} was refused")
            refused, currency = export.statement_export("T99.DE", "2026-11-20", "EUR", 99)
        self.assertEqual({}, refused)
        self.assertIsNone(currency)
        self.assertEqual(export.EXPORT_PER_DAY, store.budget_used())
        self.assertEqual([("", "", export.BUDGET_REASON)], store.stored_export_gaps("T99.DE"))

    def test_an_unreadable_file_keeps_its_slot(self):
        """The provider counted the download, so the tenth slot is gone even if we cannot read it."""
        with mock.patch.object(export, "company_id", return_value=134), \
                mock.patch.object(export, "download", return_value=b"kein xlsx") as geladen:
            export.statement_export("EIN3.DE", "2026-11-20", "EUR", 40)
            export.statement_export("EIN3.DE", "2026-11-20", "EUR", 41)
        self.assertEqual(1, store.budget_used(), "a delivered file costs a slot")
        self.assertEqual(1, geladen.call_count, "the failure must not be retried at once")
        self.assertEqual([("", "", export.UNREADABLE_REASON)], store.stored_export_gaps("EIN3.DE"))

    def test_a_failed_attempt_gives_its_slot_back(self):
        with mock.patch.object(export, "company_id", return_value=134), \
                mock.patch.object(export, "download", return_value=b""):
            export.statement_export("EIN3.DE", "2026-11-20", "EUR", 30)
        self.assertEqual(0, store.budget_used(), "a failure must not cost one of the ten")

    def test_a_failed_download_is_not_repeated_on_the_next_view(self):
        """A provider that answers 429 must not be asked again on every cold cache key."""
        with mock.patch.object(export, "company_id", return_value=134), \
                mock.patch.object(export, "download", return_value=b"") as geladen:
            first = export.statement_export("EIN3.DE", "2026-11-20", "EUR", 20)
            second = export.statement_export("EIN3.DE", "2026-11-20", "EUR", 21)
        self.assertEqual(({}, None), first)
        self.assertEqual(({}, None), second)
        self.assertEqual(1, geladen.call_count, "the second view must not knock again")

    def test_a_broken_file_leaves_the_store_alone(self):
        with mock.patch.object(export, "company_id", return_value=134), \
                mock.patch.object(export, "download", return_value=b"kein xlsx"):
            self.assertEqual(({}, None), export.statement_export("EIN3.DE", "2026-11-20", "EUR", 13))

    def test_the_provider_number_is_remembered(self):
        store.remember_provider("EIN3.DE", 134)
        self.assertEqual(134, store.stored_provider("EIN3.DE"))
        self.assertIsNone(store.stored_provider("UNBEKANNT.DE"))

    def test_the_page_names_the_daily_budget_as_the_reason(self):
        """A missing figure must come with its cause, not with silence."""
        import app
        with mock.patch.dict(export.os.environ, {"AKTIENANALYSE_DB": str(Path(self.directory.name) / "test.db")}):
            store.remember_export("EIN3.DE", 134, "", [("", "", export.BUDGET_REASON)])
            notes = app.export_notes("EIN3.DE")
        self.assertEqual([{"block": "Standardauswertung", "source": "FinancialFilings",
                           "have": 0, "need": 1, "reason": export.BUDGET_REASON}], notes)

    def test_a_slot_is_refunded_on_the_day_it_was_taken(self):
        """A download that crosses midnight must not refund tomorrow's allowance."""
        day = store.budget_take(10)
        self.assertEqual(store.budget_day(), day)
        self.assertEqual(1, store.budget_used())
        store.budget_release(day)
        self.assertEqual(0, store.budget_used())
        self.assertIsNone(store.budget_take(0), "no slot left for an empty allowance")

    def test_the_gaps_of_the_export_are_stored_with_it(self):
        store.remember_export("EIN3.DE", 134, "abc", [("2025", "totalrevenue", "Grund")])
        self.assertEqual([("2025", "totalrevenue", "Grund")], store.stored_export_gaps("EIN3.DE"))
        self.assertTrue(store.export_is_fresh("EIN3.DE", 7))
        self.assertFalse(store.export_is_fresh("EIN3.DE", 0))

    def test_the_store_round_trip_keeps_the_sections(self):
        sections, _gaps = export.sections_from_export(self.report(), "2026-11-20")
        with closing(store.connect()) as conn:
            store.write_figures(conn, "EIN3.DE", sections, source="xlsx_export", currency="EUR")
            back = store.read_figures(conn, "EIN3.DE", max_age_h=24)
        self.assertAlmostEqual(306.673e6, back["quarterly"]["2024-06-30"]["totalrevenue"], places=2)


class MergeTest(unittest.TestCase):
    def test_the_tagged_value_wins_and_the_export_only_fills(self):
        import app
        statements = {"income": {"2025-12-31": {"totalrevenue": 36_800_000_000.0, "netincome": 1.0}},
                      "quarterly": {}, "balance": {}, "cash": {}}
        supplement = {"income": {"2025-12-31": {"totalrevenue": 1.0, "basiceps": 1.79},
                                 "2024-12-31": {"totalrevenue": 34_180_000_000.0}},
                      "quarterly": {"2025-06-30": {"totalrevenue": 327_392_000.0}},
                      "balance": {}, "cash": {}}
        merged = app.merge_export(statements, supplement)
        self.assertEqual(36_800_000_000.0, merged["income"]["2025-12-31"]["totalrevenue"])
        self.assertEqual(1.79, merged["income"]["2025-12-31"]["basiceps"], "a missing figure is filled")
        self.assertIn("2024-12-31", merged["income"])
        self.assertIn("2025-06-30", merged["quarterly"])


if __name__ == "__main__":
    unittest.main()
