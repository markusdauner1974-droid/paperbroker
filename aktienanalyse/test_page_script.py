"""One test for the page script: the quarter window and the benchmark loading state.

The scoring and the display live in the page's own JavaScript, so Python cannot
reach them directly. The test therefore runs the page script in node and checks two
things the repair promised: eight consecutive quarters are recognised as a window,
and a criterion that waits for the benchmark says so instead of showing a dash.
"""

import json
import shutil
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

// The function under test is lifted out of the page, so the test exercises the
// shipped code and not a copy of it.
const start = script.indexOf("function consecutiveQuarters(");
if (start < 0) throw new Error("consecutiveQuarters not found");
let depth = 0, end = start;
for (let index = script.indexOf("{", start); index < script.length; index++) {
  if (script[index] === "{") depth++;
  if (script[index] === "}") { depth--; if (depth === 0) { end = index + 1; break; } }
}
const context = vm.createContext({Date, Array, isNum: value => typeof value === "number" && Number.isFinite(value)});
vm.runInContext(script.slice(start, end), context);

const quarters = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31",
                  "2025-09-30", "2025-06-30", "2025-03-31", "2024-12-31"].map(d => [d, {}]);
results.eightConsecutive = context.consecutiveQuarters(quarters, 8);
results.sevenOnly = context.consecutiveQuarters(quarters.slice(0, 7), 8);
const holed = quarters.filter((_, index) => index !== 4);
results.holeIsRecognised = context.consecutiveQuarters(holed, 8);
const shifted = quarters.map(([d], index) => [index < 4 ? d : d.replace("2025", "2024"), {}]);
results.gapIsRecognised = context.consecutiveQuarters(shifted, 8);

// An unreadable date must never pass as a quarter, and a broken entry must not throw.
const brokenDate = quarters.map(([d], index) => [index === 3 ? "not-a-date" : d, {}]);
results.unreadableDateRefused = context.consecutiveQuarters(brokenDate, 8) === false;
const brokenEntry = quarters.slice();
brokenEntry[3] = undefined;
let threw = false;
try { context.consecutiveQuarters(brokenEntry, 8); } catch (error) { threw = true; }
results.brokenEntryRefused = threw === false;

// The display rule: a criterion that waits for the benchmark must not show a dash.
const pending = /benchmarkPending\s*=\s*selected===3&&checkBenchmark\.loading&&ordinal>=11/;
const reading = /element\("td",benchmarkPending\?"Wird geladen …":value,"criterion-value"\)/;
results.pendingRuleShipped = pending.test(script) && reading.test(script);
const loadingSet = /checkBenchmark\.loading=Boolean\(symbol\)/.test(script);
const loadingCleared = (script.match(/checkBenchmark\.loading=false;/g) || []).length >= 2;
results.loadingFlagIsKept = loadingSet && loadingCleared;

// The Yahoo supplement must not run twice and must not overwrite a newer ticker.
const guard = /if\(active\.history_loading\)return;/.test(script);
const identity = /if\(data!==active\|\|request!==generation\)return;/.test(script);
results.supplementIsGuarded = guard && identity;
const autoFetch = new RegExp(
  "data\\.nas_primary&&data\\.prices\\.length&&" +
  "\\s*\\n?\\s*\\(!isNum\\(periodReturn\\(data\\.prices,120\\)\\)" +
  "\\|\\|!isNum\\(lastYearDividends\\(\\)\\)\\)\\)await extendStock\\(\\)"
).test(script);
results.supplementStartsByItself = autoFetch;

// Every check ranks against its own cutoff row; the quality row is the published one.
const cutoffStart = script.indexOf("const qualityPercentiles=");
const cutoffEnd = script.indexOf("const qualityLowerIsBetter");
if (cutoffStart < 0 || cutoffEnd < 0) throw new Error("cutoff rows not found");
// const declarations stay lexical inside the vm, so the rows are handed back explicitly.
const cutoffs = vm.runInContext(
  "(function(){" + script.slice(cutoffStart, cutoffEnd) +
  " return {rows:blockPercentiles, published:blockPercentilesPublished, pick:cutoffsFor}; })()", context);
results.cutoffRows = cutoffs.rows.map(row => row.length);
results.qualityRow = JSON.stringify(cutoffs.rows[0]);
results.dividendCutoffs = [...new Set(cutoffs.rows[1])];
results.growthCutoffs = [...new Set(cutoffs.rows[2])];
results.robustnessCutoffs = [...new Set(cutoffs.rows[3])];
results.cutoffForRobustness = cutoffs.pick(3)[0];
results.publishedFlags = [...cutoffs.published];
results.rankingCoversEveryCheck =
  !/mode===0\?rankAt\(member,index\)>=qualityPercentiles\[index\]/.test(script) &&
  /const cutoffs=cutoffsFor\(mode\)/.test(script) &&
  /rankAt\(member,index\)>=cutoffs\[index\]/.test(script);
results.tableUsesSelectedCutoffs =
  /const cutoffs=cutoffsFor\(selected\)/.test(script) &&
  /relativeQuality=rank!==null/.test(script);

// Waiting for the index: retry a failed load, hold the live button, tell pending from
// unavailable - and never write into the nodes this handler captured, because
// loadCheckBenchmark renders the section again and detaches them.
const waitStart = script.indexOf("if(mode===3&&!checkBenchmark.rows.length){");
const waitEnd = script.indexOf("const request=++rankingGeneration;", waitStart);
const waitBlock = waitStart >= 0 && waitEnd > waitStart ? script.slice(waitStart, waitEnd) : "";
const loadAt = waitBlock.indexOf("loadCheckBenchmark(index)");
results.benchmarkWait = {
  found: Boolean(waitBlock),
  rerunsLiveControls: /const live=\(\)=>\{\s*const form=document\.querySelector\("\.rank-controls"\)/.test(waitBlock),
  retriesLoad: /if\(!checkBenchmark\.loading\)loadCheckBenchmark\(index\)/.test(waitBlock),
  lockedBeforeLoad: waitBlock.indexOf("lock(true);") > -1 && waitBlock.indexOf("lock(true);") < loadAt,
  lockedAgainAfterLoad: waitBlock.lastIndexOf("lock(true);") > loadAt,
  unlockedAfterWait: waitBlock.indexOf("lock(false);") > waitBlock.indexOf("while(checkBenchmark.loading"),
  // The lock helper touches a freshly looked-up button; a direct write to the captured
  // button or output would go into the detached tree.
  noStaleNodes: !/button\.disabled=(true|false)/.test(waitBlock) && !/output\.replaceChildren/.test(waitBlock),
  distinguishes: /report\("Der Vergleichsindex lädt noch; bitte erneut auswerten\."/.test(waitBlock) &&
    /report\("Der Vergleichsindex ist nicht verfügbar; der Gruppenvergleich wurde nicht ausgeführt\."/.test(waitBlock),
  handled: /loadCheckBenchmark\(index\)\.catch\(/.test(waitBlock),
};

// A failing render must not leave the check loading forever, and no caller may swallow the
// rejection silently: the flag is cleared in finally, every call site attaches a catch.
const loadStart = script.indexOf("async function loadCheckBenchmark(symbol){");
const loadEnd = script.indexOf("\n}", loadStart);
const loadBody = loadStart >= 0 && loadEnd > loadStart ? script.slice(loadStart, loadEnd) : "";
const tryAt = loadBody.indexOf("try{");
results.benchmarkLoad = {
  found: Boolean(loadBody),
  renderInsideTry: tryAt > -1 && loadBody.indexOf("renderChecks();") > tryAt,
  clearedInFinally: /finally\{[\s\S]*?if\(request===checkBenchmark\.request\)checkBenchmark\.loading=false;/
    .test(loadBody),
};
const callSites = (script.match(/loadCheckBenchmark\(/g) || []).length - 1;   // minus the definition
const guardedCalls = (script.match(/loadCheckBenchmark\([\s\S]{0,60}?\.catch\(/g) || []).length;
results.benchmarkCallsHandled = callSites > 0 && guardedCalls === callSites;

process.stdout.write(JSON.stringify(results));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageScriptTest(unittest.TestCase):
    def test_quarter_window_and_benchmark_loading_state(self):
        import os
        env = {**os.environ, "PAGE_PATH": str(PAGE)}
        run = subprocess.run(["node", "-e", HARNESS],
                             capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(run.returncode, 0, run.stderr[-800:])
        result = json.loads(run.stdout)
        self.assertTrue(result["eightConsecutive"], "eight quarters without a hole were not recognised")
        self.assertFalse(result["sevenOnly"], "seven quarters must not count as an eight-quarter window")
        self.assertFalse(result["holeIsRecognised"], "a missing quarter was overlooked")
        self.assertFalse(result["gapIsRecognised"], "a one-year gap was overlooked")
        self.assertTrue(result["unreadableDateRefused"],
                        "an unreadable date is accepted as a quarter")
        self.assertTrue(result["brokenEntryRefused"], "a broken entry crashes the check")
        self.assertTrue(result["pendingRuleShipped"], "the loading label for index criteria is missing")
        self.assertTrue(result["loadingFlagIsKept"], "the loading state is not tracked")
        self.assertTrue(result["supplementIsGuarded"], "the Yahoo supplement is not protected against double runs")
        self.assertTrue(result["supplementStartsByItself"],
                        "the supplement does not start by itself for a NAS quote without history")
        self.assertEqual(result["cutoffRows"], [15, 15, 15, 15],
                         "every check needs fifteen cutoffs")
        self.assertEqual(result["qualityRow"],
                         "[50,65,50,65,50,50,50,50,50,50,50,25,25,10,10]",
                         "the published cutoffs of the quality check have been changed")
        self.assertEqual(result["dividendCutoffs"], [65], "dividend check: 65 % is published")
        self.assertEqual(result["growthCutoffs"], [50], "growth check: default 50 %")
        self.assertEqual(result["robustnessCutoffs"], [50], "robustness check: default 50 %")
        self.assertEqual(result["cutoffForRobustness"], 50, "the cutoff of the selected check is missing")
        self.assertEqual(result["publishedFlags"], [True, True, False, False],
                         "published and assumed cutoffs must stay distinguishable")
        self.assertTrue(result["rankingCoversEveryCheck"],
                        "only the quality check is ranked against the group")
        self.assertTrue(result["tableUsesSelectedCutoffs"],
                        "the table does not judge against the cutoff of the selected check")
        wait = result["benchmarkWait"]
        self.assertTrue(wait["found"], "the wait block for the index is missing")
        self.assertTrue(wait["rerunsLiveControls"],
                        "the controls are not looked up again but taken from the old closure")
        self.assertTrue(wait["retriesLoad"], "an old index symbol without prices is not loaded again")
        self.assertTrue(wait["lockedBeforeLoad"], "the button is not locked before the load")
        self.assertTrue(wait["lockedAgainAfterLoad"],
                        "the re-rendered button is not locked again")
        self.assertTrue(wait["unlockedAfterWait"], "the button stays locked after the wait")
        self.assertTrue(wait["noStaleNodes"],
                        "the wait block writes into the detached nodes of the old closure")
        self.assertTrue(wait["distinguishes"],
                        "pending and unavailable are not distinguished")
        self.assertTrue(wait["handled"], "the index load in the wait block can reject unhandled")
        load = result["benchmarkLoad"]
        self.assertTrue(load["found"], "loadCheckBenchmark not found")
        self.assertTrue(load["renderInsideTry"],
                        "the render runs outside the error handling of the index load")
        self.assertTrue(load["clearedInFinally"],
                        "a failing render leaves the check loading forever")
        self.assertTrue(result["benchmarkCallsHandled"],
                        "a call of loadCheckBenchmark can reject without being handled")


if __name__ == "__main__":
    unittest.main()
