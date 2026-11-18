"""``POST /api/v1/hand-earnings``: one date in, one honest answer out.

Pinned here:

* a valid date is stored and read back with the server's own stamp,
* every rejection carries its stable code AND a 400 - a caller that only reads
  the status code is not misled either,
* the symbol is strict (one to six capital letters) and is normalised to
  uppercase before it becomes a key,
* a save leaves no temporary file behind (atomic write),
* the route is POST-only.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperbroker import hand_calendar
from paperbroker.hand_calendar import ENV_PATH
from paperbroker.routes import hand_earnings
from paperbroker.tests.test_gap_scan_endpoint import CLOCK, _client

PATH = "/api/v1/hand-earnings"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pfad = os.path.join(self._tmp.name, "cache", "hand.json")
        self._env = mock.patch.dict(os.environ, {ENV_PATH: self.pfad})
        self._env.start()
        self.client = _client()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _post(self, body):
        antwort = self.client.post(PATH, json=body)
        return antwort, antwort.get_json()

    def _datei(self):
        return json.loads(Path(self.pfad).read_text(encoding="utf-8"))


class SaveTest(_Base):
    def test_a_valid_date_is_saved_and_read_back(self):
        antwort, body = self._post({"symbol": "ITG", "date": "2026-12-10",
                                    "source": "IR-Pressemitteilung"})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("saved", body["status"])
        self.assertEqual("ITG", body["symbol"])
        self.assertEqual("2026-12-10", body["date"])
        self.assertEqual("IR-Pressemitteilung", body["source"])
        self.assertEqual("2026-11-12", body["notiert_am"])
        self.assertEqual("2026-12-10", self._datei()["symbols"]["ITG"]["date"])

    def test_a_lowercase_symbol_is_stored_uppercase(self):
        _antwort, body = self._post({"symbol": " itg ", "date": "2026-12-10"})
        self.assertEqual("saved", body["status"])
        self.assertEqual("ITG", body["symbol"])
        self.assertIn("ITG", self._datei()["symbols"])

    def test_two_saves_keep_both_and_leave_no_tmp_file(self):
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        self._post({"symbol": "ACME", "date": "2027-01-15", "source": "Webcast"})
        self.assertEqual({"ITG", "ACME"}, set(self._datei()["symbols"]))
        liegen = sorted(p.name for p in Path(os.path.dirname(self.pfad)).iterdir())
        self.assertEqual(["hand.json", "hand.json.lock"], liegen)

    def test_a_second_save_replaces_the_same_symbol(self):
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        self._post({"symbol": "ITG", "date": "2026-12-17"})
        self.assertEqual(1, len(self._datei()["symbols"]))
        self.assertEqual("2026-12-17", self._datei()["symbols"]["ITG"]["date"])


class RejectionTest(_Base):
    def _reject(self, body, code):
        antwort, antwort_body = self._post(body)
        self.assertEqual(400, antwort.status_code)
        self.assertEqual("rejected", antwort_body["status"])
        self.assertEqual(code, antwort_body["code"])
        self.assertFalse(Path(self.pfad).exists(), "nichts geschrieben")

    def test_a_bad_symbol(self):
        for symbol in ("", "ITG1", "ABCDEFG", "it g", None):
            with self.subTest(symbol=symbol):
                self._reject({"symbol": symbol, "date": "2026-12-10"}, "invalid_symbol")

    def test_a_bad_date(self):
        for datum in ("", "10.12.2026", "2026-12-10T08:00", "2026-13-01", None):
            with self.subTest(datum=datum):
                self._reject({"symbol": "ITG", "date": datum}, "invalid_date")

    def test_a_past_date(self):
        self._reject({"symbol": "ITG", "date": "2026-11-11"}, "past_date")

    def test_a_date_beyond_the_window(self):
        self._reject({"symbol": "ITG", "date": "2027-09-01"}, "too_far_out")

    def test_the_last_day_of_the_window_still_counts(self):
        antwort, body = self._post({"symbol": "ITG", "date": "2027-08-09"})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("saved", body["status"])


class GuardTest(_Base):
    """Der eine Schreibweg hat drei Waechter - jeder mit eigenem Code."""

    def setUp(self):
        super().setUp()
        # Der Zaehler der Ratenbegrenzung ist prozessweit: jeder Test startet
        # mit leerem Fenster, sonst raeumt einer dem anderen die Antwort weg.
        hand_earnings._SAVES.clear()

    def test_a_body_that_is_not_json_is_refused_before_anything_else(self):
        antwort = self.client.post(PATH, data="symbol=ITG&date=2026-12-10",
                                   content_type="application/x-www-form-urlencoded")
        self.assertEqual(415, antwort.status_code)
        self.assertEqual("unsupported_media_type", antwort.get_json()["code"])
        self.assertFalse(Path(self.pfad).exists())

    def test_a_configured_token_is_required(self):
        with mock.patch.dict(os.environ, {hand_earnings.ENV_TOKEN: "geheim"}):
            client = _client()
            ohne = client.post(PATH, json={"symbol": "ITG", "date": "2026-12-10"})
            self.assertEqual(403, ohne.status_code)
            self.assertEqual("forbidden", ohne.get_json()["code"])
            falsch = client.post(PATH, json={"symbol": "ITG", "date": "2026-12-10"},
                                 headers={hand_earnings.TOKEN_HEADER: "daneben"})
            self.assertEqual(403, falsch.status_code)
            richtig = client.post(PATH, json={"symbol": "ITG", "date": "2026-12-10"},
                                  headers={hand_earnings.TOKEN_HEADER: "geheim"})
            self.assertEqual(200, richtig.status_code)
            self.assertEqual("saved", richtig.get_json()["status"])
        self.assertIn("ITG", self._datei()["symbols"])

    def test_without_a_token_the_route_stays_open_like_the_rest_of_the_app(self):
        with mock.patch.dict(os.environ, {hand_earnings.ENV_TOKEN: ""}):
            antwort, body = self._post({"symbol": "ITG", "date": "2026-12-10"})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("saved", body["status"])

    def test_after_the_rate_limit_the_write_is_refused(self):
        with mock.patch.object(hand_earnings, "MAX_SAVES_PER_MINUTE", 2):
            hand_earnings._SAVES.clear()
            self.assertEqual(200, self.client.post(
                PATH, json={"symbol": "ITG", "date": "2026-12-10"}).status_code)
            self.assertEqual(200, self.client.post(
                PATH, json={"symbol": "ACME", "date": "2027-01-15"}).status_code)
            dritte = self.client.post(PATH, json={"symbol": "NEW", "date": "2027-02-01"})
        self.assertEqual(429, dritte.status_code)
        self.assertEqual("too_many_requests", dritte.get_json()["code"])
        # Die beiden ersten sind geschrieben, die dritte nicht.
        self.assertNotIn("NEW", self._datei()["symbols"])

    def test_a_full_file_is_refused_with_its_own_code(self):
        with mock.patch.object(hand_calendar, "MAX_STORED_SYMBOLS", 1):
            self.assertEqual(200, self.client.post(
                PATH, json={"symbol": "ITG", "date": "2026-12-10"}).status_code)
            voll = self.client.post(PATH, json={"symbol": "NEW", "date": "2027-02-01"})
        self.assertEqual(400, voll.status_code)
        self.assertEqual("too_many_entries", voll.get_json()["code"])


class ShapeTest(_Base):
    def test_only_post_is_allowed(self):
        self.assertEqual(405, self.client.get(PATH).status_code)

    def test_a_bodyless_request_is_a_rejection_not_a_crash(self):
        antwort = self.client.post(PATH, data="kein json",
                                   content_type="application/json")
        self.assertEqual(400, antwort.status_code)
        self.assertEqual("invalid_symbol", antwort.get_json()["code"])

    def test_the_page_carries_the_token_only_when_one_is_configured(self):
        # Ohne Token steht kein Attribut im HTML - die Seite soll nicht so tun,
        # als gaebe es einen Schreibschutz.
        seite = _client().get("/").get_data(as_text=True)
        self.assertNotIn("data-hand-token", seite)
        with mock.patch.dict(os.environ, {hand_earnings.ENV_TOKEN: "geheim"}):
            mit = _client().get("/").get_data(as_text=True)
        self.assertIn('data-hand-token="geheim"', mit)

    def test_the_answer_carries_no_text_for_the_page_to_render_blindly(self):
        # Der Satz kommt aus dem Katalog, nicht aus dem Server: die Antwort
        # traegt den CODE, damit die Seite ihn in ihrer Sprache schreibt.
        _antwort, body = self._post({"symbol": "1", "date": "2026-12-10"})
        self.assertEqual({"status", "code", "message"}, set(body))
        self.assertEqual("invalid_symbol", body["code"])
