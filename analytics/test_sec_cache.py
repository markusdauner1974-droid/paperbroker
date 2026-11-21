"""The companyfacts payload is fetched once a day, not once per freshness bucket.

The page asks for a series with a bucket that rotates every fifteen minutes. Caching the heavy
download on that bucket meant a 30 MB request every quarter hour per symbol, inside a request
thread. The work now sits one level down, keyed by the day.
"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sec_statements  # noqa: E402

FIRST_BUCKET = 1_800_000_000
SAME_DAY = FIRST_BUCKET + 900
NEXT_DAY = FIRST_BUCKET + 86_400


class DayCacheTest(unittest.TestCase):
    def setUp(self):
        sec_statements._sec_statements_for_day.cache_clear()

    def load(self, bucket, fetch):
        with mock.patch.object(sec_statements, "ticker_index", return_value={"GAP": 7}), \
             mock.patch.object(sec_statements, "request_json", side_effect=fetch) as request, \
             mock.patch.object(sec_statements, "parse_statements",
                               return_value={"income": {}, "balance": {}}):
            sec_statements.load_sec_statements("GAP", date(2026, 11, 20), bucket)
        return request

    def test_two_buckets_of_the_same_day_download_once(self):
        fetch = mock.Mock(side_effect=lambda url: {"cik": 7})
        self.load(FIRST_BUCKET, fetch)
        request = self.load(SAME_DAY, fetch)
        self.assertEqual(1, fetch.call_count)
        # And the second call still reached the cached path instead of the network.
        self.assertEqual(0, request.call_count)

    def test_the_next_day_downloads_again(self):
        fetch = mock.Mock(side_effect=lambda url: {"cik": 7})
        self.load(FIRST_BUCKET, fetch)
        self.load(NEXT_DAY, fetch)
        self.assertEqual(2, fetch.call_count)

    def test_the_day_key_is_the_one_the_daily_caches_expect(self):
        """ticker_index and recent_filings are keyed by day number, not by seconds."""
        fetch = mock.Mock(side_effect=lambda url: {"cik": 7})
        with mock.patch.object(sec_statements, "ticker_index", return_value={"GAP": 7}) as index, \
             mock.patch.object(sec_statements, "request_json", side_effect=fetch), \
             mock.patch.object(sec_statements, "parse_statements",
                               return_value={"income": {}, "balance": {}}):
            sec_statements.load_sec_statements("GAP", date(2026, 11, 20), FIRST_BUCKET)
        self.assertEqual(FIRST_BUCKET // 86_400, index.call_args.args[0])

    def test_an_unknown_symbol_downloads_nothing(self):
        fetch = mock.Mock()
        with mock.patch.object(sec_statements, "ticker_index", return_value={}), \
             mock.patch.object(sec_statements, "request_json", side_effect=fetch):
            self.assertEqual({}, sec_statements.load_sec_statements("NIX", date(2026, 11, 20),
                                                                    FIRST_BUCKET))
        self.assertEqual(0, fetch.call_count)


if __name__ == "__main__":
    unittest.main()
