"""The curated report dates: strict on the way in, tolerant on the way out.

Three properties are pinned here, because each of them has cost a scan once:

* **Strict input.** A date is ``YYYY-MM-DD`` or nothing. A timestamp, a German
  date or a mistyped month is rejected with its own code - never guessed into
  the nearest day, because the funnel picks an expiry from it.
* **Tolerant reading.** The file is hand-edited and lives in a mounted cache
  directory: a broken file is an EMPTY file (plus a log line), not a failed
  scan. Rows whose symbol or date is unusable are dropped, not "repaired".
* **Atomic writing.** Tmp file in the same directory, then ``os.replace``: a
  reader sees the old file or the new one, and a crash leaves no half pair.
"""

import json
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from paperbroker import hand_calendar as hc

DAY = date(2026, 11, 17)


def _write(path, symbols):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"version": 1, "symbols": symbols}), encoding="utf-8")


class ParsingTest(unittest.TestCase):
    def test_a_date_is_a_date_only_in_the_exact_form(self):
        self.assertEqual(hc.parse_date("2026-12-10"), date(2026, 12, 10))
        for bad in ("2026-12-10T08:00", "10.12.2026", "2026-2-3", "2026-13-01",
                    "2026-12-32", "", None, 20261210):
            self.assertIsNone(hc.parse_date(bad), bad)

    def test_a_symbol_is_one_to_six_capital_letters(self):
        for good in ("A", "ITG", "ABCDEF"):
            self.assertTrue(hc.valid_symbol(good), good)
        for bad in ("", "ITG1", "itg", "ABCDEFG", "IT-G", None):
            self.assertFalse(hc.valid_symbol(bad), bad)


class ReadingTest(unittest.TestCase):
    def test_a_missing_file_is_an_empty_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = hc.HandCalendar(path=os.path.join(tmp, "none.json"))
            self.assertEqual({}, store.entries())
            self.assertIsNone(store.entry_for("ITG", DAY))

    def test_a_broken_file_is_an_empty_calendar_with_a_log_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            pfad = os.path.join(tmp, "hand.json")
            Path(pfad).write_text("{not json", encoding="utf-8")
            store = hc.HandCalendar(path=pfad)
            with self.assertLogs("paperbroker.hand_calendar", level="WARNING") as log:
                self.assertEqual({}, store.entries())
            self.assertIn("unreadable", log.output[0])

    def test_rows_without_a_usable_date_or_symbol_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            pfad = os.path.join(tmp, "hand.json")
            _write(pfad, {
                "ITG": {"date": "2026-12-10", "source": "IR", "notiert_am": "2026-11-17"},
                "BAD": {"date": "10.12.2026"},
                "TOOLONG": {"date": "2026-12-10"},
                "NOTE": "kein Objekt",
                # Handgetippt klein: der Schluessel wird gross geschrieben, weil
                # ein Ticker keine Kleinschreibung kennt - das ist keine Reparatur
                # an einer kaputten Zeile, sondern dieselbe Zeile anders getippt.
                "lower": {"date": "2026-12-10"},
            })
            store = hc.HandCalendar(path=pfad)
            self.assertEqual(["ITG", "LOWER"], list(store.entries()))

    def test_a_future_date_is_curated_and_a_past_one_is_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            pfad = os.path.join(tmp, "hand.json")
            _write(pfad, {
                "NEW": {"date": "2026-12-10", "source": "IR", "notiert_am": "2026-11-17"},
                "OLD": {"date": "2026-11-16", "source": "IR", "notiert_am": "2026-10-02"},
                "TODAY": {"date": "2026-11-17", "source": "IR", "notiert_am": "2026-11-01"},
            })
            store = hc.HandCalendar(path=pfad)
            neu = store.entry_for("NEW", DAY)
            self.assertEqual("curated", neu["status"])
            self.assertEqual("2026-12-10", neu["curated_date"])
            self.assertEqual("IR", neu["source"])
            self.assertEqual("expired", store.entry_for("OLD", DAY)["status"])
            # Der Berichtstag selbst ist vorbei: der Trichter braucht einen
            # Verfall STRIKT nach dem Bericht, also gilt auch heute als expired.
            self.assertEqual("expired", store.entry_for("TODAY", DAY)["status"])
            self.assertIsNone(store.curated_date("TODAY", DAY))
            self.assertIsNone(store.entry_for("MISSING", DAY))

    def test_a_write_from_another_process_is_visible(self):
        # Zwei Worker, eine Datei: der zweite darf nicht ewig seinen ersten
        # Stand serve - genau das war der Befund "stale cross-process cache".
        with tempfile.TemporaryDirectory() as tmp:
            pfad = os.path.join(tmp, "hand.json")
            _write(pfad, {"ITG": {"date": "2026-12-10", "source": "IR"}})
            store = hc.HandCalendar(path=pfad)
            self.assertEqual("2026-12-10", store.entry_for("ITG", DAY)["curated_date"])
            # "Ein anderer Prozess" schreibt: dieselbe Datei, neuer Inhalt.
            time.sleep(0.01)
            _write(pfad, {"ITG": {"date": "2027-01-15", "source": "Webcast"}})
            self.assertEqual("2027-01-15", store.entry_for("ITG", DAY)["curated_date"])
            self.assertEqual("Webcast", store.entry_for("ITG", DAY)["source"])

    def test_a_deleted_file_is_no_entries_not_a_stale_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            pfad = os.path.join(tmp, "hand.json")
            _write(pfad, {"ITG": {"date": "2026-12-10"}})
            store = hc.HandCalendar(path=pfad)
            self.assertIsNotNone(store.entry_for("ITG", DAY))
            os.remove(pfad)
            self.assertIsNone(store.entry_for("ITG", DAY))

    def test_the_environment_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            pfad = os.path.join(tmp, "eigene.json")
            with mock.patch.dict(os.environ, {hc.ENV_PATH: pfad}):
                self.assertEqual(pfad, hc.default_path())
                self.assertEqual(pfad, hc.HandCalendar().path)


class WritingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pfad = os.path.join(self._tmp.name, "cache", "hand.json")
        self.store = hc.HandCalendar(path=self.pfad, now_fn=lambda: _stamp())

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_valid_date_is_stored_and_read_back(self):
        antwort = self.store.save("itg", "2026-12-10", source="IR-Pressemitteilung")
        self.assertEqual("saved", antwort["status"])
        self.assertEqual("ITG", antwort["symbol"])
        self.assertEqual({"date": "2026-12-10", "source": "IR-Pressemitteilung",
                          "notiert_am": "2026-11-17"}, antwort["entry"])
        gespeichert = json.loads(Path(self.pfad).read_text(encoding="utf-8"))
        self.assertEqual(1, gespeichert["version"])
        self.assertEqual("2026-12-10", gespeichert["symbols"]["ITG"]["date"])

    def test_the_file_is_written_atomically_and_leaves_no_tmp(self):
        self.store.save("ITG", "2026-12-10")
        self.store.save("ACME", "2027-01-15")
        dateien = sorted(p.name for p in Path(os.path.dirname(self.pfad)).iterdir())
        self.assertEqual(["hand.json"], dateien)
        self.assertEqual(2, len(json.loads(Path(self.pfad).read_text())["symbols"]))

    def test_a_second_entry_keeps_the_first(self):
        self.store.save("ITG", "2026-12-10", source="IR")
        self.store.save("ACME", "2027-01-15", source="Webcast")
        self.assertEqual("2026-12-10", self.store.entry_for("ITG", DAY)["curated_date"])
        self.assertEqual("Webcast", self.store.entry_for("ACME", DAY)["source"])

    def test_a_bad_date_is_rejected_with_its_code(self):
        for text, code in (("10.12.2026", hc.CODE_INVALID_DATE),
                           ("2026-12-10T08:00", hc.CODE_INVALID_DATE),
                           ("", hc.CODE_INVALID_DATE),
                           ("2026-11-16", hc.CODE_PAST_DATE),
                           ("2027-08-15", hc.CODE_TOO_FAR_OUT)):
            with self.subTest(text=text):
                with self.assertRaises(hc.HandCalendarError) as fehler:
                    self.store.save("ITG", text)
                self.assertEqual(code, fehler.exception.code)
        self.assertFalse(Path(self.pfad).exists())

    def test_a_bad_symbol_is_rejected_before_anything_is_written(self):
        for symbol in ("", "ITG1", "ABCDEFG", "it g"):
            with self.subTest(symbol=symbol):
                with self.assertRaises(hc.HandCalendarError) as fehler:
                    self.store.save(symbol, "2026-12-10")
                self.assertEqual(hc.CODE_INVALID_SYMBOL, fehler.exception.code)
        self.assertFalse(Path(self.pfad).exists())

    def test_exactly_270_days_ahead_is_allowed(self):
        self.store.save("ITG", "2027-08-14")            # 270 Tage nach dem 17.11.
        self.assertEqual("curated", self.store.entry_for("ITG", DAY)["status"])

    def test_the_cap_refuses_a_new_symbol_but_allows_a_correction(self):
        # Jede Speicherung schreibt die GANZE Datei - die Obergrenze ist das,
        # was den einen Schreibweg davon abhaelt, unbegrenzt zu wachsen.
        with mock.patch.object(hc, "MAX_STORED_SYMBOLS", 2):
            self.store.save("ITG", "2026-12-10")
            self.store.save("ACME", "2027-01-15")
            with self.assertRaises(hc.HandCalendarError) as fehler:
                self.store.save("NEW", "2027-02-01")
            self.assertEqual(hc.CODE_TOO_MANY, fehler.exception.code)
            # Ein BESTEHENDER Eintrag bleibt korrigierbar, auch wenn voll ist.
            self.store.save("ITG", "2026-12-17")

    def test_the_scan_day_can_be_passed_instead_of_read_from_the_clock(self):
        antwort = self.store.save("ITG", "2026-12-10", day=date(2026, 12, 1))
        self.assertEqual("2026-12-01", antwort["entry"]["notiert_am"])


def _stamp():
    from datetime import datetime
    return datetime(2026, 11, 17, 15, 0)
