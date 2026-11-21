"""The Yahoo supplement survives a scanner that is one day ahead.

The scanner on the NAS has Friday's close while Yahoo still ends on Thursday. The old rule
demanded the same day, threw the whole eighteen-year series away - and with it the ten-year
performance and every dividend figure. What still has to be refused is a genuinely stale series
or one in another currency.
"""

import itertools
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402

# stock_extra is lru_cached on the bucket, so every case needs a key of its own.
BUCKETS = itertools.count(8_100_000)


def history(end="2026-11-19"):
    last = date.fromisoformat(end)
    rows = [[(last - timedelta(days=step)).isoformat(), 100.0 + step, 100.0 + step, 0.0]
            for step in (3, 2, 1, 0)]
    rows.append(["2026-05-06", 149.0, 149.0, 2.5])
    rows.sort()
    return rows


class ToleranceTest(unittest.TestCase):
    def test_one_day_behind_the_scanner_is_accepted(self):
        self.assertTrue(app.recent_enough("2026-11-19", "2026-11-20"))

    def test_a_weekend_is_accepted(self):
        self.assertTrue(app.recent_enough("2026-11-13", "2026-11-20"))

    def test_a_stale_series_is_refused(self):
        self.assertFalse(app.recent_enough("2026-11-10", "2026-11-20"))

    def test_a_series_after_the_cutoff_is_refused(self):
        """Nothing newer than the cutoff may slip in; the caller filters, this guards the edge."""
        self.assertFalse(app.recent_enough("2026-11-25", "2026-11-20"))

    def test_an_unreadable_date_is_refused(self):
        self.assertFalse(app.recent_enough("kein Datum", "2026-11-20"))

    def test_the_window_covers_a_week(self):
        self.assertEqual(7, app.CUTOFF_TOLERANCE_DAYS)

    def test_the_same_day_is_accepted(self):
        self.assertTrue(app.recent_enough("2026-11-20", "2026-11-20"))


class StockExtraStatementsTest(unittest.TestCase):
    """The supplement must take the same statement path as the rest of the app.

    Loading Yahoo directly skipped SEC for US issuers and the original reports for European ones -
    so a key never helped on an instance whose prices come from the scanner, and the page could
    not report a gap it never received.
    """

    def run_extra(self):
        ticker = mock.Mock()
        ticker.history_metadata = {"currency": "USD"}
        profile = {"statements": {"income": {"2025-12-31": {"totalrevenue": 1.0}}, "quarterly": {},
                                  "balance": {}, "cash": {}},
                   "statements_source": "SEC EDGAR",
                   "info": {"financialCurrency": "USD"},
                   "data_gaps": [{"block": "Quartale", "source": "SEC EDGAR", "have": 3, "need": 8,
                                  "reason": "Ohne acht aufeinanderfolgende Quartale."}]}
        with mock.patch.object(app.yf, "Ticker", return_value=ticker), \
             mock.patch.object(app, "load_history", return_value=history("2026-11-19")), \
             mock.patch.object(app, "statement_profile", return_value=profile) as profile_call:
            found = app.stock_extra("AAPL", next(BUCKETS), "2026-11-20", "USD")
        return found, profile_call

    def test_the_statements_come_from_the_shared_path(self):
        found, profile_call = self.run_extra()
        self.assertTrue(profile_call.called, "stock_extra still loads Yahoo itself")
        self.assertEqual("SEC EDGAR", found["statements_source"])

    def test_the_gap_report_travels_with_the_statements(self):
        found, _ = self.run_extra()
        self.assertEqual(["Quartale"], [gap["block"] for gap in found["data_gaps"]])

    def test_the_profile_bucket_is_counted_in_ten_minute_steps(self):
        _, profile_call = self.run_extra()
        bucket = profile_call.call_args.args[2]
        self.assertEqual(int(time.time() // 600), bucket)


class StockExtraTest(unittest.TestCase):
    def detail(self, end, currency="EUR", wanted="EUR"):
        ticker = mock.Mock()
        ticker.history_metadata = {"currency": currency}
        with mock.patch.object(app.yf, "Ticker", return_value=ticker), \
             mock.patch.object(app, "load_history", return_value=history(end)):
            return app.stock_extra("SAP.DE", next(BUCKETS), "2026-11-20", wanted,
                                   history_only=True)

    def test_the_long_series_is_kept_when_the_scanner_is_ahead(self):
        found = self.detail("2026-11-19")
        self.assertEqual("Yahoo Finance", found["price_source"])
        self.assertEqual([], found["issues"])
        self.assertTrue(found["prices"])

    def test_the_dividend_column_survives(self):
        found = self.detail("2026-11-19")
        self.assertEqual([2.5], [row[3] for row in found["prices"] if row[3]])

    def test_a_stale_series_says_why_it_was_refused(self):
        found = self.detail("2026-11-01")
        self.assertIsNone(found["price_source"])
        self.assertEqual([], found["prices"])
        self.assertIn("endet zu früh", found["issues"][0])

    def test_another_currency_is_still_refused(self):
        found = self.detail("2026-11-19", currency="USD")
        self.assertIsNone(found["price_source"])
        self.assertIn("Notierungswährung", found["issues"][0])


if __name__ == "__main__":
    unittest.main()
