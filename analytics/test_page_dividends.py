"""The payout row answers for papers whose quote and reports use different currencies.

BYD quotes in HKD and reports in CNY. The page guarded the payout with a same-currency check and
left the row open, although the annual report carries both numbers the ratio needs - dividends paid
and net income, both in CNY. The yield row was guarded the same way even though an amount per share
over a price in the same quote currency involves no reporting currency at all.

The rule under test: a same-currency paper keeps the per-share route; a cross-currency paper takes
the statement route; nothing usable on either leaves the row empty instead of a guess.

Both helpers are lifted out of the delivered page and run in node, so the code under test is the
shipped one.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "index.html"

HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(process.env.PAGE_PATH, "utf8");
const opening = html.indexOf("<script>");
const script = html.slice(opening + 8, html.indexOf("</script>", opening));

function lift(name, kind) {
  if (kind === "const") {
    const start = script.search(new RegExp("const\\s+" + name + "\\s*="));
    if (start < 0) throw new Error(name + " not found");
    const end = script.indexOf(";", script.indexOf("=", start));
    return script.slice(start, end + 1);
  }
  const start = script.indexOf("function " + name + "(");
  if (start < 0) throw new Error(name + " not found");
  let depth = 0, end = start;
  for (let index = script.indexOf("{", start); index < script.length; index++) {
    if (script[index] === "{") depth++;
    if (script[index] === "}") { depth--; if (depth === 0) { end = index + 1; break; } }
  }
  return script.slice(start, end);
}

const context = vm.createContext({
  isNum: value => typeof value === "number" && Number.isFinite(value),
});
// A `const` at the top of a vm script lands in the lexical scope, not on the context object, so the
// two lifted consts are published explicitly before the test calls them.
vm.runInContext([lift("isNum", "const"), lift("division", "const"),
                 lift("payoutRatio"), lift("providerDividendYield", "const"),
                 "globalThis.providerDividendYield = providerDividendYield;"].join("\n"), context);

const out = {
  // same currency: dividend per share over earnings per share
  same: context.payoutRatio(true, 4.70, 6.02, 5_000_000_000, 6_000_000_000),
  // cross currency: dividends paid over net income, both in the reporting currency (BYD 2025)
  cross: context.payoutRatio(false, 0.41, 3.94, 13_140_273_000, 32_619_022_000),
  // nothing usable on either route
  noStatements: context.payoutRatio(false, 0.41, 3.94, null, 32_619_022_000),
  lossMaking: context.payoutRatio(false, 0.41, -1.2, 13_140_273_000, -2_000_000_000),
  sameCurrencyWithoutEps: context.payoutRatio(true, 0.41, null, 13_140_273_000, 32_619_022_000),
  // the provider yield is amount per share over price, both in the quote currency
  providerYield: context.providerDividendYield({dividendRate: 0.41}, 61.34),
  providerYieldWithoutAmount: context.providerDividendYield({}, 61.34),
};
process.stdout.write(JSON.stringify(out));
"""


class PayoutRatioTests(unittest.TestCase):
    """A cross-currency paper keeps its payout row; a missing figure still leaves it open."""

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["node", "-e", HARNESS],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PAGE_PATH": str(PAGE)},
        )
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr}")
        cls.out = json.loads(result.stdout)

    def test_one_currency_takes_the_per_share_route(self):
        self.assertAlmostEqual(self.out["same"], 4.70 / 6.02, places=9)

    def test_two_currencies_take_the_statement_route(self):
        """BYD: 13,14 Mrd. CNY paid out on 32,62 Mrd. CNY net income."""
        self.assertAlmostEqual(self.out["cross"], 13_140_273_000 / 32_619_022_000, places=9)
        self.assertGreater(self.out["cross"], 0.35)
        self.assertLess(self.out["cross"], 0.45)

    def test_a_missing_statement_figure_leaves_the_row_open(self):
        self.assertIsNone(self.out["noStatements"])

    def test_a_loss_has_no_payout_ratio(self):
        self.assertIsNone(self.out["lossMaking"])

    def test_a_missing_eps_leaves_the_same_currency_row_open(self):
        self.assertIsNone(self.out["sameCurrencyWithoutEps"])

    def test_the_provider_yield_needs_no_reporting_currency(self):
        self.assertAlmostEqual(self.out["providerYield"], 0.41 / 61.34, places=9)

    def test_the_provider_yield_stays_open_without_an_amount(self):
        self.assertIsNone(self.out["providerYieldWithoutAmount"])

    def test_the_payout_row_falls_back_with_a_reason(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('unavailableLabel(sameCurrency?"Ausschüttungsquote TTM"', page)
        self.assertIn('"keine gezahlte Dividende im Abschluss gefunden"', page)

    def test_the_yield_row_no_longer_asks_for_the_reporting_currency(self):
        """Amount per share over price is quote currency over quote currency."""
        page = PAGE.read_text(encoding="utf-8")
        self.assertNotIn("sameCurrency?division(i.dividendRate,close):null", page)
        self.assertIn("i.nasDividendYield??providerDividendYield(i,close)", page)


if __name__ == "__main__":
    unittest.main()
