"""Tests for the weighted ranking list.

The series are built here, so the outcome does not depend on a provider: a rising share, a
falling share and a missing ticker are enough to prove the rule, the weighting and the handling
of holes.
"""

import math
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ranking


def series(start, steps, factor, wave=0.004, begin=date(2006, 1, 2)):
    """Daily rows [date, close, adj close, dividends].

    The wave keeps the daily returns varying: a perfectly smooth series has no spread, and
    without spread a correlation is undefined - the test would then prove nothing.
    """
    rows, value = [], float(start)
    for step in range(steps):
        rows.append([(begin + timedelta(days=step)).isoformat(), value, value, 0.0])
        value *= factor * (1 + wave * math.sin(step * 1.7))
    return rows


class UniverseTest(unittest.TestCase):
    def test_reads_tickers_and_skips_comments(self):
        with TemporaryDirectory() as folder:
            Path(folder, "probe.txt").write_text(
                "# Kommentar\nSAP.DE\n\nAAPL   # Bemerkung\nko\n", encoding="utf-8")
            with patch.object(ranking, "UNIVERSE_DIR", Path(folder)):
                self.assertEqual(ranking.load_universe("probe"), ["SAP.DE", "AAPL", "KO"])

    def test_unknown_universe_names_the_file(self):
        with TemporaryDirectory() as folder:
            with patch.object(ranking, "UNIVERSE_DIR", Path(folder)):
                with self.assertRaises(ValueError) as error:
                    ranking.load_universe("doesnotexist")
        self.assertIn("doesnotexist", str(error.exception))


class MetricTest(unittest.TestCase):
    def test_rising_share_stands_at_its_high(self):
        rows = series(100, 4000, 1.0003)
        distance = ranking.distance_to_high(rows)
        self.assertIsNotNone(distance)
        self.assertGreater(distance, 0.99)

    def test_falling_share_stands_below_its_high(self):
        rows = series(100, 4000, 0.9992)
        distance = ranking.distance_to_high(rows)
        self.assertIsNotNone(distance)
        self.assertLess(distance, 0.9)

    def test_short_history_leaves_the_distance_open(self):
        self.assertIsNone(ranking.distance_to_high(series(100, 30, 1.001)))

    def test_rising_share_earns_most_points(self):
        rows = series(100, 4000, 1.0004)
        result = ranking.robustness(rows, rows)
        self.assertGreaterEqual(result["points"], 12)
        self.assertEqual(result["comparable"] + len(result["open"]), 15)
        # Eleven years of history cannot carry a twenty-year drawdown: it stays open.
        self.assertIn("Drawdown Kalenderjahre 20", result["open"])
        # A share measured against itself correlates perfectly, so those two criteria fail.
        self.assertNotIn("Korrelation 20 Handelstage", result["open"])
        self.assertEqual(result["comparable"] - result["points"], 2)

    def test_falling_share_earns_fewer_points_than_a_rising_one(self):
        rising = ranking.robustness(series(100, 4000, 1.0004), series(100, 4000, 1.0004))
        falling = ranking.robustness(series(100, 4000, 0.9992), series(100, 4000, 0.9992))
        self.assertLess(falling["points"], rising["points"])

    def test_short_series_is_not_scored(self):
        result = ranking.robustness(series(100, 200, 1.001), series(100, 200, 1.001))
        self.assertIsNone(result["points"])
        self.assertEqual(result["comparable"], 0)


class RankListTest(unittest.TestCase):
    def setUp(self):
        self.prices = {
            "STRONG": series(100, 4000, 1.0005),
            "MIDDLE": series(100, 4000, 1.0001),
            "WEAK": series(100, 4000, 0.9995),
        }

    def fetch(self, symbol):
        if symbol not in self.prices:
            raise ValueError("no prices for " + symbol)
        return self.prices[symbol]

    def test_weights_are_ninety_ten(self):
        self.assertEqual(ranking.WEIGHT_ROBUSTNESS, 0.9)
        self.assertEqual(ranking.WEIGHT_DISTANCE, 0.1)

    def test_order_follows_the_weighted_score(self):
        result = ranking.rank_list(list(self.prices), self.fetch, self.prices["MIDDLE"])
        self.assertEqual([entry["symbol"] for entry in result["rows"]],
                         ["STRONG", "MIDDLE", "WEAK"])
        self.assertEqual([entry["rank"] for entry in result["rows"]], [1, 2, 3])

    def test_score_is_the_weighted_sum(self):
        result = ranking.rank_list(list(self.prices), self.fetch, self.prices["MIDDLE"])
        for entry in result["rows"]:
            expected = 0.9 * entry["robustness"] + 0.1 * entry["distance_rank"]
            self.assertAlmostEqual(entry["score"], expected, places=9)
            self.assertLessEqual(entry["score"], 100)

    def test_distance_becomes_a_rank_within_the_universe(self):
        result = ranking.rank_list(list(self.prices), self.fetch, self.prices["MIDDLE"])
        ranks = sorted(entry["distance_rank"] for entry in result["rows"])
        self.assertEqual(ranks, [0, 50, 100])
        farthest = min(result["rows"], key=lambda entry: entry["distance"])
        self.assertLess(farthest["distance_rank"], 100)

    def test_missing_ticker_does_not_stop_the_list(self):
        result = ranking.rank_list(["STRONG", "MISSING", "WEAK"], self.fetch,
                                   self.prices["MIDDLE"])
        self.assertEqual([entry["symbol"] for entry in result["rows"]], ["STRONG", "WEAK"])
        self.assertEqual([entry["symbol"] for entry in result["skipped"]], ["MISSING"])

    def test_duplicates_are_counted_once(self):
        result = ranking.rank_list(["strong", "STRONG", " weak "], self.fetch,
                                   self.prices["MIDDLE"])
        self.assertEqual(result["universe"], 2)
        self.assertEqual(result["scored"], 2)

    def test_empty_universe_is_refused(self):
        with self.assertRaises(ValueError):
            ranking.rank_list([], self.fetch, self.prices["MIDDLE"])

    def test_universe_limit_is_named(self):
        with self.assertRaises(ValueError) as error:
            ranking.rank_list([f"T{i}" for i in range(ranking.MAX_SYMBOLS + 1)], self.fetch,
                              self.prices["MIDDLE"])
        self.assertIn(str(ranking.MAX_SYMBOLS), str(error.exception))


if __name__ == "__main__":
    unittest.main()
