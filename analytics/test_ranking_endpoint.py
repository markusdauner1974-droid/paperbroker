"""Tests for the guards around /api/ranking.

One request downloads up to two hundred series from the provider. The guards are what keeps a
single-user server responsive: a ticker is checked before it leaves the machine, only one list
runs at a time, and the run stops when its time budget is spent.
"""

import sys
import unittest
from pathlib import Path
from time import sleep
from unittest.mock import patch

AKT_DIR = Path(__file__).resolve().parent
if str(AKT_DIR) not in sys.path:
    sys.path.insert(0, str(AKT_DIR))

import app  # noqa: E402


class SymbolCheckTest(unittest.TestCase):
    def test_valid_tickers_pass(self):
        self.assertEqual(app.checked_symbols(["SAP.DE", "^GSPC", "KO"]), ["SAP.DE", "^GSPC", "KO"])

    def test_anything_else_is_refused(self):
        for symbol in ("AA<script>", "A/B", "SAP DE", "a" * 25, "' OR 1=1", "ÜBER"):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    app.checked_symbols(["KO", symbol])


class SingleRunTest(unittest.TestCase):
    def setUp(self):
        while app._RANKING_ACTIVE:  # a failing test must not block the next one
            app.leave_ranking()

    def test_a_second_list_is_refused_while_one_runs(self):
        app.enter_ranking()
        try:
            with self.assertRaises(ValueError) as error:
                app.enter_ranking()
            self.assertIn("bereits eine Rangliste", str(error.exception))
        finally:
            app.leave_ranking()

    def test_the_slot_is_free_again_after_the_run(self):
        app.enter_ranking()
        app.leave_ranking()
        app.enter_ranking()
        app.leave_ranking()
        self.assertEqual(app._RANKING_ACTIVE, 0)


class FreshnessTest(unittest.TestCase):
    """A ranking has to see prices, not the series of its first run."""

    def test_the_bucket_rotates_with_the_clock(self):
        with patch.object(app, "time", lambda: 899.0):
            first = app.ranking_bucket()
        with patch.object(app, "time", lambda: 900.0):
            second = app.ranking_bucket()
        with patch.object(app, "time", lambda: 1800.0):
            third = app.ranking_bucket()
        self.assertEqual(first, "ranking-0")
        self.assertEqual(second, "ranking-1")
        self.assertEqual(third, "ranking-2")

    def test_symbols_and_index_share_one_bucket_per_run(self):
        seen = []

        def fake_prices(symbol, bucket):
            seen.append((symbol, bucket))
            return [["2026-01-02", 1.0]]

        with patch.object(app, "prices", fake_prices), patch.object(app, "time", lambda: 5400.0):
            fetch, index_rows = app.ranking_series("^GSPC")
            fetch("KO")
        self.assertEqual(seen, [("^GSPC", "ranking-6"), ("KO", "ranking-6")])
        self.assertEqual(index_rows, [["2026-01-02", 1.0]])


class HistoryLockTest(unittest.TestCase):
    """Every failed download used to leave a lock behind, reachable at two hundred per run."""

    def setUp(self):
        app._HISTORY.clear()
        app._HISTORY_LOCKS.clear()

    def test_a_failed_download_leaves_no_lock_behind(self):
        with patch.object(app, "load_history", side_effect=RuntimeError("kein Netz")):
            with self.assertRaises(RuntimeError):
                app.prices("NIX.DE", "ranking-1")
        self.assertEqual(dict(app._HISTORY_LOCKS), {})

    def test_a_successful_download_keeps_the_series_but_no_lock(self):
        with patch.object(app, "load_history", return_value=[["2026-01-02", 1.25]]):
            rows = app.prices("KO", "ranking-1")
        self.assertEqual(rows, [["2026-01-02", 1.25]])
        self.assertEqual(dict(app._HISTORY_LOCKS), {})
        self.assertIn(("KO", "ranking-1"), app._HISTORY)


class BudgetTest(unittest.TestCase):
    def test_a_spent_budget_stops_the_fetch(self):
        fetch = app.budgeted(lambda symbol: symbol, seconds=0)
        sleep(0.01)
        with self.assertRaises(RuntimeError) as error:
            fetch("KO")
        self.assertIn("Zeitbudget", str(error.exception))

    def test_within_the_budget_the_fetch_runs(self):
        calls = []
        fetch = app.budgeted(lambda symbol: calls.append(symbol) or symbol, seconds=60)
        self.assertEqual(fetch("KO"), "KO")
        self.assertEqual(calls, ["KO"])

    def test_the_budget_is_a_positive_number_of_seconds(self):
        self.assertGreater(app.RANKING_BUDGET_S, 0)
        self.assertLessEqual(app.RANKING_BUDGET_S, 600)


if __name__ == "__main__":
    unittest.main()
