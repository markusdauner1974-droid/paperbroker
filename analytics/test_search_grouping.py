"""One row per company in the search: six listings of L'Oreal are one company, not six.

Measured 2026-11-22: the search for "loreal" returned OR.PA (Paris), LRLCY and LRLCF (OTC, USD),
US5021172037.SG (Stuttgart), LOR.HM (Hamburg) and LOR.F (Frankfurt) - the same company six times,
with the numbers of one listing each.
"""

import unittest
from unittest import mock

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
