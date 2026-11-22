"""The header shows the same cells for a scanner paper and for one the scanner does not hold.

Written after L'Oreal's header carried six cells where SAP's carried twelve: the layout may not
depend on which scanner happens to hold a paper.
"""

import pathlib
import re
import unittest

PAGE = pathlib.Path(__file__).with_name("index.html")


def branches():
    """The scanner branch and the other branch of the header, as source text."""
    text = PAGE.read_text(encoding="utf-8")
    start = text.index("if(data.nas_primary){")
    stop = text.index("// Volatility and drawdown come from the daily closes")
    block = text[start:stop]
    # The first "}else{" belongs to a button above; the branch that matters is the last one.
    scanner, other = block.rsplit("}else{", 1)
    return scanner, other


def labels(source):
    """The cell labels a branch builds, without the source suffix."""
    found = re.findall(r'\["([^"]+?) · (?:NAS|Yahoo Finance)"', source)
    return [name.strip() for name in found]


class HeaderBranchesTests(unittest.TestCase):
    def test_both_branches_build_the_same_six_cells(self):
        scanner, other = branches()
        self.assertEqual(6, len(labels(scanner)), "the scanner branch builds six cells")
        self.assertEqual(labels(scanner), labels(other), "same fields, same order, both branches")

    def test_no_cell_is_conditional_in_the_other_branch(self):
        """A dash says 'not available'; a missing cell hides the question."""
        _scanner, other = branches()
        self.assertNotIn("if(isNum(", other, "the cells stand even when the value is missing")
        self.assertIn("providerDividendYield(", other,
                      "a payout given per share is divided by the price, not dropped")

    def test_a_payout_per_share_is_turned_into_a_yield(self):
        """Yahoo's `dividendYield` is in percent - 1.66 for L'Oreal, not 0.0166.

        Treated as a fraction it printed 166 %. The amount per share has no such doubt.
        """
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn("division(info.dividendRate, price)", text)
        self.assertNotIn("isNum(info.dividendYield)", text, "percent mistaken for a fraction")


class EveryPrintedFieldReachesThePageTests(unittest.TestCase):
    """A figure Yahoo holds but app.stock() does not pass on arrives as a dash - silently.

    `priceToBook` was missing from the tuple for months: L'Oreal has 6.84 at the source and the
    header printed a dash, which looks like a gap in the data instead of a gap in the plumbing.
    """

    # Read from the payload, not from Yahoo: these are set by the scanner or derived in the page.
    NOT_FROM_YAHOO = {"marketCapCurrency", "nasDividendYield", "nasProvider", "test"}

    def test_every_info_field_the_page_prints_is_passed_on(self):
        page = PAGE.read_text(encoding="utf-8")
        printed = {name for name in re.findall(r"\bi\.([a-zA-Z][a-zA-Z0-9]*)", page)
                   if name not in self.NOT_FROM_YAHOO and name != "priceToSalesTrailing"}
        source = (PAGE.parent / "app.py").read_text(encoding="utf-8")
        missing = sorted(name for name in printed if f'"{name}"' not in source)
        self.assertEqual([], missing, "fields the page prints but app.stock() drops")


if __name__ == "__main__":
    unittest.main()
