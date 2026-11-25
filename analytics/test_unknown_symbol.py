"""An unknown ticker is a typo, not an outage - and the answer names the right one.

What is checked:

* The classification: a delisted / unknown ticker is told apart from a timeout or a refused
  connection, so an outage never turns into "you typed it wrong".
* The suggestion: the company name decides, not the symbol stem - ``BASF.DE`` is answered with
  ``BAS.DE`` although "BAS" is not "BASF".
* ``stock()`` raises ``UnknownSymbol`` instead of an arbitrary error, and the handler answers 404
  with the suggestion the page can offer.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import app  # noqa: E402
from yfinance.exceptions import YFDataException  # noqa: E402


class YFTzMissingError(Exception):
    """The name yfinance uses for "possibly delisted"."""


class YFPricesMissingError(Exception):
    """The sibling class yfinance raises when a delisted ticker has no prices either."""


class UnknownSymbolTest(unittest.TestCase):
    def setUp(self):
        # The negative cache is process-wide; a leftover verdict would hide the lookup under test.
        app._UNKNOWN.clear()

    def test_a_delisted_ticker_is_recognised(self):
        self.assertTrue(app.unknown_symbol(YFTzMissingError("$BASF.DE: possibly delisted")))

    def test_the_wording_is_read_only_when_yfinance_raised_it(self):
        # A YFException carrying Yahoo's own words is a missing ticker; the same sentence in a
        # caller's own RuntimeError is not, because a proxy page can quote anything.
        self.assertTrue(app.unknown_symbol(YFDataException("Quote not found for symbol: ABC")))
        self.assertFalse(app.unknown_symbol(RuntimeError("HTTP Error 404: Quote not found for symbol")))

    def test_a_delisted_ticker_without_prices_is_recognised_by_its_class(self):
        # Measured: yfinance raises YFPricesMissingError for a delisted ticker without price data, and
        # its message ("no price data found, symbol may be delisted") carries none of the markers.
        self.assertTrue(app.unknown_symbol(
            YFPricesMissingError("no price data found, symbol may be delisted (period=1d)")))

    def test_an_error_page_quoting_the_words_is_not_a_typo(self):
        # A proxy or scanner answers with its own page. A message match alone called that a typo and
        # told the reader to check a ticker that was fine.
        page = "HTTP Error 502: <html><body>Quote not found</body></html>"
        self.assertFalse(app.unknown_symbol(RuntimeError(page)))

    def test_the_real_yfinance_class_is_recognised(self):
        try:
            from yfinance.exceptions import YFPricesMissingError as Real
        except ImportError:  # pragma: no cover - yfinance is a hard dependency of the app
            self.skipTest("yfinance not importable")
        self.assertTrue(app.unknown_symbol(Real("no price data found, symbol may be delisted", {})))

    def test_an_outage_is_not_an_unknown_ticker(self):
        self.assertFalse(app.unknown_symbol(TimeoutError("timed out")))
        self.assertFalse(app.unknown_symbol(OSError("connection refused")))
        self.assertFalse(app.unknown_symbol(RuntimeError("HTTP Error 503: Service Unavailable")))

    def test_the_suggestion_compares_the_name_not_the_stem(self):
        rows = [{"symbol": "BAS.DE", "name": "BASF SE", "exchange": "XETRA"},
                {"symbol": "BASF.NS", "name": "BASF India Limited", "exchange": "NSE"}]
        with mock.patch.object(app, "yahoo_matches", return_value=rows):
            self.assertEqual(app.symbol_suggestion("BASF.DE"), "BAS.DE")

    def test_a_ticker_stem_beats_a_name_that_only_contains_it(self):
        # Measured: "SIE" was answered with ENR.DE, Siemens Energy, because its name contains "sie".
        rows = [{"symbol": "ENR.DE", "name": "Siemens Energy AG", "exchange": "XETRA"},
                {"symbol": "SIE.DE", "name": "Siemens Aktiengesellschaft", "exchange": "XETRA"}]
        with mock.patch.object(app, "yahoo_matches", return_value=rows):
            self.assertEqual(app.symbol_suggestion("SIE"), "SIE.DE")

    def test_a_missing_suffix_is_resolved_from_the_ticker(self):
        # Measured: "NESN" got nothing although the search knows NESN.SW.
        rows = [{"symbol": "NESN.SW", "name": "Nestlé S.A.", "exchange": "Swiss"}]
        with mock.patch.object(app, "yahoo_matches", return_value=rows):
            self.assertEqual(app.symbol_suggestion("NESN"), "NESN.SW")

    def test_at_most_three_candidates_and_never_the_typed_ticker(self):
        rows = [{"symbol": "BASF.DE", "name": "BASF SE", "exchange": "XETRA"},
                {"symbol": "BAS.DE", "name": "BASF SE", "exchange": "XETRA"},
                {"symbol": "BASF.NS", "name": "BASF India Limited", "exchange": "NSE"},
                {"symbol": "BFFAF", "name": "BASF SE", "exchange": "OQX"},
                {"symbol": "BASFY", "name": "BASF SE", "exchange": "OQX"}]
        with mock.patch.object(app, "yahoo_matches", return_value=rows):
            found = app.symbol_candidates("BASF.DE")
        self.assertEqual(3, len(found))
        self.assertNotIn("BASF.DE", [row["symbol"] for row in found])

    def test_a_name_that_does_not_contain_the_stem_gives_no_suggestion(self):
        rows = [{"symbol": "ABC.DE", "name": "Something Else AG", "exchange": "XETRA"}]
        with mock.patch.object(app, "yahoo_matches", return_value=rows):
            self.assertIsNone(app.symbol_suggestion("XYZQ.DE"))

    def test_a_failing_search_gives_no_suggestion(self):
        with mock.patch.object(app, "yahoo_matches", side_effect=OSError("offline")):
            self.assertIsNone(app.symbol_suggestion("BASF.DE"))

    def test_stock_names_the_right_ticker_instead_of_failing(self):
        def missing(symbol, bucket):
            raise YFTzMissingError(f"${symbol}: possibly delisted; no timezone found")

        rows = [{"symbol": "BAS.DE", "name": "BASF SE", "exchange": "XETRA"}]
        with mock.patch.object(app, "prices", side_effect=missing), \
                mock.patch.object(app, "yahoo_matches", return_value=rows):
            with self.assertRaises(app.UnknownSymbol) as caught:
                app.stock("BASF.DE", 1)
        self.assertEqual(caught.exception.suggestion, "BAS.DE")
        self.assertIn("BAS.DE", str(caught.exception))

    def test_the_verdict_is_remembered_instead_of_searching_again(self):
        # Every reload of a mistyped page used to cost a failed download plus a search of up to eight
        # seconds; the endpoint takes its ticker from the request.
        def missing(symbol, bucket):
            raise YFTzMissingError(f"${symbol}: possibly delisted")

        rows = [{"symbol": "BAS.DE", "name": "BASF SE", "exchange": "XETRA"}]
        with mock.patch.object(app, "prices", side_effect=missing) as failed, \
                mock.patch.object(app, "yahoo_matches", return_value=rows) as searched:
            for _ in range(3):
                with self.assertRaises(app.UnknownSymbol):
                    app.stock("BASF.DE", 1)
        self.assertEqual(1, failed.call_count, "one failed download, not one per view")
        self.assertEqual(1, searched.call_count, "one search, not one per view")

    def test_a_remembered_verdict_carries_the_candidates(self):
        app.remember_unknown("BASF.DE", [{"symbol": "BAS.DE", "name": "BASF SE", "exchange": "XETRA"}])
        with mock.patch.object(app, "prices") as never:
            with self.assertRaises(app.UnknownSymbol) as caught:
                app.stock("BASF.DE", 1)
        never.assert_not_called()
        self.assertEqual("BAS.DE", caught.exception.suggestion)

    def test_stock_keeps_a_real_outage_a_real_outage(self):
        def broken(symbol, bucket):
            raise TimeoutError("timed out")

        with mock.patch.object(app, "prices", side_effect=broken):
            with self.assertRaises(TimeoutError):
                app.stock("BASF.DE", 1)


class PageSuggestionTest(unittest.TestCase):
    def test_the_page_offers_the_candidates_instead_of_a_dead_end(self):
        page = (HERE / "index.html").read_text(encoding="utf-8")
        self.assertIn("function offerTickers(suggestions)", page)
        # The body has to travel with the error, otherwise the message is all that is left.
        self.assertIn("failure.json=json;", page)
        self.assertIn("offerTickers(error.json&&error.json.suggestions);", page)
