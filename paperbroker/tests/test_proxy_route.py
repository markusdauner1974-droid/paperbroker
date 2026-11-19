"""Die Umlenkung ``/qualitaet/*`` gegen einen echten, kleinen Baustein.

Kein Mock: der Test startet einen eigenen ``ThreadingHTTPServer`` auf einem
freien Port und haengt das Dashboard daran. Damit ist das, was hier gemessen
wird, dieselbe Sache wie im Betrieb - eine HTTP-Antwort ueber einen Socket,
keine erfundene Attrappe.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from paperbroker.routes import quality
from paperbroker.server import create_app

LANGE_ANTWORT = 200_000  # deutlich mehr als ein Stueck (64 KiB)


class _Baustein(BaseHTTPRequestHandler):
    """Ein winziger Baustein: merkt sich die Anfrage und antwortet nach Plan."""

    protocol_version = "HTTP/1.1"
    aufzeichnung = []
    antwort_kopf = {}
    antwort_koerper = b'{"ok": true}'
    antwort_status = 200

    def log_message(self, *args):  # kein Protokollrauschen im Testlauf
        pass

    def _beantworte(self, methode):
        laenge = int(self.headers.get("Content-Length") or 0)
        koerper = self.rfile.read(laenge) if laenge else b""
        type(self).aufzeichnung.append({
            "methode": methode, "pfad": self.path, "koerper": koerper,
            "content_type": self.headers.get("Content-Type"),
        })
        daten = type(self).antwort_koerper
        self.send_response(type(self).antwort_status)
        for name, wert in type(self).antwort_kopf.items():
            self.send_header(name, wert)
        self.send_header("Content-Length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def do_GET(self):
        self._beantworte("GET")

    def do_POST(self):
        self._beantworte("POST")


def _starte_baustein():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Baustein)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _freier_port_ohne_dienst():
    """Ein Port, auf dem sicher nichts laeuft - fuer den Ausfall-Fall."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


class ProxyTest(unittest.TestCase):
    def setUp(self):
        _Baustein.aufzeichnung = []
        _Baustein.antwort_kopf = {"Content-Type": "text/html; charset=utf-8",
                                  "Cache-Control": "no-store",
                                  "X-Nicht-Weiterreichen": "geheim"}
        _Baustein.antwort_koerper = b'{"ok": true}'
        _Baustein.antwort_status = 200
        self.baustein, self.adresse = _starte_baustein()
        self.addCleanup(self.baustein.shutdown)
        self.addCleanup(self.baustein.server_close)
        self.client = create_app(quote_adapter=object()).test_client()

    def _mit_adresse(self, adresse=None):
        return mock.patch.dict("os.environ",
                               {quality.BACKEND_ENV: adresse or self.adresse})

    # --- der Weg an sich -------------------------------------------------- #

    def test_a_get_keeps_path_and_query(self):
        with self._mit_adresse():
            antwort = self.client.get("/qualitaet/api/stock?q=AAPL&lang=de")
        self.assertEqual(200, antwort.status_code)
        self.assertEqual(1, len(_Baustein.aufzeichnung))
        anfrage = _Baustein.aufzeichnung[0]
        self.assertEqual("GET", anfrage["methode"])
        # Der Baustein laeuft selbst unter /qualitaet - das Praefix bleibt dran.
        self.assertEqual("/qualitaet/api/stock?q=AAPL&lang=de", anfrage["pfad"])

    def test_a_post_body_arrives_unchanged(self):
        koerper = json.dumps({"symbol": "AAPL", "notiz": "ümlaut"}).encode()
        with self._mit_adresse():
            antwort = self.client.post("/qualitaet/api/xang", data=koerper,
                                       content_type="application/json")
        self.assertEqual(200, antwort.status_code)
        anfrage = _Baustein.aufzeichnung[0]
        self.assertEqual("POST", anfrage["methode"])
        self.assertEqual(koerper, anfrage["koerper"])
        self.assertEqual("application/json", anfrage["content_type"])

    def test_the_root_of_the_block_works_with_and_without_the_slash(self):
        with self._mit_adresse():
            mit = self.client.get("/qualitaet/?lang=de")
            ohne = self.client.get("/qualitaet?lang=de")
        self.assertEqual(200, mit.status_code)
        self.assertEqual(200, ohne.status_code)
        self.assertEqual(["/qualitaet/?lang=de", "/qualitaet?lang=de"],
                         [eintrag["pfad"] for eintrag in _Baustein.aufzeichnung])

    # --- die Antwort kommt unveraendert zurueck --------------------------- #

    def test_the_body_and_its_type_pass_through(self):
        _Baustein.antwort_koerper = b"<html>Baustein</html>"
        _Baustein.antwort_status = 418
        with self._mit_adresse():
            antwort = self.client.get("/qualitaet/")
        self.assertEqual(418, antwort.status_code)
        self.assertEqual("<html>Baustein</html>", antwort.get_data(as_text=True))
        self.assertEqual("text/html; charset=utf-8", antwort.headers["Content-Type"])

    def test_only_the_agreed_headers_come_back(self):
        with self._mit_adresse():
            antwort = self.client.get("/qualitaet/")
        self.assertEqual("no-store", antwort.headers.get("Cache-Control"))
        # Ein Kopf, den der Baustein aus Eigenem setzt, wird NICHT weitergereicht.
        self.assertIsNone(antwort.headers.get("X-Nicht-Weiterreichen"))

    def test_a_large_body_arrives_complete(self):
        # Kein Deckel, und stueckweise durchgereicht: genau das ist der Punkt bei
        # einem Ticker mit 730 KB Kursreihe.
        _Baustein.antwort_koerper = b"x" * LANGE_ANTWORT
        with self._mit_adresse():
            antwort = self.client.get("/qualitaet/api/stock?q=AAPL")
        self.assertEqual(LANGE_ANTWORT, len(antwort.get_data()))
        self.assertEqual(str(LANGE_ANTWORT), antwort.headers["Content-Length"])

    # --- der Ausfall ------------------------------------------------------ #

    def test_an_unreachable_block_answers_503_in_german(self):
        with self._mit_adresse(_freier_port_ohne_dienst()):
            with self.assertLogs(quality.logger, level="WARNING") as protokoll:
                antwort = self.client.get("/qualitaet/")
        self.assertEqual(503, antwort.status_code)
        self.assertIn("Aktienanalyse ist momentan nicht erreichbar",
                      antwort.get_data(as_text=True))
        self.assertIn(quality.BACKEND_ENV, protokoll.output[0])

    def test_the_failure_text_follows_the_language_of_the_request(self):
        with self._mit_adresse(_freier_port_ohne_dienst()):
            with self.assertLogs(quality.logger, level="WARNING"):
                deutsch = self.client.get("/qualitaet/?lang=de")
                englisch = self.client.get("/qualitaet/?lang=en")
        self.assertIn("nicht erreichbar", deutsch.get_data(as_text=True))
        self.assertIn("not reachable", englisch.get_data(as_text=True))

    def test_the_rest_of_the_dashboard_keeps_working(self):
        # Der Ausfall des Bausteins darf keine andere Route beruehren.
        with self._mit_adresse(_freier_port_ohne_dienst()):
            with self.assertLogs(quality.logger, level="WARNING"):
                kaputt = self.client.get("/qualitaet/")
        self.assertEqual(503, kaputt.status_code)
        gesund = self.client.get("/healthz")
        self.assertEqual(200, gesund.status_code)
        self.assertEqual({"status": "ok"}, gesund.get_json())

    # --- die Adresse ------------------------------------------------------ #

    def test_the_address_comes_from_the_environment_with_a_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(quality.DEFAULT_BACKEND, quality.backend_url())
        with mock.patch.dict("os.environ", {quality.BACKEND_ENV: "http://example:1/"}):
            self.assertEqual("http://example:1", quality.backend_url(),
                             "ein abschliessender Schraegstrich wuerde den Pfad verdoppeln")

    # --- der Knopf auf der Trichterseite --------------------------------- #

    def test_the_button_appears_only_when_the_address_is_set(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            ohne = self.client.get("/")
        self.assertNotIn('id="funnel-quality"', ohne.get_data(as_text=True),
                         "ohne Adresse kein Knopf - er fuehrte garantiert auf eine 503")
        with self._mit_adresse():
            mit = self.client.get("/")
        seite = mit.get_data(as_text=True)
        self.assertIn('id="funnel-quality"', seite)
        self.assertIn('href="/qualitaet/"', seite)
        self.assertIn("Aktien-Qualität", seite)

    def test_the_button_speaks_the_language_of_the_page(self):
        with self._mit_adresse():
            englisch = self.client.get("/?lang=en").get_data(as_text=True)
        self.assertIn("Stock quality", englisch)
        self.assertNotIn("Aktien-Qualität", englisch)

    def test_the_button_stands_in_the_row_of_the_funnel_buttons(self):
        # „an der vorhandenen Knopfzeile": derselbe Formularblock wie „Scannen",
        # damit die Bedienung nicht ueber zwei Ecken verteilt ist.
        with self._mit_adresse():
            seite = self.client.get("/").get_data(as_text=True)
        form = seite.split('class="funnel-form"', 1)[1].split("</form>", 1)[0]
        self.assertIn('id="funnel-run"', form)
        self.assertIn('id="funnel-quality"', form)

    def test_the_time_limit_is_generous_and_configurable(self):
        self.assertGreaterEqual(quality.DEFAULT_TIMEOUT, 120)
        with mock.patch.dict("os.environ", {quality.TIMEOUT_ENV: "240"}):
            self.assertEqual(240.0, quality.timeout_seconds())
        with mock.patch.dict("os.environ", {quality.TIMEOUT_ENV: "kaputt"}):
            self.assertEqual(quality.DEFAULT_TIMEOUT, quality.timeout_seconds())
