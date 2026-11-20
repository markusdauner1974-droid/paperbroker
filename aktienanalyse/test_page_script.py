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

// Waiting for the index must retry a failed load, keep the button shut and tell the truth.
results.benchmarkWaitRetries = /if\(!checkBenchmark\.loading\)loadCheckBenchmark\(index\)/.test(script);
results.benchmarkWaitLocksButton =
  /button\.disabled=true;\s*output\.replaceChildren\(element\("p","Vergleichsindex "/.test(script);
results.benchmarkWaitDistinguishes =
  /Der Vergleichsindex lädt noch; bitte erneut auswerten\./.test(script) &&
  /Der Vergleichsindex ist nicht verfügbar; der Gruppenvergleich wurde nicht ausgeführt\./.test(script);

process.stdout.write(JSON.stringify(results));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageScriptTest(unittest.TestCase):
    def test_quarter_window_and_benchmark_loading_state(self):
        import os
        umgebung = {**os.environ, "PAGE_PATH": str(PAGE)}
        lauf = subprocess.run(["node", "-e", HARNESS],
                              capture_output=True, text=True, timeout=120, env=umgebung)
        self.assertEqual(lauf.returncode, 0, lauf.stderr[-800:])
        ergebnis = json.loads(lauf.stdout)
        self.assertTrue(ergebnis["eightConsecutive"], "acht Quartale ohne Loch wurden nicht erkannt")
        self.assertFalse(ergebnis["sevenOnly"], "sieben Quartale dürfen kein Acht-Quartale-Fenster sein")
        self.assertFalse(ergebnis["holeIsRecognised"], "ein fehlendes Quartal wurde übersehen")
        self.assertFalse(ergebnis["gapIsRecognised"], "eine Lücke von einem Jahr wurde übersehen")
        self.assertTrue(ergebnis["unreadableDateRefused"],
                        "ein unlesbares Datum wird als Quartal akzeptiert")
        self.assertTrue(ergebnis["brokenEntryRefused"], "ein kaputter Eintrag lässt die Prüfung abstürzen")
        self.assertTrue(ergebnis["pendingRuleShipped"], "die Ladeanzeige für Indexkriterien fehlt")
        self.assertTrue(ergebnis["loadingFlagIsKept"], "der Ladezustand wird nicht geführt")
        self.assertTrue(ergebnis["supplementIsGuarded"], "die Yahoo-Ergänzung ist nicht gegen Doppelläufe gesichert")
        self.assertTrue(ergebnis["supplementStartsByItself"],
                        "bei einem NAS-Kurs ohne Historie startet die Ergänzung nicht von selbst")
        self.assertEqual(ergebnis["cutoffRows"], [15, 15, 15, 15],
                         "jeder Check braucht fünfzehn Schwellen")
        self.assertEqual(ergebnis["qualityRow"],
                         "[50,65,50,65,50,50,50,50,50,50,50,25,25,10,10]",
                         "die veröffentlichten Schwellen des Qualitäts-Checks sind verändert")
        self.assertEqual(ergebnis["dividendCutoffs"], [65], "Dividenden-Check: 65 % ist veröffentlicht")
        self.assertEqual(ergebnis["growthCutoffs"], [50], "Wachstums-Check: Standardwert 50 %")
        self.assertEqual(ergebnis["robustnessCutoffs"], [50], "Robustheits-Check: Standardwert 50 %")
        self.assertEqual(ergebnis["cutoffForRobustness"], 50, "die Schwelle des gewählten Checks fehlt")
        self.assertEqual(ergebnis["publishedFlags"], [True, True, False, False],
                         "veröffentlicht und angenommen müssen unterscheidbar bleiben")
        self.assertTrue(ergebnis["rankingCoversEveryCheck"],
                        "nur der Qualitäts-Check wird gegen die Gruppe gerankt")
        self.assertTrue(ergebnis["tableUsesSelectedCutoffs"],
                        "die Tabelle bewertet nicht gegen die Schwelle des gewählten Checks")
        self.assertTrue(ergebnis["benchmarkWaitRetries"],
                        "ein altes Index-Symbol ohne Kurse wird nicht neu geladen")
        self.assertTrue(ergebnis["benchmarkWaitLocksButton"],
                        "während des Wartens auf den Index bleibt der Knopf offen")
        self.assertTrue(ergebnis["benchmarkWaitDistinguishes"],
                        "lädt-noch und nicht-verfügbar werden nicht unterschieden")


if __name__ == "__main__":
    unittest.main()
