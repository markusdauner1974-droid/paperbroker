"""One row per company in the search: six listings of L'Oreal are one company, not six.

Measured 2026-11-22: the search for "loreal" returned OR.PA (Paris), LRLCY and LRLCF (OTC, USD),
US5021172037.SG (Stuttgart), LOR.HM (Hamburg) and LOR.F (Frankfurt) - the same company six times,
with the numbers of one listing each.
"""

import unittest
from unittest import mock
from urllib.error import URLError

import app


class CompanyKeyTests(unittest.TestCase):
    def test_spellings_of_one_company_share_a_key(self):
        """Accents, punctuation and the legal form are not part of the name."""
        self.assertEqual(app.company_key("L'Oréal S.A."), app.company_key("L'Oreal S.A. (ADRs)"))
        self.assertEqual(app.company_key("L'Oréal S.A."), app.company_key("L'Oreal SA"))
        self.assertEqual(app.company_key("SAP SE"), app.company_key("SAP AG"))

    def test_two_companies_keep_two_keys(self):
        self.assertNotEqual(app.company_key("L'Oréal S.A."), app.company_key("LVMH"))
        self.assertNotEqual(app.company_key("SAP SE"), app.company_key("Siemens AG"))

    def test_an_adr_belongs_to_its_company(self):
        """The ADR is the same company in another unit - one row, the share in front."""
        self.assertEqual(app.company_key("SAP SE"), app.company_key("SAP SE ADR"))

    def test_an_empty_or_odd_name_does_not_collapse(self):
        self.assertEqual("", app.company_key(""))
        self.assertEqual("s.a.", app.company_key("S.A."))

    def test_the_apostrophe_does_not_make_a_second_row(self):
        """Measured: "L'Oréal S.A." gave "l oreal", "L’Oréal S.A." gave "loreal" - two rows, one company."""
        self.assertEqual(app.company_key("L'Oréal S.A."), app.company_key("L’Oréal S.A."))
        self.assertEqual(app.company_key("L'Oreal SA"), app.company_key("L’Oréal"))

    def test_a_leading_the_is_one_company(self):
        """Kody: dropping "the" from the legal forms split "The Home Depot" from "Home Depot"."""
        self.assertEqual(app.company_key("Home Depot"), app.company_key("The Home Depot"))
        self.assertEqual(app.company_key("Home Depot Inc."), app.company_key("The Home Depot"))
        # a "The" in the middle belongs to the name
        self.assertNotEqual(app.company_key("Theatre Group"), app.company_key("Group"))

    def test_group_and_holdings_keep_two_companies_apart(self):
        """Kody: "X Group" and "X Holdings" became one row - the only word telling them apart."""
        self.assertNotEqual(app.company_key("Siemens Group"), app.company_key("Siemens Holdings"))
        self.assertNotEqual(app.company_key("X Holding"), app.company_key("X Holdings"))
        # ... while the legal form still does not create a second row for the same company
        self.assertEqual(app.company_key("X Holdings"), app.company_key("X Holdings AG"))


class LeadListingTests(unittest.TestCase):
    def test_the_listing_in_the_reporting_currency_wins(self):
        """Measured: Yahoo ranks NASDAQ first for ASML (USD), the home venue is Amsterdam (EUR)."""
        listings = [{"symbol": "ASML", "exchange": "NASDAQ", "exchange_code": "NMS"},
                    {"symbol": "ASML.AS", "exchange": "Amsterdam", "exchange_code": "AMS"},
                    {"symbol": "ASMLF", "exchange": "OTC Markets", "exchange_code": "PNK"}]
        facts = [{"currency": "USD", "reporting_currency": "EUR"},
                 {"currency": "EUR", "reporting_currency": "EUR"},
                 {"currency": "USD", "reporting_currency": "EUR"}]
        self.assertEqual(1, app.lead_index(listings, facts))

    def test_a_us_company_keeps_its_us_listing(self):
        listings = [{"symbol": "AAPL", "exchange": "NASDAQ", "exchange_code": "NMS"},
                    {"symbol": "APC.F", "exchange": "Frankfurt", "exchange_code": "FRA"}]
        facts = [{"currency": "USD", "reporting_currency": "USD"},
                 {"currency": "EUR", "reporting_currency": "USD"}]
        self.assertEqual(0, app.lead_index(listings, facts))

    def test_the_home_venue_wins_over_a_regional_reseller(self):
        listings = [{"symbol": "LOR.F", "exchange": "Frankfurt", "exchange_code": "FRA"},
                    {"symbol": "OR.PA", "exchange": "Paris", "exchange_code": "PAR"}]
        facts = [{"currency": "EUR"}, {"currency": "EUR"}]
        self.assertEqual(1, app.lead_index(listings, facts))

    def test_an_adr_never_stands_for_the_share(self):
        listings = [{"symbol": "LRLCY", "exchange": "OTC Markets", "exchange_code": "PNK"},
                    {"symbol": "OR.PA", "exchange": "Paris", "exchange_code": "PAR"}]
        facts = [{"currency": "USD"}, {"currency": "EUR"}]
        self.assertEqual(1, app.lead_index(listings, facts))

    def test_a_paper_only_listed_on_a_reseller_stays_visible(self):
        listings = [{"symbol": "LOR.HM", "exchange": "Hamburg", "exchange_code": "HAM"}]
        self.assertEqual(0, app.lead_index(listings, [{"currency": "EUR"}]))

    def test_the_name_decides_when_the_code_is_missing(self):
        listings = [{"symbol": "X.SG", "exchange": "Stuttgart"},
                    {"symbol": "Y.PA", "exchange": "Paris"}]
        self.assertEqual(1, app.lead_index(listings, [{}, {}]))

    def test_no_facts_at_all_still_answers(self):
        listings = [{"symbol": "X.SG", "exchange": "Stuttgart"},
                    {"symbol": "Y.PA", "exchange": "Paris"}]
        self.assertEqual(1, app.lead_index(listings, []))


class SearchSourcesTests(unittest.TestCase):
    """Yahoo answers first; the scanner only adds papers Yahoo does not return."""

    YAHOO = [{"symbol": "SAP.DE", "name": "SAP SE", "exchange": "XETRA"},
             {"symbol": "SAP", "name": "SAP SE", "exchange": "NYSE"},
             {"symbol": "SAP.TO", "name": "Saputo Inc.", "exchange": "Toronto"}]

    def test_a_scanner_row_that_is_already_a_venue_is_not_a_second_row(self):
        """Measured: "asml" gave a bare "ASML" row beside the complete "ASML.AS" one."""
        nas = mock.Mock()
        nas.search.return_value = [{"symbol": "SAP.DE", "name": "SAP SE", "exchange": "Xetra"}]
        with mock.patch.object(app, "yahoo_matches", return_value=self.YAHOO), \
                mock.patch.object(app, "group_matches",
                                  side_effect=lambda rows, *a, **k: [{"symbol": r["symbol"],
                                                                      "listings": [r]} for r in rows]):
            result = app.search("sap", nas, 0)
        self.assertEqual(3, len(result["matches"]))

    def test_a_paper_the_scanner_knows_and_yahoo_does_not_is_kept(self):
        nas = mock.Mock()
        nas.search.return_value = [{"symbol": "SAP.DE", "name": "SAP SE", "exchange": "Xetra"},
                                   {"symbol": "KRN.DE", "name": "Krones AG", "exchange": "Xetra"}]
        seen = []
        def grouping(rows, bucket=None, lookup=True, path=None):
            seen.append(lookup)
            return [{"symbol": r["symbol"], "listings": [r]} for r in rows]
        with mock.patch.object(app, "yahoo_matches", return_value=self.YAHOO), \
                mock.patch.object(app, "group_matches", side_effect=grouping):
            result = app.search("sap", nas, 0)
        self.assertIn("KRN.DE", [row["symbol"] for row in result["matches"]])
        self.assertEqual([True, True], seen, "both calls enrich, none may return bare rows")

    def test_a_failing_scanner_leaves_yahoo_alone(self):
        nas = mock.Mock()
        nas.search.side_effect = OSError("scanner down")
        with mock.patch.object(app, "yahoo_matches", return_value=self.YAHOO), \
                mock.patch.object(app, "group_matches",
                                  side_effect=lambda rows, *a, **k: [{"symbol": r["symbol"],
                                                                      "listings": [r]} for r in rows]):
            result = app.search("sap", nas, 0)
        self.assertEqual("Yahoo Finance", result["source"])

    def test_a_failing_search_says_so_instead_of_inventing_rows(self):
        with mock.patch.object(app, "yahoo_matches", side_effect=URLError("offline")):
            result = app.search("loreal", None, 0)
        self.assertEqual([], result["matches"])
        self.assertIn("offline", result["notice"])


class SameIsinTests(unittest.TestCase):
    """One ISIN, one row - measured: "apple" gave AAPL and APC.DE, both US0378331005."""

    def test_two_rows_with_one_isin_become_the_liquid_one(self):
        rows = [{"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "currency": "USD",
                 "volume": 42_000_000, "isin": "US0378331005", "listings": [{"symbol": "AAPL"}]},
                {"symbol": "APC.DE", "name": "Apple Inc.", "exchange": "GER", "currency": "EUR",
                 "volume": 19_000, "isin": "US0378331005", "listings": [{"symbol": "APC.DE"}]}]
        merged = app.merge_same_isin(rows)
        self.assertEqual(1, len(merged))
        self.assertEqual("AAPL", merged[0]["symbol"])
        self.assertEqual(42_000_000, merged[0]["volume"])
        self.assertEqual(["AAPL", "APC.DE"], [listing["symbol"] for listing in merged[0]["listings"]])

    def test_rows_without_an_isin_are_never_merged(self):
        rows = [{"symbol": "APLE", "isin": "", "volume": 1, "listings": []},
                {"symbol": "APC.DE", "isin": "", "volume": 2, "listings": []}]
        self.assertEqual(2, len(app.merge_same_isin(rows)))

    def test_two_different_isins_stay_apart(self):
        rows = [{"symbol": "OR.PA", "isin": "FR0000120321", "volume": 1, "listings": []},
                {"symbol": "LRLCY", "isin": "US5021172037", "volume": 2, "listings": []}]
        self.assertEqual(2, len(app.merge_same_isin(rows)))


class VerifiedIsinTests(unittest.TestCase):
    """A number from Yahoo is only taken when it passes the same check as the looked-up one."""

    def test_a_raw_isin_with_the_wrong_country_is_refused(self):
        with mock.patch.object(app, "company_isin", return_value="NL0010273215") as lookup:
            self.assertEqual("NL0010273215",
                             app.verified_isin("AR0725224551", "ASML Holding N.V.", "Netherlands"))
        lookup.assert_called_once()

    def test_a_raw_isin_that_passes_is_taken_without_asking_again(self):
        with mock.patch.object(app, "company_isin") as lookup:
            self.assertEqual("NL0010273215",
                             app.verified_isin("NL0010273215", "ASML Holding N.V.", "Netherlands"))
        lookup.assert_not_called()

    def test_a_malformed_raw_isin_is_refused(self):
        with mock.patch.object(app, "company_isin", return_value=""):
            for raw in ("NL001027321", "nl0010273215", "", None, 42):
                self.assertEqual("", app.verified_isin(raw, "ASML", "Netherlands"))


class GroupingTests(unittest.TestCase):
    SIX = [{"symbol": "OR.PA", "name": "L'Oréal S.A.", "exchange": "Paris"},
           {"symbol": "LRLCY", "name": "L'Oréal S.A.", "exchange": "OTC Markets"},
           {"symbol": "LRLCF", "name": "L'Oréal S.A.", "exchange": "OTC Markets"},
           {"symbol": "US5021172037.SG", "name": "L'Oreal S.A. (ADRs)", "exchange": "Stuttgart"},
           {"symbol": "LOR.HM", "name": "L'Oreal SA", "exchange": "Hamburg"},
           {"symbol": "LOR.F", "name": "L'Oréal S.A.", "exchange": "Frankfurt"}]

    def test_six_listings_become_one_row(self):
        with mock.patch.object(app, "listing_facts", return_value={"currency": "EUR",
                                                                   "volume": 362155,
                                                                   "exchange_code": "PAR",
                                                                   "isin": ""}):
            grouped = app.group_matches(self.SIX, 1)
        self.assertEqual(1, len(grouped))
        self.assertEqual("OR.PA", grouped[0]["symbol"], "the home listing carries the numbers")
        self.assertEqual("EUR", grouped[0]["currency"])

    def test_no_listing_is_lost_on_the_way(self):
        with mock.patch.object(app, "listing_facts", return_value={}):
            grouped = app.group_matches(self.SIX, 1)
        self.assertEqual(6, len(grouped[0]["listings"]))
        self.assertEqual({"OR.PA", "LRLCY", "LRLCF", "US5021172037.SG", "LOR.HM", "LOR.F"},
                         {listing["symbol"] for listing in grouped[0]["listings"]})

    def test_two_companies_stay_two_rows(self):
        matches = self.SIX + [{"symbol": "MC.PA", "name": "LVMH", "exchange": "Paris"}]
        with mock.patch.object(app, "listing_facts", return_value={}):
            grouped = app.group_matches(matches, 1)
        self.assertEqual(["OR.PA", "MC.PA"], [row["symbol"] for row in grouped])

    def test_every_row_is_offered_to_the_isin_lookup(self):
        """Kody: only the first two rows were asked, so rows three and up never got an ISIN."""
        matches = [{"symbol": symbol, "name": f"Firma {index}", "exchange": "Xetra"}
                   for index, symbol in enumerate(("A.DE", "B.DE", "C.DE", "D.DE", "E.DE"))]
        with mock.patch.object(app, "listing_facts", return_value={}), \
                mock.patch.object(app, "company_isin", side_effect=lambda name, *_: name.replace(" ", "-")) as isin:
            grouped = app.group_matches(matches, 1, path=":memory:")
        self.assertEqual(5, isin.call_count)
        self.assertEqual([f"Firma-{index}" for index in range(5)], [row["isin"] for row in grouped])

    def test_the_deadline_stops_the_lookups_not_the_rows(self):
        """A slow service must not make the search wait for every company."""
        matches = [{"symbol": symbol, "name": f"Firma {index}", "exchange": "Xetra"}
                   for index, symbol in enumerate(("A.DE", "B.DE", "C.DE"))]
        slow = mock.Mock(side_effect=lambda *args, **kwargs: "")
        with mock.patch.object(app, "listing_facts", return_value={}), \
                mock.patch.object(app, "company_isin", slow), \
                mock.patch.object(app, "monotonic", side_effect=[0.0, 0.0, 0.0, 99.0, 99.0]):
            grouped = app.group_matches(matches, 1, path=":memory:")
        self.assertEqual(3, len(grouped), "no row is lost, only its ISIN is missing")

    def test_the_scanner_path_asks_nobody(self):
        """Kody: a search must not wait on a second source that has nothing to add."""
        with mock.patch.object(app, "listing_facts") as facts, \
                mock.patch.object(app, "company_isin") as isin:
            grouped = app.group_matches(self.SIX, 1, lookup=False)
        facts.assert_not_called()
        isin.assert_not_called()
        self.assertEqual("", grouped[0]["isin"])

    def test_the_volume_travels_so_a_thin_venue_is_visible(self):
        with mock.patch.object(app, "listing_facts", return_value={"currency": "EUR", "volume": 6}):
            grouped = app.group_matches(self.SIX, 1)
        self.assertEqual(6, grouped[0]["volume"], "six shares a day must be readable as such")


if __name__ == "__main__":
    unittest.main()
