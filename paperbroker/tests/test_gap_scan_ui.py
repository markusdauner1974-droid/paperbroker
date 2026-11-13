"""Offline tests for the gap-spread funnel on ``GET /``.

The page is ONE funnel: a reaction day goes in, the funnel measures the gap,
checks turnover, waits for a known next report, asks for an expiry after that
report, looks for a debit spread with reward/risk of at least 1 to 2 and shows
what survived. The route behind it is ``GET /api/v1/gap-spreads``; this file
pins the panel that consumes it.

What is pinned here, in the order of the requirement:

* header and subtitle exist, the page carries ONE date field with yesterday as
  its default, ONE button, and starts NOTHING on load,
* the table carries exactly the fourteen agreed columns, in that order,
* a candidate of the documented answer shape becomes one row: gap with arrow
  AND word, strategy label, close, both strikes, debit, max loss, max profit,
  R/R as "1 zu 2,3", expiry, DTE, next report, and an options cell with both
  open interests, both bid/ask percentages and the liquidity word,
* the counter line names every hurdle of the funnel in plain German, built from
  ``counts`` alone,
* an empty result says "Kein Kandidat" together with the number of exclusions,
* exclusions and failures live in two collapsed ``<details>`` blocks and name
  their reason in plain German,
* ``day_state: "stale"`` is flagged while the rows stay visible,
* an error answer names itself and leaves no quiet empty state behind.

The rendering is executed, not inferred: ``_HARNESS`` runs the real
``static/gap_funnel.js`` under node against a deliberately small DOM, with
``fetch`` answering from a JSON file. The payloads are built here from the
documented answer shape (``schema_version``, ``counts``, ``results``,
``excluded``, ``failures``, ``notes``) - the panel's half of the contract. No
test touches a socket, and none needs the route to exist.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import paperbroker
from paperbroker.tests.test_gap_scan_endpoint import DAY, _client

#: The instant the panel's clock is frozen at in every render below: 15:00 UTC
#: is 10:00 ET - a US session still open, so "yesterday" is the calendar day
#: before it. The panel itself keeps reading the real clock; the harness fakes
#: it, because a test that follows the wall clock turns red at midnight.
PANEL_NOW = "2026-11-12T15:00:00Z"

#: The panel's behaviour lives in one static file; the tests execute it.
PANEL_JS = Path(paperbroker.__file__).parent / "static" / "gap_funnel.js"

#: The agreed columns, in the agreed order.
HEADERS = [
    "Nummer", "Symbol", "Gap Prozent", "Strategie", "Kurs",
    "Strikes (Long / Short)", "Debit", "maximaler Verlust", "maximaler Gewinn",
    "R/R", "Verfall", "DTE", "nächster Bericht", "Optionen",
]

#: node is the JS engine of the harness below - no package, no build step.
NODE = shutil.which("node")


def _day_before(instant):
    """The calendar day the panel shows as its default for that instant."""
    moment = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    return (moment.astimezone(UTC).date() - timedelta(days=1)).isoformat()


#: A DOM small enough to read and real enough to run the panel: the ids are the
#: ones screen.html carries.
_HARNESS = r"""
'use strict';
// Renders paperbroker/static/gap_funnel.js against ONE stubbed answer, offline.
// Usage: node harness.js <gap_funnel.js> <answer.json> [--now <ISO>] [--click]
const fs = require('fs');
const vm = require('vm');

const argv = process.argv.slice(2);
const jsPath = argv[0];
const answerPath = argv[1];
const nowAt = argv.indexOf('--now');
const FROZEN = new Date(nowAt >= 0 ? argv[nowAt + 1] : '2026-11-12T15:00:00Z').getTime();
const click = argv.indexOf('--click') >= 0;

const source = fs.readFileSync(jsPath, 'utf8');
const answer = JSON.parse(fs.readFileSync(answerPath, 'utf8'));   // {status, body}

// A text node: the panel sets td.textContent and then appends the sub line, so
// the DOM must keep the text as a real child instead of a scalar.
class Text {
  constructor(v) { this.childNodes = []; this.attrs = {}; this._own = String(v); }
  get textContent() { return this._own; }
  set textContent(v) { this._own = String(v); }
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
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
}

// The ids the panel is known by. Every one of them is a real element of
// screen.html - a typo would leave the panel unrendered here.
const ids = ['gap-funnel', 'funnel-day', 'funnel-run', 'funnel-state', 'funnel-error',
  'funnel-counts', 'funnel-notes', 'funnel-stale', 'funnel-empty', 'funnel-table-wrap',
  'funnel-tbody', 'funnel-excluded-block', 'funnel-excluded-summary', 'funnel-excluded',
  'funnel-failures-block', 'funnel-failures-summary', 'funnel-failures'];
const nodes = {};
ids.forEach((id) => {
  let tag = 'div';
  if (id === 'funnel-run') tag = 'button';
  if (id === 'funnel-day') tag = 'input';
  if (id === 'funnel-state' || id === 'funnel-notes') tag = 'p';
  if (id.indexOf('summary') >= 0) tag = 'summary';
  if (id === 'funnel-excluded' || id === 'funnel-failures') tag = 'ul';
  if (id.indexOf('-block') >= 0) tag = 'details';
  nodes[id] = new El(tag);
});

// The initial hidden flags are the ones screen.html carries; the template test
// pins them, so the harness cannot drift away from the markup.
['funnel-error', 'funnel-counts', 'funnel-notes', 'funnel-stale', 'funnel-empty',
  'funnel-table-wrap', 'funnel-excluded-block', 'funnel-failures-block']
  .forEach((id) => { nodes[id].hidden = true; });
nodes['funnel-day'].type = 'date';

const document = {
  getElementById(id) { return nodes[id] || null; },
  createElement(tag) { return new El(tag); }
};

let requested = null;
function fetch(url) {
  requested = url;
  return Promise.resolve({
    ok: answer.status >= 200 && answer.status < 300,
    status: answer.status,
    text() { return Promise.resolve(JSON.stringify(answer.body)); }
  });
}

function dumpText(node) {
  const out = [];
  (function walk(x) {
    if (x.childNodes && x.childNodes.length) x.childNodes.forEach(walk);
    else if (x._own) out.push(x._own);
    if (x.title) out.push(String(x.title));
  })(node);
  return out.join(' ');
}

function listOf(id) {
  return {
    hidden: nodes[id + '-block'].hidden,
    summary: nodes[id + '-summary'].textContent,
    items: nodes[id].childNodes.map((li) => ({
      text: li.textContent,
      title: li.title || ''
    }))
  };
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
// Freeze the panel's clock INSIDE the sandbox, before gap_funnel.js is loaded:
// `new Date()` (no argument) and `Date.now()` answer FROZEN, every other form
// keeps its meaning. Without this the harness reads the wall clock.
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

// The default the field carries BEFORE any click: "gestern", set by the panel.
const defaultDay = nodes['funnel-day'].value;

function report() {
  return {
    defaultDay,
    now: new Date(FROZEN).toISOString(),
    url: requested,
    state: nodes['funnel-state'].textContent,
    error: nodes['funnel-error'].hidden ? null : nodes['funnel-error'].textContent,
    counts: nodes['funnel-counts'].hidden ? null : nodes['funnel-counts'].textContent,
    notes: nodes['funnel-notes'].hidden ? null : nodes['funnel-notes'].textContent,
    stale: nodes['funnel-stale'].hidden ? null : nodes['funnel-stale'].textContent,
    empty: nodes['funnel-empty'].hidden ? null : nodes['funnel-empty'].textContent,
    tableHidden: nodes['funnel-table-wrap'].hidden,
    rows: nodes['funnel-tbody'].childNodes.map((tr) => ({
      symbol: tr.attrs['data-symbol'],
      direction: tr.attrs['data-direction'],
      cells: tr.childNodes.map((td) => td.textContent),
      titles: tr.childNodes.map((td) => td.title || td.attrs.title || '')
    })),
    excluded: listOf('funnel-excluded'),
    failures: listOf('funnel-failures'),
    // Every word and every tooltip the panel produced - the place a field the
    // order forbids would surface.
    text: Object.keys(nodes).map((id) => dumpText(nodes[id])).join(' \u0001 ')
  };
}

async function main() {
  if (click) nodes['funnel-run'].click();
  await new Promise((resolve) => setTimeout(resolve, 20));
  process.stdout.write(JSON.stringify(report()) + '\n');
}

main();
"""


def _answer(status=200, body=None):
    """One stubbed answer of the route: the shape ``fetch`` hands to the panel."""
    return {"status": status, "body": body}


def _render(answer, *, click=False, now=None):
    """Run the real panel against one answer and return what it drew.

    ``click`` presses "Scannen" first (the page itself does not), ``now``
    freezes the panel's clock at that instant (default ``PANEL_NOW``). A missing
    node is a failure, not a skip: the harness is the only place the rendering
    is measured.
    """
    if NODE is None:
        raise unittest.SkipTest("node is required to render the panel offline")
    with tempfile.TemporaryDirectory(prefix="gap-funnel-ui-") as tmp:
        tmp = Path(tmp)
        (tmp / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
        (tmp / "harness.js").write_text(_HARNESS, encoding="utf-8")
        befehl = [NODE, str(tmp / "harness.js"), str(PANEL_JS), str(tmp / "answer.json"),
                  "--now", now or PANEL_NOW]
        if click:
            befehl.append("--click")
        # TZ is pinned so the injected instant lands on the same calendar day
        # on every machine: 15:00 UTC is 15:00 local, never the next day.
        env = {**os.environ, "TZ": "UTC"}
        lauf = subprocess.run(befehl, capture_output=True, text=True, timeout=120, env=env)
    if lauf.returncode != 0:
        raise AssertionError("the render harness failed:\n" + lauf.stderr[-2000:])
    return json.loads(lauf.stdout)


def _page():
    return _client().get("/").get_data(as_text=True)


def _entry(symbol="CTVA", name="Corteva Inc.", gap=-24.3, direction="down",
           strategy="bear_put_debit", label="Bear Put", close=9.19,
           earnings="2026-11-11", next_earnings="2027-02-01",
           expiration="2027-03-19", dte=126, long_strike=10.0, short_strike=7.5,
           long_delta=-0.62, short_delta=-0.31, net_debit=1.35, max_loss=135.0,
           max_profit=115.0, rr=2.13, liquidity="full", oi=(820, 410),
           ba=(6.1, 9.4), position_debit=135.0):
    """One row of the documented answer shape."""
    return {
        "symbol": symbol, "name": name, "gap_pct": gap, "direction": direction,
        "strategy": strategy, "strategy_label": label, "close": close,
        "turnover_usd": 42_000_000.0, "earnings_date": earnings,
        "next_earnings_date": next_earnings, "expiration": expiration, "dte": dte,
        "long_strike": long_strike, "short_strike": short_strike,
        "long_delta": long_delta, "short_delta": short_delta,
        "net_debit": net_debit, "max_loss": max_loss, "max_profit": max_profit,
        "rr": rr, "net_return_pct": 85.2, "liquidity": liquidity,
        "open_interest": list(oi), "bid_ask_pct": list(ba),
        "position_debit": position_debit,
    }


def _counts(**over):
    counts = {
        "calendar_rows": 128, "gaps_computed": 117, "over_threshold": 27,
        "turnover_passed": 27, "price_passed": 24, "candidates": 19,
        "with_next_earnings": 14, "with_expiry_after_earnings": 13,
        "with_chain": 9, "with_spread_ge_rr": 4, "shown": 3,
    }
    counts.update(over)
    return counts


def _body(results=None, excluded=None, failures=None, notes=None, day_state="live",
          counts=None):
    return {
        "schema_version": "1.0",
        "day": DAY,
        "day_state": day_state,
        "threshold_pct": 5.0,
        "min_turnover_usd": 5_000_000.0,
        "rr_min": 2.0,
        "min_oi": 100,
        "counts": counts if counts is not None else _counts(),
        "results": [] if results is None else results,
        "excluded": [] if excluded is None else excluded,
        "failures": [] if failures is None else failures,
        "notes": [] if notes is None else notes,
    }


def _three():
    """Three rows: one down gap (put), two up gaps (call)."""
    return [
        _entry(),
        _entry(symbol="UPCO", name="Upco Holdings", gap=9.4, direction="up",
               strategy="bull_call_debit", label="Bull Call", close=21.5,
               long_strike=20.0, short_strike=22.5, net_debit=1.05, max_loss=105.0,
               max_profit=145.0, rr=2.38, dte=44, expiration="2026-12-18",
               next_earnings="2027-01-28", liquidity="thin", oi=(140, 120),
               ba=(11.5, 14.0), long_delta=0.48, short_delta=0.29),
        _entry(symbol="MIDW", name="Midwest Utilities", gap=6.1, direction="up",
               strategy="bull_call_debit", label="Bull Call", close=52.4,
               long_strike=50.0, short_strike=55.0, net_debit=2.10, max_loss=210.0,
               max_profit=290.0, rr=2.05, dte=93, expiration="2027-02-19",
               next_earnings="2027-02-04", liquidity="full", oi=(1500, 990),
               ba=(3.2, 4.8), long_delta=0.44, short_delta=0.22),
    ]


class TestThePageIsOneFunnel(unittest.TestCase):
    def test_the_head_names_the_funnel_and_its_four_conditions(self):
        seite = _page()
        self.assertIn("<h1>Gap-Spread-Trichter</h1>", seite)
        # Der Untertitel nennt die vier Bedingungen des Trichters.
        self.assertIn("Earnings-Gap", seite)
        self.assertIn("Verfall nach dem nächsten Bericht", seite)
        self.assertIn("R/R mindestens 1 zu 2", seite)
        self.assertEqual(1, seite.count("<h1>"))

    def test_one_date_field_one_button_and_yesterday_as_default(self):
        seite = _page()
        self.assertIn('<input type="date" id="funnel-day"', seite)
        self.assertIn('id="funnel-run">Scannen<', seite)
        # Das Datumsfeld traegt keinen Serverwert: "gestern" setzt das Skript.
        self.assertNotIn('id="funnel-day" value=', seite)
        self.assertEqual(1, seite.count('type="date"'))

    def test_the_table_has_the_fourteen_agreed_columns(self):
        seite = _page()
        kopf = seite.split("<thead>")[1].split("</thead>")[0]
        spalten = [t.strip() for t in re.findall(r"<th>(.*?)</th>", kopf, re.S)]
        self.assertEqual(HEADERS, spalten)
        # EINE Tabelle, nicht zwei.
        self.assertEqual(1, seite.count("<table"))

    def test_the_footer_names_the_delayed_prices(self):
        seite = _page()
        self.assertIn("CBOE-Kursdaten zeitversetzt", seite)
        self.assertIn("15 Minuten hinter Echtzeit", seite)

    def test_the_behaviour_is_one_static_file_and_no_inline_script(self):
        seite = _page()
        self.assertIn("/static/gap_funnel.js", seite)
        self.assertTrue(PANEL_JS.is_file())
        self.assertEqual(1, seite.count("<script"))
        # kein Inline-Handler, kein zweites Skript
        self.assertNotIn("onclick=", seite)
        self.assertNotIn("addEventListener", seite)

    def test_the_panel_starts_hidden(self):
        seite = _page()
        for id_ in ("funnel-error", "funnel-counts", "funnel-notes", "funnel-stale",
                    "funnel-empty", "funnel-table-wrap", "funnel-excluded-block",
                    "funnel-failures-block"):
            self.assertRegex(seite, rf'id="{id_}"[^>]*\shidden>')


class TestThePanelScansNothingOnItsOwn(unittest.TestCase):
    def test_the_field_carries_yesterday_and_no_request_is_sent(self):
        panel = _render(_answer(200, _body(results=_three())))
        # Der Vortag des eingefrorenen Zeitpunkts - nicht der der Wanduhr.
        self.assertEqual(_day_before(PANEL_NOW), panel["defaultDay"])
        self.assertEqual(DAY, panel["defaultDay"])
        self.assertIsNone(panel["url"])          # kein Automatiklauf
        self.assertEqual([], panel["rows"])
        self.assertTrue(panel["tableHidden"])

    def test_the_default_day_follows_the_injected_clock(self):
        erster = _render(_answer(200, _body()), now="2026-11-12T15:00:00Z")
        zweiter = _render(_answer(200, _body()), now="2026-11-20T15:00:00Z")
        self.assertEqual("2026-11-11", erster["defaultDay"])
        self.assertEqual("2026-11-19", zweiter["defaultDay"])
        self.assertNotEqual(erster["defaultDay"], zweiter["defaultDay"])
        self.assertEqual("2026-11-12T15:00:00.000Z", erster["now"])
        self.assertEqual("2026-11-20T15:00:00.000Z", zweiter["now"])
        # auch der zweite Lauf startet nichts von selbst
        self.assertIsNone(zweiter["url"])


class TestTheRowsSpeakTheAnswer(unittest.TestCase):
    def test_the_click_asks_the_route_for_the_default_day(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        self.assertEqual("/api/v1/gap-spreads?day=" + DAY, panel["url"])

    def test_one_candidate_becomes_one_row_of_fourteen_cells(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        self.assertEqual(3, len(panel["rows"]))
        self.assertFalse(panel["tableHidden"])
        self.assertEqual([14] * 3, [len(r["cells"]) for r in panel["rows"]])
        self.assertEqual(["CTVA", "UPCO", "MIDW"], [r["symbol"] for r in panel["rows"]])

    def test_the_cells_carry_the_numbers_of_the_answer(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        erste = panel["rows"][0]["cells"]
        self.assertEqual("1", erste[0])
        self.assertIn("CTVA", erste[1])
        self.assertIn("Corteva Inc.", erste[1])
        # Pfeil UND Wort - die Richtung haengt nicht an der Farbe.
        self.assertIn("↓", erste[2])
        self.assertIn("-24,3 %", erste[2])
        self.assertIn("abwärts", erste[2])
        self.assertEqual("Bear Put", erste[3])
        self.assertEqual("9,19", erste[4])
        self.assertIn("10,0 / 7,5", erste[5])
        self.assertIn("Long / Short", erste[5])
        self.assertEqual("1,35", erste[6])
        self.assertEqual("135,00", erste[7])
        self.assertEqual("115,00", erste[8])
        # R/R als Satz, mit Dezimalkomma.
        self.assertEqual("1 zu 2,1", erste[9])
        self.assertEqual("2027-03-19", erste[10])
        self.assertEqual("126", erste[11])
        self.assertEqual("2027-02-01", erste[12])

    def test_the_options_cell_names_both_legs_and_the_liquidity(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        erste = panel["rows"][0]["cells"][13]
        self.assertIn("OI 820 / 410", erste)
        self.assertIn("Bid-Ask 6,1 % / 9,4 %", erste)
        self.assertIn("voll liquide", erste)
        zweite = panel["rows"][1]["cells"][13]
        self.assertIn("OI 140 / 120", zweite)
        self.assertIn("dünn", zweite)
        self.assertNotIn("voll liquide", zweite)

    def test_an_up_gap_carries_the_up_arrow(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        zweite = panel["rows"][1]["cells"]
        self.assertIn("↑", zweite[2])
        self.assertIn("+9,4 %", zweite[2])
        self.assertIn("aufwärts", zweite[2])
        self.assertEqual("Bull Call", zweite[3])

    def test_the_strategy_label_comes_from_the_answer(self):
        eintrag = _entry(strategy="bear_put_debit", label="Bear Put (eigene Route)")
        panel = _render(_answer(200, _body(results=[eintrag])), click=True)
        self.assertEqual("Bear Put (eigene Route)", panel["rows"][0]["cells"][3])


class TestTheCounterLine(unittest.TestCase):
    def test_the_line_names_every_hurdle_in_plain_german(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        self.assertEqual(
            "117 Gaps gemessen, 27 über Umsatzhürde, 19 Kandidaten, "
            "14 mit nächstem Termin, 13 mit passendem Verfall, "
            "4 mit R/R mindestens 1 zu 2, 3 angezeigt",
            panel["counts"],
        )

    def test_the_line_follows_the_counts_and_nothing_else(self):
        counts = _counts(gaps_computed=4, turnover_passed=2, candidates=1,
                         with_next_earnings=1, with_expiry_after_earnings=1,
                         with_spread_ge_rr=1, shown=1)
        panel = _render(_answer(200, _body(results=[_entry()], counts=counts)), click=True)
        self.assertEqual(
            "4 Gaps gemessen, 2 über Umsatzhürde, 1 Kandidat, "
            "1 mit nächstem Termin, 1 mit passendem Verfall, "
            "1 mit R/R mindestens 1 zu 2, 1 angezeigt",
            panel["counts"],
        )


class TestTheEmptyStateAndTheLists(unittest.TestCase):
    AUSSCHLUESSE = [
        {"symbol": "ABCD", "reason": "no_next_earnings", "message": "kein Termin"},
        {"symbol": "EFGH", "reason": "no_expiry_after_earnings", "message": None},
        {"symbol": "IJKL", "reason": "no_spread_ge_rr", "message": None},
        {"symbol": "MNOP", "reason": "not_optionable", "message": None},
        {"symbol": "QRST", "reason": "chain_unavailable", "message": "Kette leer"},
    ]
    FEHLER = [
        {"symbol": "UVWX", "code": "no_reaction_bar", "message": "keine Tagesschlusszeile"},
        {"symbol": "YZAB", "code": "no_history", "message": "Quelle schwieg"},
    ]

    def test_an_empty_result_names_kein_kandidat_and_the_exclusions(self):
        panel = _render(_answer(200, _body(
            excluded=self.AUSSCHLUESSE, failures=self.FEHLER,
            counts=_counts(shown=0, with_spread_ge_rr=0))), click=True)

        self.assertIn("Kein Kandidat", panel["empty"])
        self.assertIn("5 Ausschlüsse", panel["empty"])
        self.assertIn("2 Fehlschläge", panel["empty"])
        self.assertTrue(panel["tableHidden"])
        self.assertEqual([], panel["rows"])
        self.assertIsNone(panel["error"])

    def test_the_exclusions_are_collapsed_and_named_in_plain_german(self):
        panel = _render(_answer(200, _body(
            excluded=self.AUSSCHLUESSE, failures=self.FEHLER)), click=True)

        self.assertFalse(panel["excluded"]["hidden"])
        self.assertEqual("Ausschlüsse (5)", panel["excluded"]["summary"])
        self.assertEqual(
            ["ABCD", "EFGH", "IJKL", "MNOP", "QRST"],
            [item["text"][:4] for item in panel["excluded"]["items"]],
        )
        texte = " | ".join(item["text"] for item in panel["excluded"]["items"])
        self.assertIn("kein nächster Bericht bekannt", texte)
        self.assertIn("kein Verfall nach dem nächsten Bericht", texte)
        self.assertIn("kein Spread mit R/R mindestens 1 zu 2", texte)
        self.assertIn("keine gelisteten Optionen", texte)
        self.assertIn("Optionskette nicht lesbar", texte)
        # der Bezeichner bleibt im Tooltip, nicht in der Zeile
        self.assertIn("no_next_earnings", panel["excluded"]["items"][0]["title"])
        self.assertNotIn("no_next_earnings", panel["excluded"]["items"][0]["text"])

    def test_the_failures_carry_code_and_message(self):
        panel = _render(_answer(200, _body(
            excluded=self.AUSSCHLUESSE, failures=self.FEHLER)), click=True)
        self.assertFalse(panel["failures"]["hidden"])
        self.assertEqual("Fehlschläge (2)", panel["failures"]["summary"])
        erster = panel["failures"]["items"][0]["text"]
        self.assertIn("UVWX", erster)
        self.assertIn("no_reaction_bar", erster)
        self.assertIn("keine Tagesschlusszeile", erster)

    def test_no_list_no_block(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        self.assertTrue(panel["excluded"]["hidden"])
        self.assertTrue(panel["failures"]["hidden"])

    def test_the_notes_are_one_quiet_line(self):
        panel = _render(_answer(200, _body(
            results=_three(),
            notes=["Schwelle 5,0 %, Umsatzhürde 5 Mio USD.",
                   "Verfall mindestens einen Tag nach dem Bericht."])), click=True)
        self.assertEqual("Schwelle 5,0 %, Umsatzhürde 5 Mio USD. "
                         "Verfall mindestens einen Tag nach dem Bericht.",
                         panel["notes"])

    def test_without_notes_the_line_stays_away(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        self.assertIsNone(panel["notes"])


class TestTheStatesKeepTheirOwnWords(unittest.TestCase):
    def test_a_stale_calendar_is_flagged_and_the_rows_stay(self):
        panel = _render(_answer(200, _body(results=_three(), day_state="stale")), click=True)
        self.assertIn("Kalenderstand veraltet", panel["stale"])
        self.assertFalse(panel["tableHidden"])
        self.assertEqual(3, len(panel["rows"]))
        self.assertIsNone(panel["error"])

    def test_a_live_day_carries_no_stale_flag(self):
        panel = _render(_answer(200, _body(results=_three())), click=True)
        self.assertIsNone(panel["stale"])

    def test_an_invalid_day_is_named_and_leaves_no_empty_state(self):
        panel = _render(_answer(400, {"error": "invalid_day"}), click=True)
        self.assertIn("Kein gültiges Datum", panel["error"])
        self.assertTrue(panel["tableHidden"])
        # kein stiller leerer Zustand: "keine Zeilen" wird hier nicht behauptet
        self.assertIsNone(panel["empty"])
        self.assertIsNone(panel["counts"])

    def test_an_unavailable_calendar_keeps_the_reason(self):
        panel = _render(_answer(503, {"error": "calendar_unavailable",
                                      "message": "Quelle antwortete 403."}), click=True)
        self.assertIn("Quelle antwortete 403.", panel["error"])
        self.assertTrue(panel["tableHidden"])

    def test_the_panel_sends_no_filters_of_its_own(self):
        # Der Trichter kennt nur den Tag: Schwelle, Umsatzhuerde, R/R-Grenze und
        # Mindest-OI sind Standardwerte der Route, keine Felder der Seite.
        panel = _render(_answer(200, _body(results=_three())), click=True)
        self.assertEqual("/api/v1/gap-spreads?day=" + DAY, panel["url"])
        quelle = PANEL_JS.read_text(encoding="utf-8")
        for feld in ("threshold_pct=", "min_turnover_usd=", "rr_min=", "min_oi="):
            self.assertNotIn(feld, quelle)


class TestThePanelStaysFreeOfCredit(unittest.TestCase):
    def test_no_credit_strategy_reaches_the_panel(self):
        quelle = PANEL_JS.read_text(encoding="utf-8")
        for verboten in ("Bull Put", "Bear Call", "bull_put_credit", "bear_call_credit",
                         "Stückzahl", "IV-Vorliebe"):
            self.assertNotIn(verboten, quelle)


if __name__ == "__main__":
    unittest.main()
