"""Price stability, the ROCE guard and the period label, tested on the shipped script.

The scoring lives in index.html's own JavaScript, so this test lifts the functions out of the
delivered file and runs them in node - the code under test is the shipped one, not a copy.

Checked here:

* ``priceStability`` measures ten years of month-end closes and keeps the sign of the trend.
  Twelve months of daily closes never reached the target (SAP 0.4 % against 76 % at the
  reference) and turned a straight crash into the best stability score in the file.
* ``stabilityLabel`` names the span the series really covers, so a young listing does not read
  as a ten year window.
* ``isFinancialSector`` keeps the ROCE deduction away from banks, insurers and exchanges, where
  current liabilities are customer money (Deutsche Börse 2025: 1.3 % against 11.5 %).
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

const context = vm.createContext({
  isNum: value => typeof value === "number" && Number.isFinite(value),
  meanOf: values => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null,
  clamp: x => Math.max(0, Math.min(1, x)),
  Math, Date, Number,
});
vm.runInContext([lift("calendarCloseGroups"), lift("trendFit"), lift("priceStability"),
                 lift("stabilityLabel"), lift("isFinancialSector")].join("\n"), context);

// Month-end rows, oldest first: [date, close, adjusted close, dividend].
function rowsFor(closes) {
  const dates = [];
  let year = 2026, month = 10;
  for (let index = 0; index < closes.length; index++) {
    dates.unshift(year + "-" + String(month + 1).padStart(2, "0") + "-28");
    month--;
    if (month < 0) { month = 11; year--; }
  }
  return closes.map((close, index) => [dates[index], close, close, 0]);
}
function geometric(start, rate, count) {
  return Array.from({length: count}, (_, index) => start * Math.pow(1 + rate, index));
}

const results = {};
// A steadily rising and a steadily falling price are both straight lines - only one is stability.
results.rising = context.priceStability(rowsFor(geometric(100, 0.01, 121)));
results.falling = context.priceStability(rowsFor(geometric(100, -0.01, 121)));
// A twelve month window alone is no longer enough for the judgement.
results.twelveMonths = context.priceStability(rowsFor(geometric(100, 0.01, 13)));
// A young listing still gets an answer, and the label names its own span.
results.threeYears = context.priceStability(rowsFor(geometric(100, 0.01, 37)));
results.threeYearsLabel = context.stabilityLabel(rowsFor(geometric(100, 0.01, 37)));
results.tenYearsLabel = context.stabilityLabel(rowsFor(geometric(100, 0.01, 121)));
// A choppy series is not a trend, whatever direction it drifts to.
const choppy = Array.from({length: 121}, (_, index) => 100 + (index % 2 ? 18 : -18) + index * 0.1);
results.choppy = context.priceStability(rowsFor(choppy));
// A loss year inside the window leaves the row open instead of inventing a fit.
const broken = geometric(100, 0.01, 121); broken[60] = 0;
results.zeroInside = context.priceStability(rowsFor(broken));

results.financial = ["Financial Services", "Financials"].map(sector =>
  context.isFinancialSector({sector, industry: ""}));
results.financialIndustry = ["Banks—Diversified", "Insurance—Diversified",
  "Financial Data & Stock Exchanges", "Capital Markets", "Asset Management"]
  .map(industry => context.isFinancialSector({sector: "", industry}));
results.industrial = [
  context.isFinancialSector({sector: "Technology", industry: "Software—Application"}),
  context.isFinancialSector({sector: "Industrials", industry: "Auto Manufacturers"}),
  context.isFinancialSector({}),
];
process.stdout.write(JSON.stringify(results));
"""


def run_harness():
    result = subprocess.run(
        ["node", "-e", HARNESS],
        env={"PAGE_PATH": str(PAGE), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr[-2000:])
    return json.loads(result.stdout)


class PriceStabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = run_harness()

    def test_rising_trend_scores_high(self):
        self.assertGreater(self.r["rising"], 0.95)

    def test_falling_trend_is_signed_negative(self):
        # A crash that runs straight down is a trend, not stability: the fit is high, the sign
        # must take the point away.
        self.assertLess(self.r["falling"], -0.95)

    def test_twelve_month_window_is_too_short(self):
        self.assertIsNone(self.r["twelveMonths"])

    def test_short_listing_keeps_its_own_span(self):
        self.assertGreater(self.r["threeYears"], 0.9)
        self.assertEqual(self.r["threeYearsLabel"],
                         "Kursstabilität 3 Jahre (R², negativ = fallender Trend)")
        self.assertEqual(self.r["tenYearsLabel"],
                         "Kursstabilität 10 Jahre (R², negativ = fallender Trend)")

    def test_choppy_series_is_not_stable(self):
        self.assertLess(abs(self.r["choppy"]), 0.5)

    def test_zero_inside_the_window_stays_open(self):
        self.assertIsNone(self.r["zeroInside"])


class FinancialSectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = run_harness()

    def test_sector_names_count(self):
        self.assertEqual(self.r["financial"], [True, True])

    def test_industry_names_count(self):
        self.assertEqual(self.r["financialIndustry"], [True] * 5)

    def test_industrial_papers_keep_their_roce(self):
        self.assertEqual(self.r["industrial"], [False, False, False])


if __name__ == "__main__":
    unittest.main()
