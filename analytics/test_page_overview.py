"""The overview above the page: a short search row, a closed list of venues, no leftover text.

Written after the deployed page showed the daily volume in the search row, all venues open under
the toggle, a "SAP-Archiv vom 18.11.2026" button nobody wanted, a "OR.PA geladen." line, and the
whole source list as one sentence above the first chart.
"""

import pathlib
import re
import unittest

PAGE = pathlib.Path(__file__).with_name("index.html")
TEXT = PAGE.read_text(encoding="utf-8")


def script():
    """The page's only script block."""
    found = re.search(r"<script>(.*)</script>", TEXT, re.S)
    assert found is not None, "the page has no script block"
    return found.group(1)


class SearchRowTests(unittest.TestCase):
    def test_the_row_names_the_venue_currency_and_isin(self):
        body = script()
        self.assertIn('const place=[match.symbol,match.exchange,match.currency]', body)
        self.assertIn('const line=place+(match.isin?" · ISIN "+match.isin:"")', body)

    def test_the_row_carries_no_volume_and_no_volume_warning(self):
        body = script()
        row = body[body.index("const place=[match.symbol"):body.index("button.onclick=()=>loadStock(match.symbol)")]
        self.assertNotIn("Stück/Tag", row)
        self.assertNotIn("dünn gehandelt", row)


class VenueListTests(unittest.TestCase):
    def test_the_extra_venues_start_closed(self):
        self.assertIn("extra.hidden=true", script())

    def test_a_closed_list_stays_closed(self):
        """The CSS rule for `.venues` overrode `hidden` - the five venues were always on screen."""
        self.assertIn("#matches .venues[hidden]{display:none}", TEXT)

    def test_the_venues_sit_in_columns(self):
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", TEXT)


class LeftoverTests(unittest.TestCase):
    def test_the_archive_button_is_gone(self):
        self.assertNotIn("SAP-Archiv", TEXT)
        self.assertNotIn('$("example")', script())

    def test_no_loading_sentence_and_no_loaded_sentence(self):
        body = script()
        self.assertIn('<progress id="search-busy"', TEXT)
        self.assertIn('function busy(on){$("search-busy").hidden=!on;}', body)
        self.assertNotIn('status(symbol+" geladen.")', body)
        # the peer field keeps its own loading note - that one is about another request
        self.assertNotIn('status("Daten für "', body)


class ProvenanceTests(unittest.TestCase):
    def test_the_source_line_sits_in_a_closed_fold(self):
        fold = TEXT[TEXT.index('<details class="panel wide" id="provenance">'):]
        fold = fold[:fold.index("</details>")]
        self.assertIn('<summary>Quellen und Herkunft der Daten</summary>', fold)
        self.assertIn('<p id="source"></p>', fold)

    def test_the_fold_names_every_source(self):
        fold = TEXT[TEXT.index('id="provenance"'):]
        fold = fold[:fold.index("</details>")]
        for source in ("Yahoo Finance", "NAS-Scanner", "XBRL", "Fonrex", "Wikidata"):
            self.assertIn(source, fold)

    def test_the_source_line_left_the_header(self):
        header = TEXT[TEXT.index("<header>"):TEXT.index("</header>")]
        self.assertNotIn('id="source"', header)
