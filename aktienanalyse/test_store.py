"""Tests for the persistent store of the stock analysis.

The store protects a paid credit: a filing list that was already paid for must be
read back instead of being paid for again, and a value that the report does not
carry must be absent rather than zero.
"""

import importlib.util
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

STORE_PATH = Path(__file__).resolve().parent / "store.py"


def load_store():
    spec = importlib.util.spec_from_file_location("aktienanalyse_store", STORE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"store.py not found at {STORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = load_store()
        self.path = Path(self.tmp.name) / "test.db"
        self.conn = self.store.verbindung(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_an_empty_store_is_created_with_all_four_tables(self):
        namen = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(namen, {"firma", "bericht", "wert", "kurs"})

    def test_statements_survive_a_write_and_read(self):
        abschnitte = {"income": {"2025-12-31": {"totalrevenue": 14_069_000_000.0,
                                               "dilutedeps": 0.57}}}
        self.store.werte_schreiben(self.conn, "EVK.DE", abschnitte,
                                   quelle="ixbrl_original", waehrung="EUR")
        gelesen = self.store.werte_lesen(self.conn, "EVK.DE")
        self.assertEqual(gelesen["income"]["2025-12-31"]["totalrevenue"], 14_069_000_000.0)
        self.assertEqual(gelesen["income"]["2025-12-31"]["dilutedeps"], 0.57)

    def test_a_missing_position_stays_absent_and_is_not_a_zero(self):
        self.store.werte_schreiben(self.conn, "EVK.DE",
                                   {"income": {"2025-12-31": {"totalrevenue": 14_069_000_000.0}}},
                                   quelle="ixbrl_original", waehrung="EUR")
        zeile = self.store.werte_lesen(self.conn, "EVK.DE")["income"]["2025-12-31"]
        self.assertNotIn("dilutedeps", zeile)

    def test_values_older_than_the_limit_are_not_served(self):
        self.store.werte_schreiben(self.conn, "EVK.DE",
                                   {"income": {"2025-12-31": {"totalrevenue": 1.0}}},
                                   quelle="ixbrl_original", waehrung="EUR")
        alt = (datetime.now(UTC) - timedelta(hours=30)).replace(microsecond=0).isoformat()
        self.conn.execute("UPDATE wert SET stand = ?", (alt,))
        self.conn.commit()
        self.assertEqual(self.store.werte_lesen(self.conn, "EVK.DE", max_alter_h=24), {})

    def test_a_paid_filing_list_is_remembered_and_runs_stale(self):
        self.store.berichte_merken(self.conn, "EVK.DE",
                                   [(2024, "IXBRL", "https://cdn/a.zip"),
                                    (2025, "IXBRL", "https://cdn/b.zip")])
        self.assertEqual(len(self.store.berichte(self.conn, "EVK.DE")), 2)
        alt = (datetime.now(UTC) - timedelta(days=40)).replace(microsecond=0).isoformat()
        self.conn.execute("UPDATE bericht SET abgerufen_am = ?", (alt,))
        self.conn.commit()
        self.assertEqual(self.store.berichte(self.conn, "EVK.DE"), [])

    def test_the_identity_keeps_known_fields_when_a_later_call_omits_them(self):
        self.store.firma_merken(self.conn, "EVK.DE", isin="DE000EVNK013", name="Evonik")
        self.store.firma_merken(self.conn, "EVK.DE", waehrung="EUR")
        daten = self.store.firma(self.conn, "EVK.DE")
        self.assertEqual(daten["isin"], "DE000EVNK013")
        self.assertEqual(daten["waehrung"], "EUR")

    def test_prices_are_stored_once_per_day(self):
        self.store.kurse_schreiben(self.conn, "EVK.DE",
                                   [("2026-11-18", 21.78), ("2026-11-19", 18.25)],
                                   waehrung="EUR", quelle="yahoo")
        self.store.kurse_schreiben(self.conn, "EVK.DE", [("2026-11-19", 18.251)],
                                   waehrung="EUR", quelle="yahoo")
        reihe = self.store.kurse_lesen(self.conn, "EVK.DE")
        self.assertEqual(reihe, [("2026-11-18", 21.78), ("2026-11-19", 18.251)])

    def test_the_freshness_line_names_all_three_kinds(self):
        self.store.werte_schreiben(self.conn, "EVK.DE",
                                   {"income": {"2025-12-31": {"totalrevenue": 1.0}}}, quelle="x")
        stand = self.store.stand(self.conn, "EVK.DE")
        self.assertEqual(set(stand), {"werte", "berichte", "kurse"})
        self.assertIsNotNone(stand["werte"])


if __name__ == "__main__":
    unittest.main()
