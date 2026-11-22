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
        text = PAGE.read_text(encoding="utf-8")
        self.assertIn("isNum(info.dividendYield) ? info.dividendYield", text)
        self.assertIn("division(info.dividendRate, price)", text)


if __name__ == "__main__":
    unittest.main()
