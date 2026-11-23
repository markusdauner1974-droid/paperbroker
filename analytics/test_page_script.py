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

// Two price sources close on different days: the deeper series wins, the newer days of the
// primary source are appended once, and the label names both.
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
vm.runInContext(lift("joinPrices"), context);
vm.runInContext(lift("priceLabel"), context);
// NAS rows carry no dividend column: [day, close, null, null].
const nas = [["2026-11-18", 180, null, null], ["2026-11-19", 185, null, null],
             ["2026-11-20", 187, null, null]];
const yahoo = [["1998-04-09", 30, 30, 0], ["2026-11-18", 180, 180, 0],
               ["2026-11-19", 185, 185, 2.5]];
const joined = context.joinPrices(nas, yahoo);
// A short Yahoo series must not swallow scanner days it does not cover itself.
const gappy = context.joinPrices(
  [["2025-06-02", 100, null, null], ["2026-06-01", 150, null, null],
   ["2026-11-20", 187, null, null]],
  [["2026-01-02", 120, 120, 0], ["2026-11-19", 185, 185, 0]]);
vm.runInContext(lift("adjustDate"), context);
vm.runInContext(lift("periodDividends"), context);
results.priceJoin = {
  keepsTheDeepStart: joined[0][0] === "1998-04-09",
  appendsTheNewerDay: joined.at(-1)[0] === "2026-11-20",
  noDoubleDay: joined.filter(row => row[0] === "2026-11-19").length === 1,
  sorted: joined.every((row, index) => index === 0 || joined[index - 1][0] < row[0]),
  length: joined.length,
  keepsDividend: joined.find(row => row[0] === "2026-11-19")[3] === 2.5,
  nullDividendBecomesZero: joined.at(-1)[3] === 0,
  dividendWindowStillWorks: context.periodDividends(joined, "2026-11-20") === 2.5,
  keepsScannerDaysInsideTheRange: gappy.some(row => row[0] === "2025-06-02") &&
    gappy.some(row => row[0] === "2026-06-01"),
  gappyIsSorted: gappy.map(row => row[0]).join() ===
    "2025-06-02,2026-01-02,2026-06-01,2026-11-19,2026-11-20",
  withoutDeeper: context.joinPrices(nas, []).length === 3,
  withoutPrimary: context.joinPrices([], yahoo).length === 3,
  labelNamesBoth: context.priceLabel("xang auf NAS", "Yahoo Finance").includes("Tage von"),
  labelSingle: context.priceLabel(null, "Yahoo Finance") === "Yahoo Finance",
};

// The display rule: a criterion that waits for the benchmark must not show a dash.
const pending = /benchmarkPending\s*=\s*selected===3&&checkBenchmark\.loading&&ordinal>=11/;
const reading = /element\("td",benchmarkPending\?"Wird geladen …":value,"criterion-value"\)/;
results.pendingRuleShipped = pending.test(script) && reading.test(script);
const loadingSet = /checkBenchmark\.loading=Boolean\(symbol\)/.test(script);
// Exactly one place clears the flag, it sits in the finally block and it respects ownership.
const loadingCleared = (script.match(/checkBenchmark\.loading=false;/g) || []).length === 1 &&
  /finally\{[\s\S]*?if\(request===checkBenchmark\.request\)checkBenchmark\.loading=false;/.test(script);
results.loadingFlagIsKept = loadingSet && loadingCleared;

// The gap report comes with the statements, not with the scanner: the merge has to carry it,
// and the page has to render it above the score explanation.
results.gapReport = {
  travelsWithTheSupplement: /data_gaps:detail\.data_gaps\?\.length\?detail\.data_gaps:\(baseline\.data_gaps\|\|\[\]\)/.test(script),
  blockIsBuilt: /const gaps=element\("details",undefined,"data-gaps"\)/.test(script),
  blockIsRendered: /body\.replaceChildren\(summary,metrics,\.\.\.\(gaps\.childNodes\.length\?\[gaps\]:\[\]\),details/.test(script),
  namesBlockAndYears: /"Block","Quelle","Jahre","Grund"/.test(script),
};

// The Yahoo supplement must not run twice and must not overwrite a newer ticker.
const guard = /if\(active\.history_loading\)return;/.test(script);
const identity = /if\(data!==active\|\|request!==generation\)return;/.test(script);
results.supplementIsGuarded = guard && identity;
// The page has one state, the complete paper: a NAS row with prices asks for the supplement
// on every load, not only when a return or the dividends are missing. And there is no way back
// to the bare scanner row - the button that used to offer it must not come back.
const autoFetch = /data\.nas_primary&&data\.prices\.length\)await extendStock\(\)/.test(script);
results.supplementStartsByItself = autoFetch;
results.noWayBackToTheBareRow = !/Zur Grundansicht/.test(script) &&
  !/replaceData\(data\.nasBaseline\)/.test(script);
// The tile carries a grade and is coloured for EVERY grade - the old colour hung on a ratio that
// only existed when all fifteen criteria were known, so a paper with one gap had no colour at all.
results.tileShowsGrade = /element\("strong",gradeText\(grade\)\|\|score\)/.test(script) &&
  /gradeValue\(score\.points,score\.total,missing\)/.test(script);
results.tileAlwaysColoured =
  /const tileColor=gradeColor\(grade\);/.test(script) &&
  /if\(tileColor\)badge\.style\.setProperty\("--score-color",tileColor\)/.test(script) &&
  !/isNum\(ratio\)/.test(script);
// The search answers with one card per company in a three-column grid: name, its own code and
// currency, and every venue as a chip whose suffix names the exchange. The old toggle and the
// spelled-out venue names are gone - they made the list look like several results per company.
results.searchGrid = /#matches\{display:grid;grid-template-columns:repeat\(3,minmax\(0,1fr\)\)/.test(html);
results.searchCards = /element\("div",undefined,"card"\)/.test(script) &&
  /"chips"/.test(script) && /"chip lead"/.test(script);
results.searchChipIsTheTicker = /const chip=element\("button",listing\.symbol,index\?"chip":"chip lead"\)/.test(script) &&
  !/weiterer Börsenplatz/.test(script);

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
const loadAt = waitBlock.indexOf("startBenchmark(index)");
results.benchmarkWait = {
  found: Boolean(waitBlock),
  rerunsLiveControls: /const live=\(\)=>\{\s*const form=document\.querySelector\("\.rank-controls"\)/.test(waitBlock),
  retriesLoad: /if\(!checkBenchmark\.loading\)startBenchmark\(index\);/.test(waitBlock),
  lockedBeforeLoad: waitBlock.indexOf("lock(true);") > -1 && waitBlock.indexOf("lock(true);") < loadAt,
  lockedAgainAfterLoad: waitBlock.lastIndexOf("lock(true);") > loadAt,
  unlockedAfterWait: waitBlock.indexOf("lock(false);") > waitBlock.indexOf("while(checkBenchmark.loading"),
  // The lock helper touches a freshly looked-up button; a direct write to the captured
  // button or output would go into the detached tree.
  noStaleNodes: !/button\.disabled=(true|false)/.test(waitBlock) && !/output\.replaceChildren/.test(waitBlock),
  distinguishes: /report\("Der Vergleichsindex lädt noch; bitte erneut auswerten\."/.test(waitBlock) &&
    /report\("Der Vergleichsindex ist nicht verfügbar; der Gruppenvergleich wurde nicht ausgeführt\."/.test(waitBlock),
  handled: /if\(!checkBenchmark\.loading\)startBenchmark\(index\);/.test(waitBlock),
};

// A failing render must not leave the check loading forever, and no caller may swallow the
// rejection silently: the flag is cleared in finally, every call site attaches a catch.
const loadStart = script.indexOf("async function loadCheckBenchmark(symbol){");
const loadEnd = script.indexOf("\n}", loadStart);
const loadBody = loadStart >= 0 && loadEnd > loadStart ? script.slice(loadStart, loadEnd) : "";
results.benchmarkLoad = {
  found: Boolean(loadBody),
  safeRender: /const safeRender=\(\)=>\{\s*try\{ renderChecks\(\); \}\s*catch\(error\)\{/.test(loadBody),
  rendersGuarded: (loadBody.match(/renderChecks\(\)/g) || []).length === 1 &&
    (loadBody.match(/safeRender\(\)/g) || []).length === 2,
  clearedInFinally: /finally\{[\s\S]*?if\(request===checkBenchmark\.request\)checkBenchmark\.loading=false;/
    .test(loadBody),
};
// A caller's catch must not touch shared state: it would clear the flag of a newer request.
const statefulCatch = /loadCheckBenchmark\([\s\S]{0,80}?\.catch\(\s*\(\)\s*=>\s*\{[^}]*loading/.test(script);
const directCalls = (script.match(/loadCheckBenchmark\(/g) || []).length - 1;   // minus the definition
const wrapped = (script.match(/startBenchmark\(/g) || []).length;                 // four call sites
results.benchmarkCallsHandled = !statefulCatch && directCalls === 1 && wrapped === 4;


// The dividend streak counts fifteen windows and stays open when the history is too short.
const streakStart = script.indexOf("let streak=0;");
const streakEnd = script.indexOf("const divIncrease=");
const streakClose = script.indexOf("const streakKnown=");
const streakAfter = script.indexOf("const ma=rows.length>=200");
if (streakStart < 0 || streakEnd < 0 || streakClose < 0 || streakAfter < 0)
  throw new Error("streak rule not found");
// Skip the two payout lines in between: they need the quote payload, the rule does not.
const streakBody = script.slice(streakStart, streakEnd) + script.slice(streakClose, streakAfter);
results.divYears = Number((script.match(/const divYears=(\d+);/) || [])[1]);
const streakOf = vm.runInContext(
  "(function(divYears,divHistory){" + streakBody + " return knownStreak; })", context);
const year = values => { const out = Array(16).fill(1); values.forEach(([i, v]) => { out[i] = v; }); return out; };
results.streakFull = streakOf(15, year([]));
results.streakBrokenByZero = streakOf(15, year([[7, 0]]));
results.streakOpenOnUnknown = streakOf(15, year([[8, null]]));
results.streakOpenOnShortHistory = streakOf(15, [1, 1, 1, null]);
results.streakLabelShipped = /Aufeinanderfolgende 12-Monats-Zeiträume mit Dividende \(max\. 15\)/.test(script) &&
  /knownStreak,0,5,15\)/.test(script);

process.stdout.write(JSON.stringify(results));
"""


def _run():
    """Runs the harness once and hands back its results."""
    import os
    env = {**os.environ, "PAGE_PATH": str(PAGE)}
    run = subprocess.run(["node", "-e", HARNESS],
                         capture_output=True, text=True, timeout=120, env=env)
    if run.returncode != 0:
        raise AssertionError(run.stderr[-800:])
    return json.loads(run.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageScriptTest(unittest.TestCase):
    def test_the_price_series_of_both_sources_is_joined_once(self):
        joined = _run()["priceJoin"]
        self.assertTrue(joined["keepsTheDeepStart"])
        self.assertTrue(joined["appendsTheNewerDay"])
        self.assertTrue(joined["noDoubleDay"])
        self.assertEqual(4, joined["length"])
        self.assertTrue(joined["sorted"], "the joined series is not in trading-day order")
        self.assertTrue(joined["keepsDividend"], "the dividend of the deeper series was lost")
        self.assertTrue(joined["nullDividendBecomesZero"],
                        "a scanner row without a dividend column stays null")
        self.assertTrue(joined["dividendWindowStillWorks"],
                        "the twelve-month dividend window is refused after the join")
        self.assertTrue(joined["keepsScannerDaysInsideTheRange"],
                        "scanner days inside a short Yahoo range were dropped")
        self.assertTrue(joined["gappyIsSorted"], "the gappy series is out of order")
        self.assertTrue(joined["withoutDeeper"])
        self.assertTrue(joined["withoutPrimary"])
        self.assertTrue(joined["labelNamesBoth"])
        self.assertTrue(joined["labelSingle"])
        report = _run()["gapReport"]
        self.assertTrue(report["travelsWithTheSupplement"],
                        "the gap report is dropped when the supplement replaces the scanner data")
        self.assertTrue(report["blockIsBuilt"], "the gap block is not built at all")
        self.assertTrue(report["blockIsRendered"], "the gap block is built but never shown")
        self.assertTrue(report["namesBlockAndYears"], "the gap table lost its columns")

    def test_the_page_has_one_state_the_complete_paper(self):
        """A NAS row always asks for the supplement, and no button leads back.

        Measured before: a paper the scanner held showed a bare row plus a button that
        switched the page back to it. Two states for one paper, and the reader had to
        know which one to pick. Now the supplement starts on every NAS load, and the
        way back is gone - so the check is on the shipped script, not on a copy.
        """
        import os
        env = {**os.environ, "PAGE_PATH": str(PAGE)}
        run = subprocess.run(["node", "-e", HARNESS],
                             capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(run.returncode, 0, run.stderr[-800:])
        result = json.loads(run.stdout)
        self.assertTrue(result["supplementStartsByItself"],
                        "a NAS row with prices does not always ask for the supplement")
        self.assertTrue(result["noWayBackToTheBareRow"],
                        "the page still offers a way back to the bare scanner row")
        self.assertTrue(result["tileShowsGrade"], "the tile does not show a grade")
        self.assertTrue(result["tileAlwaysColoured"],
                        "the tile colour still depends on a complete data set")

    def test_the_search_answers_with_cards_in_three_columns(self):
        """One card per company, venues as chips named by their ticker."""
        import os
        env = {**os.environ, "PAGE_PATH": str(PAGE)}
        run = subprocess.run(["node", "-e", HARNESS],
                             capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(run.returncode, 0, run.stderr[-800:])
        result = json.loads(run.stdout)
        self.assertTrue(result["searchGrid"], "the results are not a three-column grid")
        self.assertTrue(result["searchCards"], "a result is not built as a card with chips")
        self.assertTrue(result["searchChipIsTheTicker"],
                        "a chip still spells out the exchange, or the old toggle is back")

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
        self.assertTrue(result["noWayBackToTheBareRow"],
                        "the page still offers a way back to the bare scanner row")
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
        self.assertTrue(load["safeRender"],
                        "the render can reject the index load")
        self.assertTrue(load["rendersGuarded"],
                        "a render runs outside safeRender and can reject the function")
        self.assertTrue(load["clearedInFinally"],
                        "a failing render leaves the check loading forever")
        self.assertTrue(result["benchmarkCallsHandled"],
                        "a call of loadCheckBenchmark can reject without being handled")
        self.assertEqual(result["divYears"], 15, "the dividend history does not reach fifteen years back")
        self.assertEqual(result["streakFull"], 15, "fifteen years with a payment must count 15")
        self.assertEqual(result["streakBrokenByZero"], 7, "a year without a payment must end the streak")
        self.assertIsNone(result["streakOpenOnUnknown"],
                          "an unknown year must not be counted as a break")
        self.assertIsNone(result["streakOpenOnShortHistory"],
                          "a short history must not claim fifteen years")
        self.assertTrue(result["streakLabelShipped"], "label and limit of the criterion do not match")




if __name__ == "__main__":
    unittest.main()
