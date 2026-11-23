"""The quarterly growth rows fall back to the provider, and say so.

The page computes the year-on-year rate of revenue and earnings from its own quarterly series. Some
papers have no quarterly rows at all - L'Oréal reports half-yearly - and the European papers carry
five or six quarters, so neither the quarterly rate nor a trailing twelve month comparison can be
built from them. The provider publishes a rate of its own for these figures.

The rule under test: the own rate wins whenever it exists; otherwise the provider rate is shown under
a label that names the provider, never under the label of a quarter. Both missing leaves the row
empty instead of inventing a figure.

The helper is lifted out of the delivered page and run in node, so the code under test is the
shipped one.
"""

import json
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

function lift(name) {
  const start = script.indexOf("function " + name + "(");
  if (start < 0) throw new Error(name + " not found");
  let depth = 0, end = start;
  for (let index = script.indexOf("{", start); index < script.length; index++) {
    if (script[index] === "{") depth++;
    if (script[index] === "}") { depth--; if (depth === 0) { end = index + 1; break; } }
  }
  return script.slice(start, end);
}

const calls = [];
const context = vm.createContext({
  isNum: value => typeof value === "number" && Number.isFinite(value),
  pctRow: (label, value, low, goal, best) => { calls.push([label, value]); return [label, value]; },
});
vm.runInContext(lift("providerOrOwn") + "\n" + lift("unavailableLabel"), context);

const own = context.providerOrOwn("Quartal", "Anbieter (Anbieter)", 0.13, 0.06, -0.15, 0.10, 0.35,
                                  context.pctRow);
const ownOnly = context.providerOrOwn("Quartal", "Anbieter (Anbieter)", 0.13, null, -0.15, 0.10, 0.35,
                                      context.pctRow);
const fromProvider = context.providerOrOwn("Quartal", "Anbieter (Anbieter)", null, 0.058, -0.15, 0.10,
                                           0.35, context.pctRow);
const nothing = context.providerOrOwn("Quartal", "Anbieter (Anbieter)", null, null, -0.15, 0.10, 0.35,
                                      context.pctRow);
const emptyLabel = context.unavailableLabel("Umsatzwachstum (TTM)", null, "keine Quartalsreihe");
const filledLabel = context.unavailableLabel("Umsatzwachstum (TTM)", 0.07, "keine Quartalsreihe");
process.stdout.write(JSON.stringify({own, ownOnly, fromProvider, nothing, calls, emptyLabel, filledLabel}));
"""


class QuarterlyGrowthFallbackTests(unittest.TestCase):
    """The provider rate is used only where the own figures cannot produce the rate."""

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["node", "-e", HARNESS],
            capture_output=True, text=True, timeout=60,
            env={**__import__("os").environ, "PAGE_PATH": str(PAGE)},
        )
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr}")
        cls.out = json.loads(result.stdout)

    def test_the_own_rate_wins(self):
        self.assertEqual(self.out["own"], ["Quartal", 0.13])

    def test_the_own_rate_wins_even_when_the_provider_has_one(self):
        self.assertEqual(self.out["ownOnly"], ["Quartal", 0.13])

    def test_the_provider_rate_is_labelled_as_the_provider(self):
        self.assertEqual(self.out["fromProvider"], ["Anbieter (Anbieter)", 0.058])

    def test_no_rate_means_no_number(self):
        self.assertEqual(self.out["nothing"], ["Quartal", None])

    def test_an_empty_trailing_row_says_why_it_is_empty(self):
        """A row without a number must not look like a row with a zero."""
        self.assertEqual(self.out["emptyLabel"], "Umsatzwachstum (TTM) (keine Quartalsreihe)")

    def test_a_row_with_a_number_keeps_its_plain_label(self):
        self.assertEqual(self.out["filledLabel"], "Umsatzwachstum (TTM)")


if __name__ == "__main__":
    unittest.main()
