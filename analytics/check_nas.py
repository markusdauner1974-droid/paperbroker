"""Test the NAS HTTP contract and provider selection without network access."""

import json
import unittest
from contextlib import contextmanager
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

import app
import filing_statements
import sec_statements
from nas_scanner import NasScanner
from sec_statements import parse_statements


class NasResponse:
    @contextmanager
    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        route = parsed.path.removeprefix("/api/v1/")
        body = json.loads(request.data) if request.data else None
        self.requests.append((route, body))
        if route == "auth/status":
            payload = {"required": True, "authenticated": True}
        elif route == "stocks/search":
            payload = [
                {"symbol": "AAPL", "name": "Apple Inc", "sector": "Technology", "industry": "Consumer Electronics"},
                {"symbol": "AAPB", "name": "Apple ETF", "industry": "Exchange Traded Fund"},
            ]
        elif self.old and route.endswith("/batch"):
            raise HTTPError(request.full_url, 405, "Method not allowed", {}, None)
        elif route == "stocks/history/batch":
            assert body["period"] == "5y"
            payload = {"data": {ticker: self.history for ticker in body["symbols"]
                                if ticker in ("AAPL", "SAP.DE", "SPY")}, "missing": []}
        elif route == "stocks/AAPL/history":
            payload = self.history
        elif route == "stocks/fundamentals/batch":
            payload = {"data": {"AAPL": self.apple, "SAP.DE": self.sap}, "missing": []}
        elif route == "stocks/AAPL/decision-dashboard":
            payload = {"symbol": "AAPL", "fundamentals": self.apple}
        else:
            raise HTTPError(request.full_url, 404, "Missing", {}, None)
        yield BytesIO(json.dumps(payload).encode())

    def __init__(self):
        self.requests = []
        self.old = False
        self.history = [{"date": "2026-11-18", "close": 280},
                        {"date": "2026-11-17", "close": 270},
                        {"date": "2026-11-19", "close": None}]
        self.apple = {"data_source": "yfinance", "eps_current": 8.98, "roe": 1.42257,
                      "dividend_yield": .31, "market_cap": 3e12,
                      "fx_metadata": {"from_currency": "USD", "to_currency": "USD"}}
        self.sap = {"data_source": "finviz", "roe": 11.5, "dividend_yield": 1.2,
                    "market_cap": 2e11, "fx_metadata": {"from_currency": "EUR", "to_currency": "USD"}}


class NasTests(unittest.TestCase):
    def setUp(self):
        self.nas = NasScanner("http://nas.example:8089")
        self.nas.opener = NasResponse()
        self.nas.connect()
        self.external = patch.object(app.yf, "Ticker", side_effect=AssertionError("Unexpected Yahoo fetch"))
        self.yahoo_ticker = self.external.start()
        self.addCleanup(self.external.stop)
        search = patch.object(app.yf, "Search", side_effect=AssertionError("Unexpected Yahoo search"))
        self.yahoo_search = search.start()
        self.addCleanup(search.stop)

    def call(self, path, nas=True):
        handler = app.Handler.__new__(app.Handler)
        handler.path = path
        handler.nas = self.nas if nas else None
        handler.json = Mock()
        handler.do_GET()
        return handler.json.call_args.args

    def test_search_and_stock_stay_local(self):
        status, matches = self.call("/api/search?q=Apple")
        self.assertEqual(status, 200)
        self.assertEqual(matches["source"], "xang auf NAS")
        self.assertEqual([row["symbol"] for row in matches["matches"]], ["AAPL"])
        status, stock = self.call("/api/stock?q=AAPL")
        self.assertEqual(status, 200)
        self.assertEqual(stock["info"]["longName"], "Apple Inc")
        self.assertEqual(stock["info"]["returnOnEquity"], 1.42257)
        self.assertAlmostEqual(stock["info"]["nasDividendYield"], .0031)
        self.assertEqual(stock["prices"][-1], ["2026-11-18", 280, None, None])
        self.assertEqual(stock["statements"], {})
        self.yahoo_ticker.assert_not_called()
        self.yahoo_search.assert_not_called()

    def test_batch_handles_gaps_and_native_currency(self):
        status, result = self.call("/api/stock-batch?q=AAPL,SAP.DE,MISSING")
        self.assertEqual(status, 200)
        self.assertEqual([row["symbol"] for row in result["stocks"]], ["AAPL", "SAP.DE"])
        info = result["stocks"][1]["info"]
        self.assertEqual(info["marketCapCurrency"], "EUR")
        self.assertAlmostEqual(info["returnOnEquity"], .115)
        self.assertAlmostEqual(info["nasDividendYield"], .012)
        self.assertEqual(self.nas.opener.requests[-2:], [
            ("stocks/history/batch", {"symbols": ["AAPL", "SAP.DE", "MISSING"], "period": "5y"}),
            ("stocks/fundamentals/batch", {"symbols": ["AAPL", "SAP.DE", "MISSING"]}),
        ])
        status, result = self.call("/api/compare?q=SPY,MISSING")
        self.assertEqual(status, 200)
        self.assertEqual(result["series"][0]["price_source"], "xang auf NAS")
        self.assertIn("error", result["series"][1])
        self.yahoo_ticker.assert_not_called()

    def test_legacy_api_reads_cached_history_and_dashboard(self):
        self.nas.opener.old = True
        status, stock = self.call("/api/stock?q=AAPL")
        self.assertEqual(status, 200)
        self.assertEqual(stock["info"]["trailingEps"], 8.98)
        self.assertEqual(stock["quote_source"], "xang auf NAS")
        self.yahoo_ticker.assert_not_called()

    def test_missing_stock_may_fall_back_but_failed_auth_wont(self):
        with patch.object(app, "stock", return_value={"symbol": "MISSING", "issues": []}) as fallback:
            status, result = self.call("/api/stock?q=MISSING")
            self.assertEqual(status, 200)
            self.assertIn("Keine NAS-Tageskurse", result["issues"][0])
            fallback.assert_called_once()
            fallback.reset_mock()
            with patch.object(self.nas, "histories", side_effect=HTTPError("", 401, "", {}, None)):
                status, result = self.call("/api/stock?q=AAPL")
            self.assertEqual(status, 502)
            self.assertIn("NAS-Sitzung", result["error"])
            fallback.assert_not_called()

    def test_missing_price_keeps_cached_fundamentals(self):
        series = [["2026-11-18", 280, 279, 0]]
        with (patch.object(self.nas, "histories", return_value={}),
              patch.object(app, "prices", return_value=series) as fallback,
              patch.object(app, "stock") as full_fallback):
            status, result = self.call("/api/stock?q=AAPL")
        self.assertEqual(status, 200)
        self.assertEqual(result["prices"], series)
        self.assertEqual(result["quote_source"], "Yahoo Finance")
        self.assertEqual(result["fundamentals_source"], "xang auf NAS")
        self.assertEqual(result["info"]["trailingEps"], 8.98)
        fallback.assert_called_once()
        full_fallback.assert_not_called()

    def test_mueller_uses_consistent_split_basis_in_all_views(self):
        raw = [["2024-09-17", 35.61], ["2024-09-18", 70.94],
               ["2025-11-18", 105.64], ["2026-06-30", 122.93],
               ["2026-07-01", 57.42], ["2026-11-18", 53.67]]
        entries = [{"date": day, "close": close} for day, close in raw]
        reference = [["2024-09-17", 35.61, 35, 0],
                     ["2024-09-18", 35.47, 35, 0],
                     ["2025-11-18", 52.82, 52.35, 0],
                     ["2026-06-30", 61.465, 61.29, 0],
                     ["2026-07-01", 57.42, 57.26, 0],
                     ["2026-11-18", 53.67, 53.67, 0],
                     ["2026-11-19", 54, 54, 0]]
        paths = [("/api/stock?q=MLI", None), ("/api/stock-batch?q=MLI", "stocks"),
                 ("/api/compare?q=MLI", "series")]
        with (patch.object(self.nas, "histories", return_value={"MLI": entries}),
              patch.object(self.nas, "fundamentals", return_value={"MLI": self.nas.opener.apple}),
              patch.object(app, "history_reference", return_value=(reference, "USD"))):
            for path, key in paths:
                status, result = self.call(path)
                self.assertEqual(status, 200)
                series = result[key][0] if key else result
                self.assertEqual(series["price_source"], "Yahoo Finance (Splitbereinigung)")
                self.assertEqual(series["quote_price"], 53.67)
                self.assertEqual(series["prices"][-1], ["2026-11-18", 53.67, None, None])
                closes = {row[0]: row[1] for row in series["prices"]}
                self.assertAlmostEqual(closes["2026-11-18"] / closes["2025-11-18"] - 1,
                                       .01609, places=5)
                self.assertIn("splitbereinigt", series["issues"][0])

    def test_unconfirmed_jump_only_keeps_the_latest_consistent_segment(self):
        rows = [["2026-07-01", 110, None, None], ["2026-07-02", 55, None, None],
                ["2026-07-03", 60, None, None]]
        reference = [["2026-07-01", 55, None, None], *rows[1:]]
        with patch.object(app, "history_reference", return_value=(reference, "EUR")):
            series = app.nas_price_series("MLI", rows, "USD", 0)
        self.assertEqual(series["prices"], rows[1:])
        self.assertIn("2026-07-02", series["issues"][0])
        with patch.object(app, "history_reference", return_value=(rows, "USD")):
            series = app.nas_price_series("MLI", rows, "USD", 0)
        self.assertEqual(series["prices"], rows)
        self.assertEqual(series["issues"], [])

    def test_explicit_extension_rejects_foreign_currency(self):
        ticker = Mock()
        ticker.get_info.return_value = {"financialCurrency": "EUR"}
        ticker.history_metadata = {"currency": "EUR"}
        self.yahoo_ticker.side_effect = None
        self.yahoo_ticker.return_value = ticker
        self.addCleanup(app.stock_extra.cache_clear)
        rows = [["2026-11-18", 280, 279, 0], ["2026-11-19", 285, 284, 0]]
        statements = {"income": {"2026-09-30": {"totalrevenue": 100}}}
        profile = {"statements": statements, "statements_source": "Yahoo Finance",
                   "info": {"financialCurrency": "EUR"}, "data_gaps": []}
        with patch.object(app, "load_history", return_value=rows), \
                patch.object(app, "statement_profile", return_value=profile):
            status, data = self.call("/api/stock-extra?q=AAPL&cutoff=2026-11-18&currency=USD")
            self.assertEqual(status, 200)
            self.assertEqual(data["prices"], [])
            self.assertEqual(data["info"], {"financialCurrency": "EUR"})
            self.assertEqual(data["statements_source"], "Yahoo Finance")
            status, data = self.call("/api/stock-extra?q=SAP.DE&cutoff=2026-11-18&currency=EUR")
            self.assertEqual(status, 200)
            self.assertEqual(data["prices"], rows[:1])
            self.assertEqual(data["price_source"], "Yahoo Finance")
            status, _ = self.call("/api/stock-extra?q=AAPL&cutoff=2026-11-18&currency=USD", nas=False)
            self.assertEqual(status, 400)

    def test_statements_use_sec_without_yahoo_and_can_enrich_group(self):
        sec = {"income": {"2026-09-30": {"totalrevenue": 100}}}
        with patch.object(app, "load_sec_statements", return_value=sec) as provider:
            app.statement_profile.cache_clear()
            self.addCleanup(app.statement_profile.cache_clear)
            status, result = self.call("/api/statements?q=AAPL&cutoff=2026-11-18")
            self.assertEqual(status, 200)
            self.assertEqual(result["statements_source"], "SEC EDGAR")
            self.assertEqual(result["info"], {"financialCurrency": "USD"})
            status, result = self.call("/api/stock-batch?q=AAPL&statements=1")
            self.assertEqual(status, 200)
            self.assertEqual(result["stocks"][0]["statements"], sec)
            self.assertEqual(result["stocks"][0]["price_source"], "xang auf NAS")
            provider.assert_called_once()
        self.yahoo_ticker.assert_not_called()

    def test_non_us_reports_keep_currency_and_cutoff(self):
        self.yahoo_ticker.side_effect = None
        self.yahoo_ticker.return_value.get_info.return_value = {"financialCurrency": "EUR"}
        reports = {"income": {"2025-12-31": {"totalrevenue": 84}, "2026-12-31": {"totalrevenue": 100}}}
        self.addCleanup(app.statement_profile.cache_clear)
        with patch.object(app, "load_statements", return_value=reports), patch.object(app, "load_sec_statements") as sec:
            status, result = self.call("/api/statements?q=SAP.DE&cutoff=2026-11-18")
        self.assertEqual(status, 200)
        self.assertEqual(result["statements_source"], "Yahoo Finance")
        self.assertEqual(result["info"], {"financialCurrency": "EUR"})
        self.assertEqual(set(result["statements"]["income"]), {"2025-12-31"})
        sec.assert_not_called()


class FilingStatementTests(unittest.TestCase):
    def setUp(self):
        # The collector writes what it learns into a SQLite store. These tests must
        # not write into the store the application uses: a second run would then be
        # served from it and never reach the provider at all.
        self.store_dir = TemporaryDirectory()
        self.addCleanup(self.store_dir.cleanup)
        self.store_env = patch.dict(filing_statements.os.environ,
                                    {"AKTIENANALYSE_DB": str(Path(self.store_dir.name) / "test.db")})
        self.store_env.start()
        self.addCleanup(self.store_env.stop)
        self.addCleanup(filing_statements.filing_statements.cache_clear)

    def test_unknown_european_ticker_retrieves_original_report_by_identifier(self):
        company = {"id": 638, "lei": "ABCD123456ABCDEFGH12", "isins": ["DE000TEST012"]}
        base = "https://cdn.financialreports.eu/financialreports/media/filings/"
        annual = base + "638/annual.xhtml"
        half = base + "638/half.xhtml"
        interim = base + "638/interim.xhtml"
        future = base + "638/future.xhtml"

        def document(end, revenue, extension=0):
            return f"""<html xmlns="http://www.w3.org/1999/xhtml"
              xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
              xmlns:x="http://www.xbrl.org/2003/instance"
              xmlns:iso="http://www.xbrl.org/2003/iso4217"
              xmlns:f="https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full">
              <x:context id="c"><x:entity><x:identifier scheme="http://standards.iso.org/iso/17442">
              {company['lei']}</x:identifier></x:entity><x:period>
              <x:startDate>2025-01-01</x:startDate><x:endDate>{end}</x:endDate></x:period></x:context>
              <x:context id="s"><x:entity><x:identifier>{company['lei']}</x:identifier>
              <x:segment><item>Product line</item></x:segment></x:entity>
              <x:period><x:startDate>2025-01-01</x:startDate><x:endDate>{end}</x:endDate></x:period></x:context>
              <x:unit id="EUR"><x:measure>iso:EUR</x:measure></x:unit>
              <ix:nonFraction name="f:Revenue" contextRef="c" unitRef="EUR" scale="3">{revenue}</ix:nonFraction>
              <ix:nonFraction name="f:Revenue" contextRef="s" unitRef="EUR" scale="3">999</ix:nonFraction>
              <ix:nonFraction name="f:ProfitLossBeforeTax" contextRef="c" unitRef="EUR" scale="3">{extension}</ix:nonFraction>
              <ix:nonFraction name="f:BasicEarningsLossPerShare" contextRef="c" unitRef="EUR">3.50</ix:nonFraction>
              </html>""".encode()

        data = {annual: document("2025-12-31", "800", 120), half: document("2025-06-30", "300"),
                interim: document("2025-09-30", "515")}
        # The provider is asked twice: once for the annual reports, once for every report of a
        # period. A filing without a period and a package extension is an ad-hoc notice.
        def meldung(published, address, period, extension):
            return {"company": company, "release_datetime": published, "document": address,
                    "period_ending_date": None, "fiscal_period": period,
                    "file_extension": extension}

        annual_report = meldung("2026-03-01T09:00:00Z", annual, "FY", "ZIP")
        filings = [
            meldung("2026-05-01T09:00:00Z", future, "FY", "ZIP"),
            annual_report,
            meldung("2025-11-01T09:00:00Z", interim, "Q3", "ZIP"),
            meldung("2025-08-01T09:00:00Z", half, "H1", "ZIP"),
        ]
        fallback = {"income": {"2025-12-31": {"totalrevenue": 790000}}}
        self.addCleanup(filing_statements.filing_statements.cache_clear)
        self.addCleanup(app.statement_profile.cache_clear)
        with (patch.dict(filing_statements.os.environ, {"AKTIENANALYSE_FILINGS_KEY": "test-only"}),
              patch.object(filing_statements, "listing",
                           side_effect=[[company], [annual_report], filings]) as lookup,
              patch.object(filing_statements, "read_bytes", side_effect=data.__getitem__) as fetch,
              patch.object(app, "load_statements", return_value=fallback),
              patch.object(app.yf, "Ticker") as ticker):
            ticker.return_value.get_info.return_value = {"financialCurrency": "EUR"}
            first = app.statement_profile("UNBEKANNT.DE", "2026-04-01", 0)
            self.assertIs(first, app.statement_profile("UNBEKANNT.DE", "2026-04-01", 0))
            lookup.assert_any_call("companies", ticker="UNBEKANNT.DE", page_size=2)
            self.assertEqual(lookup.call_args_list[1].kwargs["company"], 638)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [annual, interim, half])
        result = first["statements"]
        self.assertIn("XBRL", first["statements_source"])
        # The report tags one figure per share class, so the earnings per share of the
        # original filing now arrive as well (3.50 in the fixture document).
        self.assertEqual(result["income"]["2025-12-31"],
                         {"totalrevenue": 800_000, "pretaxincome": 120_000, "basiceps": 3.5})
        self.assertEqual(result["quarterly"]["2025-09-30"]["totalrevenue"], 215000)
        source = result["provenance"]["quarterly"]["2025-09-30"]["totalrevenue"]
        self.assertEqual(source["url"], interim)
        self.assertEqual(source["previous_url"], half)

    def test_ambiguous_or_unconfigured_lookup_does_not_download(self):
        loader = filing_statements.filing_statements
        self.addCleanup(loader.cache_clear)
        with (patch.dict(filing_statements.os.environ, {"AKTIENANALYSE_FILINGS_KEY": ""}),
              patch.object(filing_statements, "listing") as lookup):
            self.assertEqual(loader("AMBIG.PA", "2026-04-01", "EUR", 1), ({}, None))
            lookup.assert_not_called()
        with (patch.dict(filing_statements.os.environ, {"AKTIENANALYSE_FILINGS_KEY": "test-only"}),
              patch.object(filing_statements, "listing", return_value=[{"id": 1}, {"id": 2}]),
              patch.object(filing_statements, "read_bytes") as fetch):
            self.assertEqual(loader("AMBIG.PA", "2026-04-01", "EUR", 2), ({}, None))
            fetch.assert_not_called()


class SecStatementTests(unittest.TestCase):
    def test_mueller_q4_eps_from_exhibit_uses_known_split_at_each_cutoff(self):
        # Numbers from MLI's EDGAR Q4 exhibit and Q2/Q3 comparative filings.
        exhibit = b"""<table>
          <tr><td></td><td colspan="6">For the Quarter Ended</td><td colspan="6">For the Year Ended</td></tr>
          <tr><td>In thousands, except per share data</td><td>December 27, 2025</td>
          <td>December 28, 2024</td><td>December 27, 2025</td><td>December 28, 2024</td></tr>
          <tr><td>Net sales</td><td>$</td><td>962,385</td><td>$</td><td>923,536</td>
          <td>$</td><td>4,178,547</td><td>$</td><td>3,768,766</td></tr>
          <tr><td>Basic earnings per share</td><td>$</td><td>1.41</td><td>$</td><td>1.23</td>
          <td>$</td><td>6.99</td><td>$</td><td>5.43</td></tr>
          <tr><td>Diluted earnings per share</td><td>$</td><td>1.38</td><td>$</td><td>1.21</td>
          <td>$</td><td>6.86</td><td>$</td><td>5.31</td></tr></table>"""
        index = b"""<table><tr><td>2</td><td>Press release</td>
          <td><a href="exhibit991q42025earningsre.htm">exhibit</a></td><td>EX-99.1</td></tr></table>"""
        filing = "0000089439-26-000002"
        root = "https://www.sec.gov/Archives/edgar/data/89439/000008943926000002/"
        url = root + "exhibit991q42025earningsre.htm"

        def fact(start, end, value, filed, seq, form="10-Q"):
            return {"start": start, "end": end, "val": value, "filed": filed,
                    "form": form, "accn": "0000089439-" + seq}

        periods = [
            ("2024-12-29", "2025-03-29", 1000165000, "2025-04-23", "25-000027"),
            ("2025-03-30", "2025-06-28", 1138173000, "2025-07-23", "25-000044"),
            ("2025-06-29", "2025-09-27", 1077824000, "2025-10-22", "25-000058"),
            ("2026-06-28", "2026-09-26", 1418598000, "2026-10-21", "26-000041"),
        ]
        annual = ("2024-12-29", "2025-12-27", 4178547000, "2026-02-24", "26-000008")
        paired = [
            ("2025-03-30", "2025-06-28", 2.22, 110942000, "2025-07-23", "25-000044"),
            ("2024-12-29", "2025-06-28", 3.60, 112007000, "2025-07-23", "25-000044"),
            ("2025-03-30", "2025-06-28", 1.11, 221884000, "2026-07-22", "26-000032"),
            ("2024-12-29", "2025-06-28", 1.80, 224014000, "2026-07-22", "26-000032"),
            ("2025-06-29", "2025-09-27", 1.88, 110930000, "2025-10-22", "25-000058"),
            ("2025-06-29", "2025-09-27", .94, 221860000, "2026-10-21", "26-000041"),
            ("2026-06-28", "2026-09-26", 1.00, 220117000, "2026-10-21", "26-000041"),
        ]
        eps = [fact(s, e, value, filed, seq) for s, e, value, _, filed, seq in paired]
        eps.append(fact(annual[0], annual[1], 6.86, annual[3], annual[4], "10-K"))
        facts = {
            "Revenues": {"units": {"USD": [*(fact(*row) for row in periods), fact(*annual, "10-K")]}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": eps}},
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {
                "shares": [fact(s, e, shares, filed, seq) for s, e, _, shares, filed, seq in paired]}},
        }
        submissions = {"accessionNumber": [filing], "form": ["8-K"], "filingDate": ["2026-02-03"],
                       "items": ["2.02,9.01"]}
        payload = {"cik": 89439, "facts": {"us-gaap": facts}}
        self.addCleanup(sec_statements._sec_statements_for_day.cache_clear)
        with (patch.object(sec_statements, "ticker_index", return_value={"MLI": 89439}),
              patch.object(sec_statements, "request_json", return_value=payload),
              patch.object(sec_statements, "recent_filings", return_value=submissions),
              patch.object(sec_statements, "request_html", side_effect={
                  root + filing + "-index.htm": index, url: exhibit}.__getitem__) as fetch):
            before = sec_statements.load_sec_statements("MLI", date(2026, 6, 29), 0)
            after = sec_statements.load_sec_statements("MLI", date(2026, 11, 18), 0)
            self.assertEqual(fetch.call_count, 4)
        self.assertEqual(before["quarterly"]["2025-12-27"]["dilutedeps"], 1.38)
        self.assertEqual(before["income"]["2025-12-27"]["dilutedeps"], 6.86)
        self.assertEqual(after["quarterly"]["2025-12-27"]["totalrevenue"], 962385000)
        self.assertEqual(after["quarterly"]["2025-12-27"]["dilutedeps"], .69)
        self.assertEqual(after["quarterly"]["2025-09-27"]["dilutedeps"], .94)
        self.assertEqual(after["income"]["2025-12-27"]["dilutedeps"], 3.43)
        self.assertEqual(after["provenance"]["quarterly"]["2025-12-27"]["dilutedeps"],
                         {"period_start": "2025-09-28", "period_end": "2025-12-27",
                          "filed": "2026-02-03", "unit": "USD/shares", "reported_value": 1.38,
                          "split_factor": 2.0, "url": url, "source": "earnings_release"})
        self.assertIsNone(sec_statements.release_eps(exhibit.replace(b"For the Quarter Ended", b"For the Year Ended"),
                                                      date(2025, 12, 27)))
        self.assertIsNone(sec_statements.release_eps(exhibit, date(2024, 12, 28)))
        with patch.object(sec_statements, "recent_filings", return_value=submissions):
            self.assertIsNone(sec_statements.find_q4_release(89439, date(2025, 12, 27), date(2026, 2, 2), 0))

    def test_live_companyfacts_eps_unit_is_applied(self):
        annual = {"start": "2025-09-28", "end": "2026-09-26", "val": 8.98,
                  "form": "10-K", "filed": "2026-11-03"}
        quarter = {"start": "2026-03-29", "end": "2026-06-27", "val": 2.02,
                   "form": "10-Q", "filed": "2026-07-31"}
        for unit in ("USD/shares", "USD-per-shares"):
            with self.subTest(unit=unit):
                facts = {
                    "Revenues": {"units": {"USD": [{**annual, "val": 478834000000},
                                                  {**quarter, "val": 109417000000}]}},
                    "EarningsPerShareDiluted": {"units": {unit: [annual, quarter]}},
                }
                result = parse_statements({"facts": {"us-gaap": facts}}, date(2026, 11, 18))
                self.assertEqual(result["income"]["2026-09-26"]["dilutedeps"], 8.98)
                self.assertEqual(result["quarterly"]["2026-06-27"]["dilutedeps"], 2.02)

    def test_quarters_are_periods_not_ytd_and_eps_is_not_subtracted(self):
        def fact(start, end, value, form="10-Q", filed="2026-02-01"):
            return {"start": start, "end": end, "filed": filed, "form": form, "val": value}

        quarter_rows = [fact("2025-01-01", "2025-03-31", 10), fact("2025-04-01", "2025-06-30", 20),
                        fact("2025-07-01", "2025-09-30", 30)]
        annual_rows = [fact(f"{year}-01-01", f"{year}-12-31", 100, "10-K") for year in range(2020, 2026)]
        future = fact("2025-01-01", "2025-12-31", 1000, "10-K/A", "2026-05-01")
        facts = {"Revenues": {"units": {"USD": [*quarter_rows, *annual_rows, future,
                                               fact("2025-01-01", "2025-06-30", 30)]}},
                 "EarningsPerShareDiluted": {"units": {"USD-per-shares": quarter_rows + annual_rows}}}
        result = parse_statements({"facts": {"us-gaap": facts}}, date(2026, 2, 15))
        self.assertEqual(len(result["income"]), 6)
        self.assertEqual(result["income"]["2025-12-31"]["totalrevenue"], 100)
        self.assertEqual(result["quarterly"]["2025-06-30"]["totalrevenue"], 20)
        self.assertEqual(result["quarterly"]["2025-12-31"], {"totalrevenue": 40})


if __name__ == "__main__":
    unittest.main(verbosity=2)
