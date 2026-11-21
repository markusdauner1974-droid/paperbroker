"""The single-stock view takes the SEC series - on the right key, only when needed, without
writing into the cache.

Kody's review of the first attempt found three things, all of them real: the freshness bucket was
handed to a function that derives daily cache keys from epoch seconds, the download happened even
when Yahoo delivered enough years, and ``setdefault`` wrote the Yahoo keys into the cached SEC
rows, so later requests read mixed data.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402


def yahoo_statements(years):
    """A provider series with `years` filled fiscal years (plus one empty, as Yahoo delivers)."""
    income, balance = {}, {}
    for step in range(years):
        day = f"{2026 - step}-01-31"
        income[day] = {"totalrevenue": 100.0 + step, "netincome": 5.0, "dilutedeps": 1.0}
        balance[day] = {"totalassets": 50.0, "totaldebt": 10.0}
    income["2018-01-31"] = {}  # the empty column Yahoo likes to append
    return {"income": income, "balance": balance, "cash": {}, "quarterly": {}}


def sec_statements():
    return {"income": {"2026-01-31": {"totalrevenue": 100.0}},
            "balance": {"2026-01-31": {"totalassets": 50.0}},
            "cash": {}, "quarterly": {}}


class StockSeriesTest(unittest.TestCase):
    def setUp(self):
        # stock() is lru_cached, and the same arguments would hand the second test the first
        # test's result - including its mock and its call count.
        app.stock.cache_clear()

    def run_stock(self, yahoo, sec, symbol="GAP"):
        rows = [["2026-11-20", 10.0, 10.0, 0.0]]
        with mock.patch.object(app, "prices", return_value=rows), \
             mock.patch.object(app, "load_statements", return_value=yahoo), \
             mock.patch.object(app, "load_sec_statements", return_value=sec) as load, \
             mock.patch.object(app, "merge_filings",
                               side_effect=lambda symbol, cutoff, statements, currency, isin:
                               (statements, None, False)), \
             mock.patch.object(app.yf, "Ticker") as ticker:
            ticker.return_value.get_info.return_value = {}
            ticker.return_value.get_news.return_value = []
            result = app.stock(symbol, 2_000_000)
        return result, load

    def test_the_sec_series_is_asked_for_epoch_seconds(self):
        """The bucket is a 900-second index; SEC derives its daily keys as seconds // 86400."""
        _, load = self.run_stock(yahoo_statements(4), sec_statements())
        self.assertEqual(1, load.call_count)
        self.assertEqual(2_000_000 * 900, load.call_args.args[2])

    def test_a_complete_provider_series_does_not_trigger_a_download(self):
        """The companyfacts payload can pass 30 MB; a page view should not wait for it."""
        _, load = self.run_stock(yahoo_statements(6), sec_statements())
        self.assertEqual(0, load.call_count)

    def test_the_cached_sec_rows_are_not_mutated(self):
        sec = sec_statements()
        result, _ = self.run_stock(yahoo_statements(4), sec)
        # The debt value must reach the result ...
        self.assertEqual(10.0, result["statements"]["balance"]["2026-01-31"].get("totaldebt"))
        # ... but never the cached source rows, which every later request would read.
        self.assertNotIn("totaldebt", sec["balance"]["2026-01-31"])

    def test_the_second_call_reads_the_same_rows_as_the_first(self):
        sec = sec_statements()
        first, _ = self.run_stock(yahoo_statements(4), sec)
        second, _ = self.run_stock(yahoo_statements(4), sec)
        self.assertEqual({"totalassets"}, set(sec["balance"]["2026-01-31"]))
        self.assertEqual(first["statements"]["balance"], second["statements"]["balance"])

    def test_a_foreign_listing_never_touches_sec(self):
        """SEC only files US issuers; a dotted ticker must not be looked up there."""
        _, load = self.run_stock(yahoo_statements(4), sec_statements(), symbol="SAP.DE")
        self.assertEqual(0, load.call_count)


class FiscalYearsTest(unittest.TestCase):
    def test_empty_columns_do_not_count(self):
        self.assertEqual(4, app.fiscal_years(yahoo_statements(4)["income"]))
        self.assertEqual(0, app.fiscal_years({"2018-01-31": {}}))


if __name__ == "__main__":
    unittest.main()
