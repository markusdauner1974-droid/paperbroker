"""The comparison index is chosen by listing place, and the financing rows use the resolved figures.

Two things were open on the page:

* the four criteria "An negativen Handelstagen des Vergleichsindex" were computed against no index at
  all outside Germany and the US, because only ``.DE`` and the American listings had a default - the
  rows stayed empty on every Paris, Amsterdam, Milan, Madrid, London or Zurich paper.
* "Umsatz TTM / Finanzschulden" and "Rule of 40" read the own quarterly series only. A paper without
  one left them empty even though the second source keeps twenty periods, and Rule of 40 stayed empty
  on a German paper that had all its figures but a short series.

The index table is lifted out of the delivered page and run in node, so the mapping under test is the
shipped one. The financing rows are checked against the shipped source.
"""

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "index.html"

#: One paper per listing place the page maps, plus an American class and an unknown place.
SYMBOLS = ["SAP.DE", "OR.PA", "ASML.AS", "ENI.MI", "SAN.MC", "SHEL.L", "NESN.SW", "VIG.VI",
           "ERIC-B.ST", "CARL-B.CO", "EQNR.OL", "NOKIA.HE", "ABI.BR", "GALP.LS",
           "AAPL", "BRK.B", "XYZ.QQ"]

HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(process.env.PAGE_PATH, "utf8");
const opening = html.indexOf("<script>");
const script = html.slice(opening + 8, html.indexOf("</script>", opening));

const table = script.match(/const benchmarkBySuffix=\{[\s\S]*?\};/)[0];
const start = script.indexOf("function defaultBenchmark(");
let depth = 0, end = start;
for (let index = script.indexOf("{", start); index < script.length; index++) {
  if (script[index] === "{") depth++;
  if (script[index] === "}") { depth--; if (depth === 0) { end = index + 1; break; } }
}
const source = table + "\n" + script.slice(start, end);
const context = vm.createContext({});
vm.runInContext(source, context);
// An American listing either has no dot or a single letter after it (a share class like BRK.B).
const american = symbol => {
  const dot = symbol.lastIndexOf(".");
  return dot < 0 || symbol.length - dot - 1 === 1;
};
const stocks = JSON.parse(process.env.SYMBOLS).map(symbol => ({
  symbol,
  nas_primary: american(symbol) && !symbol.includes("."),
  info: american(symbol)
    ? {currency: "USD", fullExchangeName: "NasdaqGS", exchange: "NMS"}
    : {currency: "EUR", fullExchangeName: "Xetra", exchange: "GER"},
}));
process.stdout.write(JSON.stringify(stocks.map(stock => context.defaultBenchmark(stock))));
"""


def benchmark_for(symbols):
    result = subprocess.run(
        ["node", "-e", HARNESS],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PAGE_PATH": str(PAGE), "SYMBOLS": json.dumps(symbols),
             },
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr}")
    return json.loads(result.stdout)


class BenchmarkTests(unittest.TestCase):
    """Each listing place is measured against its own market."""

    @classmethod
    def setUpClass(cls):
        cls.answers = dict(zip(SYMBOLS, benchmark_for(SYMBOLS), strict=False))

    def test_german_listings_use_the_dax(self):
        self.assertEqual(self.answers["SAP.DE"], "^GDAXI")

    def test_paris_uses_the_cac(self):
        self.assertEqual(self.answers["OR.PA"], "^FCHI")

    def test_amsterdam_uses_the_aex(self):
        self.assertEqual(self.answers["ASML.AS"], "^AEX")

    def test_milan_madrid_london_zurich(self):
        self.assertEqual(self.answers["ENI.MI"], "FTSEMIB.MI")
        self.assertEqual(self.answers["SAN.MC"], "^IBEX")
        self.assertEqual(self.answers["SHEL.L"], "^FTSE")
        self.assertEqual(self.answers["NESN.SW"], "^SSMI")

    def test_vienna_stockholm_copenhagen_oslo_helsinki(self):
        self.assertEqual(self.answers["VIG.VI"], "^ATX")
        self.assertEqual(self.answers["ERIC-B.ST"], "^OMX")
        self.assertEqual(self.answers["CARL-B.CO"], "^OMXC25")
        self.assertEqual(self.answers["EQNR.OL"], "OSEBX.OL")
        self.assertEqual(self.answers["NOKIA.HE"], "^OMXH25")

    def test_brussels_and_lisbon(self):
        self.assertEqual(self.answers["ABI.BR"], "^BFX")
        self.assertEqual(self.answers["GALP.LS"], "PSI20.LS")

    def test_a_us_listing_keeps_its_american_index(self):
        self.assertEqual(self.answers["AAPL"], "SPY")

    def test_a_us_share_class_keeps_its_american_index(self):
        self.assertEqual(self.answers["BRK.B"], "^GSPC")

    def test_an_unknown_place_gets_no_index(self):
        """No index is better than the wrong one: the four rows stay open and say so."""
        self.assertEqual(self.answers["XYZ.QQ"], "")


DOWNTURN_HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(process.env.PAGE_PATH, "utf8");
const opening = html.indexOf("<script>");
const script = html.slice(opening + 8, html.indexOf("</script>", opening));

// The page declares its helpers both ways - `function deviation(...)` and `const growth = ... => ...` -
// so the extractor has to handle both instead of assuming one.
function declaration(name) {
  const viaFunction = script.indexOf("function " + name + "(");
  if (viaFunction >= 0) {
    let depth = 0;
    for (let index = script.indexOf("{", viaFunction); index < script.length; index++) {
      if (script[index] === "{") depth++;
      if (script[index] === "}") { depth--; if (depth === 0) return script.slice(viaFunction, index + 1); }
    }
  }
  const viaConst = script.search(new RegExp("const\\s+" + name + "\\s*="));
  if (viaConst < 0) throw new Error("Deklaration fehlt: " + name);
  let round = 0, curly = 0, square = 0;
  for (let index = script.indexOf("=", viaConst) + 1; index < script.length; index++) {
    const c = script[index];
    if (c === "(") round++;
    else if (c === ")") round--;
    else if (c === "{") curly++;
    else if (c === "}") curly--;
    else if (c === "[") square++;
    else if (c === "]") square--;
    else if (c === ";" && !round && !curly && !square) return script.slice(viaConst, index + 1);
  }
  throw new Error("Ende der Deklaration fehlt: " + name);
}

const names = ["isNum", "division", "growth", "knownValues", "meanOf", "deviation", "downturn"];
const source = names.map(declaration).join("\n");
const context = vm.createContext({});
vm.runInContext(source, context);
const input = JSON.parse(process.env.CASE);
process.stdout.write(JSON.stringify(context.downturn(input.rows, input.benchmark, input.count)));
"""


def downturn_case(rows, benchmark, count):
    result = subprocess.run(
        ["node", "-e", DOWNTURN_HARNESS],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PAGE_PATH": str(PAGE), "CASE": json.dumps(
            {"rows": rows, "benchmark": benchmark, "count": count})},
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed: {result.stderr}")
    return json.loads(result.stdout)


def series(days, step):
    """A price row is [date, close]; the index falls by step on every second day."""
    rows, value = [], 100.0
    for day in range(days):
        value *= 1 - (step if day % 2 else -0.01)
        rows.append([f"2026-01-{day + 1:02d}", round(value, 4)])
    return rows


class DownturnReasonTests(unittest.TestCase):
    """A row that cannot be judged says why - a bare dash leaves the reader guessing."""

    @classmethod
    def setUpClass(cls):
        cls.page = PAGE.read_text(encoding="utf-8")

    def test_without_an_index_the_reason_is_named(self):
        correlation, excess, reason = downturn_case(series(30, 0.01), [], 20)
        self.assertIsNone(correlation)
        self.assertIsNone(excess)
        self.assertEqual("kein Vergleichsindex", reason)

    def test_too_few_common_days_name_the_window(self):
        correlation, excess, reason = downturn_case(series(10, 0.01), series(10, 0.01), 20)
        self.assertIsNone(correlation)
        self.assertIsNone(excess)
        self.assertIn("gemeinsame Handelstage", reason)
        self.assertIn("21", reason, "das Fenster nennt, was fehlt")

    def test_a_full_window_gives_numbers_and_no_reason(self):
        rows = series(30, 0.01)
        correlation, excess, reason = downturn_case(rows, series(30, 0.01), 20)
        self.assertIsNone(reason)
        self.assertIsNotNone(correlation)
        self.assertIsNotNone(excess)

    def test_the_rows_carry_the_reason_as_a_label(self):
        self.assertIn('n(unavailableLabel("Korrelation im Fenster von 20 Handelstagen",cor20,gap20)',
                      self.page)
        self.assertIn('p(unavailableLabel("Ø Mehrrendite im Fenster von 65 Handelstagen",out65,gap65)',
                      self.page)

    def test_oslo_uses_a_symbol_the_source_carries(self):
        """^OSEAX is not in the source ("no data found, symbol may be delisted")."""
        self.assertNotIn('".OL":"^OSEAX"', self.page)
        self.assertIn('".OL":"OSEBX.OL"', self.page)


class FinancingRowTests(unittest.TestCase):
    """The two financing rows fall back to the second source instead of reading an empty series."""

    @classmethod
    def setUpClass(cls):
        cls.page = PAGE.read_text(encoding="utf-8")

    def test_the_revenue_row_divides_the_resolved_revenue(self):
        self.assertIn('n("Umsatz TTM / Finanzschulden",division(ttmRevenue,debt)', self.page)

    def test_the_revenue_is_resolved_from_the_own_series_first(self):
        resolved = re.search(r"const ttmRevenue=isNum\(ttmRev\)\?ttmRev:([^;]+);", self.page)
        if resolved is None:
            self.fail("die Auflösung des TTM-Umsatzes fehlt")
        self.assertIn("revenue_total", resolved.group(1),
                      "ohne eigene Quartale muss der zweite Wert greifen")

    def test_rule_of_forty_takes_all_three_numbers_from_one_provider(self):
        """Growth, revenue and operating income are added and divided - they must be the same source."""
        start = self.page.index("const ruleOfForty=")
        block = self.page[start:start + 900]
        self.assertIn("isNum(ttmRevGrowth)&&isNum(ttmRev)&&isNum(ownOperating)", block,
                      "das eigene Paar muss vollständig sein")
        self.assertIn("isNum(secondSource.revenue)&&isNum(secondSource.revenue_total)", block)
        self.assertIn("&&isNum(secondSource.operating_income_total)", block,
                      "auch das zweite Paar muss vollständig sein")
        self.assertNotIn("operatingIncomeTTM", self.page,
                         "eine Mischung aus zwei Anbietern darf es nicht mehr geben")

    def test_an_incomplete_pair_leaves_the_row_open(self):
        block = self.page[self.page.index('p("Rule of 40 (TTM, operative Marge)"'):][:260]
        self.assertIn("ruleOfForty?compareValues(", block)
        self.assertIn(":null,", block)

    def test_a_reason_blocks_the_second_source(self):
        """A loading or failed figure must not be read as a number."""
        self.assertIn("const secondSource=ttmGrowth&&!ttmGrowth.reason?ttmGrowth:null;", self.page)


if __name__ == "__main__":
    unittest.main()
