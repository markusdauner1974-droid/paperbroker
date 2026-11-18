"""``POST /api/v1/hand-earnings``: one date in, one honest answer out.

Pinned here:

* a valid date is stored and read back with the server's own stamp,
* every rejection carries its stable code AND a 400 - a caller that only reads
  the status code is not misled either,
* the symbol is strict (one to six capital letters) and is normalised to
  uppercase before it becomes a key,
* an EMPTY date deletes the entry: the page reaches the delete path by clearing
  the field, there is no second button and no second route,
* the guards of the one write path: JSON only, optional token, rate limit,
* and the promise: a save writes nothing to disk - ``builtins.open`` is made to
  explode while the request runs,
* the route is POST-only.
"""

import unittest
from unittest import mock

from paperbroker import hand_calendar
from paperbroker.routes import hand_earnings
from paperbroker.routes.context import context
from paperbroker.server import create_app
from paperbroker.tests.test_gap_scan_endpoint import CLOCK, _calendar, _history

PATH = "/api/v1/hand-earnings"


def _app():
    """The real app with fake adapters - no network, one memory store."""
    return create_app(quote_adapter=object(), earnings_adapter=_calendar(),
                      gap_adapter=_history(), now_fn=CLOCK)


class _Base(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.client = self.app.test_client()

    def _post(self, body, **kwargs):
        antwort = self.client.post(PATH, json=body, **kwargs)
        return antwort, antwort.get_json()

    def _store(self, app=None):
        with (app or self.app).app_context():
            return context().hand_calendar

    def _entries(self, app=None):
        return self._store(app).entries()


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
        self.assertEqual("2026-12-10", self._entries()["ITG"]["date"])

    def test_a_lowercase_symbol_is_stored_uppercase(self):
        _antwort, body = self._post({"symbol": " itg ", "date": "2026-12-10"})
        self.assertEqual("saved", body["status"])
        self.assertEqual("ITG", body["symbol"])
        self.assertIn("ITG", self._entries())

    def test_two_saves_keep_both(self):
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        self._post({"symbol": "ACME", "date": "2027-01-15", "source": "Webcast"})
        self.assertEqual({"ITG", "ACME"}, set(self._entries()))

    def test_a_second_save_replaces_the_same_symbol(self):
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        self._post({"symbol": "ITG", "date": "2026-12-17"})
        self.assertEqual(1, len(self._entries()))
        self.assertEqual("2026-12-17", self._entries()["ITG"]["date"])


class DeleteTest(_Base):
    def test_an_empty_date_deletes_the_entry(self):
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        antwort, body = self._post({"symbol": "ITG", "date": ""})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("deleted", body["status"])
        self.assertTrue(body["existed"])
        self.assertEqual({}, self._entries())

    def test_deleting_an_unknown_symbol_is_no_error(self):
        antwort, body = self._post({"symbol": "ITG", "date": ""})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("deleted", body["status"])
        self.assertFalse(body["existed"])

    def test_deleting_a_broken_symbol_is_refused(self):
        antwort, body = self._post({"symbol": "IT-G", "date": ""})
        self.assertEqual(400, antwort.status_code)
        self.assertEqual("invalid_symbol", body["code"])

    def test_a_delete_keeps_the_other_entries(self):
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        self._post({"symbol": "ACME", "date": "2027-01-15"})
        self._post({"symbol": "itg", "date": ""})
        self.assertEqual({"ACME"}, set(self._entries()))


class RejectionTest(_Base):
    def _reject(self, body, code):
        antwort, antwort_body = self._post(body)
        self.assertEqual(400, antwort.status_code)
        self.assertEqual("rejected", antwort_body["status"])
        self.assertEqual(code, antwort_body["code"])
        self.assertEqual({}, self._entries(), "nichts gespeichert")

    def test_a_bad_symbol(self):
        for symbol in ("", "ITG1", "ABCDEFG", "it g", None):
            with self.subTest(symbol=symbol):
                self._reject({"symbol": symbol, "date": "2026-12-10"}, "invalid_symbol")

    def test_a_bad_date(self):
        for datum in ("10.12.2026", "2026-12-10T08:00", "2026-13-01", "  "):
            with self.subTest(datum=datum):
                self._reject({"symbol": "ITG", "date": datum}, "invalid_date")

    def test_a_missing_field_is_not_a_delete(self):
        # Ohne "date" im Rumpf ist die Anfrage unvollstaendig - ein vergessenes
        # Feld darf keinen Eintrag loeschen.
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        antwort, body = self._post({"symbol": "ITG"})
        self.assertEqual(400, antwort.status_code)
        self.assertEqual("invalid_date", body["code"])
        self.assertIn("ITG", self._entries())

    def test_an_explicit_null_is_a_delete(self):
        # Die Seite schickt beim Leeren "" - null bedeutet dasselbe.
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        antwort, body = self._post({"symbol": "ITG", "date": None})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("deleted", body["status"])

    def test_a_past_date(self):
        self._reject({"symbol": "ITG", "date": "2026-11-11"}, "past_date")

    def test_a_date_beyond_the_window(self):
        self._reject({"symbol": "ITG", "date": "2027-09-01"}, "too_far_out")

    def test_the_last_day_of_the_window_still_counts(self):
        antwort, body = self._post({"symbol": "ITG", "date": "2027-08-09"})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("saved", body["status"])


class GuardTest(_Base):
    """Der eine Schreibweg hat seine Waechter - jeder mit eigenem Code."""

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
        self.assertEqual({}, self._entries())

    def test_a_configured_token_is_required(self):
        with mock.patch.dict("os.environ", {hand_earnings.ENV_TOKEN: "geheim"}):
            app = _app()
            client = app.test_client()
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
            self.assertIn("ITG", self._entries(app))

    def test_a_non_ascii_header_is_a_refusal_not_a_crash(self):
        # Werkzeug dekodiert Header latin-1: ein Umlaut kommt als Nicht-ASCII-str
        # an, und compare_digest nimmt bei str nur ASCII - ohne Bytes-Vergleich
        # waere das ein 500er statt eines 403.
        with mock.patch.dict("os.environ", {hand_earnings.ENV_TOKEN: "geheim"}):
            antwort = _app().test_client().post(
                PATH, json={"symbol": "ITG", "date": "2026-12-10"},
                headers={hand_earnings.TOKEN_HEADER: "tökén"})
        self.assertEqual(403, antwort.status_code)
        self.assertEqual("forbidden", antwort.get_json()["code"])

    def test_a_non_ascii_configured_token_still_matches(self):
        with mock.patch.dict("os.environ", {hand_earnings.ENV_TOKEN: "kennwort-ü"}):
            antwort = _app().test_client().post(
                PATH, json={"symbol": "ITG", "date": "2026-12-10"},
                headers={hand_earnings.TOKEN_HEADER: "kennwort-ü"})
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("saved", antwort.get_json()["status"])

    def test_without_a_token_the_route_stays_open_like_the_rest_of_the_app(self):
        with mock.patch.dict("os.environ", {hand_earnings.ENV_TOKEN: ""}):
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
        # Die beiden ersten sind gespeichert, die dritte nicht.
        self.assertNotIn("NEW", self._entries())

    def test_a_forwarded_header_does_not_buy_a_fresh_bucket(self):
        # X-Forwarded-For setzt der Client selbst: daraus einen Zaehler-Schluessel
        # zu bauen hiesse, sich beliebig viele frische Kontingente auszustellen.
        with mock.patch.object(hand_earnings, "MAX_SAVES_PER_MINUTE", 1):
            hand_earnings._SAVES.clear()
            erste = self.client.post(PATH, json={"symbol": "ITG", "date": "2026-12-10"},
                                     headers={"X-Forwarded-For": "10.0.0.1"})
            zweite = self.client.post(PATH, json={"symbol": "ACME", "date": "2027-01-15"},
                                      headers={"X-Forwarded-For": "10.0.0.2"})
        self.assertEqual(200, erste.status_code)
        self.assertEqual(429, zweite.status_code)

    def test_the_tracking_table_stays_bounded(self):
        with mock.patch.object(hand_earnings, "MAX_TRACKED_CLIENTS", 10):
            hand_earnings._SAVES.clear()
            for nummer in range(500):
                hand_earnings._too_many(f"client-{nummer}")
            self.assertLessEqual(len(hand_earnings._SAVES), 10)
        hand_earnings._SAVES.clear()

    def test_a_full_calendar_is_refused_with_its_own_code(self):
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
        seite = _app().test_client().get("/").get_data(as_text=True)
        self.assertNotIn("data-hand-token", seite)
        with mock.patch.dict("os.environ", {hand_earnings.ENV_TOKEN: "geheim"}):
            mit = _app().test_client().get("/").get_data(as_text=True)
        self.assertIn('data-hand-token="geheim"', mit)

    def test_the_answer_carries_no_text_for_the_page_to_render_blindly(self):
        # Der Satz kommt aus dem Katalog, nicht aus dem Server: die Antwort
        # traegt den CODE, damit die Seite ihn in ihrer Sprache schreibt.
        _antwort, body = self._post({"symbol": "1", "date": "2026-12-10"})
        self.assertEqual({"status", "code", "message"}, set(body))
        self.assertEqual("invalid_symbol", body["code"])


class NoDiskTest(_Base):
    """Die Zusage des Speichers, an der Schnittstelle geprueft."""

    def test_a_save_and_a_delete_touch_no_file(self):
        with mock.patch("builtins.open", side_effect=AssertionError("kein Dateizugriff")):
            gespeichert = self.client.post(
                PATH, json={"symbol": "ITG", "date": "2026-12-10",
                            "source": "IR"})
            geloescht = self.client.post(PATH, json={"symbol": "ITG", "date": ""})
        self.assertEqual(200, gespeichert.status_code)
        self.assertEqual(200, geloescht.status_code)
        self.assertEqual("deleted", geloescht.get_json()["status"])

    def test_the_store_lives_on_the_app_not_in_a_directory(self):
        self._post({"symbol": "ITG", "date": "2026-12-10"})
        # Ein zweiter Client DERSELBEN App sieht den Eintrag: der Speicher
        # haengt am Prozess, nicht am Aufruf.
        zweiter = self.app.test_client()
        self.assertIn("ITG", self._entries())
        self.assertEqual(200, zweiter.get("/").status_code)


if __name__ == "__main__":
    unittest.main()
