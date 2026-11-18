"""Offline tests for the spread detail block on ``GET /``.

The funnel's table answers WHICH spread a row trades. This block answers what
that one spread looks like, and it is opened by the ROW ITSELF - no button, no
chevron: a click anywhere on the row loads ``GET /api/v1/gap-spreads/detail`` for
that symbol and day, fills the collapsed ``<details id="funnel-detail">`` between
table and exclusions and opens it. Enter and Space do the same from the keyboard.

Pinned here, in the order of the requirement:

* the block exists in the markup, sits between the table and the exclusions, and
  is closed and invisible until a row is chosen,
* the row carries ``tabindex``, ``aria-expanded`` and ``aria-controls``; a row
  WITH alternatives additionally carries one counter (``▸ +3``) in its first
  cell, a row WITHOUT them carries none,
* the counter opens only its group (``1.1``, ``1.2``, ``1.3``) and loads no
  detail, while a click on a sub-row fetches the detail of THAT structure -
  with ``expiration``, ``long`` and ``short``,
* "alle aufklappen" opens every group and leaves each one individually
  closable afterwards; the groups stay together in the sorted order,
* a click on the row fetches that symbol and the SCANNED day, opens the block and
  marks exactly one row,
* Enter and Space on the focused row do the same, and Space does not scroll,
* a second row replaces the content and the mark - one block, one active row,
* the block shows strategy, expiry, the four numbers and a leg row per leg, two
  curves in one SVG, and seven scenarios with seven columns,
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
import urllib.parse
from pathlib import Path

import paperbroker
from paperbroker import texts
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
const i18nAt = argv.indexOf('--i18n');
const I18N_TEXT = i18nAt >= 0 ? fs.readFileSync(argv[i18nAt + 1], 'utf8') : '{}';
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
  'funnel-tbody', 'funnel-foot', 'funnel-cap', 'funnel-rr', 'funnel-expand-all', 'funnel-detail',
  'funnel-detail-summary', 'funnel-detail-body',
  'funnel-excluded-block', 'funnel-excluded-summary', 'funnel-excluded',
  'funnel-failures-block', 'funnel-failures-summary', 'funnel-failures'];
const nodes = {};
ids.forEach((id) => {
  let tag = 'div';
  if (id === 'funnel-run' || id === 'funnel-expand-all') tag = 'button';
  if (id === 'funnel-day') tag = 'input';
  if (id === 'funnel-state' || id === 'funnel-notes') tag = 'p';
  if (id.indexOf('summary') >= 0) tag = 'summary';
  if (id === 'funnel-excluded' || id === 'funnel-failures') tag = 'ul';
  if (id.indexOf('-block') >= 0 || id === 'funnel-detail') tag = 'details';
  nodes[id] = new El(tag);
});

['funnel-error', 'funnel-counts', 'funnel-notes', 'funnel-stale', 'funnel-empty',
  'funnel-table-wrap', 'funnel-foot', 'funnel-expand-all', 'funnel-detail',
  'funnel-excluded-block', 'funnel-failures-block']
  .forEach((id) => { nodes[id].hidden = true; });
nodes['funnel-day'].type = 'date';

const document = {
  // The page inlines the catalogue as one JSON block; the panel reads its texts
  // from it. Handing it in keeps the harness on the REAL path instead of
  // letting T() fall back to the key.
  getElementById(id) {
    if (id === 'i18n-js') { return { textContent: I18N_TEXT }; }
    return nodes[id] || null;
  },
  createElement(tag) { return new El(tag); },
  // SVG nodes come through this path - the chart is built with the SVG
  // namespace, exactly as a browser needs it.
  createElementNS(namespace, tag) { const node = new El(tag); node.namespace = namespace; return node; }
};

let urls = [];
let posts = [];
let detailCalls = 0;
function nextAnswer(url) {
  if (url.indexOf('/detail') >= 0) {
    const liste = Array.isArray(answers.details) ? answers.details : [answers.detail];
    const answer = liste[Math.min(detailCalls, liste.length - 1)];
    detailCalls += 1;
    return answer;
  }
  if (url.indexOf('/hand-earnings') >= 0) {
    // Der EINE Schreibweg des Handkalenders: die Antwort kommt aus der Fixture,
    // der gesendete Rumpf wird mitgeschrieben statt nur geglaubt.
    return answers.hand || { status: 200, body: { status: 'saved' } };
  }
  return answers.list;
}
function fetch(url, options) {
  urls.push(url);
  if (options && options.method === 'POST') {
    posts.push({ url, body: options.body ? JSON.parse(options.body) : null });
  }
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
    // Was im Browser ein reflektiertes Attribut ist, steht hier als Eigenschaft:
    // der Typ eines Eingabefelds, das Ziel eines Links. Genau diese Werte
    // entscheiden, ob ein Datumswaehler aufgeht und ob ein Link in einem neuen
    // Tab landet - sie gehoeren deshalb in den Baum.
    type: node.type || (node.attrs || {}).type || null,
    href: node.href || (node.attrs || {}).href || null,
    target: node.target || (node.attrs || {}).target || null,
    rel: node.rel || (node.attrs || {}).rel || null,
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

function buttonsIn(tr) {
  return findAll(tr, (n) => String(n.tagName).toUpperCase() === 'BUTTON');
}

function isSub(tr) {
  return String(tr.className || '').split(/\s+/).indexOf('sub-row') >= 0;
}

// Die Unterzeilen der i-ten Gruppe: die Zeilen zwischen ihrer Hauptzeile und
// der naechsten Hauptzeile.
function subsOf(index) {
  const out = [];
  let gesehen = -1;
  rows().forEach((tr) => {
    if (!isSub(tr)) { gesehen += 1; return; }
    if (gesehen === index) out.push(tr);
  });
  return out;
}

function rankOf(tr) {
  const spanne = findAll(tr, (n) => String(n.className || '').split(/\s+/)
    .indexOf('rank-num') >= 0)[0];
  return spanne ? spanne.textContent.trim() : null;
}

function rowInfo(tr) {
  const knopf = buttonsIn(tr)[0];
  return {
    rank: rankOf(tr),
    symbol: tr.attrs['data-symbol'],
    variant: tr.attrs['data-variant'] || null,
    id: tr.attrs['id'] || null,
    hidden: !!tr.hidden,
    sub: isSub(tr),
    expanded: tr.attrs['aria-expanded'],
    controls: tr.attrs['aria-controls'],
    tabindex: tr.attrs['tabindex'],
    role: tr.attrs['role'] || null,
    className: tr.className,
    buttons: buttonsIn(tr).length,
    buttonText: knopf ? knopf.textContent : null,
    buttonExpanded: knopf ? knopf.attrs['aria-expanded'] : null,
    buttonControls: knopf ? knopf.attrs['aria-controls'] : null,
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
    excluded: nodes['funnel-excluded'].textContent,
    // Die Ausschlussliste als BAUM: der Handkalender haengt dort als eigene Box
    // mit Link, Feld und Knopf - Text allein wuerde das Feld nicht belegen.
    handCalendar: {
      tree: tree(nodes['funnel-excluded']),
      text: nodes['funnel-excluded'].textContent,
      posts: posts.slice()
    },
    foot: nodes['funnel-foot'].textContent,
    footHidden: nodes['funnel-foot'].hidden,
    cap: nodes['funnel-cap'].textContent,
    rr: nodes['funnel-rr'].textContent,
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
    expandAll: {
      hidden: nodes['funnel-expand-all'].hidden,
      text: nodes['funnel-expand-all'].textContent,
      expanded: nodes['funnel-expand-all'].attrs['aria-expanded'] || null
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
    if (parts[0] === 'btn') {
      // Ein Klick auf den Zaehler - MIT Blasen bis zur Zeile, wie im Browser:
      // bleibt er nicht stehen (stopPropagation), laedt die Zeile das Detail.
      const tr = rows()[Number(parts[1])];
      const knopf = buttonsIn(tr)[0];
      if (knopf) {
        const ev = { type: 'click', target: knopf, defaultPrevented: false,
          preventDefault() {}, stopPropagation() { ev.stopped = true; } };
        knopf.dispatchEvent(ev);
        if (!ev.stopped) tr.click();
      }
    }
    if (parts[0] === 'btnkey') {
      const tr = rows()[Number(parts[1])];
      const knopf = buttonsIn(tr)[0];
      if (knopf) {
        const taste = parts[2] === 'Space' ? ' ' : (parts[2] || 'Enter');
        const ev = { type: 'keydown', key: taste, defaultPrevented: false,
          preventDefault() { ev.defaultPrevented = true; },
          stopPropagation() { ev.stopped = true; } };
        knopf.dispatchEvent(ev);
        if (!ev.stopped) tr.dispatchEvent(ev);
        prevented.push({ spec, prevented: ev.defaultPrevented });
      }
    }
    if (parts[0] === 'greek') {
      // Eine Griechen-Taste ueber dem Bild: ein echter Klick, wie im Browser.
      const knopf = findAll(nodes['funnel-detail-body'],
        (n) => String(n.className || '').split(/\s+/).indexOf('greek-btn') >= 0)[Number(parts[1])];
      if (knopf) knopf.click();
    }
    if (parts[0] === 'greekkey') {
      const knopf = findAll(nodes['funnel-detail-body'],
        (n) => String(n.className || '').split(/\s+/).indexOf('greek-btn') >= 0)[Number(parts[1])];
      if (knopf) {
        const taste = parts[2] === 'Space' ? ' ' : (parts[2] || 'Enter');
        const ev = { type: 'keydown', key: taste, defaultPrevented: false,
          preventDefault() { ev.defaultPrevented = true; } };
        knopf.dispatchEvent(ev);
        prevented.push({ spec, prevented: ev.defaultPrevented });
      }
    }
    if (parts[0] === 'sub') {
      const liste = subsOf(Number(parts[1]));
      const stelle = liste[Number(parts[2])];
      if (stelle) stelle.click();
    }
    if (parts[0] === 'hcdate') {
      // Das Datumsfeld in der Handkalender-Box: ein echter Wert, wie ihn ein
      // Mensch tippt - getippt wird in DIESES Feld, nicht in eine Variable.
      const feld = findAll(nodes['funnel-excluded'],
        (n) => String(n.className || '').split(/\s+/).indexOf('hc-date') >= 0)[Number(parts[1])];
      if (feld) feld.value = parts.slice(2).join(':');
    }
    if (parts[0] === 'hckey2') {
      // Zwei Enter-Anschlaege in Folge: der zweite darf keinen zweiten
      // Schreibvorgang ausloesen.
      const feld = findAll(nodes['funnel-excluded'],
        (n) => String(n.className || '').split(/\s+/).indexOf('hc-date') >= 0)[Number(parts[1])];
      if (feld) {
        for (let i = 0; i < 2; i += 1) {
          feld.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 13,
            preventDefault() {} });
        }
      }
    }
    if (parts[0] === 'hckey') {
      // Enter im Datumsfeld - der Weg, den ein Mensch nimmt, wenn er das Datum
      // getippt hat und nicht zur Maus greifen will.
      const feld = findAll(nodes['funnel-excluded'],
        (n) => String(n.className || '').split(/\s+/).indexOf('hc-date') >= 0)[Number(parts[1])];
      if (feld) feld.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 13,
        preventDefault() {} });
    }
    if (parts[0] === 'hcsave') {
      const knopf = findAll(nodes['funnel-excluded'],
        (n) => String(n.className || '').split(/\s+/).indexOf('hc-save') >= 0)[Number(parts[1])];
      if (knopf) knopf.click();
    }
    if (parts[0] === 'all') nodes['funnel-expand-all'].click();
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


def _series():
    """The axis of the fixture: a 46-cent grid plus the seven scenario prices.

    Every point carries the four greeks - the page draws the overlay from these
    numbers and computes nothing itself. The delta is the fixture's model curve
    (minus 40 at the spot, one point per percent of move), so the marker at a
    scenario price carries exactly the number of that row.
    """
    preise = {round(4.6 + index * 0.46, 2) for index in range(21)}
    preise |= {round(9.19 * (1 + pct / 100.0), 2)
               for pct in (-30, -20, -10, 0, 10, 20, 30)}
    return [
        {"price": preis,
         "pl_expiry": -135.0 if preis < 9.19 * 1.07 else 115.0,
         "pl_today": round(-60.0 + (preis - 4.6) * 19.0, 2),
         "delta": -40.0 + round((preis / 9.19 - 1.0) * 100.0),
         "gamma": 4.0, "vega": 1.2, "theta": -0.3}
        for preis in sorted(preise)
    ]


def _detail(symbol="CTVA", label="Bear Put"):
    """One answer of the detail route, of the documented shape."""
    return {
        "schema_version": "1.0", "symbol": symbol, "name": "Corteva Inc.",
        "day": DAY, "strategy": "bear_put_debit", "strategy_label": label,
        "expiration": "2027-03-19", "dte": 126, "spot": 9.19,
        "spot_source": "chain_underlying",
        "structure_label": "Hebel (Zeile)", "is_row_pick": True,
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
        "greek_explain": {"delta_value": -40, "theta_value": -0.3, "theta_cents": 30},
        "series": _series(),
        "scenarios": [
            {"change_pct": pct, "underlying": round(9.19 * (1 + pct / 100.0), 2),
             "pl_expiry": -135.0 if pct < -6 else 115.0,
             "delta": -40.0 + pct, "gamma": 4.0, "vega": 1.2, "theta": -0.3}
            for pct in (-30, -20, -10, 0, 10, 20, 30)
        ],
        "notes": ["G&V je 1 Spread (100 Aktien), ohne Gebühren."],
    }


def _variant(label="nah am Geld", long_strike=13.0, short_strike=17.0, debit=0.90,
             max_profit=310.0, rr=3.44, move=-4.8, bid_ask=8.4, hit=22.0,
             windows=394, liquidity="full", oi=(820, 410)):
    """One character of the row, in the fields the list delivers."""
    return {"label": label, "long_strike": long_strike, "short_strike": short_strike,
            "net_debit": debit, "max_loss": round(debit * 100, 2),
            "max_profit": max_profit, "rr": rr,
            # Chance = Trefferquote x R/R, wie die Route sie liefert.
            "chance": None if hit is None else round(hit / 100.0 * rr, 2),
            "required_move_pct": move,
            "bid_ask_pct": bid_ask, "hit_rate_pct": hit,
            "windows_measured": windows, "liquidity": liquidity,
            "open_interest": list(oi)}


def _detail_structure(long_strike=13.0, short_strike=17.0, label="nah am Geld"):
    """The detail answer of ONE alternative - not the pick."""
    detail = _detail()
    detail.update({
        "structure_label": label, "is_row_pick": False,
        "strikes": {"long": long_strike, "short": short_strike},
        "net_debit": 0.90, "max_loss": 90.0, "max_profit": 310.0,
        "breakeven": 12.1, "rr": 3.44,
    })
    return detail


def _row(symbol="CTVA", direction="down", label="Bear Put", move=-5.9,
         volatility=54.2, daily=3.1, hit=31.0, windows=394, state="ok",
         sessions=87, variants=None):
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
        "chance": None if hit is None else round(hit / 100.0 * 2.13, 2),
        "net_return_pct": 85.2, "liquidity": "full", "open_interest": [820, 410],
        "bid_ask_pct": [6.1, 9.4], "position_debit": 135.0,
        "required_move_pct": move,
        "required_move_direction": (None if move is None
                                    else ("up" if move > 0 else "down")),
        "volatility_pct": volatility, "daily_move_pct": daily,
        "hit_rate_pct": hit, "windows_measured": windows,
        "window_sessions": sessions, "window_calendar_days": 126,
        "history_state": state,
        "variants": list(variants or []),
    }


def _list():
    return {
        "schema_version": "1.0", "day": DAY, "day_state": "live",
        "threshold_pct": 5.0, "min_turnover_usd": 5_000_000.0, "rr_min": 2.0,
        "min_oi": 100, "max_required_move_pct": 20.0,
        # Die Huerden des Laufs in EINEM Block: die Texte der Seite nennen
        # keine Zahl mehr aus eigener Kenntnis, sondern diese.
        "limits": {"rr_min": 2.0, "min_long_delta": 20.0, "min_net_debit": 0.5,
                   "max_required_move_pct": 20.0},
        "counts": {"calendar_rows": 128, "gaps_computed": 117, "over_threshold": 27,
                   "turnover_passed": 27, "price_passed": 24, "candidates": 19,
                   "with_next_earnings": 14, "with_expiry_after_earnings": 13,
                   "with_chain": 9, "with_spread_ge_rr": 4,
                   "with_reachable_move": 2, "shown": 2},
        "results": [_row(), _row("UPCO", "up", "Bull Call")],
        "excluded": [], "failures": [], "notes": [],
    }


def _render(*, list_body=None, details=None, actions=None, scan=True, hand=None):
    """Run the real panel and return what it drew.

    ``details`` is a list of answers for successive detail requests (the last
    one repeats), so a retry can be measured. ``actions`` is the harness's small
    script - ``click:0``, ``key:1:Enter``, ``key:0:Space``, ``hcdate:0:2026-12-10``,
    ``hcsave:0``. ``hand`` is the answer of the hand-earnings POST.
    """
    if NODE is None:
        raise unittest.SkipTest("node is required to render the panel offline")
    antworten = {
        "list": {"status": 200, "body": list_body if list_body is not None else _list()},
        "details": details if details is not None else [{"status": 200, "body": _detail()}],
    }
    if hand is not None:
        antworten["hand"] = hand
    with tempfile.TemporaryDirectory(prefix="gap-detail-ui-") as tmp:
        tmp = Path(tmp)
        (tmp / "answers.json").write_text(json.dumps(antworten), encoding="utf-8")
        (tmp / "harness.js").write_text(_HARNESS, encoding="utf-8")
        (tmp / "i18n.json").write_text(json.dumps(texts.block("js")), encoding="utf-8")
        befehl = [NODE, str(tmp / "harness.js"), str(PANEL_JS), str(tmp / "answers.json"),
                  "--now", PANEL_NOW, "--i18n", str(tmp / "i18n.json")]
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


def _head(panel):
    """The detail head: the ONE block of class ``detail-head``."""
    koepfe = _by_class(panel["detail"]["tree"], "detail-head")
    if len(koepfe) != 1:
        raise AssertionError(f"expected exactly one detail-head, got {len(koepfe)}")
    return koepfe[0]


def _levels(node):
    """The element children of a node, in reading order."""
    return list(node.get("children") or [])


def _figures(kopf):
    """The raster's cells as ``(label, value)`` pairs, in reading order.

    A cell is label ABOVE value - the text of the cell alone would read
    "Debit1,90", so the pair is taken from the two spans, not from the string.
    """
    raster = _by_class(kopf, "detail-figures")
    if len(raster) != 1:
        raise AssertionError(f"expected exactly one detail-figures, got {len(raster)}")
    paare = []
    for zelle in _by_class(raster[0], "fig"):
        beschriftung = _by_class(zelle, "fig-label")
        wert = _by_class(zelle, "fig-value")
        if len(beschriftung) != 1 or len(wert) != 1:
            raise AssertionError("a cell needs one label and one value: " + zelle["text"])
        paare.append((beschriftung[0]["text"], wert[0]["text"]))
    return paare


class TestTheThreeReachabilityColumns(unittest.TestCase):
    """Nötige Bewegung, Schwankung, Treffer - direkt nach dem R/R."""

    def _headers(self):
        seite = _page()
        kopf = seite.split("<thead>")[1].split("</thead>")[0]
        return [t.strip() for t in re.findall(r"<th>(.*?)</th>", kopf, re.S)]

    def test_the_table_keeps_loss_without_a_duplicate_debit_column(self):
        headers = self._headers()
        self.assertNotIn("Debit", headers)
        self.assertEqual(headers.count("maximaler Verlust"), 1)
        panel = _render()
        for cells in panel["cells"]:
            self.assertEqual(len(cells), len(headers))
        self.assertEqual(panel["cells"][0][6], "135,00")
        self.assertEqual(panel["cells"][0][7], "115,00")

    def test_the_ratio_uses_a_colon_and_decimal_comma(self):
        entry = {**_row(), "rr": 8.1}
        detail = _detail()
        detail["rr"] = 8.1
        panel = _render(list_body={**_list(), "results": [entry]},
                        details=[{"status": 200, "body": detail}], actions=["click:0"])
        self.assertEqual(panel["cells"][0][8], "1 : 8,1")
        self.assertEqual("1 : 8,1", dict(_figures(_head(panel)))["R/R"])
        self.assertNotIn("1 zu", panel["detail"]["text"])
        self.assertIn("R/R mindestens 1 : 2", panel["counts"])

    def test_the_ratio_hurdle_is_written_from_the_answer(self):
        # ``limits.rr_min`` 2.5 - die Seite schreibt BEIDE Stellen aus der
        # Antwort (Zaehlerzeile und Ausschlusssatz), nicht aus eigenem Wissen.
        limits = {**_list()["limits"], "rr_min": 2.5}
        panel = _render(list_body={**_list(), "limits": limits,
                                   "excluded": [{"symbol": "SPIR",
                                                 "reason": "no_spread_ge_rr",
                                                 "message": "kein Paar"}]})
        self.assertIn("mit R/R mindestens 1 : 2,5", panel["counts"])
        self.assertIn("kein Spread mit R/R mindestens 1 : 2,5", panel["excluded"])
        self.assertNotIn("1 : 2 ", panel["counts"])

    def test_without_limits_the_old_sentence_stays(self):
        # Ein alter Server liefert kein ``limits``: die Seite faellt auf den
        # heutigen Satz zurueck, statt "1 : undefined" zu schreiben.
        alt = {name: wert for name, wert in _list().items() if name != "limits"}
        alt["excluded"] = [{"symbol": "SPIR", "reason": "no_spread_ge_rr"}]
        panel = _render(list_body=alt)
        self.assertIn("mit R/R mindestens 1 : 2", panel["counts"])
        self.assertIn("kein Spread mit R/R mindestens 1 : 2", panel["excluded"])
        self.assertNotIn("undefined", panel["counts"] + panel["excluded"])

    def test_the_move_cap_is_read_from_the_limits_block(self):
        limits = {**_list()["limits"], "max_required_move_pct": 35.0}
        panel = _render(list_body={**_list(), "limits": limits,
                                   "max_required_move_pct": 35.0})
        self.assertIn("nötiger Bewegung bis 35 %", panel["counts"])

    def test_the_three_columns_stand_directly_after_the_ratio(self):
        spalten = self._headers()
        stelle = spalten.index("R/R")
        # Direkt am R/R steht die Chance (eine Lesehilfe, keine Sortierung);
        # die drei Messgroessen folgen unmittelbar darauf.
        self.assertEqual(spalten[stelle + 1], "Chance")
        self.assertEqual(spalten[stelle + 2:stelle + 5], ["nötig %", "Schwankung", "Treffer"])
        # EINE Spalte je Frage, keine vierte: was danach kommt, ist der Verfall.
        self.assertEqual(spalten[stelle + 5], "Verfall")

    def test_the_head_names_the_rule_of_the_run(self):
        # Die Kopfzeile traegt die Huerde als eigenen Knoten: sie wird aus
        # ``limits`` gefuellt und nicht im Template festgeschrieben.
        seite = _page()
        self.assertIn('id="funnel-rr"', seite)
        self.assertIn('id="funnel-cap"', seite)

    def test_the_head_follows_the_limits_of_the_response(self):
        # Ein Lauf mit anderer Huerde darf nicht behaupten, es seien 1 : 2.
        limits = {**_list()["limits"], "rr_min": 2.5}
        panel = _render(list_body={**_list(), "limits": limits, "results": []})
        self.assertEqual("1 : 2,5", panel["rr"])

    def test_the_head_falls_back_without_limits(self):
        alt = {name: wert for name, wert in _list().items() if name != "limits"}
        panel = _render(list_body={**alt, "results": []})
        self.assertEqual("1 : 2", panel["rr"])

    def test_the_row_shows_all_three_numbers_in_order(self):
        panel = _render(actions=[])
        zellen = panel["cells"][0]
        # Nach R/R (Index 8) folgt die Chance, dann noetig %, Schwankung, Treffer.
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
        self.assertIn("—", zellen[9])          # Chance: keine Trefferquote, keine Zahl
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
        figuren = dict(_figures(_head(panel)))
        # Die drei Zahlen der Erreichbarkeit stehen im Raster des Kopfes, unter
        # ihrer Beschriftung - nicht mehr als Satz hinter Trennpunkt.
        self.assertEqual("-5,9 %", figuren["nötig"])
        self.assertEqual("54,2 %", figuren["Schwankung"])
        self.assertEqual("31 %", figuren["Treffer"])
        text = panel["detail"]["text"]
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

    def test_the_block_adds_no_table_and_only_the_two_switches(self):
        seite = _page()
        # Nur die eine Ergebnistabelle; die beiden Knoepfe sind der Lauf und der
        # Schalter "alle aufklappen" - der Detailblock hat keinen eigenen.
        self.assertEqual(1, seite.count("<table"))
        self.assertEqual(2, seite.count("<button"))
        self.assertIn('id="funnel-expand-all"', seite)
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
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=CTVA&day=" + DAY + "&lang=de",
                         panel["urls"][1])
        self.assertFalse(panel["detail"]["hidden"])
        self.assertTrue(panel["detail"]["open"])
        self.assertIn("CTVA", panel["detail"]["summary"])
        self.assertIn("Bear Put", panel["detail"]["text"])
        self.assertIn("19.03.2027", panel["detail"]["text"])
        self.assertIn("1 : 0,8", panel["detail"]["text"])
        self.assertEqual("true", panel["rows"][0]["expanded"])

    def test_the_row_is_keyboard_reachable(self):
        enter = _render(actions=["key:0:Enter"])
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=CTVA&day=" + DAY + "&lang=de",
                         enter["urls"][1])
        self.assertTrue(enter["detail"]["open"])
        self.assertEqual("true", enter["rows"][0]["expanded"])

        space = _render(actions=["key:1:Space"])
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=UPCO&day=" + DAY + "&lang=de",
                         space["urls"][1])
        self.assertTrue(space["detail"]["open"])
        # Space darf die Seite nicht scrollen
        self.assertTrue(space["prevented"][0]["prevented"])

    def test_a_row_announces_its_target_and_a_title_without_alternatives_has_no_toggle(self):
        panel = _render(actions=[])
        for row in panel["rows"]:
            self.assertEqual("0", row["tabindex"])
            self.assertEqual("false", row["expanded"])
            self.assertEqual("funnel-detail", row["controls"])
            self.assertIsNone(row["role"])          # keine role="button"
            # Ohne Alternativen gibt es nichts aufzuklappen - also keinen Knopf.
            self.assertEqual(0, row["buttons"])
            self.assertEqual(0, row["links"])
            self.assertIn("row-detail", row["className"])

    def test_only_one_row_is_marked_at_a_time(self):
        panel = _render(actions=["click:0", "click:1"],
                        details=[{"status": 200, "body": _detail()},
                                 {"status": 200, "body": _detail("UPCO", "Bull Call")}])
        self.assertEqual(3, len(panel["urls"]))
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=CTVA&day=" + DAY + "&lang=de",
                         panel["urls"][1])
        self.assertEqual("/api/v1/gap-spreads/detail?symbol=UPCO&day=" + DAY + "&lang=de",
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
        kopf = _head(panel)
        titel, unter = _levels(kopf)[:2]
        self.assertEqual("detail-title", titel["cls"])
        self.assertIn("CTVA", titel["text"])
        self.assertIn("Corteva Inc.", titel["text"])
        # Der Verfall steht deutsch im Untertitel, die Strategie daneben.
        self.assertEqual("Bear Put · Verfall 19.03.2027", unter["text"])
        figuren = dict(_figures(kopf))
        self.assertEqual("1,35", figuren["Debit"])
        self.assertEqual("135,00", figuren["max. Verlust"])
        self.assertEqual("8,65", figuren["Break-even"])
        self.assertEqual("115,00", figuren["max. Gewinn"])
        self.assertEqual("1 : 0,8", figuren["R/R"])

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

    def test_the_scenarios_are_seven_rows_with_seven_columns(self):
        panel = _render(actions=["click:0"])
        szenarien = panel["detail"]["tables"][1]
        self.assertEqual(["Prozent", "Basiswert", "G&V", "Delta", "Gamma", "Vega",
                          "Theta"], szenarien["headers"])
        self.assertEqual(7, szenarien["rows"])
        self.assertEqual(49, szenarien["cells"])
        text = panel["detail"]["text"]
        self.assertIn("-30 %", text)
        self.assertIn("+20 %", text)
        self.assertIn("+30 %", text)
        self.assertIn("-135,00", text)
        self.assertIn("+115,00", text)


class TestTheGreekOverlay(unittest.TestCase):
    """The four buttons over the chart and the one curve they lay on top.

    The two P&L curves never move: the overlay is added above them, with its own
    axis on the right and its own legend entry, and it is gone again after a
    second click. Everything is read from the answer - the panel computes no
    second curve.
    """

    def _buttons(self, panel):
        gruppen = _by_class(panel["detail"]["tree"], "detail-greeks")
        self.assertEqual(1, len(gruppen))
        return _by_tag(gruppen[0], "button")

    def test_the_four_buttons_stand_between_the_chart_title_and_the_image(self):
        panel = _render(actions=["click:0"])
        knoepfe = self._buttons(panel)
        self.assertEqual(["Delta", "Gamma", "Vega", "Theta"],
                         [knopf["text"] for knopf in knoepfe])
        # Inaktiv beim Oeffnen - alle vier.
        self.assertEqual({"false"}, {k["attrs"]["aria-pressed"] for k in knoepfe})
        # Echte Tasten, keine nachgebauten Kacheln.
        self.assertEqual({"button"}, {str(k["tag"]).lower() for k in knoepfe})
        gruppe = _by_class(panel["detail"]["tree"], "detail-greeks")[0]
        self.assertEqual("group", gruppe["attrs"]["role"])
        # Die Zeile steht zwischen Diagrammtitel und SVG.
        klassen = [child["cls"] for child in panel["detail"]["tree"]["children"]]
        self.assertEqual(klassen.index("detail-greeks") + 1, klassen.index("detail-chart"))

    def test_opening_the_detail_shows_the_two_curves_and_nothing_else(self):
        panel = _render(actions=["click:0"])
        self.assertEqual(1, panel["detail"]["svgs"])
        self.assertEqual(2, len(panel["detail"]["paths"]))
        tree = panel["detail"]["tree"]
        # keine rechte Achse, kein dritter Legendeneintrag
        self.assertEqual([], _by_class(tree, "chart-greek-tick"))
        self.assertEqual([], _by_class(tree, "chart-greek-value"))
        legende = _by_class(tree, "detail-legend")[0]
        self.assertEqual({"solid", "dashed"},
                         {n["cls"] for n in _by_tag(legende, "i")})

    def test_a_click_on_delta_lays_one_curve_over_the_two_pnl_curves(self):
        panel = _render(actions=["click:0", "greek:0"])
        pfade = panel["detail"]["paths"]
        self.assertEqual(3, len(pfade))
        # Die beiden G&V-Kurven bleiben, unveraendert in Farbe und Strichart.
        self.assertTrue(any(p.get("stroke") == "#fafafa" for p in pfade))
        self.assertTrue(any(p.get("stroke") == "#e05534"
                            and p.get("stroke-dasharray") == "5 4" for p in pfade))
        # Und darueber genau eine Griechen-Kurve.
        griechen = [p for p in pfade if p.get("stroke") == "#4dc3ff"]
        self.assertEqual(1, len(griechen))
        self.assertEqual("6 4", griechen[0].get("stroke-dasharray"))
        self.assertEqual("1.6", griechen[0].get("stroke-width"))
        self.assertTrue(griechen[0]["d"].startswith("M"))
        # Genau EINE Taste ist aktiv.
        knoepfe = self._buttons(panel)
        self.assertEqual(["true", "false", "false", "false"],
                         [k["attrs"]["aria-pressed"] for k in knoepfe])

    def test_the_right_axis_appears_with_the_overlay_and_names_the_metric(self):
        panel = _render(actions=["click:0", "greek:0"])
        tree = panel["detail"]["tree"]
        ticks = _by_class(tree, "chart-greek-tick")
        self.assertGreaterEqual(len(ticks), 4)
        for tick in ticks:
            self.assertRegex(tick["text"], r"^[+-]?\d+(,\d+)?$")
        titel = _by_class(tree, "chart-greek-axis-title")
        self.assertEqual(1, len(titel))
        self.assertEqual("Delta", titel[0]["text"])
        # Die linke Achse bleibt die G&V-Achse.
        links = [n for n in _by_class(tree, "chart-axis-title")
                 if "chart-greek-axis-title" not in n["cls"]]
        self.assertEqual(["Kurs", "G&V (USD je Spread)"], [n["text"] for n in links])

    def test_the_legend_names_the_overlay_as_a_third_line(self):
        panel = _render(actions=["click:0", "greek:2"])
        legende = _by_class(panel["detail"]["tree"], "detail-legend")[0]
        text = _all_text(legende)
        self.assertIn("heute", text)
        self.assertIn("bei Verfall", text)
        self.assertIn("Vega", text)
        # Wort UND Strichart - die hellblaue Linie ist nicht der einzige Traeger.
        self.assertEqual({"solid", "dashed", "greek"},
                         {n["cls"] for n in _by_tag(legende, "i")})

    def test_the_legend_explains_the_active_metric_in_one_sentence(self):
        panel = _render(actions=["click:0", "greek:0"])
        legende = _by_class(panel["detail"]["tree"], "detail-legend")[0]
        saetze = _by_class(legende, "detail-legend-note")
        self.assertEqual(1, len(saetze))
        # Screenreader lesen den Wechsel, ohne dass der Fokus springt.
        self.assertEqual("polite", saetze[0]["attrs"]["aria-live"])
        text = saetze[0]["text"]
        self.assertIn("Beteiligung", text)
        # Die Zahl der Position steht im Satz - 40 Delta, also 40 Cent je Dollar.
        self.assertIn("-40", text)
        self.assertIn("40 Cent", text)

    def test_the_sentence_follows_the_metric_and_keeps_gamma_vega_without_a_number(self):
        panel = _render(actions=["click:0", "greek:3"])
        satz = _by_class(_by_class(panel["detail"]["tree"], "detail-legend")[0],
                         "detail-legend-note")[0]["text"]
        self.assertIn("Zeitzins", satz)
        self.assertIn("-0,30", satz)
        self.assertIn("30 Cent", satz)
        # Gamma und Vega tragen bewusst KEIN Zahlenbeispiel: die Konvention
        # (per Kurspunkt, per ganze Vol-Einheit) macht eine Kurzzeile falsch.
        for index, wort in ((1, "Tempo"), (2, "Nervosität")):
            panel = _render(actions=["click:0", f"greek:{index}"])
            text = _by_class(_by_class(panel["detail"]["tree"], "detail-legend")[0],
                             "detail-legend-note")[0]["text"]
            self.assertIn(wort, text)
            self.assertFalse(any(z.isdigit() for z in text),
                             f"{wort}-Satz enthaelt eine Zahl: {text}")

    def test_no_sentence_without_an_overlay(self):
        panel = _render(actions=["click:0"])
        self.assertEqual([], _by_class(panel["detail"]["tree"], "detail-legend-note"))
        panel = _render(actions=["click:0", "greek:0", "greek:0"])
        self.assertEqual([], _by_class(panel["detail"]["tree"], "detail-legend-note"))

    def test_a_second_metric_replaces_the_first(self):
        panel = _render(actions=["click:0", "greek:0", "greek:1"])
        self.assertEqual(3, len(panel["detail"]["paths"]))
        knoepfe = self._buttons(panel)
        self.assertEqual(["false", "true", "false", "false"],
                         [k["attrs"]["aria-pressed"] for k in knoepfe])

    def test_a_second_click_on_the_active_button_takes_the_overlay_away(self):
        panel = _render(actions=["click:0", "greek:0", "greek:0"])
        tree = panel["detail"]["tree"]
        self.assertEqual(2, len(panel["detail"]["paths"]))
        self.assertEqual([], _by_class(tree, "chart-greek-tick"))
        self.assertEqual([], _by_class(tree, "chart-greek-value"))
        self.assertEqual({"false"}, {k["attrs"]["aria-pressed"]
                                     for k in self._buttons(panel)})
        legende = _by_class(tree, "detail-legend")[0]
        self.assertEqual({"solid", "dashed"},
                         {n["cls"] for n in _by_tag(legende, "i")})

    def test_the_seven_markers_carry_the_numbers_of_the_scenario_table(self):
        panel = _render(actions=["click:0", "greek:0"])
        tree = panel["detail"]["tree"]
        marken = [m["text"] for m in _by_class(tree, "chart-greek-value")]
        self.assertEqual(7, len(marken))
        # Dieselben sieben Zahlen wie die Delta-Spalte der Szenarientabelle,
        # in derselben Reihenfolge von -30 % bis +30 %.
        tabellen = _by_tag(tree, "table")
        self.assertEqual(2, len(tabellen))
        zeilen = _by_tag(tabellen[1], "tr")[1:]
        self.assertEqual(7, len(zeilen))
        self.assertEqual([_by_tag(zeile, "td")[3]["text"] for zeile in zeilen],
                         marken)

    def test_the_keyboard_switches_the_overlay_too(self):
        panel = _render(actions=["click:0", "greekkey:3:Enter"])
        knoepfe = self._buttons(panel)
        self.assertEqual(["false", "false", "false", "true"],
                         [k["attrs"]["aria-pressed"] for k in knoepfe])
        self.assertEqual(3, len(panel["detail"]["paths"]))
        # Die Leertaste nimmt dieselbe Kurve wieder weg.
        panel = _render(actions=["click:0", "greekkey:3:Enter", "greekkey:3:Space"])
        self.assertEqual(2, len(panel["detail"]["paths"]))
        self.assertEqual({"false"}, {k["attrs"]["aria-pressed"]
                                     for k in self._buttons(panel)})

    def test_the_buttons_are_neutral_until_they_are_active(self):
        seite = _page()
        ruhe = re.search(r"\.detail-greeks button\{([^}]*)\}", seite)
        assert ruhe is not None
        ruhig = ruhe.group(1).replace(" ", "").replace("\n", "")
        # Kein gefuellter Knopf: der Grund bleibt durchsichtig.
        self.assertIn("background:none", ruhig)
        aktiv = re.search(r"\.detail-greeks button\[aria-pressed=\"true\"\]\{([^}]*)\}",
                          seite)
        assert aktiv is not None
        farbe = aktiv.group(1).replace(" ", "").replace("\n", "")
        self.assertIn("border-color:var(--primary)", farbe)
        self.assertIn("color:var(--primary)", farbe)
        # --primary ist die Farbe der Verfallslinie (#e05534).
        self.assertIn("--primary:#e05534", seite)


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


def _three_variants():
    """The three characters of one row, in the order the route delivers them."""
    return [
        _variant("nah am Geld", 13.0, 17.0, 0.90, 310.0, 3.44, -4.8, 8.4, 22.0),
        _variant("am Widerstand", 15.0, 22.0, 1.10, 590.0, 5.36, -8.1, 12.4, 12.0),
        _variant("engster Spread", 10.0, 13.0, 1.40, 160.0, 1.14, -2.2, 4.1, 41.0),
    ]


class TestTheChanceColumn(unittest.TestCase):
    """Die Spalte ``Chance``: eine Zahl zum Lesen, keine zweite Sortierung."""

    def _headers(self):
        seite = _page()
        kopf = seite.split("<thead>")[1].split("</thead>")[0]
        return [t.strip() for t in re.findall(r"<th>(.*?)</th>", kopf, re.S)]

    def test_the_column_stands_directly_after_the_ratio(self):
        spalten = self._headers()
        self.assertEqual(spalten[spalten.index("R/R") + 1], "Chance")
        self.assertEqual(spalten.count("Chance"), 1)

    def test_the_row_shows_the_formatted_number(self):
        # 31 % x 2,13 = 0,66 - deutsches Komma, zwei Stellen.
        panel = _render()
        self.assertEqual("0,66", panel["cells"][0][9])
        self.assertEqual(len(panel["cells"][0]), len(self._headers()))

    def test_a_missing_chance_is_a_dash_and_never_a_zero(self):
        # Kein R/R, keine Chance: der Strich sagt "nicht gerechnet".
        panel = _render(list_body={**_list(), "results": [
            {**_row("SPIR"), "chance": None}]})
        self.assertEqual("—", panel["cells"][0][9])
        self.assertNotIn("0", panel["cells"][0][9])

    def test_the_sub_row_carries_its_own_chance_at_the_same_position(self):
        # Die Unterzeile hat ihre eigene Trefferquote und ihr eigenes R/R -
        # 22 % x 3,44 = 0,76 - und sie steht in DERSELBEN Spalte wie oben.
        panel = _render(list_body={**_list(), "results": [
            _row(variants=_three_variants())]}, actions=["btn:0"])
        self.assertEqual("0,66", panel["cells"][0][9])
        self.assertEqual(["0,76", "0,64", "0,47"],
                         [panel["cells"][i][9] for i in (1, 2, 3)])
        for zellen in panel["cells"]:
            self.assertEqual(len(zellen), len(self._headers()))

    def test_the_list_keeps_the_order_of_the_answer(self):
        # Zwei Zeilen, gegenlaeufig: die erste hat die HOEhere Trefferquote
        # und die NIEDRIGERE Chance. Die Seite sortiert nicht - sie zeigt die
        # Reihenfolge der Antwort.
        hoch = {**_row("AAA", hit=60.0), "rr": 3.0, "chance": 1.8}
        tief = {**_row("BBB", hit=40.0), "rr": 10.0, "chance": 4.0}
        panel = _render(list_body={**_list(), "results": [hoch, tief]})
        self.assertEqual(["AAA", "BBB"], [row["symbol"] for row in panel["rows"]])
        self.assertEqual(["1,80", "4,00"], [panel["cells"][i][9] for i in (0, 1)])

    def test_the_foot_explains_the_chance_column_too(self):
        # Die Spalte steht in der Liste, also muss der Fuss sie erklaeren -
        # inklusive der einen Aussage, die sie NICHT macht: sortieren tut sie nichts.
        panel = _render(actions=[])
        self.assertIn("Chance = Trefferquote × R/R", panel["foot"])
        self.assertIn("Obergrenze des Erwartungswerts", panel["foot"])
        self.assertIn("Sortiert wird nach Trefferquote", panel["foot"])

    def test_the_methodology_sentence_names_the_bound_and_the_order(self):
        panel = _render(actions=["click:0"])
        text = panel["detail"]["text"]
        self.assertIn("Chance = Trefferquote × R/R", text)
        self.assertIn("Obergrenze des Erwartungswerts", text)
        self.assertIn("keine Zusage", text)
        self.assertIn("Sortiert wird weiter nach Trefferquote", text)


class TestTheAlternativesAreSubRows(unittest.TestCase):
    """Die Alternativen stehen als Unterzeilen 1.1, 1.2, 1.3 in der Liste.

    Der Zaehler ``▸ +3`` klappt NUR die Gruppe auf, die Hauptzeile oeffnet ihr
    Detail, die Unterzeile das ihre - mit Verfall, Long und Short dieser
    Struktur. Der Schalter ``alle aufklappen`` oeffnet alle Gruppen und laesst
    jede danach einzeln wieder zuklappen.
    """

    def _list(self, rows=None):
        """CTVA mit drei Alternativen, UPCO ohne: eine Gruppe und ein Titel
        ohne Knopf in derselben Liste."""
        return {**_list(), "results": rows if rows is not None else [
            _row(variants=_three_variants()), _row("UPCO", "up", "Bull Call")]}

    def _two_groups(self):
        """Zwei Titel MIT Alternativen - fuer den Schalter ueber der Tabelle."""
        return {**_list(), "results": [
            _row(variants=_three_variants()),
            _row("UPCO", "up", "Bull Call", variants=[
                _variant("nah am Geld", 5.0, 7.0, 0.70, 130.0, 1.86, -3.1, 6.2, 44.0),
                _variant("engster Spread", 4.0, 6.0, 0.80, 120.0, 1.50, -1.9, 3.3, 51.0)]),
        ]}

    def _params(self, url):
        return dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))

    def test_the_counter_names_the_number_of_alternatives(self):
        panel = _render(list_body=self._list())
        zeilen = panel["rows"]
        self.assertEqual(5, len(zeilen))          # 1 + 1.1 + 1.2 + 1.3 + 2
        self.assertEqual("▸ +3", zeilen[0]["buttonText"])
        self.assertEqual("false", zeilen[0]["buttonExpanded"])
        self.assertEqual("funnel-sub-1-1 funnel-sub-1-2 funnel-sub-1-3",
                         zeilen[0]["buttonControls"])
        # Titel ohne Alternativen: kein Knopf, die Zelle traegt nur die Nummer.
        self.assertEqual(0, zeilen[4]["buttons"])
        self.assertIsNone(zeilen[4]["buttonText"])

    def test_the_sub_rows_carry_their_number_and_their_character(self):
        panel = _render(list_body=self._list())
        nummern = [panel["cells"][i][0] for i in (1, 2, 3)]
        self.assertEqual(["1.1", "1.2", "1.3"], nummern)
        self.assertEqual("CTVA · nah am Geld", panel["cells"][1][1])
        self.assertEqual("CTVA · engster Spread", panel["cells"][3][1])
        # Die Strikes der Unterzeile sind IHRE, nicht die der Hauptzeile.
        self.assertIn("13,0 / 17,0", panel["cells"][1][5])
        self.assertIn("15,0 / 22,0", panel["cells"][2][5])

    def test_a_title_without_alternatives_keeps_its_rank_without_a_button(self):
        panel = _render(list_body={**_list(), "results": [_row("UPCO", "up", "Bull Call")]})
        self.assertEqual(1, len(panel["rows"]))
        self.assertEqual(0, panel["rows"][0]["buttons"])
        self.assertEqual("1", panel["rows"][0]["rank"])
        self.assertEqual("1", panel["cells"][0][0])

    def test_the_counter_opens_only_the_group_and_loads_no_detail(self):
        panel = _render(list_body=self._list(), actions=["btn:0"])
        # Eine Anfrage: die Liste. Der Zaehler hat kein Detail geladen.
        self.assertEqual(1, len(panel["urls"]))
        self.assertTrue(panel["detail"]["hidden"])
        self.assertEqual("▾ +3", panel["rows"][0]["buttonText"])
        self.assertEqual("true", panel["rows"][0]["buttonExpanded"])
        self.assertEqual([False, False, False], [panel["rows"][i]["hidden"] for i in (1, 2, 3)])
        # Die zweite Zeile hat keine Alternativen - sie bleibt unberuehrt.
        self.assertEqual(0, panel["rows"][4]["buttons"])

    def test_a_click_again_closes_the_group(self):
        panel = _render(list_body=self._list(), actions=["btn:0", "btn:0"])
        self.assertEqual("▸ +3", panel["rows"][0]["buttonText"])
        self.assertEqual([True, True, True], [panel["rows"][i]["hidden"] for i in (1, 2, 3)])

    def test_a_sub_row_opens_the_detail_of_that_structure(self):
        panel = _render(list_body=self._list(),
                        details=[{"status": 200, "body": _detail_structure()}],
                        actions=["btn:0", "sub:0:0"])
        self.assertEqual(2, len(panel["urls"]))
        params = self._params(panel["urls"][1])
        self.assertEqual({"symbol", "day", "expiration", "long", "short", "lang"}, set(params))
        self.assertEqual("CTVA", params["symbol"])
        self.assertEqual(DAY, params["day"])
        self.assertEqual("2027-03-19", params["expiration"])
        self.assertEqual("13", params["long"])
        self.assertEqual("17", params["short"])
        self.assertTrue(panel["detail"]["open"])
        titel = _levels(_head(panel))[0]
        self.assertIn("13/17", titel["text"])
        self.assertEqual("nah am Geld", _by_class(titel, "detail-tag")[0]["text"])
        # Die Unterzeile ist markiert, nicht die Hauptzeile.
        self.assertEqual("true", panel["rows"][1]["expanded"])
        self.assertEqual("false", panel["rows"][0]["expanded"])

    def test_the_main_row_still_opens_the_detail_of_the_row(self):
        panel = _render(list_body=self._list(), actions=["click:0"])
        params = self._params(panel["urls"][1])
        self.assertEqual({"symbol", "day", "lang"}, set(params))
        self.assertIn("10/7,5", _levels(_head(panel))[0]["text"])
        self.assertEqual("Hebel (Zeile)",
                         _by_class(_levels(_head(panel))[0], "detail-tag")[0]["text"])

    def test_the_detail_of_an_alternative_shows_the_alternatives_numbers(self):
        panel = _render(list_body=self._list(),
                        details=[{"status": 200, "body": _detail_structure(
                            15.0, 22.0, "am Widerstand")}],
                        actions=["btn:0", "sub:0:1"])
        # 1.2 braucht -8,1 % und traf in 12 % der Fenster - nicht die Zahlen
        # der Hauptzeile (-5,9 %, 31 %).
        figuren = dict(_figures(_head(panel)))
        self.assertEqual("-8,1 %", figuren["nötig"])
        self.assertEqual("12 %", figuren["Treffer"])
        self.assertNotEqual("-5,9 %", figuren["nötig"])
        titel = _levels(_head(panel))[0]
        self.assertIn("15/22", titel["text"])
        self.assertEqual("am Widerstand", _by_class(titel, "detail-tag")[0]["text"])

    def test_an_adjusted_strike_matches_the_rounded_number_of_the_row(self):
        # Ein adjustierter Kontrakt: die Kette listet 15,335, die Zeile traegt
        # 15,34, das Detail kommt mit 15,34. Beide sind DERSELBE Strike - sonst
        # stuenden Striche statt der Zahlen dieser Alternative.
        zeile = _row(variants=[_variant("nah am Geld", long_strike=15.335,
                                        short_strike=17.0)])
        panel = _render(list_body={**_list(), "results": [zeile]},
                        details=[{"status": 200, "body": _detail_structure(
                            15.34, 17.0, "nah am Geld")}],
                        actions=["btn:0", "sub:0:0"])
        figuren = dict(_figures(_head(panel)))
        self.assertEqual("-4,8 %", figuren["nötig"])
        self.assertEqual("22 %", figuren["Treffer"])
        self.assertNotEqual("—", figuren["nötig"])

    def test_a_strike_a_cent_away_is_not_the_alternative(self):
        # Die Toleranz ist ein halber Cent: 15,44 ist NICHT 15,335, und die
        # Seite leiht sich dessen Zahlen nicht.
        zeile = _row(variants=[_variant("nah am Geld", long_strike=15.335,
                                        short_strike=17.0)])
        panel = _render(list_body={**_list(), "results": [zeile]},
                        details=[{"status": 200, "body": _detail_structure(
                            15.44, 17.0, "nah am Geld")}],
                        actions=["btn:0", "sub:0:0"])
        figuren = dict(_figures(_head(panel)))
        self.assertEqual("—", figuren["nötig"])
        self.assertNotEqual("-4,8 %", figuren["nötig"])
        self.assertNotEqual("22 %", figuren["Treffer"])

    def test_alle_aufklappen_opens_every_group(self):
        panel = _render(list_body=self._two_groups(), actions=["all"])
        self.assertEqual("alle zuklappen", panel["expandAll"]["text"])
        self.assertEqual("true", panel["expandAll"]["expanded"])
        self.assertEqual([False, False, False, False, False],
                         [row["hidden"] for row in panel["rows"] if row["sub"]])
        self.assertEqual("▾ +3", panel["rows"][0]["buttonText"])
        self.assertEqual("▾ +2", panel["rows"][4]["buttonText"])

    def test_one_group_can_close_after_the_switch_opened_all(self):
        panel = _render(list_body=self._two_groups(), actions=["all", "btn:0"])
        self.assertEqual([True, True, True], [panel["rows"][i]["hidden"] for i in (1, 2, 3)])
        self.assertEqual("▸ +3", panel["rows"][0]["buttonText"])
        # Die andere Gruppe bleibt offen: der Schalter ist keine Klammer.
        self.assertEqual("▾ +2", panel["rows"][4]["buttonText"])
        self.assertEqual([False, False], [panel["rows"][5]["hidden"], panel["rows"][6]["hidden"]])

    def test_the_groups_stay_together_in_the_sorted_order(self):
        panel = _render(list_body=self._list())
        nummern = [row["rank"] for row in panel["rows"]]
        self.assertEqual(["1", "1.1", "1.2", "1.3", "2"], nummern)
        # Die Reihenfolge INNERHALB der Gruppe ist fest und NICHT die
        # Trefferquote: 1.3 hat die beste Quote (41 %) und bleibt trotzdem
        # hinter 1.1 (22 %) und 1.2 (12 %).
        self.assertEqual([22.0, 12.0, 41.0],
                         [variante["hit_rate_pct"] for variante in _three_variants()])
        self.assertEqual("CTVA · engster Spread", panel["cells"][3][1])

    def test_enter_on_the_counter_opens_the_group_only(self):
        panel = _render(list_body=self._list(), actions=["btnkey:0:Enter"])
        self.assertEqual(1, len(panel["urls"]))
        self.assertEqual("▾ +3", panel["rows"][0]["buttonText"])
        self.assertTrue(panel["prevented"][0]["prevented"])
        self.assertFalse(panel["rows"][1]["hidden"])

    def test_enter_on_the_main_row_opens_the_detail(self):
        panel = _render(list_body=self._list(), actions=["key:0:Enter"])
        self.assertEqual(2, len(panel["urls"]))
        # Die Sprache reist mit, sie ist aber kein Filter: Symbol und Tag bleiben
        # die einzigen Angaben, die die Zeile ueber den Spread macht.
        self.assertEqual({"symbol", "day", "lang"}, set(self._params(panel["urls"][1])))
        self.assertTrue(panel["detail"]["open"])

    def test_the_detail_carries_no_second_alternatives_block(self):
        panel = _render(list_body=self._list(),
                        details=[{"status": 200, "body": _detail_structure()}],
                        actions=["btn:0", "sub:0:0"])
        # Leg-Tabelle und Szenarien - und keine dritte Tabelle mit Varianten.
        self.assertEqual(2, len(panel["detail"]["tables"]))
        self.assertNotIn("keine weitere Struktur", panel["detail"]["text"])

    def test_the_switch_is_hidden_without_a_group(self):
        panel = _render(list_body={**_list(), "results": [_row()]})
        self.assertTrue(panel["expandAll"]["hidden"])
        gemischt = _render(list_body=self._list())
        self.assertFalse(gemischt["expandAll"]["hidden"])


class TestTheHeadHasThreeLevels(unittest.TestCase):
    """Der Kopf ist Titel, Untertitel, Raster - nicht ein Absatz mit 13 Angaben.

    In EINER Zeile mit ' · ' zwischen allen Zahlen ging die Zuordnung Zahl ->
    Bedeutung verloren: "· Break-even 25,90 · Debit 1,90 · max. Verlust 190,00"
    liest niemand als Tabelle. Titel, Untertitel und ein Raster mit der
    Beschriftung UEBER dem Wert sagen dasselbe, ohne dass der Leser zaehlt.
    """

    FIGUREN = ["Debit", "Break-even", "max. Verlust", "max. Gewinn",
               "R/R", "nötig", "Schwankung", "Treffer"]

    def test_the_head_is_three_nodes_and_not_one_run_on_paragraph(self):
        panel = _render(actions=["click:0"])
        kopf = _head(panel)
        # Ein Knoten traegt den Kopf, drei Ebenen stehen darin.
        self.assertEqual("div", kopf["tag"])
        self.assertEqual(["detail-title", "detail-sub detail-head-sub",
                          "detail-figures"],
                         [ebene["cls"] for ebene in _levels(kopf)])
        # Der eine Absatz von frueher ist weg: keine Ebene traegt den ganzen Kopf.
        self.assertEqual(3, len(_levels(kopf)))
        for ebene in _levels(kopf):
            self.assertLess(len(ebene["text"]), len(kopf["text"]))

    def test_the_title_carries_symbol_pair_and_name_with_the_character_as_tag(self):
        panel = _render(actions=["click:0"])
        titel = _levels(_head(panel))[0]
        self.assertIn("CTVA", titel["text"])
        self.assertIn("10/7,5", titel["text"])          # das Strike-Paar
        self.assertIn("Corteva Inc.", titel["text"])    # der Name
        # Der Charakter ist ein eigenes Etikett IN der Titelzeile - kein
        # Satzglied hinter einem Trennungspunkt.
        etiketten = _by_class(titel, "detail-tag")
        self.assertEqual(1, len(etiketten))
        self.assertEqual("Hebel (Zeile)", etiketten[0]["text"])
        # ... und es sitzt direkt hinter dem Paar, vor dem Namen.
        text = titel["text"]
        self.assertLess(text.index("10/7,5"), text.index("Hebel (Zeile)"))
        self.assertLess(text.index("Hebel (Zeile)"), text.index("Corteva Inc."))
        self.assertNotIn(" · Hebel (Zeile)", text)

    def test_the_subtitle_writes_the_expiry_the_german_way(self):
        panel = _render(actions=["click:0"])
        unter = _levels(_head(panel))[1]
        self.assertEqual("Bear Put · Verfall 19.03.2027", unter["text"])
        self.assertNotIn("2027-03-19", unter["text"])
        # Ohne Datum bleibt ein Strich - kein erfundenes Datum.
        ohne = _detail()
        ohne.pop("expiration")
        ohne_datum = _render(details=[{"status": 200, "body": ohne}], actions=["click:0"])
        self.assertEqual("Bear Put · Verfall —",
                         _levels(_head(ohne_datum))[1]["text"])

    def test_the_raster_has_eight_cells_in_order_with_label_over_value(self):
        panel = _render(actions=["click:0"])
        kopf = _head(panel)
        raster = _levels(kopf)[2]
        zellen = _by_class(raster, "fig")
        self.assertEqual(8, len(zellen))
        self.assertEqual(self.FIGUREN, [label for label, _ in _figures(kopf)])
        # Jede Zelle: erst die Beschriftung, dann der Wert - uebereinander.
        for zelle in zellen:
            teile = _levels(zelle)
            self.assertEqual(["fig-label", "fig-value"], [teil["cls"] for teil in teile])
            self.assertTrue(teile[0]["text"])
            self.assertTrue(teile[1]["text"])
        # Die Zahlen sind die der Antwort - unveraendert, nur beschriftet.
        self.assertEqual(
            {"Debit": "1,35", "Break-even": "8,65", "max. Verlust": "135,00",
             "max. Gewinn": "115,00", "R/R": "1 : 0,8", "nötig": "-5,9 %",
             "Schwankung": "54,2 %", "Treffer": "31 %"},
            dict(_figures(kopf)))

    def test_without_the_row_the_three_reachability_cells_stay_dashes(self):
        # Der Titel steht nicht in der Liste des Tages: die drei Zahlen der
        # Erreichbarkeit gehoeren zur Zeile und werden nicht geliehen. Das
        # Raster bleibt achtzellig - die Zellen fallen nicht weg.
        panel = _render(details=[{"status": 200, "body": _detail("ZZZZ")}],
                        actions=["click:0"])
        figuren = _figures(_head(panel))
        self.assertEqual(8, len(figuren))
        self.assertEqual(self.FIGUREN, [label for label, _ in figuren])
        werte = dict(figuren)
        self.assertEqual("—", werte["nötig"])
        self.assertEqual("—", werte["Schwankung"])
        self.assertEqual("—", werte["Treffer"])
        # Die vier Zahlen des Spreads selbst stehen weiterhin da.
        self.assertEqual("1,35", werte["Debit"])

    def test_no_dot_separator_between_the_figures_of_the_head(self):
        panel = _render(actions=["click:0"])
        kopf = _head(panel)
        text = kopf["text"]
        # Kein ' · ' mehr vor einer Zahl ...
        for label in self.FIGUREN:
            self.assertNotIn(" · " + label, text)
        # ... und im Raster gar keiner: die Spalten trennen, nicht Zeichen.
        self.assertNotIn("·", _levels(kopf)[2]["text"])
        # Der Titel behaelt genau zwei Trennpunkte: Symbol|Paar und Paar|Name.
        self.assertEqual(2, _levels(kopf)[0]["text"].count(" · "))


class TestTheHeadIsStyledAsThreeLevels(unittest.TestCase):
    """Das Raster ist ein Raster: vier Spalten, unter 700 px zwei."""

    def _css(self):
        seite = _page()
        return seite[seite.index("<style>"):seite.index("</style>")]

    @staticmethod
    def _rule(css, muster):
        treffer = re.search(muster + r"\{([^}]*)\}", css)
        if treffer is None:
            raise AssertionError("no rule for " + muster)
        return treffer.group(1)

    @staticmethod
    def _font_size(block):
        treffer = re.search(r"font-size:([\d.]+)px", block)
        if treffer is None:
            raise AssertionError("no font-size in " + block)
        return float(treffer.group(1))

    def test_the_raster_is_four_columns_and_two_below_seven_hundred(self):
        css = self._css()
        self.assertIn("repeat(4", self._rule(css, r"\.detail-figures"))
        schmal = re.search(r"@media\s*\(max-width:700px\)\{.*?\.detail-figures"
                           r"\{([^}]*)\}", css, re.S)
        if schmal is None:
            self.fail("no raster rule under 700px")
        self.assertIn("repeat(2", schmal.group(1))

    def test_the_label_sits_above_its_value_in_small_caps(self):
        css = self._css()
        label = self._rule(css, r"\.fig-label")
        wert = self._rule(css, r"\.fig-value")
        self.assertIn("text-transform:uppercase", label)
        self.assertIn("var(--faint)", label)
        self.assertIn("var(--font-mono)", wert)
        # Der Wert ist groesser als seine Beschriftung.
        self.assertGreater(self._font_size(wert), self._font_size(label))
        # Eine Zelle ist eine Spalte: Beschriftung ueber dem Wert.
        self.assertIn("flex-direction:column", self._rule(css, r"\.fig"))

    def test_the_title_is_larger_and_the_subtitle_muted(self):
        css = self._css()
        titel = self._rule(css, r"\.detail-title")
        unter = self._rule(css, r"\.detail-head \.detail-sub")
        self.assertIn("var(--font-mono)", titel)
        self.assertIn("var(--muted)", unter)
        # Titel groesser als der alte Kopf (11 px), Untertitel darunter und
        # leiser als der Titel.
        self.assertGreater(self._font_size(titel), 11)
        self.assertLess(self._font_size(unter), self._font_size(titel))
        # Das Etikett ist eine Beschriftung, kein zweiter Titel.
        self.assertLess(self._font_size(self._rule(css, r"\.detail-tag")), 11)

    def test_the_new_rules_introduce_no_hex_colour_and_no_inline_style(self):
        css = self._css()
        block = css[css.index(".detail-head{"):css.index(".detail-table{")]
        self.assertNotIn("#", block)          # nur Variablen der Seite
        for farbe in ("var(--ink)", "var(--muted)", "var(--faint)", "var(--primary)"):
            self.assertIn(farbe, block)
        # Kein Inline-Style im Skript: die Form steht in der Stildatei.
        quelle = PANEL_JS.read_text(encoding="utf-8")
        self.assertNotIn("style=", quelle)
        self.assertNotIn(".style.", quelle)


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
        # symbol und day sind die Angaben ueber den Spread; lang ist die Sprache
        # der Antwort und kein Filter.
        self.assertEqual({"symbol", "day", "lang"},
                         set(re.findall(r"[?&]([a-z_]+)=", detail_url)))


class TestTheHandCalendar(unittest.TestCase):
    """Die Box neben einem Titel ohne Berichtstermin - im echten Skript gerendert.

    Geprueft wird die STRUKTUR: die Box erscheint nur mit Signal, das Feld nur
    mit Signal, die beiden Abzeichen in ihren Zustaenden, und der Knopf schickt
    Symbol und Datum an den einen Schreibweg. Kein Test fasst einen Socket an.
    """

    SIGNAL = {"status": "signal",
              "title": "ITG Inc. to Report Q4 Earnings on December 10 - Reuters",
              "link": "https://news.example.com/itg",
              "curated_date": None, "source": None, "notiert_am": None}

    def _list(self, hand):
        body = _list()
        body["excluded"] = [{
            "symbol": "ITG", "reason": "no_next_earnings",
            "message": "kein Termin im Fenster",
            "pairs_capped": False, "pairs_enumerated": None, "pairs_possible": None,
            "required_move_pct": None, "max_required_move_pct": None,
            "hand_calendar": hand,
        }]
        return body

    def _box(self, block, **kwargs):
        panel = _render(list_body=self._list(block), **kwargs)
        return panel, _by_class(panel["handCalendar"]["tree"], "detail-handcal")

    def test_a_signal_offers_the_announcement_and_the_field(self):
        panel, boxen = self._box(self.SIGNAL)
        self.assertEqual(1, len(boxen))
        box = boxen[0]
        # Der Link oeffnet die Ankuendigung in einem neuen Tab - ohne Referrer.
        links = [n for n in _by_tag(box, "a") if n["href"] == self.SIGNAL["link"]]
        self.assertEqual(2, len(links), "Titel und 'Ankuendigung oeffnen'")
        for link in links:
            self.assertEqual("_blank", link["target"])
            self.assertIn("noopener", link["rel"])
        self.assertIn(self.SIGNAL["title"], box["text"])
        # Das Feld ist ein echter input mit Typ date und einer Beschriftung.
        felder = _by_tag(box, "input")
        self.assertEqual(1, len(felder))
        self.assertEqual("date", felder[0]["type"])
        self.assertIn("hc-date", felder[0]["cls"])
        self.assertTrue(felder[0]["attrs"].get("aria-describedby"))
        knoepfe = _by_tag(box, "button")
        self.assertEqual(1, len(knoepfe))
        self.assertEqual("Enter", knoepfe[0]["text"])
        self.assertIn("Berichtstermin", box["text"])

    def test_a_link_without_http_scheme_is_shown_as_text_only(self):
        # Der Server laesst so etwas gar nicht erst durch; die Seite prueft es
        # trotzdem - ein Link ist ein Link, egal woher die Zeile kommt.
        panel, boxen = self._box({"status": "signal",
                                  "title": "Acme schedules Q4 earnings",
                                  "link": "javascript:alert(1)",
                                  "curated_date": None, "source": None,
                                  "notiert_am": None})
        self.assertEqual(1, len(boxen))
        self.assertEqual([], _by_tag(boxen[0], "a"))
        self.assertIn("Acme schedules Q4 earnings", boxen[0]["text"])

    def test_without_a_signal_there_is_a_sentence_and_no_field(self):
        panel, boxen = self._box({"status": "no_signal", "title": None, "link": None,
                                  "curated_date": None, "source": None,
                                  "notiert_am": None})
        self.assertEqual([], boxen)
        self.assertIn("keine Ankündigung im Feed-Zeitraum", panel["handCalendar"]["text"])
        self.assertEqual([], _by_tag(panel["handCalendar"]["tree"], "input"))

    def test_an_expired_entry_carries_the_badge(self):
        panel, _boxen = self._box({"status": "expired", "title": None, "link": None,
                                   "curated_date": "2026-11-10", "source": "IR",
                                   "notiert_am": "2026-10-01"})
        abzeichen = _by_class(panel["handCalendar"]["tree"], "hc-badge")
        self.assertEqual(1, len(abzeichen))
        self.assertIn("expired", abzeichen[0]["cls"])
        self.assertEqual("abgelaufen", abzeichen[0]["text"])
        # Ohne Signal bleibt es beim Satz - kein Feld ohne Ankuendigung.
        self.assertEqual([], _by_tag(panel["handCalendar"]["tree"], "input"))

    def test_without_a_hand_calendar_block_the_row_stays_as_before(self):
        panel = _render(list_body=self._list(None))
        self.assertEqual([], _by_class(panel["handCalendar"]["tree"], "detail-handcal"))
        self.assertEqual([], _by_tag(panel["handCalendar"]["tree"], "input"))
        self.assertNotIn("abgelaufen", panel["handCalendar"]["text"])

    def test_saving_posts_the_symbol_and_the_typed_date(self):
        panel, _boxen = self._box(self.SIGNAL, actions=["hcdate:0:2026-12-10", "hcsave:0"])
        posts = panel["handCalendar"]["posts"]
        self.assertEqual(1, len(posts))
        self.assertIn("/api/v1/hand-earnings", posts[0]["url"])
        self.assertEqual({"symbol": "ITG", "date": "2026-12-10"}, posts[0]["body"])

    def test_a_saved_date_starts_the_scan_again_by_itself(self):
        # Der Kern des Ablaufs: EIN Lauf statt zwei. Nach dem Speichern fragt
        # die Seite den Trichter sofort neu ab - mit demselben Tag.
        panel, _boxen = self._box(self.SIGNAL, actions=["hcdate:0:2026-12-10", "hcsave:0"])
        scans = [u for u in panel["urls"] if "/api/v1/gap-spreads" in u]
        self.assertEqual(2, len(scans), f"erster Lauf + Nachlauf, gesehen: {panel['urls']}")
        # Derselbe Tag wie beim ersten Lauf - der Nachlauf erfindet keinen.
        self.assertEqual(scans[0], scans[1])

    def test_a_cleared_field_deletes_the_entry(self):
        panel, _boxen = self._box(
            self.SIGNAL, actions=["hcdate:0:", "hcsave:0"],
            hand={"status": 200, "body": {"status": "deleted", "existed": True}})
        posts = panel["handCalendar"]["posts"]
        self.assertEqual(1, len(posts))
        self.assertEqual({"symbol": "ITG", "date": ""}, posts[0]["body"])
        scans = [u for u in panel["urls"] if "/api/v1/gap-spreads" in u]
        self.assertEqual(2, len(scans), "auch das Loeschen laesst den Trichter neu laufen")

    def test_a_second_enter_while_saving_does_not_start_a_second_save(self):
        # Kody-Befund: der deaktivierte Knopf schluckt nur Klicks, die
        # Enter-Taste im Feld geht daran vorbei. Ohne Sperre wuerde jedes
        # weitere Enter einen POST und einen vollen Trichterlauf nachziehen.
        panel, _boxen = self._box(self.SIGNAL, actions=["hcdate:0:2026-12-10", "hckey2:0"])
        self.assertEqual(1, len(panel["handCalendar"]["posts"]),
                         "zwei Enter, ein Schreibvorgang")
        scans = [u for u in panel["urls"] if "/api/v1/gap-spreads" in u]
        self.assertEqual(2, len(scans), "und genau ein Nachlauf")

    def test_the_enter_key_in_the_field_saves(self):
        # Dieselbe Taste wie der Knopf: tippen, Enter, fertig.
        panel, _boxen = self._box(self.SIGNAL, actions=["hcdate:0:2026-12-10", "hckey:0"])
        posts = panel["handCalendar"]["posts"]
        self.assertEqual(1, len(posts))
        self.assertEqual({"symbol": "ITG", "date": "2026-12-10"}, posts[0]["body"])

    def test_a_rejection_is_written_into_the_box_with_its_own_sentence(self):
        panel, _boxen = self._box(
            self.SIGNAL, actions=["hcdate:0:2026-11-01", "hcsave:0"],
            hand={"status": 400, "body": {"status": "rejected", "code": "past_date",
                                          "message": "the date lies before the scan day"}})
        self.assertIn("Das Datum liegt vor dem Scandatum.", panel["handCalendar"]["text"])

    def test_the_row_of_a_curated_title_says_where_the_date_comes_from(self):
        body = _list()
        body["results"][0]["next_earnings_date"] = "2027-02-01"
        body["results"][0]["next_earnings_source"] = "manual_curated"
        body["results"][0]["hand_calendar"] = {
            "status": "curated", "curated_date": "2027-02-01",
            "source": "IR-Pressemitteilung", "notiert_am": "2026-11-17"}
        panel = _render(list_body=body)
        zelle = [c for c in panel["cells"][0] if c.startswith("2027-02-01")]
        self.assertEqual(1, len(zelle), panel["cells"][0])
        zelle = zelle[0]
        # Vier Zeilen: das Datum, dann drei graue - Herkunft, Quelle, Notizdatum.
        # Der Baum meldet textContent, die Zeilen entstehen im CSS (display:block);
        # geprueft wird hier die Reihenfolge der drei Texte.
        self.assertEqual("2027-02-01Termin manuell kuratiert"
                         "Quelle: IR-Pressemitteilungnotiert 2026-11-17", zelle)
        # Die gewoehnliche Zeile traegt das Abzeichen NICHT.
        self.assertNotIn("Termin manuell kuratiert", " ".join(panel["cells"][1]))

    def test_a_curated_row_without_a_source_still_names_the_day(self):
        body = _list()
        body["results"][0]["next_earnings_date"] = "2027-02-01"
        body["results"][0]["next_earnings_source"] = "manual_curated"
        body["results"][0]["hand_calendar"] = {"status": "curated",
                                               "curated_date": "2027-02-01",
                                               "source": None, "notiert_am": None}
        panel = _render(list_body=body)
        zelle = [c for c in panel["cells"][0] if c.startswith("2027-02-01")][0]
        # Ohne getippte Quelle steht "manuell" - keine Luecke, keine leere Zeile.
        # Ohne Notizdatum entfaellt dessen Zeile statt leer dazustehen.
        self.assertEqual("2027-02-01Termin manuell kuratiertQuelle: manuell", zelle)

    def test_the_curated_lines_are_grey_and_stacked(self):
        # Die drei Zeilen sind klein und grau und stehen untereinander - das
        # CSS allein entscheidet das, also wird es geprueft und nicht geraten.
        css = (Path(__file__).resolve().parents[1] / "templates" / "screen.html").read_text(encoding="utf-8")
        # Keine eigene Groesse und keine eigene Farbe: die drei Zeilen erben die
        # Unterzeilen-Regel der Tabelle (.sub: 9.5px, gedaempft, display:block) -
        # so klein wie jede andere graue Info in einer Zelle.
        self.assertIn(".sub{display:block;font-size:9.5px;color:var(--faint)", css)
        self.assertNotIn("td.hc-curated .sub", css)


if __name__ == "__main__":
    unittest.main()
