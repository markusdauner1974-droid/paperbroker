"""Die Aktienanalyse als Seite dieses Programms: ``/qualitaet/``.

Kein zweiter Dienst, keine Weiterleitung: die Seite wird hier ausgeliefert, und
die Datenendpunkte rufen die Funktionen des gelieferten Programms direkt auf.

Die Rechenwege selbst (Kriterien, CAGR, Piotroski, Abschlüsse) liegen weiter in
``aktienanalyse/`` und werden dort geprüft — ``check_nas.py`` deckt sie mit 15
Tests ab. Hier steht der **Verteiler**: welche Adresse welche Funktion ruft,
welche Grenzen gelten und was bei einem Fehler herauskommt. Deshalb ersetzt in
diesen Tests ein einfaches Doppel das gelieferte Modul: kein Netz, keine Kurse,
nur der Weg.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from paperbroker.routes import quality
from paperbroker.server import create_app


def _fake(**ersatz):
    """Ein Doppel des gelieferten Programms, das nur mitschreibt."""
    aufrufe = []

    def merke(name, ergebnis):
        def ruf(*args, **kwargs):
            aufrufe.append((name, args, kwargs))
            return ergebnis

        return ruf

    modul = SimpleNamespace(
        search=merke("search", {"source": "Yahoo Finance", "matches": [{"symbol": "AAPL"}]}),
        stock=merke("stock", {"symbol": "AAPL", "prices": [[1, 2, 3, 4]], "issues": []}),
        nas_stocks=merke("nas_stocks", []),
        statement_profile=merke("statement_profile", {"statements": {}}),
        stock_extra=merke("stock_extra", {"symbol": "AAPL"}),
        prices=merke("prices", [[1, 2, 3, 4]]),
        with_statements=merke("with_statements", {"symbol": "AAPL"}),
        nas_rows=merke("nas_rows", []),
        price_jumps=lambda rows: [],
        nas_info=merke("nas_info", {}),
        nas_price_series=merke("nas_price_series", {"symbol": "AAPL", "prices": []}),
    )
    for name, wert in ersatz.items():
        setattr(modul, name, wert)
    return modul, aufrufe


class QualityPageTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(quote_adapter=object()).test_client()

    def test_the_page_is_served_here(self):
        antwort = self.client.get("/qualitaet/")
        seite = antwort.get_data(as_text=True)
        self.assertEqual(200, antwort.status_code)
        self.assertIn("text/html", antwort.headers["Content-Type"])
        # Die echte Seite, nicht ein Platzhalter: sie ist rund 88 000 Zeichen groß.
        self.assertGreater(len(seite), 80_000)
        self.assertIn("<title>", seite)

    def test_the_page_knows_where_it_runs(self):
        seite = self.client.get("/qualitaet/").get_data(as_text=True)
        self.assertEqual(1, seite.count('<script>window.AKT_BASE_PATH="/qualitaet"</script>'),
                         "genau eine Einspielung, direkt hinter dem <html>-Tag")

    def test_the_address_works_with_and_without_the_slash(self):
        self.assertEqual(200, self.client.get("/qualitaet/").status_code)
        self.assertEqual(200, self.client.get("/qualitaet").status_code)

    def test_an_unknown_data_path_answers_404(self):
        with mock.patch.object(quality, "_akt", lambda: _fake()[0]):
            antwort = self.client.get("/qualitaet/api/gibtsnicht")
        self.assertEqual(404, antwort.status_code)
        self.assertIn("Datenweg unbekannt", antwort.get_json()["error"])


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(quote_adapter=object()).test_client()

    def _mit(self, **ersatz):
        modul, aufrufe = _fake(**ersatz)
        return mock.patch.object(quality, "_akt", lambda: modul), \
            mock.patch.object(quality, "nas", lambda: None), aufrufe

    def test_a_search_reaches_the_module_with_the_typed_text(self):
        akt, scanner, aufrufe = self._mit()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/search?q=apple")
        self.assertEqual(200, antwort.status_code)
        self.assertEqual({"source": "Yahoo Finance", "matches": [{"symbol": "AAPL"}]},
                         antwort.get_json())
        self.assertEqual("apple", aufrufe[0][1][0])

    def test_an_empty_search_is_refused_with_a_sentence(self):
        akt, scanner, _ = self._mit()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/search?q=")
        self.assertEqual(400, antwort.status_code)
        self.assertIn("Namen, Ticker oder eine ISIN", antwort.get_json()["error"])

    def test_a_stock_reaches_the_module(self):
        akt, scanner, aufrufe = self._mit()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/stock?q=AAPL")
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("AAPL", antwort.get_json()["symbol"])
        self.assertEqual("stock", aufrufe[0][0])

    def test_two_tickers_where_one_belongs_are_refused(self):
        akt, scanner, _ = self._mit()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/stock?q=AAPL,MSFT")
        self.assertEqual(400, antwort.status_code)
        self.assertIn("genau einen Ticker", antwort.get_json()["error"])

    def test_sixteen_tickers_in_a_batch_are_refused(self):
        akt, scanner, _ = self._mit()
        with akt, scanner:
            antwort = self.client.get(
                "/qualitaet/api/stock-batch?q=" + ",".join(f"A{i}" for i in range(16)))
        self.assertEqual(400, antwort.status_code)
        self.assertIn("bis zu 15", antwort.get_json()["error"])

    def test_a_broken_date_is_refused(self):
        akt, scanner, _ = self.mit_statements()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/statements?q=AAPL&cutoff=murks")
        self.assertEqual(400, antwort.status_code)
        self.assertIn("error", antwort.get_json())

    def mit_statements(self):
        return self._mit()

    def test_the_comparison_answers_with_series(self):
        akt, scanner, aufrufe = self._mit()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/compare?q=AAPL,MSFT")
        self.assertEqual(200, antwort.status_code)
        self.assertEqual(2, len(antwort.get_json()["series"]))
        self.assertEqual("AAPL", antwort.get_json()["series"][0]["symbol"])

    def test_the_nas_is_optional_and_says_so(self):
        akt, scanner, _ = self._mit()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/xang?q=AAPL")
        self.assertEqual(200, antwort.status_code)
        self.assertEqual({"status": "disabled"}, antwort.get_json())

    def test_the_extension_needs_the_nas(self):
        akt, scanner, _ = self._mit()
        with akt, scanner:
            antwort = self.client.get("/qualitaet/api/stock-extra?q=AAPL&cutoff=2026-11-18")
        self.assertEqual(400, antwort.status_code)
        self.assertIn("NAS-Kursprofil", antwort.get_json()["error"])


class ButtonTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(quote_adapter=object()).test_client()

    def test_the_button_stands_in_the_button_row_of_the_funnel(self):
        seite = self.client.get("/").get_data(as_text=True)
        form = seite.split('class="funnel-form"', 1)[1].split("</form>", 1)[0]
        self.assertIn('id="funnel-run"', form)
        self.assertIn('id="funnel-quality"', form)
        self.assertIn('href="/qualitaet/"', form)
        self.assertIn("Aktien-Qualität", form)

    def test_the_button_speaks_the_language_of_the_page(self):
        englisch = self.client.get("/?lang=en").get_data(as_text=True)
        self.assertIn("Stock quality", englisch)
        self.assertNotIn("Aktien-Qualität", englisch)
