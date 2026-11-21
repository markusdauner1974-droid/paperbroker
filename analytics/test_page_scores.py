"""The two rules the alignment with the reference turned on, tested on the shipped script.

The page's scoring lives in its own JavaScript, so this test lifts the functions out of
index.html and runs them in node - the code under test is the delivered one, not a copy.

Checked here:

* ``trendFit`` reports how evenly a series runs, not where it goes. A steadily falling
  revenue used to score 0 % stability, which cost GAP two groups in the quality check.
* ``applyGroupRanks`` hands the pass/fail of a criterion to the percentile inside the
  comparison group, with the cutoffs the reference publishes (50 %, 65 %, 25 %, 10 %).
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
const results = {};

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

const cutoffs = [50, 65, 50, 65, 50, 50, 50, 50, 50, 50, 50, 25, 25, 10, 10];
const context = vm.createContext({
  isNum: value => typeof value === "number" && Number.isFinite(value),
  meanOf: values => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null,
  clamp: x => Math.max(0, Math.min(1, x)),
  cutoffsFor: () => cutoffs,
  qualityLowerIsBetter: new Set([7, 10, 13, 14]),
  Math,
});
vm.runInContext(lift("trendFit") + "\n" + lift("applyGroupRanks"), context);

// R² of a straight line is 1 - in both directions.
results.fallingLine = context.trendFit([5, 4, 3, 2, 1]);
results.risingLine = context.trendFit([1, 2, 3, 4, 5]);
results.flat = context.trendFit([3, 3, 3, 3]);
results.zigzag = context.trendFit([1, -1, 1, -1, 1]);
// A revenue that wobbles but holds its level must not read as perfectly stable either.
results.wobble = context.trendFit([100, 130, 110, 140, 105, 135]);

const row = (label, value) => [label, String(value), null, "Ziel ≥ 50 %", null, null, value];
const group = (ranks, mask, score, count) => ({
  result: {ranks, mask, score, count, results: [{}, {}], error: null},
});
const groups = () => [["Gruppe", [row("A", 1), row("B", 2), row("C", 3)]]];

// Ranks 80 / 40 / 65 against the cutoffs 50 / 65 / 50: only the first and third pass.
const pass = groups();
const applied = context.applyGroupRanks(pass, group([80, 40, 65], [true, true, true], 2, 3), 0);
results.applied = applied;
results.flags = pass[0][1].map(r => r[2]);
results.thresholds = pass[0][1].map(r => r[3]);
results.bars = pass[0][1].map(r => r[5]);

// A criterion the group cannot compare (mask false) keeps its own verdict.
const masked = groups();
context.applyGroupRanks(masked, group([80, 0, 0], [true, false, false], 1, 3), 0);
results.maskedStays = masked[0][1][1][2];

// A failed ranking must leave the rows alone.
const broken = groups();
results.brokenApplied = context.applyGroupRanks(broken, {result: {error: "zu wenige"}}, 0);
results.brokenFlags = broken[0][1].map(r => r[2]);

console.log(JSON.stringify(results));
"""


class PageScoresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = json.loads(subprocess.run(
            ["node", "-e", HARNESS], capture_output=True, text=True, check=True,
            env={"PAGE_PATH": str(PAGE), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        ).stdout)

    def test_a_steadily_falling_series_counts_as_stable(self):
        self.assertAlmostEqual(1.0, self.result["fallingLine"], places=6)
        self.assertAlmostEqual(1.0, self.result["risingLine"], places=6)

    def test_a_flat_series_has_no_spread(self):
        self.assertEqual(0, self.result["flat"])

    def test_a_zigzag_is_not_stable(self):
        self.assertLess(self.result["zigzag"], 0.5)
        self.assertLess(self.result["wobble"], 0.5)

    def test_the_group_rank_decides_the_criterion(self):
        self.assertTrue(self.result["applied"])
        self.assertEqual([True, False, True], self.result["flags"])
        self.assertEqual([80, 40, 65], self.result["bars"])
        self.assertIn("besser als 80 %", self.result["thresholds"][0])
        self.assertIn("Ziel ≥ 50 %", self.result["thresholds"][0])

    def test_a_criterion_without_a_rank_keeps_its_own_verdict(self):
        self.assertIsNone(self.result["maskedStays"])

    def test_a_failed_ranking_changes_nothing(self):
        self.assertFalse(self.result["brokenApplied"])
        self.assertEqual([None, None, None], self.result["brokenFlags"])


if __name__ == "__main__":
    unittest.main()
