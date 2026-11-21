"""The page wears the type of the funnel, and it fetches it from the program itself.

The fonts ship once, for the funnel, and the analysis serves the same files through its own
address. Tested here: the files exist where the page expects them, the placeholder in
index.html is replaced, and a name that is not a font is refused.
"""

import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402  — the delivered program, loaded by path in production


class _Seite:
    """Just enough of the request handler for page(), which only reads base_path."""

    base_path = ""


class FontsTest(unittest.TestCase):
    def test_the_three_fonts_ship_with_the_program(self):
        for name in ("instrument-sans-latin.woff2", "martian-mono-latin.woff2",
                     "oswald-latin.woff2"):
            with self.subTest(name=name):
                datei = app.FONT_DIR / name
                self.assertTrue(datei.is_file(), f"{datei} fehlt")
                self.assertGreater(datei.stat().st_size, 5_000)

    def test_the_rules_name_the_three_families(self):
        regeln = app.FONT_CSS.read_text(encoding="utf-8")
        for familie in ("Instrument Sans", "Martian Mono", "Oswald"):
            with self.subTest(familie=familie):
                self.assertIn(familie, regeln)

    def test_the_served_page_links_the_rules_and_leaves_no_placeholder(self):
        seite = app.Handler.page(_Seite()).decode()
        self.assertIn('href="/fonts.css"', seite)
        self.assertNotIn(app.ASSET_BASE, seite)

    def test_a_missing_stylesheet_answers_404_instead_of_raising(self):
        """Without the funnel's files the page must fall back to system fonts, not drop the socket."""
        fehlt = Path("/nicht/vorhanden/fonts.css")
        with mock.patch.object(app, "FONT_CSS", fehlt):
            inhalt, typ = app.Handler.asset(_Seite(), "/fonts.css")
        self.assertIsNone(inhalt)
        self.assertIsNone(typ)

    def test_a_name_that_is_not_a_font_is_refused(self):
        stub = _Seite()
        for pfad in ("/fonts/passwd", "/fonts/../app.py", "/fonts/app.py",
                     "/fonts/instrument-sans-latin.woff2.bak"):
            with self.subTest(pfad=pfad):
                inhalt, typ = app.Handler.asset(stub, pfad)
                self.assertIsNone(inhalt)
                self.assertIsNone(typ)

    def test_a_font_is_read_from_the_program_folder(self):
        inhalt, typ = app.Handler.asset(_Seite(), "/fonts/oswald-latin.woff2")
        self.assertEqual("font/woff2", typ)
        self.assertGreater(len(inhalt), 5_000)
        regeln, typ = app.Handler.asset(_Seite(), "/fonts.css")
        self.assertEqual("text/css; charset=utf-8", typ)
        self.assertIn("@font-face", regeln.decode())


class _ServerTest(unittest.TestCase):
    """The real socket, because Cache-Control is written by the handler, not by asset()."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def holen(self, pfad):
        verbindung = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            verbindung.request("GET", pfad)
            antwort = verbindung.getresponse()
            return antwort.status, dict(antwort.getheaders()), antwort.read()
        finally:
            verbindung.close()

    def test_a_font_may_be_cached_like_the_program_serves_it(self):
        """no-store here would make the browser fetch six files on every page view."""
        status, kopf, inhalt = self.holen("/fonts/oswald-latin.woff2")
        self.assertEqual(200, status)
        self.assertEqual("font/woff2", kopf["Content-Type"])
        self.assertIn("max-age=604800", kopf["Cache-Control"])
        self.assertGreater(len(inhalt), 5_000)

    def test_the_rules_may_be_cached_as_well(self):
        status, kopf, inhalt = self.holen("/fonts.css")
        self.assertEqual(200, status)
        self.assertIn("max-age=604800", kopf["Cache-Control"])
        self.assertIn(b"@font-face", inhalt)

    def test_an_unknown_font_answers_404_without_dropping_the_socket(self):
        status, _, inhalt = self.holen("/fonts/app.py")
        self.assertEqual(404, status)
        self.assertIn("error", inhalt.decode())

    def test_the_page_still_answers_no_store(self):
        status, kopf, _ = self.holen("/")
        self.assertEqual(200, status)
        self.assertEqual("no-store", kopf["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
