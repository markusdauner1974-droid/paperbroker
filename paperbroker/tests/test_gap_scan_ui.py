"""Offline tests for the gap-scan panel on ``GET /`` (Slice 4).

The panel is the page's THIRD action: after the calendar says WHO released, it
shows what the market did AFTER the numbers - the close of the reaction session
against the close before it, ranked by the size of the move. It calls the route
Slice 3 already delivered (``/api/v1/gap-scan``) and nothing else: no chain, no
spread, no option prices. The chain costs seconds per title and is fetched when
a title is opened, not before.

What is pinned here, in the order of the requirement:

* the section, its date field and its button exist, and the page starts NO scan
  on load - the field carries yesterday, the run stays a click,
* three candidates of the real route become exactly three rows with gap,
  direction, strategy and close - measured, not grepped,
* ``session_uncertain`` is in the answer and NOT in the display (Markus'
  decision: the session is not a criterion),
* ``selected: 0`` names "Kein Kandidat" together with the number of failures,
* ``result_state: "history_not_published"`` names the late closes in the panel's
  note - no rows, no error,
* ``invalid_day`` and ``calendar_unavailable`` name themselves in their own
  words and leave the rest of the page alone,
* ``day_state: "stale"`` is flagged while the rows stay visible,
* "Öffnen" puts the ticker AND the strategy of that direction into the existing
  form and submits it - no second form.

The rendering is executed, not inferred: ``_HARNESS`` runs the real
``static/gap_scan.js`` under node against a deliberately small DOM, with
``fetch`` answering from a JSON file. The payloads come from the real route
with the fake adapters of ``test_gap_scan_endpoint``, so the two halves of the
contract (answer shape, rendering) cannot drift apart. No test touches a socket.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import paperbroker
from paperbroker.gap_scan import GapBar, GapSourceError
from paperbroker.tests.test_gap_scan_endpoint import (
    DAY,
    _calendar,
    _client,
    _History,
    _scan,
)

#: The panel's behaviour lives in one static file; the tests execute it.
PANEL_JS = Path(paperbroker.__file__).parent / "static" / "gap_scan.js"

#: node is the JS engine of the harness below - no package, no build step.
NODE = shutil.which("node")

#: A DOM small enough to read and real enough to run the panel: the ids are the
#: ones screen.html carries, the form answers the one selector the panel uses.
_HARNESS = r"""
'use strict';
// Renders paperbroker/static/gap_scan.js against ONE stubbed answer, offline.
// Usage: node harness.js <gap_scan.js> <answer.json> [--click] [--open N]
const fs = require('fs');
const vm = require('vm');

const argv = process.argv.slice(2);
const jsPath = argv[0];
const answerPath = argv[1];
const click = argv.indexOf('--click') >= 0;
const openAt = argv.indexOf('--open');
const openRow = openAt >= 0 ? Number(argv[openAt + 1]) : null;

const source = fs.readFileSync(jsPath, 'utf8');
const answer = JSON.parse(fs.readFileSync(answerPath, 'utf8'));   // {status, body}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.attrs = {};
    this.handlers = {};
    this.className = '';
    this.value = '';
    this.type = '';
    this.hidden = false;
    this.checked = false;
    this.disabled = false;
    this._own = '';
    this._query = [];
    const self = this;
    this.classList = {
      add(c) {
        if (!self.classList.contains(c)) {
          self.className = (self.className ? self.className + ' ' : '') + c;
        }
      },
      remove(c) {
        self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(' ');
      },
      contains(c) { return self.className.split(/\s+/).indexOf(c) >= 0; },
      toggle(c, on) {
        const want = on === undefined ? !self.classList.contains(c) : !!on;
        if (want) self.classList.add(c); else self.classList.remove(c);
        return want;
      }
    };
  }
  get textContent() {
    if (this.childNodes.length) return this.childNodes.map((c) => c.textContent).join('');
    return this._own;
  }
  set textContent(v) { this.childNodes = []; this._own = String(v); }
  get innerHTML() { return this._own; }
  set innerHTML(v) { this.childNodes = []; this._own = String(v); }
  appendChild(child) { this.childNodes.push(child); return child; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  addEventListener(name, fn) { (this.handlers[name] = this.handlers[name] || []).push(fn); }
  removeEventListener() {}
  dispatchEvent(ev) { (this.handlers[ev.type] || []).forEach((fn) => fn(ev)); return true; }
  click() { this.dispatchEvent({ type: 'click', target: this, preventDefault() {} }); }
  focus() {}
  scrollIntoView() {}
  querySelectorAll() { return this._query; }
  querySelector() { return this._query.length ? this._query[0] : null; }
  submit() { this._submitted = true; }
  requestSubmit() { this._submitted = true; }
}

// The ids the panel and the existing form are known by. Every one of them is a
// real element of screen.html - a typo would leave the panel unrendered here.
const ids = ['gap-scan', 'gscan-day', 'gscan-run', 'gscan-state', 'gscan-error',
  'gscan-counts', 'gscan-stale', 'gscan-empty', 'gscan-table-wrap', 'gscan-thead',
  'gscan-tbody', 'ticker', 'screener-form'];
const nodes = {};
ids.forEach((id) => {
  const tag = id === 'gscan-run' ? 'button' : ((id === 'ticker' || id === 'gscan-day') ? 'input' : 'div');
  nodes[id] = new El(tag);
});

// The initial hidden flags are the ones screen.html carries for this panel; the
// template test pins them, so the harness cannot drift away from the markup.
['gscan-error', 'gscan-counts', 'gscan-stale', 'gscan-empty', 'gscan-table-wrap']
  .forEach((id) => { nodes[id].hidden = true; });
nodes['gscan-day'].type = 'date';
nodes['ticker'].type = 'text';

// The four strategy radios of the existing form: the only selector the panel uses.
const radios = ['bull_call_debit', 'bear_put_debit', 'bull_put_credit', 'bear_call_credit']
  .map((value) => {
    const r = new El('input');
    r.type = 'radio';
    r.name = 'strategy';
    r.value = value;
    return r;
  });
nodes['screener-form']._query = radios;

const document = {
  getElementById(id) { return nodes[id] || null; },
  createElement(tag) { return new El(tag); },
  querySelectorAll() { return []; }
};

let requested = null;
function fetch(url) {
  requested = url;
  return Promise.resolve({
    ok: answer.status >= 200 && answer.status < 300,
    status: answer.status,
    text() { return Promise.resolve(JSON.stringify(answer.body)); },
    json() { return Promise.resolve(answer.body); }
  });
}

function collect(node, out) {
  out.push(node._own);
  if (node.title) out.push(String(node.title));
  out.push(Object.keys(node.attrs).map((k) => node.attrs[k]).join(' '));
  node.childNodes.forEach((c) => collect(c, out));
  return out;
}

const sandbox = {
  document,
  fetch,
  Event: class { constructor(type) { this.type = type; } },
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout() { return 1; },
  clearTimeout() {},
  console
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'gap_scan.js' });

// The default the field carries BEFORE any click: "gestern", set by the panel.
const defaultDay = nodes['gscan-day'].value;

function rows() {
  return nodes['gscan-tbody'].childNodes.map((tr) => ({
    symbol: tr.attrs['data-symbol'],
    direction: tr.attrs['data-direction'],
    cells: tr.childNodes.map((td) => td.textContent),
    titles: tr.childNodes.map((td) => td.title || td.attrs.title || '')
  }));
}

function report() {
  return {
    defaultDay,
    url: requested,
    state: nodes['gscan-state'].textContent,
    error: nodes['gscan-error'].hidden ? null : nodes['gscan-error'].textContent,
    counts: nodes['gscan-counts'].hidden ? null : nodes['gscan-counts'].textContent,
    stale: nodes['gscan-stale'].hidden ? null : nodes['gscan-stale'].textContent,
    empty: nodes['gscan-empty'].hidden ? null : nodes['gscan-empty'].textContent,
    tableHidden: nodes['gscan-table-wrap'].hidden,
    head: nodes['gscan-thead'].childNodes.map((tr) => tr.childNodes.map((th) => th.textContent)),
    rows: rows(),
    // Every word and every tooltip the panel produced - the place a field the
    // order forbids would surface.
    text: Object.keys(nodes).map((id) => collect(nodes[id], []).join(' ')).join(' \u0001 '),
    opened: null
  };
}

async function main() {
  if (click) nodes['gscan-run'].click();
  await new Promise((resolve) => setTimeout(resolve, 20));
  const out = report();
  if (openRow !== null && out.rows.length > openRow) {
    const tr = nodes['gscan-tbody'].childNodes[openRow];
    tr.childNodes[tr.childNodes.length - 1].childNodes[0].click();
    out.opened = {
      ticker: nodes['ticker'].value,
      strategy: radios.filter((r) => r.checked).map((r) => r.value),
      submitted: !!nodes['screener-form']._submitted
    };
  }
  process.stdout.write(JSON.stringify(out) + '\n');
}

main();
"""


def _answer(status=200, body=None):
    """One stubbed answer of the route: the shape ``fetch`` hands to the panel."""
    return {"status": status, "body": body}


def _render(answer, *, click=False, open_row=None):
    """Run the real panel against one answer and return what it drew.

    ``click`` presses "Scannen" first (the page itself does not), ``open_row``
    presses "Öffnen" in that row afterwards. A missing node is a failure, not a
    skip: the harness is the only place the rendering is measured.
    """
    if NODE is None:
        raise unittest.SkipTest("node is required to render the panel offline")
    with tempfile.TemporaryDirectory(prefix="gap-scan-ui-") as tmp:
        tmp = Path(tmp)
        (tmp / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
        (tmp / "harness.js").write_text(_HARNESS, encoding="utf-8")
        befehl = [NODE, str(tmp / "harness.js"), str(PANEL_JS), str(tmp / "answer.json")]
        if click:
            befehl.append("--click")
        if open_row is not None:
            befehl += ["--open", str(open_row)]
        lauf = subprocess.run(befehl, capture_output=True, text=True, timeout=120)
    if lauf.returncode != 0:
        raise AssertionError("the render harness failed:\n" + lauf.stderr[-2000:])
    return json.loads(lauf.stdout)


def _page():
    return _client().get("/").get_data(as_text=True)


def _three_candidates():
    """The route's own answer for a day with three measurable titles."""
    antwort, koerper, _kalender, _quelle = _scan()
    return _answer(antwort.status_code, koerper), koerper


class TestThePageCarriesThePanel(unittest.TestCase):
    def test_the_page_carries_the_gap_section(self):
        seite = _page()
        self.assertIn("GAP-SCAN nach Earnings", seite)
        self.assertIn('id="gap-scan"', seite)
        # die drei Bedienteile des Abschnitts, in der Reihenfolge des Auftrags
        self.assertIn('<input type="date" id="gscan-day"', seite)
        self.assertIn('id="gscan-run">Scannen<', seite)
        # Das Verhalten ist eine Datei, kein zweites Inline-Skript
        self.assertIn("/static/gap_scan.js", seite)
        self.assertTrue(PANEL_JS.is_file())

    def test_the_panel_starts_hidden_and_the_page_scans_nothing(self):
        seite = _page()
        # Ein Fehler, eine leere Liste und ein Kalenderstand-Hinweis sind beim
        # Laden nicht da - sie erscheinen mit einer Antwort oder gar nicht.
        for id_ in ("gscan-error", "gscan-empty", "gscan-stale", "gscan-counts",
                    "gscan-table-wrap"):
            self.assertRegex(seite, rf'id="{id_}"[^>]*\shidden>')
        # Das Datumsfeld traegt keinen Serverwert: "gestern" setzt das Skript.
        self.assertNotIn('id="gscan-day" value=', seite)
        panel = _render(_three_candidates()[0])
        self.assertIsNone(panel["url"])        # kein Automatiklauf beim Laden
        self.assertEqual([], panel["rows"])
        gestern = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(gestern, panel["defaultDay"])


class TestTheListSpeaksTheAnswer(unittest.TestCase):
    def test_three_candidates_become_three_rows(self):
        antwort, koerper = _three_candidates()
        panel = _render(antwort, click=True)

        self.assertEqual("/api/v1/gap-scan?day=" + DAY, panel["url"])
        self.assertEqual([["#", "Symbol", "Gap %", "Richtung", "Strategie", "Kurs",
                           "Umsatz", "Termin", "Öffnen"]], panel["head"])
        self.assertEqual(3, len(panel["rows"]))
        self.assertEqual(["AAA", "CCC", "BBB"], [r["symbol"] for r in panel["rows"]])
        self.assertFalse(panel["tableHidden"])

        # Gap, Richtung, Strategie und Kurs je Zeile - die Zahlen der Route.
        erwartet = [
            ("AAA", "+10.00 %", "↑", "Bull Call", "11.00"),
            ("CCC", "-10.00 %", "↓", "Bear Put", "9.00"),
            ("BBB", "+4.00 %", "↑", "Bull Call", "10.40"),
        ]
        for row, (symbol, gap, pfeil, strategie, kurs) in zip(panel["rows"], erwartet, strict=True):
            self.assertEqual(symbol, row["symbol"])
            self.assertEqual([gap, pfeil, strategie, kurs], row["cells"][2:6])
        # die Zahlen, aus denen die Liste entsteht, stehen als Text darueber
        self.assertIn("Tag " + DAY, panel["counts"])
        self.assertIn("geprüft 3", panel["counts"])
        self.assertIn("angezeigt 3", panel["counts"])
        # Keine Kette, kein Spread in der Liste: neun Spalten, keine mehr.
        self.assertEqual([9] * 3, [len(r["cells"]) for r in panel["rows"]])

    def test_the_session_is_not_shown(self):
        antwort, koerper = _three_candidates()
        # Die Antwort traegt die Zweifel - die Anzeige liest sie nicht.
        self.assertTrue(koerper["candidates"][0]["session_uncertain"])
        self.assertEqual("unknown", koerper["candidates"][0]["session"])
        panel = _render(antwort, click=True)

        self.assertEqual(3, len(panel["rows"]))
        self.assertNotIn("session_uncertain", panel["text"])
        self.assertNotIn("before_market", panel["text"])
        self.assertIn("Symbol", panel["head"][0])
        self.assertNotIn("Session", panel["head"][0])
        # auch nicht im Code: das Feld wird nicht gelesen, nur im Kommentar
        # als bewusst nicht gelesen benannt
        quelle = PANEL_JS.read_text(encoding="utf-8")
        lesend = [zeile for zeile in quelle.splitlines()
                  if "session_uncertain" in zeile
                  and not zeile.strip().startswith(("//", "*", "/*"))]
        self.assertEqual([], lesend)
        self.assertNotIn("entry.session", quelle)

    def test_no_candidate_names_the_failures(self):
        # Der Tag ist beantwortet, aber kein Titel liess sich messen.
        kaputt = _History(errors={s: GapSourceError("no_history", "no rows")
                                  for s in ("AAA", "BBB", "CCC")})
        antwort, koerper, _kalender, _quelle = _scan(history=kaputt)

        self.assertEqual(200, antwort.status_code)
        self.assertEqual(0, koerper["counts"]["selected"])
        panel = _render(_answer(200, koerper), click=True)
        self.assertIn("Kein Kandidat", panel["empty"])
        self.assertIn("3 Fehlschläge", panel["empty"])
        self.assertTrue(panel["tableHidden"])
        self.assertEqual([], panel["rows"])

    def test_an_unpublished_day_is_named(self):
        # Der Fall vom 12.11. 17:00 ET: der Kalender listet den Tag, die
        # Tagestabelle hat den Schluss noch nicht. Die Anzeige darf das nicht
        # als ruhigen Tag verkaufen - und nicht als Fehler.
        vorlauf = _History(bars={
            symbol: [GapBar(session_date=date(2026, 11, 10), open=10.0, close=10.0,
                            volume=1_000_000.0)]
            for symbol in ("AAA", "BBB", "CCC")
        })
        antwort, koerper, _kalender, _quelle = _scan(history=vorlauf)
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("history_not_published", koerper["result_state"])

        panel = _render(_answer(200, koerper), click=True)

        self.assertIn("Tagesschlusskurse", panel["empty"])
        self.assertIn("noch nicht veröffentlicht", panel["empty"])
        self.assertIn("erneut scannen", panel["empty"])
        # Keine erfundene Zeile, kein Fehlerzustand - die Zaehlerzeile bleibt.
        self.assertEqual([], panel["rows"])
        self.assertTrue(panel["tableHidden"])
        self.assertIsNone(panel["error"])
        self.assertIn("Tag " + DAY, panel["counts"])
        self.assertIn("geprüft 0", panel["counts"])
        self.assertIn("angezeigt 0", panel["counts"])
        # Nicht der Text des leeren Ergebnisses: "kein Kandidat" waere hier falsch.
        self.assertNotIn("Kein Kandidat", panel["empty"])

    def test_the_open_button_carries_the_strategy(self):
        antwort, koerper = _three_candidates()
        # Zeile 0 ist der Aufwaerts-Gap (AAA), Zeile 1 der Abwaerts-Gap (CCC).
        hoch = _render(antwort, click=True, open_row=0)
        self.assertEqual("AAA", hoch["opened"]["ticker"])
        self.assertEqual(["bull_call_debit"], hoch["opened"]["strategy"])

        runter = _render(antwort, click=True, open_row=1)
        self.assertEqual("CCC", runter["opened"]["ticker"])
        self.assertEqual(["bear_put_debit"], runter["opened"]["strategy"])
        self.assertTrue(runter["opened"]["submitted"])

        # Das BESTEHENDE Formular wird benutzt, kein zweites gebaut.
        quelle = PANEL_JS.read_text(encoding="utf-8")
        self.assertIn("screener-form", quelle)
        self.assertNotIn("createElement('form')", quelle)


class TestTheErrorsAreNamed(unittest.TestCase):
    def _assert_page_intact(self):
        seite = _page()
        self.assertIn('id="screener-form"', seite)
        self.assertIn("GAP-SCAN nach Earnings", seite)

    def test_an_invalid_day_is_named(self):
        antwort, koerper, _kalender, _quelle = _scan("?day=2026-13-01")
        self.assertEqual(400, antwort.status_code)
        self.assertEqual("invalid_day", koerper["error"])

        panel = _render(_answer(400, koerper), click=True)
        self.assertIn("Kein gültiges Datum", panel["error"])
        self.assertTrue(panel["tableHidden"])
        # kein stiller leerer Zustand: "keine Zeilen" wird hier nicht behauptet
        self.assertIsNone(panel["empty"])
        self.assertIsNone(panel["counts"])
        self._assert_page_intact()

    def test_an_unavailable_calendar_is_named(self):
        antwort, koerper, _kalender, _quelle = _scan(
            calendar=_calendar(state="unavailable", omit=(DAY,)))
        self.assertEqual(503, antwort.status_code)
        self.assertEqual("calendar_unavailable", koerper["error"])

        panel = _render(_answer(503, koerper), click=True)
        self.assertIn("Kalender nicht verfügbar", panel["error"])
        # der mitgelieferte Grund bleibt sichtbar - er unterscheidet 403 von Schweigen
        self.assertIn(koerper["message"], panel["error"])
        self.assertTrue(panel["tableHidden"])
        self._assert_page_intact()

    def test_a_stale_day_is_flagged(self):
        antwort, koerper, _kalender, _quelle = _scan(calendar=_calendar(state="stale"))
        self.assertEqual(200, antwort.status_code)
        self.assertEqual("stale", koerper["day_state"])

        panel = _render(_answer(200, koerper), click=True)
        self.assertIn("Kalenderstand veraltet", panel["stale"])
        # der Hinweis versteckt die Liste nicht: alte Zeilen sind alt, nicht falsch
        self.assertFalse(panel["tableHidden"])
        self.assertEqual(3, len(panel["rows"]))
        self.assertIsNone(panel["error"])


if __name__ == "__main__":
    unittest.main()
