"""The quarter table filled from the second source, tested against the delivered functions.

What is checked:

* Only gaps are filled. A quarter the provider already reports keeps its figures, a field it left
  empty is completed, and net income is never invented - the second source does not publish it.
* A complete series (eight consecutive quarters) is left alone and costs no request.
* A source that is down leaves the table as it was instead of raising.
* The payload the page reads carries the note that names the source, and the page renders it.
* ``stock()`` is cached: the rows handed in must not be modified in place.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import app  # noqa: E402

PAGE = HERE / "index.html"

M = app.stockanalysis.MILLIONS


def period(day, revenue=None, eps=None, operating=None):
    return {"period_end": day, "label": "", "revenue": revenue, "eps": eps,
            "operating_income": operating}


class SecondSourceQuartersTest(unittest.TestCase):
    def setUp(self):
        self.statements = {"quarterly": {
            "2026-09-30": {"totalrevenue": 9e9, "netincome": 1e9, "dilutedeps": 1.1},
            "2026-06-30": {"totalrevenue": 8e9, "netincome": None, "dilutedeps": None},
        }}

    def run_with(self, periods):
        with mock.patch.object(app.stockanalysis, "series", return_value=periods):
            return app.second_source_quarters("SAP.DE", self.statements)

    def test_gap_quarters_are_added_and_existing_ones_kept(self):
        quarterly, note = self.run_with([
            period("2026-09-30", revenue=9000, eps=1.1, operating=2000),
            period("2026-06-30", revenue=8000, eps=1.0, operating=1800),
            period("2026-03-31", revenue=7000, eps=0.9, operating=1500),
        ])
        self.assertEqual(note["added"], ["2026-03-31"])
        self.assertEqual(quarterly["2026-03-31"], {"totalrevenue": 7e9, "dilutedeps": 0.9,
                                                   "operatingincome": 1.5e9})
        # The reported quarter keeps its own revenue instead of the second source's rounding.
        self.assertEqual(quarterly["2026-09-30"]["totalrevenue"], 9e9)

    def test_empty_fields_of_a_reported_quarter_are_completed(self):
        quarterly, note = self.run_with([period("2026-09-30", revenue=9000, eps=1.1),
                                         period("2026-06-30", revenue=8000, eps=1.0)])
        self.assertEqual(note["filled"], ["2026-06-30"])
        self.assertEqual(quarterly["2026-06-30"]["dilutedeps"], 1.0)  # per share, not in millions
        self.assertIsNone(quarterly["2026-06-30"]["netincome"])  # not published there

    def test_a_zero_eps_of_an_earning_quarter_is_completed(self):
        # SAP Q3 2026: the feed says 0.00 EUR while the quarter earned 2 249 Mio on 1 157 Mio shares.
        self.statements["quarterly"]["2026-09-30"]["dilutedeps"] = 0.0
        quarterly, note = self.run_with([period("2026-09-30", revenue=9000, eps=1.94),
                                         period("2026-06-30", revenue=8000, eps=1.0)])
        self.assertEqual(quarterly["2026-09-30"]["dilutedeps"], 1.94)
        self.assertIsNotNone(note)
        self.assertIn("2026-09-30", note["filled"])

    def test_a_reported_zero_stays_when_the_quarter_lost_money(self):
        # A loss quarter can carry 0.00 legitimately; nothing is overwritten there.
        self.statements["quarterly"]["2026-09-30"].update(dilutedeps=0.0, netincome=-5e8)
        quarterly, _ = self.run_with([period("2026-09-30", revenue=9000, eps=1.94),
                                      period("2026-06-30", revenue=8000, eps=1.0)])
        self.assertEqual(quarterly["2026-09-30"]["dilutedeps"], 0.0)

    def test_net_income_is_never_invented(self):
        quarterly, _ = self.run_with([period("2026-06-30", revenue=8000, eps=1.0),
                                      period("2026-03-31", revenue=7000, eps=0.9)])
        self.assertNotIn("netincome", quarterly["2026-03-31"])

    def test_a_complete_series_is_left_alone(self):
        full = {"totalrevenue": 1.0, "dilutedeps": 0.5, "operatingincome": 0.2}
        statements = {"quarterly": {f"2026-{month:02d}-28": dict(full)
                                    for month in (1, 4, 7, 10)}}
        statements["quarterly"].update({f"2025-{month:02d}-28": dict(full)
                                        for month in (1, 4, 7, 10)})
        with mock.patch.object(app.stockanalysis, "series") as series:
            quarterly, note = app.second_source_quarters("SAP.DE", statements)
        series.assert_not_called()
        self.assertIsNone(note)
        self.assertEqual(quarterly, statements["quarterly"])

    def test_one_empty_eps_in_a_complete_series_is_completed(self):
        # The quiet case: Yahoo leaves the diluted EPS of a US fourth quarter empty, so the page's
        # four-quarter sums turn null although twelve quarters are there. The series is complete -
        # the row is not.
        quarters: dict[str, dict] = {f"2026-{month:02d}-28": {"totalrevenue": 1.0,
                                                              "dilutedeps": 0.5,
                                            "operatingincome": 0.2} for month in (1, 4, 7, 10)}
        quarters.update({f"2025-{month:02d}-28": {"totalrevenue": 1.0, "dilutedeps": 0.5,
                                                 "operatingincome": 0.2} for month in (1, 4, 7, 10)})
        quarters["2026-07-28"]["dilutedeps"] = None
        periods = [period(f"2026-{month:02d}-28", revenue=1000, eps=0.44) for month in (10, 7, 4, 1)]
        periods += [period(f"2025-{month:02d}-28", revenue=900, eps=0.4) for month in (10, 7, 4, 1)]
        with mock.patch.object(app.stockanalysis, "series", return_value=periods):
            quarterly, note = app.second_source_quarters("SAP.DE", {"quarterly": quarters})
        assert note is not None
        self.assertEqual(note["filled"], ["2026-07-28"])
        self.assertEqual(note["added"], [])
        self.assertEqual(quarterly["2026-07-28"]["dilutedeps"], 0.44)
        self.assertEqual(quarterly["2026-07-28"]["totalrevenue"], 1.0)  # own figure stays

    def test_a_broken_source_changes_nothing(self):
        with mock.patch.object(app.stockanalysis, "series", side_effect=OSError("down")):
            quarterly, note = app.second_source_quarters("SAP.DE", self.statements)
        self.assertIsNone(note)
        self.assertEqual(quarterly, self.statements["quarterly"])

    def test_one_gap_at_the_head_does_not_make_it_a_half_year_reporter(self):
        # A quarterly paper whose newest quarter is missing on the page shows one gap of half a year.
        # Reading only that pair threw the whole paper into the half-year bucket.
        quarterly, note = self.run_with([
            period("2026-03-31", revenue=7000, eps=0.9), period("2025-12-31", revenue=6800, eps=1.0),
            period("2025-09-30", revenue=6600, eps=0.9), period("2025-06-30", revenue=6400, eps=1.1)])
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("2026-03-31", note["added"])

    def test_two_intervals_with_a_head_gap_still_read_as_quarterly(self):
        # Three rows, two gaps: one of them is the missing newest quarter. Taking the upper median
        # read the gap and threw the paper out of the quarter table.
        quarterly, note = self.run_with([
            period("2025-12-31", revenue=6800, eps=1.0), period("2025-06-30", revenue=6400, eps=1.1),
            period("2025-03-31", revenue=6200, eps=0.9)])
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("2025-06-30", note["added"])

    def test_a_malformed_row_does_not_take_the_answer_down(self):
        # The series is stored as JSON; a row without a usable period_end must not raise out of the
        # request handler.
        quarterly, note = self.run_with([
            {"period_end": "2026-06-30", "revenue": 8000, "eps": 1.0},
            {"revenue": 7000, "eps": 0.9},
            {"period_end": "not-a-date", "revenue": 6800, "eps": 1.0},
            {"period_end": "2025-09-30", "revenue": 6600, "eps": 0.9}])
        self.assertIsNone(note)
        self.assertEqual(sorted(quarterly), ["2026-06-30", "2026-09-30"])  # untouched

    def test_a_half_year_reporter_gets_no_quarter_rows(self):
        # L'Oréal and Nestlé publish two periods a year; a table titled "Quartale" would mislabel them.
        quarterly, note = self.run_with([period("2026-06-30", revenue=23776, eps=6.63),
                                         period("2025-12-31", revenue=21579, eps=5.2)])
        self.assertIsNone(note)
        self.assertEqual(quarterly, self.statements["quarterly"])

    def test_the_cached_rows_are_not_modified(self):
        before = {day: dict(row) for day, row in self.statements["quarterly"].items()}
        self.run_with([period("2026-06-30", revenue=8000, eps=1.0),
                       period("2026-03-31", revenue=7000, eps=0.9)])
        self.assertEqual(self.statements["quarterly"], before)


class WithSecondSourceTest(unittest.TestCase):
    def test_the_answer_carries_the_note_and_the_longer_table(self):
        result = {"symbol": "SAP.DE", "statements": {"quarterly": {
            "2026-09-30": {"totalrevenue": 9e9}}}}
        with mock.patch.object(app.stockanalysis, "series",
                               return_value=[period("2026-06-30", revenue=8000, eps=1.0),
                                             period("2026-03-31", revenue=7000, eps=0.9)]), \
                mock.patch.object(app, "long_quarterly_growth", return_value=None):
            answer = app.with_second_source("SAP.DE", result)
        self.assertEqual(answer["quarterly_source"]["source"], "stockanalysis.com")
        self.assertIn("2026-06-30", answer["statements"]["quarterly"])
        # The cached payload keeps its own table.
        self.assertNotIn("2026-06-30", result["statements"]["quarterly"])

    def test_the_series_is_fetched_once_per_request(self):
        # Growth and quarter rows read the same series; asking the source twice cost two waits when
        # it was slow.
        fetched = []

        def series(symbol, store_module=None, budget_s=None):
            fetched.append(symbol)
            return [period("2026-06-30", revenue=8000, eps=1.0),
                    period("2026-03-31", revenue=7000, eps=0.9),
                    period("2025-12-31", revenue=6800, eps=1.0)]

        result = {"symbol": "SAP.DE", "statements": {"quarterly": {
            "2026-06-30": {"totalrevenue": 8e9}}}}
        with mock.patch.object(app.stockanalysis, "series", side_effect=series):
            answer = app.with_second_source("SAP.DE", result)
        self.assertEqual(fetched, ["SAP.DE"], "one request must not fetch twice")
        self.assertIn("ttm_growth", answer)

    def test_a_failed_fetch_is_not_retried(self):
        # The second call used to become a new flight leader and wait out the budget again.
        # The background reload is not the subject here and would open a socket; it is also the
        # reason the next test on the same ticker sees a pending load.
        with mock.patch.object(app.stockanalysis, "series", side_effect=OSError("down")) as series, \
                mock.patch.object(app.stockanalysis, "refresh_in_background"):
            answer = app.with_second_source("SAP.DE", {"symbol": "SAP.DE", "statements": {
                "quarterly": {"2026-06-30": {"totalrevenue": 8e9}}}})
        self.assertEqual(series.call_count, 1)
        self.assertNotIn("quarterly_source", answer)

    def test_without_an_addition_the_answer_keeps_its_shape(self):
        result = {"symbol": "SAP.DE", "statements": {"quarterly": {}}}
        with mock.patch.object(app.stockanalysis, "series", side_effect=OSError("down")), \
                mock.patch.object(app.stockanalysis, "refresh_in_background"), \
                mock.patch.object(app, "long_quarterly_growth", return_value=None):
            answer = app.with_second_source("SAP.DE", result)
        self.assertNotIn("quarterly_source", answer)
        self.assertIs(answer["statements"], result["statements"])


class PageNoteTest(unittest.TestCase):
    def test_the_page_names_the_source_under_the_table(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("const secondQuarters=data.quarterly_source;", page)
        self.assertIn("Der Gewinn steht dort nicht", page)


if __name__ == "__main__":
    unittest.main()
