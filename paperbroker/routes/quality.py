"""The stock analysis as a page **inside** this program — not a second service.

The analysis (folder ``analytics/``) is Python code like the rest: it is loaded
here and served under ``/qualitaet/``. No proxy, no second container, no image of
its own — one process, one port, one source tree.

It still starts on its own (``python3 analytics/app.py``, port 8766): the same
files, the same code, just a different entry point.

Layout:

* ``page()`` serves the interface (``analytics/index.html``) and injects the
  path prefix it runs under.
* ``api(name)`` maps the former ``/api/...`` paths of the block onto its
  functions. The arithmetic stays wherever it was delivered; only the dispatcher
  that ``app.py`` used to carry lives here.
* The NAS scanner (xang) is optional: ``AKTIENANALYSE_NAS_URL`` set means it is
  used, otherwise the analysis answers from Yahoo Finance and says so.
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
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError

from flask import Response, jsonify, request

# The folder holding the delivered code, next to this package.
AKT_DIR = Path(__file__).resolve().parents[2] / "analytics"
NAS_URL_ENV = "AKTIENANALYSE_NAS_URL"
PAGE_PATH = "/qualitaet"

# The page wears the same type as the funnel: Oswald for headings, Instrument Sans for text,
# Martian Mono for labels and numbers. The files already ship for the funnel, so they are served
# from there instead of being copied - one source, and no request leaves the LAN.
FONT_DIR = Path(__file__).resolve().parents[1] / "static" / "fonts"
FONT_NAME = re.compile(r"[a-z0-9-]+\.woff2")
#: Placeholder in index.html, replaced with the prefix the page runs under.
ASSET_BASE = "__ASSET_BASE__"

# The same pattern the delivered program uses (analytics/app.py): a narrower
# one here would reject tickers the standalone entry point happily serves.
SYMBOL = re.compile(r"[A-Z0-9.^=\-]{1,24}")

_lock = threading.Lock()
_module = None
_nas = None
_nas_tried = False


def _akt():
    """The delivered program as a module, loaded once.

    It is called ``app`` and would collide with anything else of that name, hence a
    module name of its own. Its folder goes onto the import path because it imports
    its neighbours by bare name (``from nas_scanner import ...``); that is the state
    it was delivered in, and nothing about that is changed.
    """
    global _module
    with _lock:
        if _module is None:
            if str(AKT_DIR) not in sys.path:
                sys.path.insert(0, str(AKT_DIR))
            spec = importlib.util.spec_from_file_location("analytics_app", AKT_DIR / "app.py")
            if spec is None or spec.loader is None:
                raise RuntimeError(f"analytics is not readable: {AKT_DIR}")
            module = importlib.util.module_from_spec(spec)
            sys.modules["analytics_app"] = module
            spec.loader.exec_module(module)
            _module = module
    return _module


def nas():
    """The NAS scanner, if an address is configured — connected once.

    A failed sign-in stops nothing here: the analysis then answers from Yahoo
    Finance, and the source line on the page says so.

    The address is claimed under the lock and the connection is made outside it:
    a network round trip behind a process-wide mutex would queue every request of
    the cold start behind the slowest thing in the process. Whoever loses the race
    answers from Yahoo Finance for that one request and finds the scanner in place
    on the next.
    """
    global _nas, _nas_tried
    with _lock:
        if _nas_tried:
            return _nas
        _nas_tried = True
    address = (os.environ.get(NAS_URL_ENV) or "").strip()
    if not address:
        return None
    if str(AKT_DIR) not in sys.path:
        sys.path.insert(0, str(AKT_DIR))
    try:
        from nas_scanner import NasScanner

        scanner = NasScanner(address)
        scanner.connect()
    except (URLError, OSError, ValueError, EOFError, HTTPException) as error:
        print(f"NAS source unavailable ({error}). Continuing without NAS.", flush=True)
        return None
    with _lock:
        _nas = scanner
    return scanner


def page() -> Response:
    """The interface, with the path prefix injected."""
    html = (AKT_DIR / "index.html").read_text(encoding="utf-8").replace(ASSET_BASE, PAGE_PATH)
    start = html.find("<html")
    end = html.find(">", start) if start >= 0 else -1
    if start >= 0 and end >= 0:
        injection = f'<script>window.AKT_BASE_PATH="{PAGE_PATH}"</script>'
        html = html[:end + 1] + injection + html[end + 1:]
    return Response(html, content_type="text/html; charset=utf-8")


def _error(message: str, status: int = 400):
    return jsonify(error=message), status


def _query() -> str:
    return (request.args.get("q") or "").strip()


def _symbols(maximum: int) -> list[str]:
    symbols = list(dict.fromkeys(_query().upper().split(",")))
    if len(symbols) > maximum or any(not SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise ValueError(f"Bitte bis zu {maximum} gültige Börsenticker eingeben.")
    return symbols


def _one() -> str:
    symbols = _symbols(5)
    if len(symbols) != 1:
        raise ValueError("Bitte genau einen Ticker auswählen.")
    return symbols[0]


def _bucket() -> int:
    return int(time.time() // 900)


def fonts_css() -> Response:
    """The @font-face rules; their relative urls land on the route below.

    A missing file answers 404 like a missing font does: the page then falls back to the
    system fonts, which is readable — a 500 on a stylesheet would not be.
    """
    rules = FONT_DIR.parent / "fonts.css"
    if not rules.is_file():
        return Response("", status=404)
    return Response(rules.read_text(encoding="utf-8"),
                    content_type="text/css; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=604800"})


def font(name: str) -> Response:
    """One self-hosted woff2 file, but only the ones that are actually there."""
    if not FONT_NAME.fullmatch(name) or not (FONT_DIR / name).is_file():
        return Response("", status=404)
    return Response((FONT_DIR / name).read_bytes(), content_type="font/woff2",
                    headers={"Cache-Control": "public, max-age=604800"})


def _with_quarters(akt, symbol: str, result: dict) -> dict:
    """The answer plus the twelve month growth from the second quarterly source.

    The delivered dispatcher (``analytics/app.py``) reads this where it answers ``/api/stock``; the
    embedded one calls the functions itself, so the step has to be repeated here or the page gets no
    ``ttm_growth`` at all and the two TTM rows and both financing rows stay empty. A new dict every
    time: ``stock()`` is cached, and writing into the cached object would hand that answer to every
    later caller.
    """
    return {**result,
            "ttm_growth": akt.long_quarterly_growth(symbol, result.get("statements") or {})}


def api(name: str):
    """One data endpoint of the block, under the same name as before.

    Every answer is a complete JSON body built before it is handed to Flask, so
    there is no streaming here and a failure can still become a clean status code.
    """
    try:
        akt = _akt()
        scanner = nas()
        bucket = _bucket()

        if name == "search":
            text = _query()
            if not 1 <= len(text) <= 100:
                raise ValueError("Bitte einen Namen, Ticker oder eine ISIN eingeben.")
            # The bucket keys the per-listing cache; without it every lookup lands under the same
            # key and a currency or volume from weeks ago is served for good.
            return jsonify(akt.search(text, scanner, bucket))

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
                    # A ticker can be in the scanner with fundamentals but without
                    # daily closes. The delivered dispatcher fills in the Yahoo
                    # series then; without that, the embedded answer would carry an
                    # empty price list where the standalone one carries a chart.
                    result = cached[0]
                    if not result["prices"]:
                        result.update(prices=akt.prices(symbol, bucket),
                                      price_source="Yahoo Finance",
                                      quote_source="Yahoo Finance",
                                      issues=["Keine NAS-Tageskurse für diesen Ticker."])
                    return jsonify(_with_quarters(akt, symbol, result))
                result = akt.stock(symbol, bucket)
                return jsonify(_with_quarters(akt, symbol, {
                    **result,
                    "issues": ["Keine NAS-Tageskurse für diesen Ticker.",
                               *result["issues"]]}))
            return jsonify(_with_quarters(akt, symbol, akt.stock(symbol, bucket)))

        if name == "stock-batch":
            symbols = _symbols(15)
            if scanner is not None:
                results = akt.nas_stocks(scanner, symbols, bucket=bucket)
                if results and (request.args.get("statements") or "") == "1":
                    statements_bucket = int(time.time() // 600)

                    def enrich(item):
                        try:
                            return akt.with_statements(item, statements_bucket)
                        except (URLError, OSError, ValueError):
                            return item

                    with ThreadPoolExecutor(max_workers=min(3, len(results))) as pool:
                        results = list(pool.map(enrich, results))
                return jsonify(stocks=results)

            def fetch(symbol):
                try:
                    return akt.stock(symbol, bucket)
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=min(3, len(symbols))) as pool:
                return jsonify(stocks=[item for item in pool.map(fetch, symbols) if item])

        if name == "compare":
            symbols = _symbols(5)
            series = scanner.histories(symbols) if scanner is not None else None
            rows = ({symbol: akt.nas_rows(series.get(symbol)) for symbol in symbols}
                    if series is not None else {})
            jumpy = [symbol for symbol, values in rows.items() if akt.price_jumps(values)]
            fundamentals = {}
            if jumpy and scanner is not None:
                try:
                    fundamentals = scanner.fundamentals(jumpy)
                except (URLError, OSError, ValueError):
                    pass

            def price_series(symbol):
                try:
                    if series is not None:
                        values = rows[symbol]
                        if not values:
                            return {"symbol": symbol, "error": "Kursreihe fehlt im NAS-Bestand."}
                        raw = fundamentals.get(symbol)
                        info = akt.nas_info(raw, {}) if isinstance(raw, dict) else {}
                        return {"symbol": symbol,
                                **akt.nas_price_series(symbol, values, info.get("currency"),
                                                       bucket)}
                    return {"symbol": symbol, "prices": akt.prices(symbol, bucket),
                            "price_source": "Yahoo Finance"}
                except Exception:
                    return {"symbol": symbol, "error": "Kursreihe fehlt."}

            with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
                # "series" is the name the page expects.
                return jsonify({"series": list(pool.map(price_series, symbols))})

        return _error("Datenweg unbekannt.", 404)
    except ValueError as error:
        return _error(str(error))
    except HTTPError as error:
        message = ("Die NAS-Sitzung ist abgelaufen. Bitte die Aktienanalyse neu starten "
                   "und erneut anmelden." if error.code in (401, 403)
                   else "Der NAS-Scanner konnte die Anfrage nicht beantworten.")
        error.close()
        return _error(message, 502)
    except (URLError, OSError):
        return _error("Die Datenquelle ist nicht erreichbar oder liefert für diesen "
                      "Ticker derzeit keine Daten.", 502)
    except Exception:
        # The delivered dispatcher caught everything and answered 502 JSON; the page
        # reads json.error, so an escaping exception must not become a Flask 500 page.
        return _error("Die Datenquelle ist nicht erreichbar oder liefert für diesen "
                      "Ticker derzeit keine Daten.", 502)


def register(app) -> None:
    app.add_url_rule(PAGE_PATH + "/", endpoint="quality_page", view_func=page,
                     methods=["GET"], strict_slashes=False)
    app.add_url_rule(PAGE_PATH + "/api/<name>", endpoint="quality_api", view_func=api,
                     methods=["GET"])
    app.add_url_rule(PAGE_PATH + "/fonts.css", endpoint="quality_fonts_css", view_func=fonts_css,
                     methods=["GET"])
    app.add_url_rule(PAGE_PATH + "/fonts/<name>", endpoint="quality_font", view_func=font,
                     methods=["GET"])
