"""(f2) Die Sprach-Merkung: Anweisung im Kopf, Merkung im Browser.

Der Server bleibt zustandslos. Geprueft wird hier die Anweisung, die im
``<head>`` steht - nicht eine Nachbildung: der Test liest die Zeichenkette aus
der GERENDERTEN Seite und laesst sie unter node gegen einen winzigen Browser
laufen (``document``, ``window.location``, ``window.localStorage``). Browser-
Verhalten selbst beweist das nicht; es beweist die Entscheidung der Anweisung -
Weiterspielen, Merken, Verwerfen.

Dazu die Naht: das JSON-Datenblatt kommt aus der Registry (aufgeloestes Kuerzel
plus erlaubte Kuerzel), steht VOR der Anweisung, und die Anweisung liest nur
daraus - kein Kuerzel ist in den Anweisungstext hineingeschrieben.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from paperbroker import lang_memo, texts
from paperbroker.tests.test_gap_spreads_detail_endpoint import _client

NODE = shutil.which("node")

_HARNESS = r"""
'use strict';
// Runs ONE inline script from the page against a tiny browser. Usage:
//   node harness.js <config.json> <script.js>
// config: {boot: "<json text>", search: "?lang=en", href: "...", stored: {}}
const fs = require('fs');
const vm = require('vm');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const code = fs.readFileSync(process.argv[3], 'utf8');

const store = new Map(Object.entries(cfg.stored || {}));
let replaced = null;
const location = {
  search: cfg.search,
  href: cfg.href,
  replace: (ziel) => { replaced = ziel; },
};
const sandbox = {
  JSON, URL, URLSearchParams, console,
  document: { getElementById: (id) => ({ textContent: id === cfg.bootId ? cfg.boot : '' }) },
  window: {
    location,
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => { store.set(k, String(v)); },
      removeItem: (k) => { store.delete(k); },
    },
  },
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
process.stdout.write(JSON.stringify({
  replaced,
  store: Object.fromEntries(store),
}));
"""


def _seite(pfad: str = "/") -> str:
    antwort = _client().get(pfad)
    return antwort.get_data(as_text=True)


def _kopf_stuecke(seite: str):
    """Das JSON-Blatt und die Anweisung aus dem gerenderten ``<head>``."""
    kopf = seite.split("</head>")[0]
    blatt = re.search(r'<script type="application/json" id="' + lang_memo.BOOT_ID + r'">(.*?)</script>',
                      kopf, re.S)
    anweisung = re.search(r'<script id="' + lang_memo.SCRIPT_ID + r'">(.*?)</script>', kopf, re.S)
    return (None if blatt is None else blatt.group(1),
            None if anweisung is None else anweisung.group(1))


class TheHeadCarriesTheDataStationTest(unittest.TestCase):
    """Die Naht zur Registry: server-gerendert, vor der Anweisung, JSON-sicher."""

    def test_the_data_station_comes_from_the_registry(self):
        blatt, anweisung = _kopf_stuecke(_seite())
        self.assertIsNotNone(blatt, "das Datenblatt steht nicht im Kopf")
        self.assertIsNotNone(anweisung, "die Merk-Anweisung steht nicht im Kopf")
        daten = json.loads(blatt)
        self.assertEqual({"resolved", "allowed"}, set(daten))
        self.assertEqual(texts.active_language(), daten["resolved"])
        self.assertEqual(list(texts.languages()), daten["allowed"])

    def test_the_data_station_stands_before_the_instruction(self):
        kopf = _seite().split("</head>")[0]
        blatt = kopf.index('id="' + lang_memo.BOOT_ID + '"')
        anweisung = kopf.index('id="' + lang_memo.SCRIPT_ID + '"')
        self.assertLess(blatt, anweisung, "die Anweisung liest, was noch nicht da ist")

    def test_no_language_code_is_written_into_the_instruction(self):
        """Kein Kuerzel im Anweisungstext - er liest ausschliesslich das Blatt."""
        _blatt, anweisung = _kopf_stuecke(_seite())
        for code in texts.languages():
            with self.subTest(code=code):
                self.assertNotIn(f"'{code}'", anweisung)
                self.assertNotIn(f'"{code}"', anweisung)
        self.assertNotIn("|safe", anweisung)

    def test_the_instruction_is_inline_and_appears_once(self):
        seite = _seite()
        self.assertEqual(1, seite.count(f'id="{lang_memo.SCRIPT_ID}"'))
        self.assertEqual(1, seite.count(f'id="{lang_memo.BOOT_ID}"'))
        kopf = seite.split("</head>")[0]
        self.assertIn(f'<script id="{lang_memo.SCRIPT_ID}">', kopf)
        self.assertNotIn(f'<script id="{lang_memo.SCRIPT_ID}" src=', seite)

    def test_the_language_follows_the_request_without_redirecting(self):
        """Bei ``?lang=en`` nennt das Blatt en - die Merkung spielt nicht dazwischen."""
        blatt, _anweisung = _kopf_stuecke(_seite("/?lang=en"))
        self.assertEqual("en", json.loads(blatt)["resolved"])


@unittest.skipIf(NODE is None, "node fehlt")
class TheInstructionDecidesTest(unittest.TestCase):
    """Die sechs Entscheidungen der Anweisung, gegen einen winzigen Browser."""

    @classmethod
    def setUpClass(cls):
        seite = _seite()
        blatt, anweisung = _kopf_stuecke(seite)
        assert blatt is not None and anweisung is not None, "Kopf-Stuecke fehlen"
        cls.boot = blatt
        cls.anweisung = anweisung
        cls.tmp = Path(tempfile.mkdtemp(prefix="langmemo-"))
        (cls.tmp / "harness.js").write_text(_HARNESS, encoding="utf-8")
        (cls.tmp / "script.js").write_text(anweisung, encoding="utf-8")

    def lauf(self, search: str, stored: dict | None = None,
             href: str = "http://localhost/") -> dict:
        cfg = {"boot": self.boot, "bootId": lang_memo.BOOT_ID, "search": search,
               "href": href, "stored": stored or {}}
        (self.tmp / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
        ergebnis = subprocess.run([NODE, str(self.tmp / "harness.js"),
                                   str(self.tmp / "cfg.json"), str(self.tmp / "script.js")],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(0, ergebnis.returncode, ergebnis.stderr)
        return json.loads(ergebnis.stdout)

    def test_a_bare_call_with_a_remembered_other_language_redirects(self):
        aus = self.lauf("", {"paperbroker.lang": "en"})
        self.assertIsNotNone(aus["replaced"], "die Merkung wurde nicht angewandt")
        self.assertTrue(aus["replaced"].endswith("/?lang=en"), aus["replaced"])

    def test_a_bare_call_with_the_resolved_language_does_not_redirect(self):
        aus = self.lauf("", {"paperbroker.lang": "de"})
        self.assertIsNone(aus["replaced"])
        self.assertEqual({"paperbroker.lang": "de"}, aus["store"])

    def test_an_explicit_language_is_remembered_and_not_redirected(self):
        aus = self.lauf("?lang=en")
        self.assertIsNone(aus["replaced"])
        self.assertEqual({"paperbroker.lang": "en"}, aus["store"])

    def test_an_explicit_back_switch_replaces_the_remembered_language(self):
        aus = self.lauf("?lang=de", {"paperbroker.lang": "en"})
        self.assertIsNone(aus["replaced"])
        self.assertEqual({"paperbroker.lang": "de"}, aus["store"])

    def test_an_invalid_remembered_language_is_dropped(self):
        aus = self.lauf("", {"paperbroker.lang": "fr"})
        self.assertIsNone(aus["replaced"], "eine ungueltige Merkung darf nicht spielen")
        self.assertEqual({}, aus["store"], "die ungueltige Merkung bleibt liegen")

    def test_the_other_parameters_survive_the_redirect(self):
        aus = self.lauf("", {"paperbroker.lang": "en"}, href="http://localhost/?ticker=X&day=Y")
        self.assertIsNotNone(aus["replaced"])
        self.assertTrue(aus["replaced"].endswith("/?ticker=X&day=Y&lang=en"), aus["replaced"])
