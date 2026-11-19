#!/usr/bin/env python3
"""Compare the running dashboard's scores with visible TraderFox Apple scores."""

import argparse
import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
REFERENCE = "https://aktie.traderfox.com/visualizations/US0378331005/DI/apple"
CHECKS = (
    ("Qualität", "quality"),
    ("Dividenden", "dividend"),
    ("Wachstum", "growth"),
    ("Robustheit", "the_big_call"),
)
APPLE_2026 = {
    "revenue": ("Umsatz FY2026", 478_834_000_000, 256),
    "operatingIncome": ("Betriebsergebnis FY2026", 158_063_000_000, 256),
    "netIncome": ("Nettogewinn FY2026", 132_175_000_000, 256),
    "dilutedEps": ("Verwässertes EPS FY2026", 8.98, 0.00001),
}
APPLE_FILING = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/aapl-20260926.htm"


def download(url):
    request = Request(url, headers={"User-Agent": "Aktienanalyse-Pruefung/1.0"})
    with urlopen(request, timeout=35) as response:
        data = response.read(8_000_001)
    if len(data) > 8_000_000:
        raise ValueError("Antwort ist zu groß.")
    return data


def reference_scores(html):
    page = BeautifulSoup(html, "html.parser")
    if not any("US0378331005" in title.get_text(" ", strip=True) or "AAPL" in title.get_text(" ", strip=True)
               for title in page.select("title,h1")):
        raise ValueError("Die Referenzseite ist keine bestätigte Apple-Ansicht.")
    values = []
    for _, key in CHECKS:
        panel = page.find(id=f"tdf-{key}-check-pane")
        output = None
        if panel:
            candidates = set()
            for text in panel.stripped_strings:
                if match := re.fullmatch(r"(\d+)\s*/\s*15", text):
                    candidates.add(int(match[1]))
            if len(candidates) == 1 and 0 <= next(iter(candidates)) <= 15:
                output = f"{next(iter(candidates))}/15"
            elif key == "dividend":
                text = "".join(panel.stripped_strings).replace("\u00ad", "")
                if "ausgesetzt" in "".join(text.casefold().split()):
                    output = "Ausgesetzt"
        values.append(output)
    return values


def local_scores(port, with_yahoo):
    base = f"http://127.0.0.1:{port}"
    stock = json.loads(download(base + "/api/stock?q=AAPL"))
    if stock.get("symbol") != "AAPL" or stock.get("info", {}).get("currency") != "USD":
        raise ValueError("Lokale Kursreihe ist nicht eindeutig Apple in USD.")
    benchmark_symbol = "SPY" if stock.get("nas_primary") else "^GSPC"
    if stock.get("nas_primary"):
        cutoff = stock["prices"][-1][0]
        profile = json.loads(download(base + "/api/statements?" + urlencode({"q": "AAPL", "cutoff": cutoff})))
        stock.update(statements=profile["statements"], info={**stock["info"], **profile["info"]})
        if with_yahoo:
            query = urlencode({"q": "AAPL", "cutoff": cutoff, "currency": "USD", "history_only": "1"})
            extra = json.loads(download(base + "/api/stock-extra?" + query))
            stock.update(info={**stock["info"], **extra["info"]}, prices=extra["prices"] or stock["prices"])
    comparison = json.loads(download(base + "/api/compare?" + urlencode({"q": benchmark_symbol})))
    benchmark = next((series.get("prices") or [] for series in comparison.get("series", [])
                      if series.get("symbol") == benchmark_symbol), [])
    with TemporaryDirectory() as directory:
        html = Path(directory) / "index.html"
        inputs = Path(directory) / "input.json"
        html.write_bytes(download(base + "/"))
        inputs.write_text(json.dumps({"stock": stock, "benchmarkRows": benchmark}), encoding="utf-8")
        result = subprocess.run(["node", str(ROOT / "check_scores.cjs"), str(html), str(inputs)],
                                check=True, capture_output=True, text=True, timeout=20)
        return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--referenz-html", type=Path, help="Optional: gespeicherte öffentliche TraderFox-Apple-Seite")
    parser.add_argument("--mit-yahoo", action="store_true",
                        help="Im NAS-Modus die optionale Yahoo-Ergänzung für Apple mitprüfen.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Ungültiger Port.")
    source = str(args.referenz_html) if args.referenz_html else REFERENCE
    try:
        local = local_scores(args.port, args.mit_yahoo)
        target = reference_scores(args.referenz_html.read_bytes() if args.referenz_html else download(REFERENCE))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"PRÜFUNG BLOCKIERT: {error}")
        return 2

    print(f"AAPL in USD · Lokaler Kursstand {local['date']} · Referenz {source}")
    print("TraderFox-Referenz: USA 2000 (v), Dollar-Indikation. Abgleich der sichtbaren Ergebnisse.")
    failed = False
    for (name, _), expected, actual in zip(CHECKS, target, local["scores"], strict=True):
        value = re.sub(r"\s*/\s*", "/", actual["value"])
        exact = expected is not None and value == expected
        status = "ANZEIGE GLEICH" if exact else "REFERENZ FEHLT" if expected is None else "ABWEICHUNG"
        print(f"{status:15} {name:<12} lokal {value:<11} TraderFox {expected or '—':<11} "
              f"lokale Daten {actual['available']}/{actual['total']}")
        failed |= not exact
    print(f"Originalabschluss: {APPLE_FILING}")
    for key, (name, expected, tolerance) in APPLE_2026.items():
        actual = local["raw"].get(key)
        exact = actual is not None and abs(actual - expected) <= tolerance
        print(f"{'OK' if exact else 'ABWEICHUNG':15} {name}: lokal {actual}, Apple {expected}")
        failed |= not exact
    print("Gleiche Einzelwerte belegen keine Gleichheit der Berechnung, der Kursbasis oder des Marktuniversums.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
