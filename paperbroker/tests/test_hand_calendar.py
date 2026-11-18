"""The curated dates: validation, the one store, and nothing on disk.

Pinned here:

* every rule that decides whether a typed date is accepted or refused, each with
  its own stable code,
* the two states an entry can have - ``curated`` and ``expired`` - and the fact
  that an expired entry is never handed out as a date,
* the in-memory store: the cap, the copy handed to readers, the delete path and
  the pruning of expired entries on the next save,
* and the promise this module makes above all others: **a save touches no
  file**. ``builtins.open`` is made to explode for that test, so a future edit
  that reintroduces a path fails loudly instead of silently writing somewhere.
"""

import threading
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from paperbroker import hand_calendar as hc

DAY = date(2026, 11, 17)


def _clock(day=DAY):
    """A ``now_fn`` for the store and the endpoint: frozen, never the wall clock."""
    return lambda: datetime(day.year, day.month, day.day, 14, 0)


class EmptyStoreTest(unittest.TestCase):
    def test_an_untouched_store_holds_nothing(self):
        store = hc.HandCalendar(now_fn=_clock())
        self.assertEqual({}, store.entries())
        self.assertEqual(0, len(store))
        self.assertIsNone(store.entry_for("ITG", DAY))
        self.assertIsNone(store.curated_date("ITG", DAY))


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.store = hc.HandCalendar(now_fn=_clock())

    def test_a_valid_date_is_stored_with_the_server_day(self):
        ergebnis = self.store.save("ITG", "2026-12-10", source="IR-Pressemitteilung")
        self.assertEqual("saved", ergebnis["status"])
        self.assertEqual("ITG", ergebnis["symbol"])
        self.assertEqual({"date": "2026-12-10", "source": "IR-Pressemitteilung",
                          "notiert_am": "2026-11-17"}, ergebnis["entry"])
        eintrag = self.store.entry_for("ITG", DAY)
        self.assertEqual("curated", eintrag["status"])
        self.assertEqual("2026-12-10", eintrag["curated_date"])
        self.assertEqual(date(2026, 12, 10), self.store.curated_date("ITG", DAY))

    def test_a_lowercase_symbol_is_normalised(self):
        self.store.save("itg", "2026-12-10")
        self.assertIn("ITG", self.store.entries())
        self.assertNotIn("itg", self.store.entries())

    def test_the_source_is_cut_and_trimmed(self):
        self.store.save("ITG", "2026-12-10", source="  " + "x" * 200 + "  ")
        self.assertEqual(80, len(self.store.entry_for("ITG", DAY)["source"]))

    def test_a_second_save_replaces_the_same_symbol(self):
        self.store.save("ITG", "2026-12-10", source="IR")
        self.store.save("ITG", "2026-12-17", source="Webcast")
        self.assertEqual(1, len(self.store))
        self.assertEqual("2026-12-17", self.store.entry_for("ITG", DAY)["curated_date"])
        self.assertEqual("Webcast", self.store.entry_for("ITG", DAY)["source"])

    def test_two_symbols_live_side_by_side(self):
        self.store.save("ITG", "2026-12-10")
        self.store.save("ACME", "2027-01-15")
        self.assertEqual({"ITG", "ACME"}, set(self.store.entries()))

    def test_the_boundary_of_the_window_is_inclusive(self):
        # 270 Tage nach dem 17.11. ist der 14.08.2027 - der letzte erlaubte Tag.
        self.store.save("ITG", (DAY + timedelta(days=270)).isoformat())
        self.assertEqual("curated", self.store.entry_for("ITG", DAY)["status"])


class RejectionTest(unittest.TestCase):
    def setUp(self):
        self.store = hc.HandCalendar(now_fn=_clock())

    def _code(self, symbol, date_text):
        with self.assertRaises(hc.HandCalendarError) as fehler:
            self.store.save(symbol, date_text)
        return fehler.exception.code

    def test_a_symbol_has_to_be_one_to_six_capital_letters(self):
        for falsch in ("", "TOOLONG", "IT-G", "1TG", None):
            self.assertEqual(hc.CODE_INVALID_SYMBOL, self._code(falsch, "2026-12-10"))

    def test_only_an_iso_day_is_a_date(self):
        for falsch in ("", "10.12.2026", "2026-12-10T08:00", "2026-13-01",
                       "2026-02-30", "2026-1-1", None, 20261210):
            self.assertEqual(hc.CODE_INVALID_DATE, self._code("ITG", falsch), falsch)

    def test_the_scan_day_itself_is_accepted_but_reads_expired(self):
        # Der Tag selbst ist erlaubt einzutragen - er ist aber kein Termin mehr:
        # der Bericht liegt hinter dem Schluss, den der Gap misst.
        self.store.save("ITG", DAY.isoformat())
        self.assertEqual("expired", self.store.entry_for("ITG", DAY)["status"])

    def test_a_date_before_the_scan_day_is_refused(self):
        self.assertEqual(hc.CODE_PAST_DATE, self._code("ITG", "2026-11-16"))

    def test_a_date_further_out_than_the_source_window_is_refused(self):
        self.assertEqual(hc.CODE_TOO_FAR_OUT,
                         self._code("ITG", (DAY + timedelta(days=271)).isoformat()))

    def test_a_rejection_leaves_the_store_untouched(self):
        with self.assertRaises(hc.HandCalendarError):
            self.store.save("ITG", "2026-11-01")
        self.assertEqual({}, self.store.entries())


class ExpiredTest(unittest.TestCase):
    def test_an_entry_past_its_date_is_expired_and_never_a_date(self):
        store = hc.HandCalendar(now_fn=_clock())
        store.save("ITG", "2026-12-10")
        spaeter = date(2026, 12, 10)
        self.assertEqual("expired", store.entry_for("ITG", spaeter)["status"])
        self.assertIsNone(store.curated_date("ITG", spaeter),
                          "ein abgelaufener Termin ist kein Termin")

    def test_one_day_before_the_report_the_entry_is_still_curated(self):
        store = hc.HandCalendar(now_fn=_clock())
        store.save("ITG", "2026-12-10")
        self.assertEqual("curated", store.entry_for("ITG", date(2026, 12, 9))["status"])

    def test_the_next_save_drops_the_expired_entries(self):
        # Niemand traegt sie je aus: ohne diese Auskehr wuerde der Speicher mit
        # jedem Quartal weiter wachsen.
        store = hc.HandCalendar(now_fn=_clock())
        store.save("ITG", "2026-12-10", day=date(2026, 11, 17))
        self.assertIn("ITG", store.entries())
        # Ein Vorgang im Januar raeumt den abgelaufenen Eintrag weg.
        store.save("ACME", "2027-02-01", day=date(2027, 1, 10))
        self.assertEqual({"ACME"}, set(store.entries()))


class CapTest(unittest.TestCase):
    def test_the_cap_refuses_a_new_symbol_but_allows_a_correction(self):
        store = hc.HandCalendar(now_fn=_clock())
        with mock.patch.object(hc, "MAX_STORED_SYMBOLS", 2):
            store.save("ITG", "2026-12-10")
            store.save("ACME", "2027-01-15")
            with self.assertRaises(hc.HandCalendarError) as fehler:
                store.save("NEW", "2027-02-01")
            self.assertEqual(hc.CODE_TOO_MANY, fehler.exception.code)
            # Ein BESTEHENDER Eintrag bleibt korrigierbar, auch wenn voll ist.
            store.save("ITG", "2026-12-17")

    def test_an_expired_entry_makes_room_for_a_new_one(self):
        store = hc.HandCalendar(now_fn=_clock(day=date(2027, 3, 1)))
        with mock.patch.object(hc, "MAX_STORED_SYMBOLS", 1):
            store.save("ITG", "2026-12-10", day=date(2026, 11, 17))
            # Der alte Eintrag ist abgelaufen und wird beim Speichern entfernt.
            store.save("NEW", "2027-04-01")
        self.assertEqual({"NEW"}, set(store.entries()))


class DeleteTest(unittest.TestCase):
    def setUp(self):
        self.store = hc.HandCalendar(now_fn=_clock())

    def test_a_known_symbol_is_forgotten(self):
        self.store.save("ITG", "2026-12-10")
        self.assertTrue(self.store.delete("ITG"))
        self.assertEqual({}, self.store.entries())

    def test_an_unknown_symbol_is_no_error(self):
        self.assertFalse(self.store.delete("ITG"))
        self.assertEqual({}, self.store.entries())

    def test_the_symbol_is_normalised_like_on_save(self):
        self.store.save("ITG", "2026-12-10")
        self.assertTrue(self.store.delete("itg"))

    def test_a_broken_symbol_is_refused(self):
        with self.assertRaises(hc.HandCalendarError) as fehler:
            self.store.delete("IT-G")
        self.assertEqual(hc.CODE_INVALID_SYMBOL, fehler.exception.code)

    def test_delete_does_not_touch_the_other_entries(self):
        self.store.save("ITG", "2026-12-10")
        self.store.save("ACME", "2027-01-15")
        self.store.delete("ITG")
        self.assertEqual({"ACME"}, set(self.store.entries()))


class ReadersGetACopyTest(unittest.TestCase):
    def test_a_reader_cannot_change_the_store(self):
        store = hc.HandCalendar(now_fn=_clock())
        store.save("ITG", "2026-12-10")
        kopie = store.entries()
        kopie["ITG"]["date"] = "1999-01-01"
        kopie["NEU"] = {"date": "2027-01-01"}
        self.assertEqual("2026-12-10", store.entry_for("ITG", DAY)["curated_date"])
        self.assertNotIn("NEU", store.entries())


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_saves_do_not_lose_each_other(self):
        # Der Scan liest in Arbeitern, gespeichert wird aus Anfragen: der eine
        # Speicher muss beides aushalten, ohne dass ein Eintrag verschwindet.
        store = hc.HandCalendar(now_fn=_clock())
        # Zwei Buchstaben: das Symbol-Regex laesst nur A-Z zu, keine Ziffern.
        symbole = ["A" + buchstabe for buchstabe in "ABCDEFGHIJKLMNOPQRST"]

        def speichern(symbol):
            store.save(symbol, "2026-12-10")

        faeden = [threading.Thread(target=speichern, args=(s,)) for s in symbole]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join()
        self.assertEqual(set(symbole), set(store.entries()))


class NoDiskTest(unittest.TestCase):
    """Der Speicher haelt seine Zusage: kein Schreibvorgang, nirgends."""

    def test_saving_open_close_never_touches_the_file_system(self):
        store = hc.HandCalendar(now_fn=_clock())
        with mock.patch("builtins.open", side_effect=AssertionError("kein Dateizugriff")):
            store.save("ITG", "2026-12-10", source="IR")
            self.assertEqual("2026-12-10", store.entry_for("ITG", DAY)["curated_date"])
            self.assertEqual(1, len(store.entries()))
            store.delete("ITG")
            self.assertEqual({}, store.entries())

    def test_the_module_has_no_path_to_a_file(self):
        # Auch die Konstanten sind weg: es gibt keinen Pfad, den jemand
        # versehentlich wieder benutzen koennte.
        for name in ("DEFAULT_PATH", "FALLBACK_PATH", "ENV_PATH", "FILE_VERSION"):
            self.assertFalse(hasattr(hc, name), f"{name} sollte es nicht mehr geben")
        self.assertFalse(hasattr(hc.HandCalendar, "path"))
        self.assertFalse(hasattr(hc.HandCalendar, "_write"))
        self.assertFalse(hasattr(hc.HandCalendar, "_read"))


if __name__ == "__main__":
    unittest.main()
