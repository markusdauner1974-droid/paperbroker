"""Tests for the collector: it must pay once, not once per page view.

Two defects are pinned here, both found in review: two simultaneous page views of
the same share used to buy the same filing list twice, and the stored list was
written but never read, so the list was paid again as soon as the values went
stale.
"""

import importlib
import os
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

AKT_DIR = Path(__file__).resolve().parent


def load_collector():
    if str(AKT_DIR) not in sys.path:
        sys.path.insert(0, str(AKT_DIR))
    return importlib.import_module("filing_statements")


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        self.env = mock.patch.dict(os.environ, {"AKTIENANALYSE_DB": self.db})
        self.env.start()
        self.fs = load_collector()
        self.store = self.fs.store
        with self.store.verbindung(self.db) as conn:
            self.store.berichte_merken(conn, "EVK.DE", [
                (2023, "IXBRL", "https://cdn.example/financialreports/media/filings/a.zip", "2024-03-19"),
                (2024, "IXBRL", "https://cdn.example/financialreports/media/filings/b.zip", "2025-03-17"),
            ])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_the_stored_list_is_used_instead_of_asking_the_provider_again(self):
        def verboten(*args, **kwargs):
            raise AssertionError("the provider was asked although the list was stored")

        with mock.patch.object(self.fs, "listing", verboten):
            lei, entries = self.fs.filing_entries("EVK.DE", "2026-11-19", None)
        self.assertEqual(lei, "")
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0][0].endswith("a.zip"))

    def test_the_stored_release_date_travels_with_the_entry(self):
        _lei, entries = self.fs.filing_entries("EVK.DE", "2026-11-19", None)
        self.assertEqual(entries[1][2], "2025-03-17")

    def test_without_a_stored_list_the_provider_decides_the_addresses(self):
        gesehen = {}

        def erfunden(pfad, **query):
            gesehen[pfad] = query
            if pfad == "companies":
                return [{"id": 150, "lei": "41GUOJQTALQHLF39XJ34", "isins": ["DE000EVNK013"]}]
            return [{"release_datetime": "2026-03-19T10:00:00Z",
                     "document": "https://cdn.financialreports.eu/financialreports/media/filings/c.zip",
                     "company": {"id": 150}, "period_ending_date": "2025-12-31"}]

        with mock.patch.object(self.fs, "listing", erfunden):
            # A symbol without a stored list, so the provider really is asked.
            lei, entries = self.fs.filing_entries("TEST.DE", "2026-11-19", "DE000EVNK013")
        self.assertEqual(lei, "41GUOJQTALQHLF39XJ34")
        self.assertEqual(len(entries), 1)
        self.assertEqual(gesehen["filings"]["company"], 150)

    def test_an_address_outside_the_cdn_is_refused(self):
        def boese(pfad_, **query):
            if pfad_ == "companies":
                return [{"id": 150, "lei": "41GUOJQTALQHLF39XJ34", "isins": ["DE000EVNK013"]}]
            return [{"release_datetime": "2026-03-19T10:00:00Z",
                     "document": "https://boese.example/steal.zip",
                     "company": {"id": 150}, "period_ending_date": "2025-12-31"}]

        with mock.patch.object(self.fs, "listing", boese):
            _lei, entries = self.fs.filing_entries("TEST.DE", "2026-11-19", "DE000EVNK013")
        self.assertEqual(entries, [])

    def test_two_views_of_the_same_share_share_one_lock(self):
        self.assertIs(self.fs.symbol_lock("EVK.DE"), self.fs.symbol_lock("EVK.DE"))
        self.assertIsNot(self.fs.symbol_lock("EVK.DE"), self.fs.symbol_lock("AAPL"))

    def test_the_lock_holds_while_the_first_caller_collects(self):
        """A second caller must wait for the first and then read, not pay again."""
        self.fs.symbol_lock("EVK.DE")
        sperre = self.fs.symbol_lock("EVK.DE")
        self.assertFalse(sperre.locked())
        with sperre:
            self.assertTrue(sperre.locked())

    def test_the_issuer_is_read_from_the_report_when_no_lookup_was_made(self):
        bericht = (b'<?xml version="1.0"?><html><body>'
                   b'<xbrli:identifier scheme="http://standards.iso.org/iso/17442">'
                   b'41GUOJQTALQHLF39XJ34</xbrli:identifier></body></html>')
        self.assertEqual(self.fs.issuer_identifier(bericht), "41GUOJQTALQHLF39XJ34")

    def test_a_report_without_identifier_leaves_the_question_open(self):
        self.assertEqual(self.fs.issuer_identifier(b"<html><body>nichts</body></html>"), "")


class StoreFilingTest(unittest.TestCase):
    def test_the_release_date_survives_a_round_trip(self):
        fs = load_collector()
        store = fs.store
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "x.db")
            with store.verbindung(db) as conn:
                store.berichte_merken(conn, "EVK.DE", [
                    (2025, "IXBRL", "https://cdn.example/a.zip", "2026-03-19")])
                gespeichert = store.berichte(conn, "EVK.DE")
        self.assertEqual(gespeichert, [(2025, "IXBRL", "https://cdn.example/a.zip", "2026-03-19")])

    def test_an_older_database_gets_the_new_column(self):
        fs = load_collector()
        store = fs.store
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "alt.db")
            roh = sqlite3.connect(db)
            roh.execute("""CREATE TABLE bericht (symbol TEXT, geschaeftsjahr INTEGER, typ TEXT,
                           adresse TEXT, abgerufen_am TEXT, PRIMARY KEY (symbol, geschaeftsjahr, typ))""")
            roh.commit()
            roh.close()
            with store.verbindung(db) as conn:
                spalten = {row[1] for row in conn.execute("PRAGMA table_info(bericht)")}
        self.assertIn("veroeffentlicht", spalten)


class LockConcurrencyTest(unittest.TestCase):
    def test_concurrent_callers_are_serialised(self):
        fs = load_collector()
        reihenfolge = []
        sperre = fs.symbol_lock("EVK.DE")

        def arbeiter(name):
            with sperre:
                reihenfolge.append(f"start {name}")
                reihenfolge.append(f"ende {name}")

        faeden = [threading.Thread(target=arbeiter, args=(n,)) for n in (1, 2, 3)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join()
        # Nothing overlaps: every start is followed by its own end before the next start.
        for index in range(0, len(reihenfolge), 2):
            self.assertEqual(reihenfolge[index].split()[0], "start")
            self.assertEqual(reihenfolge[index + 1], "ende " + reihenfolge[index].split()[1])


if __name__ == "__main__":
    unittest.main()
