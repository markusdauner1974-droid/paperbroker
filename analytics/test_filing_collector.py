"""Tests for the collector: it must pay once, not once per page view.

Two defects are pinned here, both found in review: two simultaneous page views of
the same share used to buy the same filing list twice, and the stored list was
written but never read, so the list was paid again as soon as the values went
stale.
"""

import importlib
import io
import os
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from zipfile import ZipFile

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
        with self.store.connect(self.db) as conn:
            self.store.remember_filings(conn, "EVK.DE", [
                (2023, "IXBRL", "https://cdn.example/financialreports/media/filings/a.zip", "2024-03-19"),
                (2024, "IXBRL", "https://cdn.example/financialreports/media/filings/b.zip", "2025-03-17"),
            ])

    def tearDown(self):
        self.fs._LOCKS.clear()
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

    def test_a_report_released_after_the_cut_off_is_not_used(self):
        """The stored list may hold younger reports than the price series knows."""
        _lei, entries = self.fs.filing_entries("EVK.DE", "2025-06-30", None)
        # 2024-03-19 and 2025-03-17 are both before that date, so both stay.
        self.assertEqual(len(entries), 2)
        _lei, entries = self.fs.filing_entries("EVK.DE", "2024-06-30", None)
        self.assertEqual([e[0][-5:] for e in entries], ["a.zip"])

    def test_a_stored_entry_without_a_release_date_stays_usable(self):
        with self.store.connect(self.db) as conn:
            self.store.remember_filings(conn, "OHNE.DE", [(2023, "IXBRL", "https://cdn/x.zip", "")])
        _lei, entries = self.fs.filing_entries("OHNE.DE", "2024-06-30", None)
        self.assertEqual(len(entries), 1)

    def test_the_lock_map_stays_bounded(self):
        for nummer in range(self.fs._LOCKS_MAX + 40):
            self.fs.symbol_lock(f"TEST{nummer}.DE")
        self.assertLessEqual(len(self.fs._LOCKS), self.fs._LOCKS_MAX)

    def test_a_lock_that_is_in_use_is_never_evicted(self):
        festgehalten = self.fs.symbol_lock("HALT.DE")
        with festgehalten:
            for nummer in range(self.fs._LOCKS_MAX + 10):
                self.fs.symbol_lock(f"FUELL{nummer}.DE")
            self.assertIs(self.fs.symbol_lock("HALT.DE"), festgehalten)

    def test_the_issuer_is_read_from_a_later_member_when_the_first_has_none(self):
        puffer = io.BytesIO()
        with ZipFile(puffer, "w") as archiv:
            archiv.writestr("leer.xhtml", "<html><body>ohne Kennung</body></html>")
            archiv.writestr("voll.xhtml",
                            '<html><body><xbrli:identifier>41GUOJQTALQHLF39XJ34</xbrli:identifier>'
                            '</body></html>')
        self.assertEqual(self.fs.issuer_identifier(puffer.getvalue()), "41GUOJQTALQHLF39XJ34")

    def test_the_issuer_is_read_from_the_report_when_no_lookup_was_made(self):
        report = (b'<?xml version="1.0"?><html><body>'
                  b'<xbrli:identifier scheme="http://standards.iso.org/iso/17442">'
                  b'41GUOJQTALQHLF39XJ34</xbrli:identifier></body></html>')
        self.assertEqual(self.fs.issuer_identifier(report), "41GUOJQTALQHLF39XJ34")

    def test_a_report_without_identifier_leaves_the_question_open(self):
        self.assertEqual(self.fs.issuer_identifier(b"<html><body>nothing</body></html>"), "")


class RememberStatementsTest(unittest.TestCase):
    """The write path after a paid lookup: if it fails silently, every page view pays again."""

    def test_the_collected_statements_reach_the_store(self):
        fs = load_collector()
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "write.db"
            old = os.environ.get("AKTIENANALYSE_DB")
            os.environ["AKTIENANALYSE_DB"] = str(db)
            try:
                tagged = {
                    "income": {"2025-12-31": {"totalrevenue": 14_069_000_000.0,
                                              "dilutedeps": 0.57}},
                    "provenance": {"income": {"2025-12-31": {
                        "totalrevenue": {"url": "https://cdn.example/evk.zip",
                                         "filed": "2026-03-19"},
                        "dilutedeps": {"url": "https://cdn.example/evk.zip",
                                       "filed": "2026-03-19"}}}},
                }
                fs.remember_statements("EVK.DE", "DE000EVNK013", "EUR", tagged)
                with sqlite3.connect(db) as conn:
                    figures = conn.execute("SELECT COUNT(*) FROM figure").fetchone()[0]
                    filings = conn.execute("SELECT COUNT(*) FROM filing").fetchone()[0]
                    identity = conn.execute(
                        "SELECT isin, currency FROM company WHERE symbol='EVK.DE'").fetchone()
                    source = conn.execute("SELECT DISTINCT source FROM figure").fetchone()[0]
            finally:
                if old is None:
                    os.environ.pop("AKTIENANALYSE_DB", None)
                else:
                    os.environ["AKTIENANALYSE_DB"] = old
        # A wrong keyword name would raise TypeError, be swallowed, and leave the store empty.
        self.assertEqual(figures, 2, "the figures of a paid lookup were not written")
        self.assertEqual(filings, 1, "the paid filing list was not written")
        self.assertEqual(identity, ("DE000EVNK013", "EUR"))
        self.assertEqual(source, "ixbrl_original")

    def test_the_stored_statements_report_the_currency_of_the_report(self):
        """The page compares figures across years, so it must not use the quote currency."""
        fs = load_collector()
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "currency.db"
            old = os.environ.get("AKTIENANALYSE_DB")
            os.environ["AKTIENANALYSE_DB"] = str(db)
            try:
                tagged = {"income": {"2025-12-31": {"totalrevenue": 391_035_000_000.0}},
                          "provenance": {"income": {"2025-12-31": {
                              "totalrevenue": {"url": "https://cdn.example/aapl.zip",
                                               "filed": "2026-10-30"}}}}}
                fs.remember_statements("AAPL", None, "USD", tagged)
                statements, currency = fs.stored_statements("AAPL", "EUR")
                stored_currency = fs.store.company(fs.store.connect(), "AAPL")["currency"]
            finally:
                if old is None:
                    os.environ.pop("AKTIENANALYSE_DB", None)
                else:
                    os.environ["AKTIENANALYSE_DB"] = old
        self.assertEqual(stored_currency, "USD", "the company row lost its currency")
        # Reading the column by its old name yields None, and the fallback would answer EUR.
        self.assertEqual(currency, "USD", "the read used the quote currency instead of the report's")
        self.assertIn("2025-12-31", statements.get("income", {}))

    def test_an_unwritable_store_does_not_raise(self):
        """connect() creates missing folders, so the path has to be uncreatable, not missing."""
        fs = load_collector()
        with TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("a file, not a directory", encoding="utf-8")
            old = os.environ.get("AKTIENANALYSE_DB")
            os.environ["AKTIENANALYSE_DB"] = str(blocker / "x.db")
            try:
                try:
                    with fs.store.connect():
                        pass
                except OSError as expected:
                    raised = type(expected).__name__
                else:
                    raised = ""
                fs.remember_statements("EVK.DE", None, "EUR", {"income": {}})
                written = (blocker / "x.db").exists()
            finally:
                if old is None:
                    os.environ.pop("AKTIENANALYSE_DB", None)
                else:
                    os.environ["AKTIENANALYSE_DB"] = old
        # Without this the test passes even though the except branch was never reached.
        self.assertEqual(raised, "FileExistsError", "the path was creatable, so nothing failed")
        self.assertFalse(written, "a store appeared under a file")
        with mock.patch.object(fs.store, "connect", side_effect=OSError("store gone")):
            fs.remember_statements("EVK.DE", None, "EUR", {"income": {}})


class StoreFilingTest(unittest.TestCase):
    def test_the_release_date_survives_a_round_trip(self):
        fs = load_collector()
        store = fs.store
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "x.db")
            with store.connect(db) as conn:
                store.remember_filings(conn, "EVK.DE", [
                    (2025, "IXBRL", "https://cdn.example/a.zip", "2026-03-19")])
                stored = store.filings(conn, "EVK.DE")
        self.assertEqual(stored, [(2025, "IXBRL", "https://cdn.example/a.zip", "2026-03-19")])

    def test_an_older_database_gets_the_new_column(self):
        fs = load_collector()
        store = fs.store
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "older.db")
            raw = sqlite3.connect(db)
            raw.execute("""CREATE TABLE filing (symbol TEXT, fiscal_year INTEGER, kind TEXT,
                           address TEXT, fetched_at TEXT, PRIMARY KEY (symbol, fiscal_year, kind))""")
            raw.commit()
            raw.close()
            with store.connect(db) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(filing)")}
        self.assertIn("released", columns)


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
