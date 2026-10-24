#!/usr/bin/env python3
"""Turn the pytest JUnit report into a result a reviewer can SEE.

Two outputs, both without any third-party action:

* a Markdown summary on the workflow run (``$GITHUB_STEP_SUMMARY``), which the
  pull request shows under "Checks -> Regression pytest (uv)",
* one ``::error`` annotation per failing test, so a red run names file, line and
  message in the PR instead of hiding them in a log.

Missing report = the step that produced it failed; this script then says so
instead of inventing zeros.
"""

import os
import sys
import xml.etree.ElementTree as ET


def _text(node):
    return " ".join((node.text or "").split())


def main(path):
    zeilen = []
    try:
        baum = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        zeilen.append("## Regression pytest (uv)\n")
        zeilen.append(f"**Kein Testergebnis lesbar:** `{path}` ({type(exc).__name__}). "
                      "Der Testschritt ist vor der Berichtserzeugung gescheitert.\n")
        _schreiben(zeilen)
        return 0

    suite = baum.getroot()
    if suite.tag != "testsuite":
        suite = suite.find("testsuite")
    if suite is None:
        _schreiben(["## Regression pytest (uv)\n", "Kein `<testsuite>` im Bericht gefunden.\n"])
        return 0

    gesamt = int(suite.get("tests", 0))
    fehler = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    uebersprungen = int(suite.get("skipped", 0))
    bestanden = gesamt - fehler - uebersprungen
    dauer = float(suite.get("time", 0) or 0)

    zeilen.append("## Regression pytest (uv)\n")
    zeilen.append("| Kennzahl | Wert |")
    zeilen.append("|---|---|")
    zeilen.append(f"| Tests | **{gesamt}** |")
    zeilen.append(f"| bestanden | **{bestanden}** |")
    zeilen.append(f"| fehlgeschlagen | **{fehler}** |")
    zeilen.append(f"| übersprungen | {uebersprungen} |")
    zeilen.append(f"| Dauer | {dauer:.1f} s |")
    zeilen.append(f"| Python | {os.environ.get('PYTHON_VERSION_REPORT', '3.13.5')} |")
    sha = os.environ.get("GITHUB_SHA") or "lokal"
    zeilen.append(f"| Commit | `{sha[:7]}` |")
    zeilen.append("")

    kaputt = []
    for fall in suite.iter("testcase"):
        problem = fall.find("failure")
        if problem is None:
            problem = fall.find("error")
        if problem is None:
            continue
        name = fall.get("name", "?")
        klasse = (fall.get("classname") or "").split(".")[-1]
        datei = problem.get("file") or fall.get("file") or ""
        zeile = problem.get("line") or fall.get("line") or "1"
        meldung = _text(problem).split("\n")[0][:400] or "fehlgeschlagen"
        kaputt.append((klasse, name, datei, zeile, meldung))
        pfad = f"file={datei}," if datei else ""
        print(f"::error {pfad}line={zeile},title={klasse}::{name}: {meldung}")

    if kaputt:
        zeilen.append("### Fehlgeschlagene Tests\n")
        zeilen.append("| Test | Datei | Meldung |")
        zeilen.append("|---|---|---|")
        for klasse, name, datei, zeile, meldung in kaputt:
            ort = f"`{datei}:{zeile}`" if datei else "-"
            zeilen.append(f"| `{klasse}::{name}` | {ort} | {meldung.replace('|', '/')} |")
        zeilen.append("")
    else:
        zeilen.append("Alle Tests bestanden.\n")

    _schreiben(zeilen)
    return 0


def _schreiben(zeilen):
    text = "\n".join(zeilen) + "\n"
    ziel = os.environ.get("GITHUB_STEP_SUMMARY")
    if ziel:
        with open(ziel, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "artifacts/junit.xml"))
