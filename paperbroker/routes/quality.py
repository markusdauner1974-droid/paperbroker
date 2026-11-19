"""Die Aktienanalyse als Seite **in** diesem Programm — kein zweiter Dienst.

Die Analyse (Ordner ``aktienanalyse/``) ist Python-Code wie der uebrige: hier
wird sie geladen und unter ``/qualitaet/`` ausgeliefert. Kein Proxy, kein zweiter
Container, kein Abbild — ein Prozess, ein Port, ein Quelltext.

Der Baustein bleibt trotzdem eigenstaendig startbar (``python3
aktienanalyse/app.py``, Port 8766): dieselben Dateien, derselbe Code, nur ein
anderer Einstiegspunkt.

Aufbau:

* ``page()`` liefert die Oberflaeche (``aktienanalyse/index.html``) und spielt den
  Pfadanfang ein, unter dem sie laeuft.
* ``api(name)`` bildet die frueheren ``/api/...``-Pfade des Bausteins auf seine
  Funktionen ab. Die Rechenwege bleiben dort, wo sie sind; hier steht nur der
  Verteiler, den vorher ``app.py`` hatte.
* Der NAS-Scanner (xang) ist optional: ``AKTIENANALYSE_NAS_URL`` gesetzt = er wird
  benutzt, sonst antwortet die Analyse aus Yahoo Finance und sagt es auch.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError

from flask import Response, jsonify, request

# Der Ordner mit dem gelieferten Code, neben diesem Paket.
AKT_DIR = Path(__file__).resolve().parents[2] / "aktienanalyse"
NAS_URL_ENV = "AKTIENANALYSE_NAS_URL"
PAGE_PATH = "/qualitaet"

SYMBOL = re.compile(r"[A-Z0-9^][A-Z0-9.\-=^]{0,14}")

_lock = threading.Lock()
_module = None
_nas = None
_nas_tried = False


def _akt():
    """Das gelieferte Programm als Modul, einmal geladen.

    Es heisst ``app`` und wuerde mit allem kollidieren, was ebenfalls so heisst -
    deshalb ein eigener Modulname. Sein Ordner kommt in den Suchpfad, weil es
    seine Nachbarn mit blossen Namen importiert (``from nas_scanner import ...``);
    das ist der Zustand, in dem es geliefert wurde, und daran wird nichts gedreht.
    """
    global _module
    with _lock:
        if _module is None:
            if str(AKT_DIR) not in sys.path:
                sys.path.insert(0, str(AKT_DIR))
            spec = importlib.util.spec_from_file_location("aktienanalyse_app", AKT_DIR / "app.py")
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Aktienanalyse nicht lesbar: {AKT_DIR}")
            modul = importlib.util.module_from_spec(spec)
            sys.modules["aktienanalyse_app"] = modul
            spec.loader.exec_module(modul)
            _module = modul
    return _module


def nas():
    """Der NAS-Scanner, wenn eine Adresse gesetzt ist — einmal verbunden.

    Eine gescheiterte Anmeldung beendet hier nichts: die Analyse antwortet dann
    aus Yahoo Finance, und die Quellenzeile auf der Seite sagt es.
    """
    global _nas, _nas_tried
    with _lock:
        if not _nas_tried:
            _nas_tried = True
            adresse = (os.environ.get(NAS_URL_ENV) or "").strip()
            if adresse:
                if str(AKT_DIR) not in sys.path:
                    sys.path.insert(0, str(AKT_DIR))
                try:
                    from nas_scanner import NasScanner

                    scanner = NasScanner(adresse)
                    scanner.connect()
                    _nas = scanner
                except (URLError, OSError, ValueError, EOFError) as fehler:
                    print(f"NAS-Quelle nicht verfügbar ({fehler}). Es geht ohne NAS weiter.",
                          flush=True)
        return _nas


def page() -> Response:
    """Die Oberflaeche, mit eingespieltem Pfadanfang."""
    html = (AKT_DIR / "index.html").read_text(encoding="utf-8")
    start = html.find("<html")
    ende = html.find(">", start) if start >= 0 else -1
    if start >= 0 and ende >= 0:
        einspiel = f'<script>window.AKT_BASE_PATH="{PAGE_PATH}"</script>'
        html = html[:ende + 1] + einspiel + html[ende + 1:]
    return Response(html, content_type="text/html; charset=utf-8")


def _error(message: str, status: int = 400):
    return jsonify(error=message), status


def _query() -> str:
    return (request.args.get("q") or "").strip()


def _symbols(maximum: int) -> list[str]:
    symbole = list(dict.fromkeys(_query().upper().split(",")))
    if len(symbole) > maximum or any(not SYMBOL.fullmatch(s) for s in symbole):
        raise ValueError(f"Bitte bis zu {maximum} gültige Börsenticker eingeben.")
    return symbole


def _one() -> str:
    symbole = _symbols(5)
    if len(symbole) != 1:
        raise ValueError("Bitte genau einen Ticker auswählen.")
    return symbole[0]


def _bucket() -> int:
    return int(time.time() // 900)


def api(name: str):
    """Ein Datenendpunkt des Bausteins, unter demselben Namen wie bisher."""
    akt = _akt()
    scanner = nas()
    bucket = _bucket()
    try:
        if name == "search":
            text = _query()
            if not 1 <= len(text) <= 100:
                raise ValueError("Bitte einen Namen, Ticker oder eine ISIN eingeben.")
            return jsonify(akt.search(text, scanner))

        if name == "xang":
            if not SYMBOL.fullmatch(_query().upper()):
                raise ValueError("Bitte einen gültigen Börsenticker eingeben.")
            if scanner is None:
                return jsonify(status="disabled")
            try:
                return jsonify(scanner.snapshot(_query().upper()))
            except (URLError, ValueError, OSError):
                return _error("NAS-Snapshot momentan nicht abrufbar.", 502)

        if name == "statements":
            symbol = _one()
            cutoff = date.fromisoformat((request.args.get("cutoff") or "").strip()).isoformat()
            return jsonify(akt.statement_profile(symbol, cutoff, int(time.time() // 600)))

        if name == "stock-extra":
            symbol = _one()
            if scanner is None:
                raise ValueError("Die Ergänzung setzt ein NAS-Kursprofil voraus.")
            cutoff = date.fromisoformat((request.args.get("cutoff") or "").strip()).isoformat()
            currency = (request.args.get("currency") or "").strip()
            if currency and not re.fullmatch(r"[A-Z]{3}|GBp|ZAc", currency):
                raise ValueError("Die Notierungswährung ist ungültig.")
            history_only = (request.args.get("history_only") or "") == "1"
            return jsonify(akt.stock_extra(symbol, bucket, cutoff, currency, history_only))

        if name == "stock":
            symbol = _one()
            if scanner is not None:
                cached = akt.nas_stocks(scanner, [symbol], identify=True, bucket=bucket)
                if cached:
                    return jsonify(cached[0])
                ergebnis = akt.stock(symbol, bucket)
                return jsonify({**ergebnis,
                                "issues": ["Keine NAS-Tageskurse für diesen Ticker.",
                                           *ergebnis["issues"]]})
            return jsonify(akt.stock(symbol, bucket))

        if name == "stock-batch":
            symbole = _symbols(15)
            if scanner is not None:
                ergebnisse = akt.nas_stocks(scanner, symbole, bucket=bucket)
                if ergebnisse and (request.args.get("statements") or "") == "1":
                    bucket_statements = int(time.time() // 600)

                    def ergaenze(item):
                        try:
                            return akt.with_statements(item, bucket_statements)
                        except (URLError, OSError, ValueError):
                            return item

                    with ThreadPoolExecutor(max_workers=min(3, len(ergebnisse))) as pool:
                        ergebnisse = list(pool.map(ergaenze, ergebnisse))
                return jsonify(stocks=ergebnisse)

            def hole(symbol):
                try:
                    return akt.stock(symbol, bucket)
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=min(3, len(symbole))) as pool:
                return jsonify(stocks=[item for item in pool.map(hole, symbole) if item])

        if name == "compare":
            symbole = _symbols(5)
            verläufe = scanner.histories(symbole) if scanner is not None else None
            reihen = ({symbol: akt.nas_rows(verläufe.get(symbol)) for symbol in symbole}
                      if verläufe is not None else {})
            auffaellig = [symbol for symbol, rows in reihen.items() if akt.price_jumps(rows)]
            kennzahlen = {}
            if auffaellig and scanner is not None:
                try:
                    kennzahlen = scanner.fundamentals(auffaellig)
                except (URLError, OSError, ValueError):
                    pass

            def kursreihe(symbol):
                try:
                    if verläufe is not None:
                        rows = reihen[symbol]
                        if not rows:
                            return {"symbol": symbol, "error": "Kursreihe fehlt im NAS-Bestand."}
                        raw = kennzahlen.get(symbol)
                        info = akt.nas_info(raw, {}) if isinstance(raw, dict) else {}
                        return {"symbol": symbol,
                                **akt.nas_price_series(symbol, rows, info.get("currency"),
                                                       bucket)}
                    return {"symbol": symbol, "prices": akt.prices(symbol, bucket),
                            "price_source": "Yahoo Finance"}
                except Exception:
                    return {"symbol": symbol, "error": "Kursreihe fehlt."}

            with ThreadPoolExecutor(max_workers=len(symbole)) as pool:
                # ``series`` ist der Name, den die Seite erwartet.
                return jsonify({"series": list(pool.map(kursreihe, symbole))})

        return _error("Datenweg unbekannt.", 404)
    except ValueError as fehler:
        return _error(str(fehler))
    except HTTPError as fehler:
        nachricht = ("Die NAS-Sitzung ist abgelaufen. Bitte die Aktienanalyse neu starten "
                     "und erneut anmelden." if fehler.code in (401, 403)
                     else "Der NAS-Scanner konnte die Anfrage nicht beantworten.")
        fehler.close()
        return _error(nachricht, 502)
    except (URLError, OSError):
        return _error("Die Datenquelle ist nicht erreichbar oder liefert für diesen "
                      "Ticker derzeit keine Daten.", 502)


def register(app) -> None:
    app.add_url_rule(PAGE_PATH + "/", endpoint="quality_page", view_func=page,
                     methods=["GET"], strict_slashes=False)
    app.add_url_rule(PAGE_PATH + "/api/<name>", endpoint="quality_api", view_func=api,
                     methods=["GET"])
