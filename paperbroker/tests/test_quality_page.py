"""The stock analysis as a page of this program: ``/qualitaet/``.

No second service, no forwarding: the page is served here, and the data endpoints
call the functions of the delivered program directly.

The arithmetic itself (criteria, CAGR, Piotroski, statements) stays in
``analytics/`` and is tested there — ``check_nas.py`` covers it with 15 tests.
What is tested here is the **dispatch**: which address calls which function, which
limits apply, and what a failure turns into. That is why a simple stand-in replaces
the delivered module in these tests: no network, no quotes, just the path.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from paperbroker.routes import quality
from paperbroker.server import create_app


def _fake(**overrides):
    """A stand-in for the delivered program that only takes notes."""
    calls = []

    def record(name, result):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result

        return call

    module = SimpleNamespace(
        search=record("search", {"source": "Yahoo Finance", "matches": [{"symbol": "AAPL"}]}),
        stock=record("stock", {"symbol": "AAPL", "prices": [[1, 2, 3, 4]], "issues": []}),
        nas_stocks=record("nas_stocks", []),
        statement_profile=record("statement_profile", {"statements": {}}),
        stock_extra=record("stock_extra", {"symbol": "AAPL"}),
        prices=record("prices", [[1, 2, 3, 4]]),
        with_statements=record("with_statements", {"symbol": "AAPL"}),
        nas_rows=record("nas_rows", []),
        price_jumps=lambda rows: [],
        nas_info=record("nas_info", {}),
        nas_price_series=record("nas_price_series", {"symbol": "AAPL", "prices": []}),
    )
    for name, value in overrides.items():
        setattr(module, name, value)
    return module, calls


class QualityPageTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(quote_adapter=object()).test_client()

    def test_the_page_is_served_here(self):
        response = self.client.get("/qualitaet/")
        page = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("text/html", response.headers["Content-Type"])
        # The real page, not a placeholder: it is roughly 88 000 characters long.
        self.assertGreater(len(page), 80_000)
        self.assertIn("<title>", page)

    def test_the_page_knows_where_it_runs(self):
        page = self.client.get("/qualitaet/").get_data(as_text=True)
        self.assertEqual(1, page.count('<script>window.AKT_BASE_PATH="/qualitaet"</script>'),
                         "exactly one injection, right behind the <html> tag")

    def test_the_address_works_with_and_without_the_slash(self):
        self.assertEqual(200, self.client.get("/qualitaet/").status_code)
        self.assertEqual(200, self.client.get("/qualitaet").status_code)

    def test_an_unknown_data_path_answers_404(self):
        with mock.patch.object(quality, "_akt", lambda: _fake()[0]):
            response = self.client.get("/qualitaet/api/doesnotexist")
        self.assertEqual(404, response.status_code)
        self.assertIn("Datenweg unbekannt", response.get_json()["error"])


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(quote_adapter=object()).test_client()

    def _with_fakes(self):
        module, calls = _fake()
        return (mock.patch.object(quality, "_akt", lambda: module),
                mock.patch.object(quality, "nas", lambda: None),
                calls)

    def test_a_search_reaches_the_module_with_the_typed_text(self):
        akt, scanner, calls = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/search?q=apple")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"source": "Yahoo Finance", "matches": [{"symbol": "AAPL"}]},
                         response.get_json())
        self.assertEqual("apple", calls[0][1][0])

    def test_an_empty_search_is_refused_with_a_sentence(self):
        akt, scanner, _ = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/search?q=")
        self.assertEqual(400, response.status_code)
        self.assertIn("Namen, Ticker oder eine ISIN", response.get_json()["error"])

    def test_a_stock_reaches_the_module(self):
        akt, scanner, calls = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/stock?q=AAPL")
        self.assertEqual(200, response.status_code)
        self.assertEqual("AAPL", response.get_json()["symbol"])
        self.assertEqual("stock", calls[0][0])

    def test_two_tickers_where_one_belongs_are_refused(self):
        akt, scanner, _ = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/stock?q=AAPL,MSFT")
        self.assertEqual(400, response.status_code)
        self.assertIn("genau einen Ticker", response.get_json()["error"])

    def test_sixteen_tickers_in_a_batch_are_refused(self):
        akt, scanner, _ = self._with_fakes()
        with akt, scanner:
            response = self.client.get(
                "/qualitaet/api/stock-batch?q=" + ",".join(f"A{i}" for i in range(16)))
        self.assertEqual(400, response.status_code)
        self.assertIn("bis zu 15", response.get_json()["error"])

    def test_a_broken_date_is_refused(self):
        akt, scanner, _ = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/statements?q=AAPL&cutoff=murks")
        self.assertEqual(400, response.status_code)
        self.assertIn("error", response.get_json())

    def test_the_comparison_answers_with_series(self):
        akt, scanner, _ = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/compare?q=AAPL,MSFT")
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(response.get_json()["series"]))
        self.assertEqual("AAPL", response.get_json()["series"][0]["symbol"])

    def test_the_nas_is_optional_and_says_so(self):
        akt, scanner, _ = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/xang?q=AAPL")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "disabled"}, response.get_json())

    def test_a_broken_installation_answers_502_not_a_stack_trace(self):
        # The page reads json.error; an escaping exception must not become a 500 page.
        with mock.patch.object(quality, "_akt", side_effect=RuntimeError("broken")):
            with mock.patch.object(quality, "nas", lambda: None):
                response = self.client.get("/qualitaet/api/search?q=apple")
        self.assertEqual(502, response.status_code)
        self.assertIn("error", response.get_json())

    def test_a_ticker_of_up_to_24_signs_is_accepted(self):
        # The delivered program allows 24 signs; a narrower rule here would reject
        # what the standalone entry point serves.
        akt, scanner, calls = self._with_fakes()
        with akt, scanner:
            response = self.client.get(
                "/qualitaet/api/stock?q=" + "ABCDEFGHIJKLMNOPQRSTUVWX")
        self.assertEqual(200, response.status_code)
        self.assertEqual("ABCDEFGHIJKLMNOPQRSTUVWX", calls[0][1][0])

    def test_a_scanner_hit_without_closes_gets_the_yahoo_series(self):
        # nas_stocks admits tickers with fundamentals but no daily closes; the
        # delivered dispatcher fills in the Yahoo series, so this one must too.
        module, _ = _fake(nas_stocks=lambda *a, **k: [{"symbol": "AAPL",
                                                      "prices": [],
                                                      "issues": []}])
        with mock.patch.object(quality, "_akt", lambda: module), \
                mock.patch.object(quality, "nas", lambda: object()):
            response = self.client.get("/qualitaet/api/stock?q=AAPL")
        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertEqual([[1, 2, 3, 4]], data["prices"], "Yahoo series fills the gap")
        self.assertEqual("Yahoo Finance", data["price_source"])
        self.assertEqual(["Keine NAS-Tageskurse für diesen Ticker."], data["issues"])

    def test_the_extension_needs_the_nas(self):
        akt, scanner, _ = self._with_fakes()
        with akt, scanner:
            response = self.client.get("/qualitaet/api/stock-extra?q=AAPL&cutoff=2026-11-18")
        self.assertEqual(400, response.status_code)
        self.assertIn("NAS-Kursprofil", response.get_json()["error"])


class ButtonTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(quote_adapter=object()).test_client()

    def test_the_button_stands_in_the_button_row_of_the_funnel(self):
        page = self.client.get("/").get_data(as_text=True)
        form = page.split('class="funnel-form"', 1)[1].split("</form>", 1)[0]
        self.assertIn('id="funnel-run"', form)
        self.assertIn('id="funnel-quality"', form)
        self.assertIn('href="/qualitaet/"', form)
        self.assertIn("Aktien-Qualität", form)

    def test_the_button_speaks_the_language_of_the_page(self):
        english = self.client.get("/?lang=en").get_data(as_text=True)
        self.assertIn("Stock quality", english)
        self.assertNotIn("Aktien-Qualität", english)
