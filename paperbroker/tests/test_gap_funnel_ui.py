"""Offline tests for the spread detail block on ``GET /``.

The funnel's table answers WHICH spread a row trades. This block answers what
that one spread looks like, and it is opened by the ROW ITSELF - no button, no
chevron: a click anywhere on the row loads ``GET /api/v1/gap-spreads/detail`` for
that symbol and day, fills the collapsed ``<details id="funnel-detail">`` between
table and exclusions and opens it. Enter and Space do the same from the keyboard.

Pinned here, in the order of the requirement:

* the block exists in the markup, sits between the table and the exclusions, and
  is closed and invisible until a row is chosen,
* the row carries ``tabindex``, ``aria-expanded`` and ``aria-controls`` - and NO
  button, no chevron, nothing clickable inside it,
* a click on the row fetches that symbol and the SCANNED day, opens the block and
  marks exactly one row,
* Enter and Space on the focused row do the same, and Space does not scroll,
* a second row replaces the content and the mark - one block, one active row,
* the block shows strategy, expiry, the four numbers and a leg row per leg, two
  curves in one SVG, and six scenarios with seven columns,
* an error answer is written INTO the block (not as an alert) and the row stays
  usable for a second attempt,
* no inline handler in the HTML and no foreign URL in the script.

The rendering is executed, not inferred: the harness below runs the real
``static/gap_funnel.js`` under node against a deliberately small DOM, with
``fetch`` answering two documents (the list and the detail) from a JSON file.
No test touches a socket.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import paperbroker
from paperbroker.tests.test_gap_scan_endpoint import DAY, _client

PANEL_JS = Path(paperbroker.__file__).parent / "static" / "gap_funnel.js"
NODE = shutil.which("node")
PANEL_NOW = "2026-11-12T15:00:00Z"

_HARNESS = r"""
'use strict';
// Renders paperbroker/static/gap_funnel.js against TWO stubbed answers:
// the list, then the detail of one row. Usage:
//   node harness.js <gap_funnel.js> <answers.json> [--now ISO]
//        [--scan] [--actions "click:0,key:1:Enter"]
const fs = require('fs');
const vm = require('vm');

const argv = process.argv.slice(2);
const jsPath = argv[0];
const answerPath = argv[1];
const nowAt = argv.indexOf('--now');
const FROZEN = new Date(nowAt >= 0 ? argv[nowAt + 1] : '2026-11-12T15:00:00Z').getTime();
const scanFirst = argv.indexOf('--scan') >= 0;
const actionsAt = argv.indexOf('--actions');
const ACTIONS = actionsAt >= 0 ? argv[actionsAt + 1].split(',').filter(Boolean) : [];

const source = fs.readFileSync(jsPath, 'utf8');
const answers = JSON.parse(fs.readFileSync(answerPath, 'utf8'));

class Text {
  constructor(v) { this.childNodes = []; this.attrs = {}; this._own = String(v); }
  get textContent() { return this._own; }
  set textContent(v) { this._own = String(v); }
}

class El {
  constructor(tag) {
    this.tagName = String(tag);
    this.childNodes = [];
    this.attrs = {};
    this.handlers = {};
    this.className = '';
    this.title = '';
    this.value = '';
    this.type = '';
    this.hidden = false;
    this.open = false;
    this.disabled = false;
    this._own = '';
    this.scrolled = 0;
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
  set textContent(v) {
    this._own = '';
    this.childNodes = String(v) === '' ? [] : [new Text(v)];
  }
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
  scrollIntoView() { this.scrolled += 1; }
}

const ids = ['gap-funnel', 'funnel-day', 'funnel-run', 'funnel-state', 'funnel-error',
  'funnel-counts', 'funnel-notes', 'funnel-stale', 'funnel-empty', 'funnel-table-wrap',
  'funnel-tbody', 'funnel-foot', 'funnel-cap', 'funnel-detail',
  'funnel-detail-summary', 'funnel-detail-body',
  'funnel-excluded-block', 'funnel-excluded-summary', 'funnel-excluded',
  'funnel-failures-block', 'funnel-failures-summary', 'funnel-failures'];
const nodes = {};
ids.forEach((id) => {
  let tag = 'div';
  if (id === 'funnel-run') tag = 'button';
  if (id === 'funnel-day') tag = 'input';
  if (id === 'funnel-state' || id === 'funnel-notes') tag = 'p';
  if (id.indexOf('summary') >= 0) tag = 'summary';
  if (id === 'funnel-excluded' || id === 'funnel-failures') tag = 'ul';
  if (id.indexOf('-block') >= 0 || id === 'funnel-detail') tag = 'details';
  nodes[id] = new El(tag);
});

['funnel-error', 'funnel-counts', 'funnel-notes', 'funnel-stale', 'funnel-empty',
  'funnel-table-wrap', 'funnel-foot', 'funnel-detail', 'funnel-excluded-block',
  'funnel-failures-block']
  .forEach((id) => { nodes[id].hidden = true; });
nodes['funnel-day'].type = 'date';

const document = {
  getElementById(id) { return nodes[id] || null; },
  createElement(tag) { return new El(tag); },
  // SVG nodes come through this path - the chart is built with the SVG
  // namespace, exactly as a browser needs it.
  createElementNS(namespace, tag) { const node = new El(tag); node.namespace = namespace; return node; }
};

let urls = [];
let detailCalls = 0;
function nextAnswer(url) {
  if (url.indexOf('/detail') >= 0) {
    const liste = Array.isArray(answers.details) ? answers.details : [answers.detail];
    const answer = liste[Math.min(detailCalls, liste.length - 1)];
    detailCalls += 1;
    return answer;
  }
  return answers.list;
}
function fetch(url) {
  urls.push(url);
  const answer = nextAnswer(url);
  return Promise.resolve({
    ok: answer.status >= 200 && answer.status < 300,
    status: answer.status,
    text() { return Promise.resolve(JSON.stringify(answer.body)); }
  });
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
// Freeze the panel's clock INSIDE the sandbox before gap_funnel.js loads, so
// "yesterday" and the day of the request are the fixture's, not the wall clock's.
vm.runInContext(`
(function () {
  var Real = Date;
  var FIXED = ${FROZEN};
  function Frozen() {
    if (!(this instanceof Frozen)) return new Real(FIXED).toString();
    return arguments.length ? new Real(...arguments) : new Real(FIXED);
  }
  Frozen.now = function () { return FIXED; };
  Frozen.parse = Real.parse;
  Frozen.UTC = Real.UTC;
  Frozen.prototype = Real.prototype;
  globalThis.Date = Frozen;
}());
`, sandbox, { filename: 'frozen-clock.js' });
vm.runInContext(source, sandbox, { filename: 'gap_funnel.js' });

function tree(node) {
  return {
    tag: String(node.tagName || ''),
    attrs: node.attrs || {},
    cls: node.className || (node.attrs || {}).class || '',
    text: node.textContent,
    children: (node.childNodes || []).filter((c) => c.tagName).map(tree)
  };
}

function findAll(node, predicate, out) {
  out = out || [];
  if (predicate(node)) out.push(node);
  (node.childNodes || []).forEach((child) => { if (child.tagName) findAll(child, predicate, out); });
  return out;
}

function rows() { return nodes['funnel-tbody'].childNodes.filter((c) => c.tagName); }

function rowInfo(tr) {
  return {
    symbol: tr.attrs['data-symbol'],
    expanded: tr.attrs['aria-expanded'],
    controls: tr.attrs['aria-controls'],
    tabindex: tr.attrs['tabindex'],
    role: tr.attrs['role'] || null,
    className: tr.className,
    buttons: findAll(tr, (n) => String(n.tagName).toUpperCase() === 'BUTTON').length,
    links: findAll(tr, (n) => String(n.tagName).toUpperCase() === 'A').length
  };
}

function cells(tr) {
  return (tr.childNodes || []).filter((c) => String(c.tagName).toLowerCase() === 'td')
    .map((td) => td.textContent);
}

function report(prevented) {
  const body = nodes['funnel-detail-body'];
  const paths = findAll(body, (n) => String(n.tagName).toLowerCase() === 'path');
  const svgs = findAll(body, (n) => String(n.tagName).toLowerCase() === 'svg');
  const tables = findAll(body, (n) => String(n.tagName).toLowerCase() === 'table');
  return {
    urls,
    rows: rows().map(rowInfo),
    cells: rows().map(cells),
    counts: nodes['funnel-counts'].textContent,
    foot: nodes['funnel-foot'].textContent,
    footHidden: nodes['funnel-foot'].hidden,
    cap: nodes['funnel-cap'].textContent,
    detail: {
      hidden: nodes['funnel-detail'].hidden,
      open: nodes['funnel-detail'].open,
      summary: nodes['funnel-detail-summary'].textContent,
      text: body.textContent,
      tree: tree(body),
      paths: paths.map((p) => p.attrs),
      svgs: svgs.length,
      svgAttrs: svgs.map((sv) => sv.attrs),
      tables: tables.map((t) => ({
        headers: findAll(t, (n) => String(n.tagName).toLowerCase() === 'th').map((n) => n.textContent),
        rows: findAll(t, (n) => String(n.tagName).toLowerCase() === 'tr').length - 1,
        cells: findAll(t, (n) => String(n.tagName).toLowerCase() === 'td').length
      })),
      scrolled: nodes['funnel-detail'].scrolled
    },
    pageError: nodes['funnel-error'].hidden ? null : nodes['funnel-error'].textContent,
    detailBlockCount: findAll(document.getElementById('gap-funnel'),
      (n) => n.attrs && n.attrs.id === 'funnel-detail').length,
    prevented
  };
}

function tick(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function main() {
  if (scanFirst) nodes['funnel-run'].click();
  await tick(25);
  const prevented = [];
  for (const spec of ACTIONS) {
    const parts = spec.split(':');
    if (parts[0] === 'scan') nodes['funnel-run'].click();
    if (parts[0] === 'click') rows()[Number(parts[1])].click();
    if (parts[0] === 'key') {
      const taste = parts[2] === 'Space' ? ' ' : (parts[2] || 'Enter');
      const ev = { type: 'keydown', key: taste, defaultPrevented: false,
                   preventDefault() { ev.defaultPrevented = true; } };
      rows()[Number(parts[1])].dispatchEvent(ev);
      prevented.push({ spec, prevented: ev.defaultPrevented });
    }
    await tick(25);
  }
  process.stdout.write(JSON.stringify(report(prevented)) + '\n');
}

main();
"""


def _detail(symbol="CTVA", label="Bear Put"):
    """One answer of the detail route, of the documented shape."""
    return {
        "schema_version": "1.0", "symbol": symbol, "name": "Corteva Inc.",
        "day": DAY, "strategy": "bear_put_debit", "strategy_label": label,
        "expiration": "2027-03-19", "dte": 126, "spot": 9.19,
        "spot_source": "chain_underlying",
        "strikes": {"long": 10.0, "short": 7.5},
        "legs": [
            {"side": "long", "strike": 10.0, "bid": 1.32, "ask": 1.38, "mid": 1.35,
             "iv": 42.51, "delta": -62.0, "gamma": 11.33, "vega": 2.96, "theta": -0.51,
             "open_interest": 820, "volume": 41},
            {"side": "short", "strike": 7.5, "bid": 0.03, "ask": 0.07, "mid": 0.05,
             "iv": 44.10, "delta": -31.0, "gamma": 6.21, "vega": 1.58, "theta": -0.19,
             "open_interest": 410, "volume": 0}
        ],
        "net_debit": 1.35, "net_debit_basis": "ask_long_minus_bid_short",
        "max_loss": 135.0, "max_profit": 115.0, "breakeven": 8.65, "rr": 0.85,
        "position_greeks": {"delta": -31.0, "gamma": 5.12, "vega": 1.38,
                            "theta": -0.32, "rho": None},
        "series": [
            {"price": round(4.6 + index * 0.46, 2),
             "pl_expiry": -135.0 if index < 8 else 115.0,
             "pl_today": -60.0 + index * 9.0}
            for index in range(21)
        ],
        "scenarios": [
            {"change_pct": pct, "underlying": round(9.19 * (1 + pct / 100.0), 2),
             "pl_expiry": -135.0 if pct < -6 else 115.0,
             "delta": -40.0 + pct, "gamma": 4.0, "vega": 1.2, "theta": -0.3}
            for pct in (-30, -20, -10, 0, 10, 20)
        ],
        "notes": ["G&V je 1 Spread (100 Aktien), ohne Gebühren."],
    }


def _row(symbol="CTVA", direction="down", label="Bear Put", move=-5.9,
         volatility=54.2, daily=3.1, hit=31.0, windows=394, state="ok",
         sessions=87):
    """One row of the documented answer, with its reachability numbers.

    ``move`` is the required move in percent (-5.9 % against a break-even of
    8.65 and a close of 9.19), ``hit`` the measured frequency, ``windows`` its
    denominator and ``sessions`` the window length in TRADING SESSIONS - 87 of
    them inside the 126 CALENDAR days of ``dte``.
    """
    return {
        "symbol": symbol, "name": "Corteva Inc.", "gap_pct": -24.3,
        "direction": direction, "strategy": "bear_put_debit",
        "strategy_label": label, "close": 9.19, "turnover_usd": 42_000_000.0,
        "earnings_date": "2026-11-10", "next_earnings_date": "2027-02-01",
        "expiration": "2027-03-19", "dte": 126, "long_strike": 10.0,
        "short_strike": 7.5, "long_delta": -62.0, "short_delta": -31.0,
        "net_debit": 1.35, "max_loss": 135.0, "max_profit": 115.0, "rr": 2.13,
        "net_return_pct": 85.2, "liquidity": "full", "open_interest": [820, 410],
        "bid_ask_pct": [6.1, 9.4], "position_debit": 135.0,
        "required_move_pct": move,
        "required_move_direction": (None if move is None
                                    else ("up" if move > 0 else "down")),
        "volatility_pct": volatility, "daily_move_pct": daily,
        "hit_rate_pct": hit, "windows_measured": windows,
        "window_sessions": sessions, "window_calendar_days": 126,
        "history_state": state,
    }


def _list():
    return {
        "schema_version": "1.0", "day": DAY, "day_state": "live",
        "threshold_pct": 5.0, "min_turnover_usd": 5_000_000.0, "rr_min": 2.0,
        "min_oi": 100, "max_required_move_pct": 20.0,
        "counts": {"calendar_rows": 128, "gaps_computed": 117, "over_threshold": 27,
                   "turnover_passed": 27, "price_passed": 24, "candidates": 19,
                   "with_next_earnings": 14, "with_expiry_after_earnings": 13,
                   "with_chain": 9, "with_spread_ge_rr": 4,
                   "with_reachable_move": 2, "shown": 2},
        "results": [_row(), _row("UPCO", "up", "Bull Call")],
        "excluded": [], "failures": [], "notes": [],
    }


def _render(*, list_body=None, details=None, actions=None, scan=True):
    """Run the real panel and return what it drew.

    ``details`` is a list of answers for successive detail requests (the last
    one repeats), so a retry can be measured. ``actions`` is the harness's small
    script - ``click:0``, ``key:1:Enter``, ``key:0:Space``.
    """
    if NODE is None:
        raise unittest.SkipTest("node is required to render the panel offline")
    antworten = {
        "list": {"status": 200, "body": list_body if list_body is not None else _list()},
        "details": details if details is not None else [{"status": 200, "body": _detail()}],
    }
    with tempfile.TemporaryDirectory(prefix="gap-detail-ui-") as tmp:
        tmp = Path(tmp)
        (tmp / "answers.json").write_text(json.dumps(antworten), encoding="utf-8")
        (tmp / "harness.js").write_text(_HARNESS, encoding="utf-8")
        befehl = [NODE, str(tmp / "harness.js"), str(PANEL_JS), str(tmp / "answers.json"),
                  "--now", PANEL_NOW]
        if scan:
            befehl.append("--scan")
        if actions:
            befehl.extend(["--actions", ",".join(actions)])
        env = {**os.environ, "TZ": "UTC"}
        lauf = subprocess.run(befehl, capture_output=True, text=True, timeout=120, env=env)
    if lauf.returncode != 0:
        raise AssertionError("the render harness failed:\n" + lauf.stderr[-2000:])
    return json.loads(lauf.stdout)


def _page():
    return _client().get("/").get_data(as_text=True)


def _all_text(node):
    out = [node.get("text") or ""]
    for child in node.get("children") or []:
        out.append(_all_text(child))
    return " ".join(out)


def _nodes(node, predicate):
    """Every node of the rendered tree that matches, depth first."""
    out = [node] if predicate(node) else []
    for child in node.get("children") or []:
        out.extend(_nodes(child, predicate))
    return out


def _by_class(node, name):
    """Nodes whose class attribute carries ``name`` (one class of several)."""
    return _nodes(node, lambda n: name in str(n.get("cls") or "").split())


def _by_tag(node, name):
    return _nodes(node, lambda n: str(n.get("tag", "")).lower() == name)


class TestTheThreeReachabilityColumns(unittest.TestCase):
    """Nötige Bewegung, Schwankung, Treffer - direkt nach dem R/R."""

    def _headers(self):
        seite = _page()
        kopf = seite.split("<thead>")[1].split("</thead>")[0]
        return [t.strip() for t in re.findall(r"<th>(.*?)</th>", kopf, re.S)]

    def test_the_three_columns_stand_directly_after_the_ratio(self):
        spalten = self._headers()
        stelle = spalten.index("R/R")
        self.assertEqual(spalten[stelle + 1:stelle + 4], ["nötig %", "Schwankung", "Treffer"])
        # EINE Spalte je Frage, keine vierte: was danach kommt, ist der Verfall.
        self.assertEqual(spalten[stelle + 4], "Verfall")

    def test_the_head_names_the_rule_of_the_run(self):
        seite = _page()
        self.assertIn("Zugelassen: R/R ≥ 1:2 und eine nötige Bewegung bis", seite)

    def test_the_row_shows_all_three_numbers_in_order(self):
        panel = _render(actions=[])
        zellen = panel["cells"][0]
        # Nach R/R (Index 9) folgen noetig %, Schwankung, Treffer.
        self.assertIn("↓ -5,9 %", zellen[10])
        self.assertIn("54,2 %", zellen[11])
        self.assertIn("Tag 3,1 %", zellen[11])
        self.assertIn("31 %", zellen[12])
        self.assertIn("n=394", zellen[12])

    def test_the_hit_cell_names_the_sessions_behind_the_rate(self):
        # Die Zelle nennt BEIDE Einheiten: n = Zahl der Fenster, HT = ihre
        # Laenge in Handelstagen. dte (126) steht in der Verfallspalte daneben -
        # Kalendertage, nicht Handelstage.
        panel = _render(actions=[])
        treffer = panel["cells"][0][12]
        self.assertIn("n=394", treffer)
        self.assertIn("87 HT", treffer)
        self.assertNotIn("126 HT", treffer)

    def test_a_row_without_sessions_names_no_session_length(self):
        # Kein Fenster, keine Fensterlaenge: die Unterzeile nennt den Zustand,
        # nicht "0 HT" (eine Laenge von null gibt es nicht).
        panel = _render(list_body={**_list(), "results": [
            _row("SPIR", hit=None, windows=0, sessions=0, state="too_short")]})
        treffer = panel["cells"][0][12]
        self.assertNotIn("HT", treffer)
        self.assertIn("zu wenig Historie", treffer)

    def test_a_rising_move_carries_the_up_arrow_and_the_plus(self):
        panel = _render(list_body={**_list(), "results": [
            _row("LAND", "up", "Bull Call", move=10.1)]})
        zelle = panel["cells"][0][10]
        self.assertIn("↑", zelle)
        self.assertIn("+10,1 %", zelle)
        self.assertIn("steigen", zelle)

    def test_a_missing_number_is_a_dash_and_never_a_zero(self):
        # Kein Wert wird erfunden: die Route liefert null, die Zelle zeigt einen
        # Strich - eine 0 waere eine Messung, die es nicht gibt.
        panel = _render(list_body={**_list(), "results": [
            _row("SPIR", move=None, volatility=None, daily=None, hit=None,
                 windows=0, sessions=0, state="too_short")]})
        zellen = panel["cells"][0]
        self.assertIn("—", zellen[10])
        self.assertNotIn("0 %", zellen[10])
        self.assertIn("—", zellen[11])
        self.assertIn("—", zellen[12])
        self.assertNotIn("n=0", zellen[12])
        self.assertIn("zu wenig Historie", zellen[12])

    def test_the_foot_explains_the_frequency_with_window_length_and_count(self):
        panel = _render(actions=[])
        self.assertFalse(panel["footHidden"])
        fuss = panel["foot"]
        # Die exakte Formulierung: Fenster in HANDELSTAGEN, Restlaufzeit in
        # KALENDERTAGEN - zwei Einheiten, zwei Worte.
        self.assertIn(
            "Treffer = Anteil der vergangenen Fenster von 87 Handelstagen "
            "(≈ 126 Kalendertage bis Verfall), in denen der Kurs die nötige "
            "Bewegung mindestens erreicht hat; gemessen an 394 Fenstern aus "
            "höchstens 520 Tagesbars.", fuss)
        self.assertIn("Handelstagen", fuss)
        self.assertIn("Kalendertage", fuss)
        self.assertNotIn("Handelstage-Fenster", fuss)
        self.assertIn("Eine Häufigkeit, keine Wahrscheinlichkeit", fuss)

    def test_the_counter_line_names_the_move_hurdle_and_its_bound(self):
        panel = _render(actions=[])
        self.assertIn("2 mit nötiger Bewegung bis 20 %", panel["counts"])
        # Die Grenze im Kopf ist die der Antwort, nicht eine Seitenkonstante.
        self.assertEqual("20", panel["cap"])

    def test_the_detail_repeats_the_three_numbers_and_the_method(self):
        panel = _render(actions=["click:0"])
        text = panel["detail"]["text"]
        self.assertIn("nötig -5,9 %", text)
        self.assertIn("Schwankung 54,2 %", text)
        self.assertIn("Treffer 31 %", text)
        # Derselbe Satz wie im Fuss - Handelstage fuer das Fenster,
        # Kalendertage fuer die Restlaufzeit.
        self.assertIn(
            "Methodik: Treffer = Anteil der vergangenen Fenster von 87 "
            "Handelstagen (≈ 126 Kalendertage bis Verfall)", text)
        self.assertIn("gemessen an 394 Fenstern", text)
        self.assertNotIn("Fensterlänge 126 Handelstage", text)


class TestTheBlockSitsBetweenTableAndExclusions(unittest.TestCase):
    def test_the_markup_carries_one_closed_block(self):
        seite = _page()
        self.assertEqual(1, seite.count('id="funnel-detail"'))
        self.assertRegex(seite, r'id="funnel-detail"[^>]*\shidden>')
        # zwischen Tabelle und Ausschluessen, nicht in einer Zeile
        tabelle = seite.index('id="funnel-table-wrap"')
        detail = seite.index('id="funnel-detail"')
        ausschluesse = seite.index('id="funnel-excluded-block"')
        self.assertLess(tabelle, detail)
        self.assertLess(detail, ausschluesse)

    def test_the_block_is_not_a_second_table_and_carries_no_button(self):
        seite = _page()
        # Nur die eine Ergebnistabelle; der Detailblock hat keinen eigenen Knopf.
        self.assertEqual(1, seite.count("<table"))
        self.assertEqual(1, seite.count("<button"))
        self.assertNotIn("onclick=", seite)

    def test_nothing_is_open_before_a_row_is_chosen(self):
        panel = _render(actions=[])
        self.assertTrue(panel["detail"]["hidden"])
        self.assertFalse(panel["detail"]["open"])
        self.assertEqual("", panel["detail"]["text"])

    def test_a_new_scan_closes_the_block_of_the_old_rows(self):
        panel = _render(actions=["click:0", "scan"])
        self.assertTrue(panel["detail"]["hidden"])
        self.assertFalse(panel["detail"]["open"])
        self.assertEqual({"false"}, {r["expanded"] for r in panel["rows"]})


class TestTheRowItselfLoadsTheDetail(unittest.TestCase):
    def test_the_row_itself_loads_the_detail(self):
        panel = _render(actions=["click:0"])
        self.assertEqual(2, len(panel["urls"]))
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=CTVA&day=" + DAY,
                         panel["urls"][1])
        self.assertFalse(panel["detail"]["hidden"])
        self.assertTrue(panel["detail"]["open"])
        self.assertIn("CTVA", panel["detail"]["summary"])
        self.assertIn("Bear Put", panel["detail"]["text"])
        self.assertIn("2027-03-19", panel["detail"]["text"])
        self.assertIn("1 zu 0,85", panel["detail"]["text"])
        self.assertEqual("true", panel["rows"][0]["expanded"])

    def test_the_row_is_keyboard_reachable(self):
        enter = _render(actions=["key:0:Enter"])
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=CTVA&day=" + DAY,
                         enter["urls"][1])
        self.assertTrue(enter["detail"]["open"])
        self.assertEqual("true", enter["rows"][0]["expanded"])

        space = _render(actions=["key:1:Space"])
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=UPCO&day=" + DAY,
                         space["urls"][1])
        self.assertTrue(space["detail"]["open"])
        # Space darf die Seite nicht scrollen
        self.assertTrue(space["prevented"][0]["prevented"])

    def test_a_row_announces_its_target_and_never_carries_a_button(self):
        panel = _render(actions=[])
        for row in panel["rows"]:
            self.assertEqual("0", row["tabindex"])
            self.assertEqual("false", row["expanded"])
            self.assertEqual("funnel-detail", row["controls"])
            self.assertIsNone(row["role"])          # keine role="button"
            self.assertEqual(0, row["buttons"])
            self.assertEqual(0, row["links"])
            self.assertIn("row-detail", row["className"])

    def test_only_one_row_is_marked_at_a_time(self):
        panel = _render(actions=["click:0", "click:1"],
                        details=[{"status": 200, "body": _detail()},
                                 {"status": 200, "body": _detail("UPCO", "Bull Call")}])
        self.assertEqual(3, len(panel["urls"]))
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=CTVA&day=" + DAY,
                         panel["urls"][1])
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=UPCO&day=" + DAY,
                         panel["urls"][2])
        self.assertEqual(["false", "true"], [r["expanded"] for r in panel["rows"]])
        self.assertIn("UPCO", panel["detail"]["summary"])
        self.assertIn("Bull Call", panel["detail"]["text"])
        self.assertNotIn("CTVA", panel["detail"]["text"])
        # EIN Block, EIN Bild: der zweite Klick ersetzt, er stapelt nicht.
        self.assertEqual(1, panel["detail"]["svgs"])

    def test_the_active_row_is_marked_in_the_markup(self):
        panel = _render(actions=["click:1"])
        self.assertIn("row-active", panel["rows"][1]["className"])
        self.assertNotIn("row-active", panel["rows"][0]["className"])


class TestWhatTheBlockShows(unittest.TestCase):
    def test_the_head_names_strategy_expiry_and_the_four_numbers(self):
        panel = _render(actions=["click:0"])
        text = panel["detail"]["text"]
        self.assertIn("CTVA", text)
        self.assertIn("Corteva Inc.", text)
        self.assertIn("Bear Put", text)
        self.assertIn("Verfall 2027-03-19", text)
        self.assertIn("Debit 1,35", text)
        self.assertIn("max. Verlust 135,00", text)
        self.assertIn("Break-even 8,65", text)
        self.assertIn("max. Gewinn 115,00", text)
        self.assertIn("R/R 1 zu 0,85", text)

    def test_both_legs_are_a_row_with_quote_iv_oi_and_delta(self):
        panel = _render(actions=["click:0"])
        legs = panel["detail"]["tables"][0]
        self.assertEqual(["Seite", "Strike", "Bid", "Ask", "Mid", "IV", "OI", "Delta"],
                         legs["headers"])
        self.assertEqual(2, legs["rows"])
        self.assertEqual(16, legs["cells"])
        text = panel["detail"]["text"]
        self.assertIn("Long", text)
        self.assertIn("Short", text)
        self.assertIn("10,00", text)
        self.assertIn("1,32", text)
        self.assertIn("42,51 %", text)
        self.assertIn("-62,00", text)

    def test_the_chart_is_one_svg_with_two_curves(self):
        panel = _render(actions=["click:0"])
        self.assertEqual(1, panel["detail"]["svgs"])
        self.assertEqual(2, len(panel["detail"]["paths"]))
        pfade = panel["detail"]["paths"]
        # heute durchgezogen, Verfall gestrichelt - zwei verschiedene Kurven
        self.assertTrue(any(not p.get("stroke-dasharray") for p in pfade))
        self.assertTrue(any(p.get("stroke-dasharray") for p in pfade))
        for pfad in pfade:
            self.assertTrue(pfad["d"].startswith("M"))
            self.assertIn("L", pfad["d"])
        # Marker und Achsen sind Text, keine Farbe allein
        self.assertIn("Spot 9,19", panel["detail"]["text"])
        self.assertEqual(["Kurs", "G&V (USD je Spread)"],
                         [n["text"] for n in _by_class(panel["detail"]["tree"],
                                                       "chart-axis-title")])

    def test_the_chart_fills_its_container(self):
        panel = _render(actions=["click:0"])
        svg = panel["detail"]["svgAttrs"][0]
        # Die Breite kommt vom Kasten, nicht aus einer Pixelzahl.
        self.assertEqual("100%", svg.get("width"))
        self.assertTrue(svg.get("viewBox"))
        self.assertEqual("xMidYMid meet", svg.get("preserveAspectRatio"))
        # keine feste Hoehe: das Bild folgt dem Seitenverhaeltnis der viewBox
        self.assertNotIn("height", svg)
        # und das Blatt deckelt das Bild nicht mehr bei 680 px
        regel = re.search(r"\.detail-chart svg\{([^}]*)\}", _page())
        assert regel is not None
        css = regel.group(1).replace(" ", "")
        self.assertIn("width:100%", css)
        self.assertIn("max-width:100%", css)
        self.assertIn("height:auto", css)
        self.assertNotIn("680", css)

    def test_the_chart_names_both_curves(self):
        panel = _render(actions=["click:0"])
        legenden = _by_class(panel["detail"]["tree"], "detail-legend")
        self.assertEqual(1, len(legenden))
        text = _all_text(legenden[0])
        self.assertIn("heute", text)
        self.assertIn("bei Verfall", text)
        # Strichart als zweites Signal neben dem Wort, nicht nur die Farbe.
        self.assertEqual({"solid", "dashed"},
                         {n["cls"] for n in _by_tag(legenden[0], "i")})
        # Fuer Screenreader: Titel und Beschreibung im Bild selbst.
        svg = _by_tag(panel["detail"]["tree"], "svg")[0]
        self.assertTrue(_by_tag(svg, "title"))
        self.assertTrue(_by_tag(svg, "desc"))
        self.assertIn("heute", svg["attrs"]["aria-label"])
        self.assertIn("Verfall", svg["attrs"]["aria-label"])

    def test_the_chart_labels_the_price_axis(self):
        panel = _render(actions=["click:0"])
        tree = panel["detail"]["tree"]
        ticks = _by_class(tree, "chart-x-tick")
        self.assertGreaterEqual(len(ticks), 5)
        werte = [t["text"] for t in ticks]
        # jede Marke eine Zahl mit Dezimalkomma, keine doppelt
        for wert in werte:
            self.assertRegex(wert, r"^\d+,\d{2}$")
        self.assertEqual(len(werte), len(set(werte)))
        # in Kursreihenfolge: links kleiner Kurs, rechts groesser
        zahlen = [float(w.replace(",", ".")) for w in werte]
        self.assertEqual(sorted(zahlen), zahlen)
        # auch die G&V-Achse traegt Werte, nicht nur den Rahmen
        self.assertGreaterEqual(len(_by_class(tree, "chart-y-tick")), 5)

    def test_the_table_scrolls_sideways_instead_of_cutting_the_last_column(self):
        seite = _page()
        kasten = re.search(r"\.tbl\{([^}]*)\}", seite)
        assert kasten is not None
        self.assertIn("overflow-x:auto", kasten.group(1).replace(" ", ""))
        self.assertIn("max-width:100%", kasten.group(1).replace(" ", ""))
        # Die Nummer bleibt beim Scrollen stehen: die Zeile bleibt zuordenbar.
        sticky = re.search(r"\.tbl th:first-child,\.tbl td:first-child\{([^}]*)\}",
                           seite)
        assert sticky is not None
        self.assertIn("position:sticky", sticky.group(1).replace(" ", ""))
        self.assertIn("left:0", sticky.group(1).replace(" ", ""))

    def test_the_scenarios_are_six_rows_with_seven_columns(self):
        panel = _render(actions=["click:0"])
        szenarien = panel["detail"]["tables"][1]
        self.assertEqual(["Prozent", "Basiswert", "G&V", "Delta", "Gamma", "Vega",
                          "Theta"], szenarien["headers"])
        self.assertEqual(6, szenarien["rows"])
        self.assertEqual(42, szenarien["cells"])
        text = panel["detail"]["text"]
        self.assertIn("-30 %", text)
        self.assertIn("+20 %", text)
        self.assertIn("-135,00", text)
        self.assertIn("+115,00", text)


class TestAnErrorStaysInsideTheBlock(unittest.TestCase):
    def test_a_failed_detail_is_written_into_the_block(self):
        panel = _render(actions=["click:0"],
                        details=[{"status": 404,
                                  "body": {"error": "chain_unavailable",
                                           "message": "Kette weg"}}])
        self.assertFalse(panel["detail"]["hidden"])
        self.assertTrue(panel["detail"]["open"])
        self.assertIn("Optionskette", panel["detail"]["text"])
        self.assertIn("nicht lesbar", panel["detail"]["text"])
        # die Zeile bleibt markiert und der Block bleibt bedienbar
        self.assertEqual("true", panel["rows"][0]["expanded"])

    def test_a_second_click_is_the_second_attempt(self):
        panel = _render(
            actions=["click:0", "click:0"],
            details=[{"status": 404, "body": {"error": "chain_unavailable"}},
                     {"status": 200, "body": _detail()}])
        self.assertEqual(2, len([u for u in panel["urls"] if "/detail" in u]))
        self.assertIn("Bear Put", panel["detail"]["text"])
        self.assertNotIn("nicht lesbar", panel["detail"]["text"])

    def test_a_failed_detail_raises_no_alert_on_the_page(self):
        panel = _render(actions=["click:0"],
                        details=[{"status": 500, "body": {"error": "scan_failed"}}])
        # Kein Alert: der Lauf selbst ist nicht gescheitert, nur sein Detail.
        self.assertIsNone(panel["pageError"])
        self.assertIn("HTTP 500", panel["detail"]["text"])


class TestThePanelStaysFreeOfForeignCode(unittest.TestCase):
    def test_no_inline_handler_and_no_foreign_url(self):
        seite = _page()
        self.assertNotIn("onclick=", seite)
        self.assertNotIn("onkeydown=", seite)
        quelle = PANEL_JS.read_text(encoding="utf-8")
        for verboten in ("https://", "cdn.", "unpkg", "jsdelivr"):
            self.assertNotIn(verboten, quelle)
        # Die einzige Adresse im Skript ist der SVG-Namensraum - keine Ressource.
        self.assertEqual(["http://www.w3.org/2000/svg"],
                         re.findall(r"https?://[^'\"]+", quelle))
        # gezeichnet wird mit createElementNS im SVG-Namensraum, nicht per Markup
        self.assertIn("createElementNS", quelle)
        self.assertNotIn("innerHTML = '<svg", quelle)

    def test_the_detail_route_is_called_with_symbol_and_day_only(self):
        quelle = PANEL_JS.read_text(encoding="utf-8")
        self.assertIn("/api/v1/gap-spreads/detail", quelle)
        panel = _render(actions=["click:0"])
        detail_url = panel["urls"][1]
        self.assertEqual({"symbol", "day"},
                         set(re.findall(r"[?&]([a-z_]+)=", detail_url)))


if __name__ == "__main__":
    unittest.main()
