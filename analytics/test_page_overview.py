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
    """The search answers with one card per company: name, code and currency, venues as chips.

    The row used to spell out the venue and the ISIN and to hide the other venues behind a toggle.
    Markus: the arrangement was unreadable, the cards belong in three columns, and the venue does
    not need a name - the ticker says it (9PDA.DU is Düsseldorf).
    """

    def test_the_card_names_the_company_and_its_own_code(self):
        body = script()
        self.assertIn('const facts=element("div",[match.symbol,match.currency]', body)
        self.assertIn('const name=element("div",match.name,"card-name")', body)

    def test_the_venue_is_a_chip_named_by_its_ticker(self):
        body = script()
        self.assertIn('const chip=element("button",listing.symbol,index?"chip":"chip lead")', body)
        # The spelled-out venue sits in the tooltip, not in the chip.
        self.assertIn("chip.title=listing.exchange||listing.symbol", body)
        self.assertNotIn("weiterer Börsenplatz", body)

    def test_the_card_carries_no_volume_and_no_volume_warning(self):
        body = script()
        card = body[body.index('const card=element("div",undefined,"card")'):body.index("status(result.notice||")]
        self.assertNotIn("Stück/Tag", card)
        self.assertNotIn("dünn gehandelt", card)


class VenueListTests(unittest.TestCase):
    def test_the_cards_sit_in_a_three_column_grid(self):
        self.assertIn("#matches{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))", TEXT)

    def test_the_grid_falls_back_to_two_and_one_column(self):
        self.assertIn("@media(max-width:1000px){#matches{grid-template-columns:repeat(2,minmax(0,1fr))}}", TEXT)
        self.assertIn("@media(max-width:700px){#matches{grid-template-columns:1fr}}", TEXT)

    def test_the_chips_wrap_inside_the_card(self):
        self.assertIn(".chips{display:flex;flex-wrap:wrap", TEXT)

    def test_the_old_toggle_is_gone(self):
        self.assertNotIn("extra.hidden=true", script())
        self.assertNotIn("#matches .venues[hidden]{display:none}", TEXT)


class LeftoverTests(unittest.TestCase):
    def test_the_archive_button_is_gone(self):
        self.assertNotIn("SAP-Archiv", TEXT)
        self.assertNotIn('$("example")', script())

    def test_no_loading_sentence_and_no_loaded_sentence(self):
        body = script()
        self.assertIn('<progress id="search-busy"', TEXT)
        self.assertNotIn('status(symbol+" geladen.")', body)
        # the peer field keeps its own loading note - that one is about another request
        self.assertNotIn('status("Daten für "', body)


class ProgressBarTests(unittest.TestCase):
    """The bar fills with the work; an empty or spinning bar says nothing about progress."""

    def test_the_bar_has_a_value_and_is_not_a_spinner(self):
        body = script()
        self.assertIn("bar.value=shown", body, "a spinner has no value - this bar is filled")
        self.assertIn("bar.value=0", body)
        self.assertIn("requestAnimationFrame(tick)", body, "it grows frame by frame")

    def test_the_bar_knows_it_runs_to_a_hundred(self):
        """A <progress> stops at max and the default is 1: percentages would be full at once."""
        self.assertIn('max="100"', TEXT, "the markup must not leave the element at max=1")
        self.assertIn("bar.max=100", script(), "and the script sets it too, whatever the markup says")

    def test_the_load_reports_its_steps(self):
        body = script()
        step = body[body.index("async function loadStock(symbol)"):body.index("async function completeStatements")]
        self.assertIn("busy.start()", step)
        self.assertIn("busy.step(.6)", step, "the main data is the first big share")
        self.assertIn("busy.step(.97)", step)
        self.assertIn("busy.done()", step, "and it ends full")

    def test_statements_move_the_bar_too(self):
        body = script()
        step = body[body.index("async function completeStatements"):body.index("$(" + '"search").onsubmit')]
        self.assertIn("busy.step(.8)", step)

    def test_the_end_is_full_before_it_disappears(self):
        body = script()
        self.assertIn("done(){to(100);", body)
        self.assertIn("setTimeout(()=>{bar.hidden=true", body)

    def test_a_finished_load_leaves_no_text_behind(self):
        body = script()
        step = body[body.index("async function loadStock(symbol)"):body.index("async function completeStatements")]
        self.assertIn('busy.step(.97);status("")', step)


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
