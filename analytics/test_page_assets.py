"""The page wears the type of the funnel, and it fetches it from the program itself.

The fonts ship once, for the funnel, and the analysis serves the same files through its own
address. Tested here: the files exist where the page expects them, the placeholder in
index.html is replaced, a name that is not a font is refused, and the answers carry the headers
they promise.
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


class _Request:
    """Just enough of the request handler for page(), which only reads base_path."""

    base_path = ""


class FontsTest(unittest.TestCase):
    def test_the_three_fonts_ship_with_the_program(self):
        for name in ("instrument-sans-latin.woff2", "martian-mono-latin.woff2",
                     "oswald-latin.woff2"):
            with self.subTest(name=name):
                font = app.FONT_DIR / name
                self.assertTrue(font.is_file(), f"{font} is missing")
                self.assertGreater(font.stat().st_size, 5_000)

    def test_the_rules_name_the_three_families(self):
        rules = app.FONT_CSS.read_text(encoding="utf-8")
        for family in ("Instrument Sans", "Martian Mono", "Oswald"):
            with self.subTest(family=family):
                self.assertIn(family, rules)

    def test_the_served_page_links_the_rules_and_leaves_no_placeholder(self):
        page = app.Handler.page(_Request()).decode()
        self.assertIn('href="/fonts.css"', page)
        self.assertNotIn(app.ASSET_BASE, page)

    def test_a_missing_stylesheet_answers_404_instead_of_raising(self):
        """Without the funnel's files the page must fall back to system fonts, not drop the socket."""
        missing = Path("/nicht/vorhanden/fonts.css")
        with mock.patch.object(app, "FONT_CSS", missing):
            content, content_type = app.Handler.asset(_Request(), "/fonts.css")
        self.assertIsNone(content)
        self.assertIsNone(content_type)

    def test_a_name_that_is_not_a_font_is_refused(self):
        request = _Request()
        for path in ("/fonts/passwd", "/fonts/../app.py", "/fonts/app.py",
                     "/fonts/instrument-sans-latin.woff2.bak"):
            with self.subTest(path=path):
                content, content_type = app.Handler.asset(request, path)
                self.assertIsNone(content)
                self.assertIsNone(content_type)

    def test_a_font_is_read_from_the_program_folder(self):
        content, content_type = app.Handler.asset(_Request(), "/fonts/oswald-latin.woff2")
        self.assertEqual("font/woff2", content_type)
        self.assertGreater(len(content or b""), 5_000)
        content, content_type = app.Handler.asset(_Request(), "/fonts.css")
        self.assertEqual("text/css; charset=utf-8", content_type)
        self.assertIn("@font-face", (content or b"").decode())


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

    def fetch(self, path):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", path)
            answer = connection.getresponse()
            return answer.status, dict(answer.getheaders()), answer.read()
        finally:
            connection.close()

    def test_a_font_may_be_cached_like_the_program_serves_it(self):
        """no-store here would make the browser fetch six files on every page view."""
        status, headers, content = self.fetch("/fonts/oswald-latin.woff2")
        self.assertEqual(200, status)
        self.assertEqual("font/woff2", headers["Content-Type"])
        self.assertIn("max-age=604800", headers["Cache-Control"])
        self.assertGreater(len(content), 5_000)

    def test_the_rules_may_be_cached_as_well(self):
        status, headers, content = self.fetch("/fonts.css")
        self.assertEqual(200, status)
        self.assertIn("max-age=604800", headers["Cache-Control"])
        self.assertIn(b"@font-face", content)

    def test_an_unknown_font_answers_404_without_dropping_the_socket(self):
        status, _, content = self.fetch("/fonts/app.py")
        self.assertEqual(404, status)
        self.assertIn("error", content.decode())

    def test_the_page_still_answers_no_store(self):
        status, headers, _ = self.fetch("/")
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
