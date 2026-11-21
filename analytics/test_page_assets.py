"""The page wears the type of the funnel, and it fetches it from the program itself.

The fonts ship once, for the funnel, and the analysis serves the same files through its own
address. Tested here: the files exist where the page expects them, the placeholder in
index.html is replaced, and a name that is not a font is refused.
"""

import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
