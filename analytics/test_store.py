"""Tests for the persistent store of the stock analysis.

The store protects a paid credit: a filing list that was already paid for must be
read back instead of being paid for again, and a value that the report does not
carry must be absent rather than zero.

The last test covers the migration: a database written by the German version has to
keep its rows when the English names arrive.
"""

import importlib.util
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

STORE_PATH = Path(__file__).resolve().parent / "store.py"

GERMAN_SCHEMA = """
CREATE TABLE firma (symbol TEXT PRIMARY KEY, isin TEXT, name TEXT, boersenplatz TEXT,
                    waehrung TEXT, stand TEXT);
CREATE TABLE bericht (symbol TEXT, geschaeftsjahr INTEGER, typ TEXT, adresse TEXT,
                      veroeffentlicht TEXT, abgerufen_am TEXT,
                      PRIMARY KEY (symbol, geschaeftsjahr, typ));
CREATE TABLE wert (symbol TEXT, abschnitt TEXT, tag TEXT, position TEXT, wert REAL,
                   waehrung TEXT, quelle TEXT, stand TEXT,
                   PRIMARY KEY (symbol, abschnitt, tag, position));
CREATE TABLE kurs (symbol TEXT, tag TEXT, schluss REAL, waehrung TEXT, quelle TEXT,
                   PRIMARY KEY (symbol, tag));
"""


def load_store():
    spec = importlib.util.spec_from_file_location("analytics_store", STORE_PATH)
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
        self.conn = self.store.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_an_empty_store_is_created_with_all_tables(self):
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"company", "filing", "figure", "price", "export", "export_budget", "fonrex"})

    def test_statements_survive_a_write_and_read(self):
        sections = {"income": {"2025-12-31": {"totalrevenue": 14_069_000_000.0,
                                              "dilutedeps": 0.57}}}
        self.store.write_figures(self.conn, "EVK.DE", sections,
                                 source="ixbrl_original", currency="EUR")
        read = self.store.read_figures(self.conn, "EVK.DE")
        self.assertEqual(read["income"]["2025-12-31"]["totalrevenue"], 14_069_000_000.0)
        self.assertEqual(read["income"]["2025-12-31"]["dilutedeps"], 0.57)

    def test_a_missing_item_stays_absent_and_is_not_a_zero(self):
        self.store.write_figures(self.conn, "EVK.DE",
                                 {"income": {"2025-12-31": {"totalrevenue": 14_069_000_000.0}}},
                                 source="ixbrl_original", currency="EUR")
        line = self.store.read_figures(self.conn, "EVK.DE")["income"]["2025-12-31"]
        self.assertNotIn("dilutedeps", line)

    def test_values_older_than_the_limit_are_not_served(self):
        self.store.write_figures(self.conn, "EVK.DE",
                                 {"income": {"2025-12-31": {"totalrevenue": 1.0}}},
                                 source="ixbrl_original", currency="EUR")
        old = (datetime.now(UTC) - timedelta(hours=30)).replace(microsecond=0).isoformat()
        self.conn.execute("UPDATE figure SET updated_at = ?", (old,))
        self.conn.commit()
        self.assertEqual(self.store.read_figures(self.conn, "EVK.DE", max_age_h=24), {})

    def test_a_paid_filing_list_is_remembered_and_runs_stale(self):
        self.store.remember_filings(self.conn, "EVK.DE",
                                    [(2024, "IXBRL", "https://cdn/a.zip"),
                                     (2025, "IXBRL", "https://cdn/b.zip")])
        self.assertEqual(len(self.store.filings(self.conn, "EVK.DE")), 2)
        old = (datetime.now(UTC) - timedelta(days=40)).replace(microsecond=0).isoformat()
        self.conn.execute("UPDATE filing SET fetched_at = ?", (old,))
        self.conn.commit()
        self.assertEqual(self.store.filings(self.conn, "EVK.DE"), [])

    def test_the_identity_keeps_known_fields_when_a_later_call_omits_them(self):
        self.store.remember_company(self.conn, "EVK.DE", isin="DE000EVNK013", name="Evonik")
        self.store.remember_company(self.conn, "EVK.DE", currency="EUR")
        data = self.store.company(self.conn, "EVK.DE")
        self.assertEqual(data["isin"], "DE000EVNK013")
        self.assertEqual(data["currency"], "EUR")

    def test_prices_are_stored_once_per_day(self):
        # Relative days on purpose: read_prices drops a series whose newest close is older than a
        # day, so a fixed date in the past makes this test fail after midnight - and only then.
        today = datetime.now(UTC).date()
        previous_day = (today - timedelta(days=1)).isoformat()
        today = today.isoformat()
        self.store.write_prices(self.conn, "EVK.DE", [(previous_day, 21.78), (today, 18.25)],
                                currency="EUR", source="yahoo")
        self.store.write_prices(self.conn, "EVK.DE", [(today, 18.251)],
                                currency="EUR", source="yahoo")
        series = self.store.read_prices(self.conn, "EVK.DE")
        self.assertEqual(series, [(previous_day, 21.78), (today, 18.251)])

    def test_the_freshness_line_names_all_three_kinds(self):
        self.store.write_figures(self.conn, "EVK.DE",
                                 {"income": {"2025-12-31": {"totalrevenue": 1.0}}}, source="x")
        state = self.store.freshness(self.conn, "EVK.DE")
        self.assertEqual(set(state), {"figures", "filings", "prices"})
        self.assertIsNotNone(state["figures"])

    def test_a_german_database_keeps_its_rows(self):
        self.conn.close()
        german = Path(self.tmp.name) / "german.db"
        with sqlite3.connect(german) as conn:
            conn.executescript(GERMAN_SCHEMA)
            # Same reason as above: a fetched_at fixed in the past trips the freshness filter.
            stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
            conn.execute("INSERT INTO firma VALUES ('EVK.DE','DE000EVNK013','Evonik','XETRA',"
                         "'EUR',?)", (stamp,))
            conn.execute("INSERT INTO bericht VALUES ('EVK.DE',2025,'IXBRL',"
                         "'https://cdn/b.zip','',?)", (stamp,))
            now = datetime.now(UTC).replace(microsecond=0).isoformat()
            conn.execute("INSERT INTO wert VALUES ('EVK.DE','income','2025-12-31',"
                         "'totalrevenue',14069000000.0,'EUR','ixbrl_original',?)", (now,))
            conn.execute("INSERT INTO kurs VALUES ('EVK.DE',?,'18.25','EUR','yahoo')", (now[:10],))
            conn.commit()

        self.conn = self.store.connect(german)
        identity = self.store.company(self.conn, "EVK.DE")
        self.assertEqual(identity["isin"], "DE000EVNK013")
        self.assertEqual(identity["exchange"], "XETRA")
        self.assertEqual(identity["currency"], "EUR")
        self.assertEqual(len(self.store.filings(self.conn, "EVK.DE")), 1)
        self.assertEqual(self.store.read_figures(self.conn, "EVK.DE")["income"]["2025-12-31"]
                         ["totalrevenue"], 14_069_000_000.0)
        self.assertEqual(self.store.read_prices(self.conn, "EVK.DE"), [(now[:10], 18.25)])
        # The old tables must be gone, otherwise the page could read the wrong one.
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"company", "filing", "figure", "price", "export", "export_budget", "fonrex"})
        # Opening again must not touch the migrated database.
        self.conn.close()
        self.conn = self.store.connect(german)
        self.assertEqual(self.store.company(self.conn, "EVK.DE")["exchange"], "XETRA")


if __name__ == "__main__":
    unittest.main()
