import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from time import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import ranking
import yfinance as yf
from filing_statements import filings_available, merge_filings
from sec_statements import load_sec_statements

ROOT = Path(__file__).resolve().parent
SYMBOL = re.compile(r"[A-Z0-9.^=\-]{1,24}")
# Die Fassade traegt dieselbe Schrift wie der Trichter. Die Dateien liegen beim Programm,
# nicht doppelt hier: ein Ursprung, und keine Anfrage verlaesst das LAN.
FONT_DIR = ROOT.parent / "paperbroker" / "static" / "fonts"
FONT_CSS = FONT_DIR.parent / "fonts.css"
FONT_NAME = re.compile(r"[a-z0-9-]+\.woff2")
#: Platzhalter in index.html; hier ohne Praefix, weil die Seite an der Wurzel laeuft.
ASSET_BASE = "__ASSET_BASE__"
GROUPS = {
    "income": "income_stmt", "balance": "balance_sheet", "cash": "cash_flow",
    "quarterly": "quarterly_income_stmt",
}
SAP = {"symbol": "SAP.DE", "name": "SAP SE", "exchange": "Xetra"}
# A ranking downloads up to two hundred series from the provider. One run at a time, and a
# wall-clock budget: a second click must not multiply the outbound requests of the first.
# Measured: two hundred titles over eight workers take a little more than three minutes on a cold
# cache, so a budget of 180 seconds dropped the tail of a full run and reported it as skipped.
RANKING_BUDGET_S = 300
RANKING_BUCKET_S = 900
_RANKING_LOCK = Lock()
_RANKING_ACTIVE = 0


def enter_ranking():
    """Claim the single ranking slot, or say that one is already running."""
    global _RANKING_ACTIVE
    with _RANKING_LOCK:
        if _RANKING_ACTIVE:
            raise ValueError("Es läuft bereits eine Rangliste; bitte warten.")
        _RANKING_ACTIVE += 1


def leave_ranking():
    """Give the slot back, also when the run failed."""
    global _RANKING_ACTIVE
    with _RANKING_LOCK:
        _RANKING_ACTIVE = max(0, _RANKING_ACTIVE - 1)


def checked_symbols(symbols):
    """Every ticker is checked before it becomes part of an outbound request."""
    if any(not SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise ValueError("Bitte gültige Börsenticker angeben.")
    return symbols


def capped_symbols(symbols):
    """A universe file may name more titles than one run may fetch; the first ones count.

    The files arrive in TraderFox order, so the cut keeps the most traded titles. The number of
    entries is returned as well, because the page has to say that it ranked a part.
    """
    return symbols[:ranking.MAX_SYMBOLS], len(symbols)


def ranking_bucket():
    """One bucket per quarter hour, like the page's own view.

    A fixed bucket would score a small universe from the series cached at the first run, and the
    list would rank on prices that never age.
    """
    return f"ranking-{int(time() // RANKING_BUCKET_S)}"


def ranking_series(index_symbol):
    """The symbol fetch and the index series of one ranking run, from the same bucket."""
    bucket = ranking_bucket()
    return budgeted(lambda symbol: prices(symbol, bucket)), prices(index_symbol, bucket)


def budgeted(fetch, seconds=RANKING_BUDGET_S):
    """Wrap a fetch so the ranking stops asking once its time is spent."""
    deadline = time() + seconds

    def within_budget(symbol):
        if time() > deadline:
            raise RuntimeError("Zeitbudget der Rangliste erschöpft")
        return fetch(symbol)

    return within_budget


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def history_rows(frame):
    rows = []
    for tag, row in frame.iterrows():
        close = finite(row.get("Close"))
        if close is not None and close > 0:
            rows.append([tag.strftime("%Y-%m-%d"), close, finite(row.get("Adj Close")),
                         finite(row.get("Dividends"))])
    return rows


def statement_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def statement_rows(frame):
    result = {}
    for tag in sorted(frame.columns):
        values = {statement_key(key): finite(value) for key, value in frame[tag].items()}
        present = {key: value for key, value in values.items() if value is not None}
        if present:
            result[tag.strftime("%Y-%m-%d")] = present
    return result


def load_history(ticker, period):
    result = ticker.history(period=period, auto_adjust=False, actions=True, timeout=10, raise_errors=True)
    rows = history_rows(result)
    if not rows:
        raise ValueError("Für dieses Symbol wurden keine Kurse geliefert.")
    return rows


_HISTORY_GUARD = Lock()
_HISTORY = {}
HISTORY_PLACES = 96


class Flight:
    """One lock per (symbol, bucket) and the number of callers waiting for it.

    The map entry may only disappear when the last waiter has left: popping it earlier lets a new
    arrival create a second lock and download the same series beside the waiters.
    """

    __slots__ = ("lock", "waiting")

    def __init__(self):
        self.lock = Lock()
        self.waiting = 0


_HISTORY_LOCKS = {}


def prices(symbol, bucket):
    """The whole price series of one ticker, computed once per bucket.

    lru_cache alone does not coalesce concurrent cold calls: eight requests for the
    same ticker would each start their own download. A lock per ticker means the
    first one fetches and the rest wait for its result.
    """
    key = (symbol, bucket)
    with _HISTORY_GUARD:
        if key in _HISTORY:
            return _HISTORY[key]
        flight = _HISTORY_LOCKS.setdefault(key, Flight())
        flight.waiting += 1
    try:
        with flight.lock:
            with _HISTORY_GUARD:
                if key in _HISTORY:
                    return _HISTORY[key]
            rows = load_history(yf.Ticker(symbol), "max")
            with _HISTORY_GUARD:
                if len(_HISTORY) >= HISTORY_PLACES:
                    _HISTORY.pop(next(iter(_HISTORY)))
                _HISTORY[key] = rows
            return rows
    finally:
        # Counted, not popped blindly: a failed download must not leave the entry behind, and it
        # must not pull the lock away from callers that are still waiting on it either.
        with _HISTORY_GUARD:
            flight.waiting -= 1
            if flight.waiting <= 0:
                _HISTORY_LOCKS.pop(key, None)


def load_statements(ticker):
    statements = {}
    for name, prop in GROUPS.items():
        try:
            statements[name] = statement_rows(getattr(ticker, prop))
        except Exception:
            statements[name] = {}
    return statements


@lru_cache(maxsize=48)
def stock(symbol, bucket):
    ticker = yf.Ticker(symbol)
    rows = prices(symbol, bucket)
    info = {}
    issues = []
    try:
        raw = ticker.get_info()
        text_keys = ("shortName", "longName", "currency", "financialCurrency", "sector", "isin",
                     "industry", "exchange", "fullExchangeName", "quoteType")
        number_keys = ("marketCap", "trailingPE", "forwardPE", "priceToSalesTrailing12Months",
                       "revenueGrowth", "earningsQuarterlyGrowth", "grossMargins",
                       "profitMargins", "returnOnEquity", "totalDebt", "totalRevenue",
                       "operatingMargins", "targetMeanPrice", "recommendationMean",
                       "numberOfAnalystOpinions", "trailingEps", "dividendRate")
        info = {key: str(raw[key]) for key in text_keys if raw.get(key)}
        info.update({key: finite(raw.get(key)) for key in number_keys})
    except Exception:
        issues.append("Unternehmensprofil und aktuelle Fundamentalkennzahlen fehlen.")
    if not info.get("currency"):
        try:
            info["currency"] = ticker.history_metadata.get("currency")
            info["exchange"] = ticker.history_metadata.get("exchangeName")
        except Exception:
            pass
    statements = load_statements(ticker)
    # Yahoo hands over four to five fiscal years and the oldest column often arrives empty.
    # Five-year growth needs six years, so the SEC series fills in - but only when the provider
    # stays short: the companyfacts payload can pass 30 MB, and a page view should not wait for
    # it when nothing is missing.
    sec_source = None
    if rows and "." not in symbol and fiscal_years(statements["income"]) < SEC_YEARS:
        try:
            # Epoch seconds, not the freshness bucket: load_sec_statements derives its daily
            # cache keys as bucket // 86400, and a 900-second index lands near 23 forever.
            sec = load_sec_statements(symbol, date.fromisoformat(rows[-1][0]), bucket * 900)
        except (URLError, OSError, ValueError, TypeError, KeyError):
            sec = {}
        if sec.get("income") and sec.get("balance"):
            yahoo = statements
            # Copies, not references: load_sec_statements is cached, and setdefault on its rows
            # would write the Yahoo keys into that cache for every later request.
            statements = {name: {day: dict(row) for day, row in (sec.get(name) or {}).items()}
                          or yahoo.get(name, {})
                          for name in ("income", "balance", "cash", "quarterly")}
            # SEC lists no total-debt tag; the balance value of the same date from Yahoo fills
            # the gap. Without it "debt / total assets" would drop out of the check, although it
            # was the one figure that already matched the reference.
            for day, row in statements["balance"].items():
                for key, value in (yahoo.get("balance", {}).get(day) or {}).items():
                    if finite(value) is not None:
                        row.setdefault(key, value)
            for day, row in statements["income"].items():
                for key, value in (yahoo.get("income", {}).get(day) or {}).items():
                    if finite(value) is not None:
                        row.setdefault(key, value)
            sec_source = "SEC EDGAR"
    statements, report_currency, official = merge_filings(
        symbol, rows[-1][0], statements, info.get("financialCurrency"), info.get("isin"))
    if report_currency:
        info["financialCurrency"] = report_currency
    if not any("totalrevenue" in row for row in statements["income"].values()):
        issues.append("Jahresabschlüsse fehlen.")
    # What the page can honestly say about its own data: source, span, and why a span is short.
    # Filling a gap silently would be worse than naming it - a score built on four years is a
    # different score, and the reader has to see that.
    gaps = statement_gaps(statements, sec_source, official, filings_available())
    news = []
    try:
        for entry in ticker.get_news(count=8):
            item = entry.get("content", entry)
            url = (item.get("canonicalUrl") or {}).get("url") or item.get("link", "")
            title = item.get("title")
            if title and urlsplit(url).scheme == "https":
                news.append({"title": title, "url": url,
                             "date": item.get("pubDate", "")[:10]})
    except Exception:
        pass
    return {"symbol": symbol, "prices": rows, "info": info,
            "statements": statements, "issues": issues, "news": news, "data_gaps": gaps,
            "price_source": "Yahoo Finance", "quote_source": "Yahoo Finance",
            "fundamentals_source": "Yahoo Finance" if info else None,
            "statements_source": statement_source(statements, official, sec_source)}


# Five growth steps need six fiscal years; below that the SEC series is worth a download.
SEC_YEARS = 6


def fiscal_years(income):
    """How many fiscal years carry a revenue figure - empty columns do not count."""
    return sum(1 for row in income.values() if finite(row.get("totalrevenue")) is not None)


# The page only builds its TTM growth from eight consecutive quarters; below that the figures
# stay open, and the gap report has to use the same number.
QUARTER_WINDOW = 8


def consecutive_quarters(quarterly):
    """How many quarters run without a hole at the newest end, revenue included.

    Counting entries alone was wrong: four empty rows passed as a complete block while the page
    could not add a single TTM figure. A quarter counts when it carries a revenue value and sits
    65 to 120 days after its successor - the same window the page uses.
    """
    days = sorted(quarterly, reverse=True)
    run = 0
    for index, day in enumerate(days):
        if finite((quarterly[day] or {}).get("totalrevenue")) is None:
            break
        if index:
            try:
                distance = (date.fromisoformat(days[index - 1]) - date.fromisoformat(day)).days
            except ValueError:
                break
            if not 65 <= distance <= 120:
                break
        run += 1
    return run


def statement_gaps(statements, sec_source, official, filings_key):
    """One entry per block that is short, with the reason it is short.

    The page shows this next to the score. "Fehlende Werte bleiben sichtbar offen" was the
    agreement from the start; this is the place where a reader can see *why* they are open.
    """
    years = fiscal_years(statements.get("income") or {})
    quarters = consecutive_quarters(statements.get("quarterly") or {})
    if official:
        source = "Originalberichte (XBRL)"
    elif sec_source:
        source = sec_source
    else:
        source = "Yahoo Finance"
    gaps = []
    if years < SEC_YEARS:
        gaps.append({
            "block": "Jahresabschlüsse",
            "source": source,
            "have": years,
            "need": SEC_YEARS,
            "reason": ("Der Schlüssel AKTIENANALYSE_FILINGS_KEY ist nicht gesetzt, deshalb werden "
                       "Originalberichte gar nicht erst abgefragt."
                       if not filings_key else
                       "Für diesen Titel wurde kein auswertbarer Originalbericht gefunden."),
        })
    if quarters < QUARTER_WINDOW:
        gaps.append({
            "block": "Quartale",
            "source": source,
            "have": quarters,
            "need": QUARTER_WINDOW,
            "reason": ("Ohne acht aufeinanderfolgende Quartale mit Umsatz bleibt das TTM-Wachstum "
                       "offen."),
        })
    empty = [day for day, row in sorted((statements.get("income") or {}).items())
             if finite(row.get("totalrevenue")) is None]
    if empty:
        gaps.append({
            "block": "Geschäftsjahre ohne Umsatz",
            "source": source,
            "have": len(empty),
            "need": 0,
            "reason": "Die Zeile kam leer an: " + ", ".join(empty) + ".",
        })
    return gaps


def statement_source(statements, official, sec_source=None):
    if official:
        return "XBRL-Originalberichte · Yahoo Finance für verfügbare Ergänzungen"
    if sec_source:
        return sec_source if any(statements.values()) else None
    return "Yahoo Finance" if any(statements.values()) else None


def search(query, nas=None):
    normalized = query.upper()
    fallback = [SAP] if normalized in {"SAP", "SAP SE", "SAP.DE", "DE0007164600", "716460"} else []
    if nas is not None and len(query) <= 50:
        matches = []
        seen = set()
        nas_query = SAP["symbol"] if fallback else query
        try:
            rows = nas.search(nas_query)
        except (URLError, OSError, ValueError, EOFError):
            # A failing scanner must not take the search down: fall through to Yahoo.
            rows = []
        for row in rows:
            symbol = row.get("symbol", "") if isinstance(row, dict) else ""
            if (not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or symbol in seen
                    or row.get("industry") == "Exchange Traded Fund"):
                continue
            seen.add(symbol)
            matches.append({"symbol": symbol, "name": row.get("name") or symbol,
                            "exchange": row.get("exchange") or row.get("market") or ""})
        if matches:
            return {"matches": matches, "source": "xang auf NAS"}
    try:
        results = yf.Search(query, max_results=20, news_count=0, timeout=8).quotes
        seen = set()
        matches = []
        for row in results:
            symbol = row.get("symbol", "")
            if row.get("quoteType") != "EQUITY" or not SYMBOL.fullmatch(symbol) or symbol in seen:
                continue
            seen.add(symbol)
            matches.append({"symbol": symbol, "name": row.get("longname") or row.get("shortname") or symbol,
                            "exchange": row.get("exchDisp") or row.get("exchange") or ""})
        if fallback and SAP["symbol"] not in seen:
            matches.insert(0, SAP)
        return {"matches": matches, "source": "Yahoo Finance"}
    except Exception:
        return {"matches": fallback, "notice": "Die Namenssuche ist zurzeit offline. Ein Börsenticker kann direkt geladen werden."}


def nas_rows(entries):
    rows = {}
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        close = finite(entry.get("close"))
        day = entry.get("date")
        if close is None or close <= 0 or not isinstance(day, str):
            continue
        try:
            day = date.fromisoformat(day).isoformat()
        except ValueError:
            continue
        rows[day] = [day, close, None, None]
    return [rows[day] for day in sorted(rows)]


def price_jumps(rows):
    # Flag large moves between nearby observations for verification. The
    # magnitude alone cannot distinguish a split from a genuine market move.
    return [(previous, current) for previous, current in zip(rows, rows[1:], strict=False)
            if not 1 / 1.4 < current[1] / previous[1] < 1.4
            and date.fromisoformat(current[0]) - date.fromisoformat(previous[0]) <= timedelta(days=7)]


@lru_cache(maxsize=96)
def history_reference(symbol, bucket):
    ticker = yf.Ticker(symbol)
    rows = load_history(ticker, "5y")
    return rows, ticker.history_metadata.get("currency")


def nas_price_series(symbol, rows, currency, bucket):
    result = {"prices": rows, "price_source": "xang auf NAS", "issues": []}
    jumps = price_jumps(rows)
    if not jumps:
        return result

    cutoff, quote = rows[-1][:2]
    if currency:
        try:
            reference, reference_currency = history_reference(symbol, bucket)
        except Exception:
            reference, reference_currency = [], None
        checked = [row for row in reference if rows[0][0] <= row[0] <= cutoff]
        by_day = {row[0]: row[1] for row in checked}
        complete = all(previous[0] in by_day and current[0] in by_day for previous, current in jumps)
        if reference_currency == currency and complete and abs(by_day.get(cutoff, 0) / quote - 1) <= .005:
            if all(abs((by_day[cur[0]] / by_day[prev[0]]) / (cur[1] / prev[1]) - 1) <= .05
                   for prev, cur in jumps):
                return result
            # Use one consistent price basis. Do not splice raw NAS prices or
            # dividend-adjusted closes into the split-adjusted price series.
            return {"prices": [[day, close, None, None] for day, close, *_ in checked],
                    "quote_price": quote,
                    "price_source": "Yahoo Finance (Splitbereinigung)",
                    "issues": ["NAS-Kurssprünge weichen von der Referenz ab. "
                               "Die Historie nutzt splitbereinigte Schlusskurse von Yahoo Finance."]}

    start = jumps[-1][1][0]
    return {**result, "prices": [row for row in rows if row[0] >= start],
            "issues": [f"NAS-Kurssprung am {start} konnte nicht geprüft werden. "
                       "Die Auswertung beginnt ab diesem Handelstag."]}


def nas_info(raw, profile):
    profile_map = {"name": "longName", "market": "nasMarket", "exchange": "exchange",
                   "sector": "sector", "industry": "industry"}
    info = {target: profile.get(key) or raw.get(key) for key, target in profile_map.items()
            if profile.get(key) or raw.get(key)}
    fx = raw.get("fx_metadata")
    currency = fx.get("from_currency") if isinstance(fx, dict) else None
    if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}|GBp|ZAc", currency):
        info["currency"] = currency
    fields = {"pe_ratio": "trailingPE", "forward_pe": "forwardPE",
              "price_to_sales": "priceToSalesTrailing12Months", "price_to_book": "priceToBook",
              "peg_ratio": "pegRatio", "eps_current": "trailingEps", "revenue_current": "totalRevenue"}
    info.update({target: value for key, target in fields.items()
                 if (value := finite(raw.get(key))) is not None})
    provider = raw.get("data_source")
    if isinstance(provider, str):
        info["nasProvider"] = provider
    # Finviz stores percentage points, Yahoo provides decimal fractions here.
    # A merged or unspecified provider does not identify the unit of these fields.
    if provider in ("finviz", "yfinance"):
        ratio_fields = {"profit_margin": "profitMargins", "operating_margin": "operatingMargins",
                        "gross_margin": "grossMargins", "roe": "returnOnEquity"}
        scale = .01 if provider == "finviz" else 1
        info.update({target: value * scale for key, target in ratio_fields.items()
                     if (value := finite(raw.get(key))) is not None})
    # market_cap is native currency; market_cap_usd is the converted field.
    cap = finite(raw.get("market_cap"))
    if cap is not None:
        info["marketCap"] = cap
        info["marketCapCurrency"] = info.get("currency")
    provider_yield = finite(raw.get("dividend_yield"))
    if provider_yield is not None and 0 <= provider_yield <= 1000:
        info["nasDividendYield"] = provider_yield / 100
    return info


def nas_stocks(nas, symbols, *, identify=False, bucket=None):
    histories = nas.histories(symbols)
    try:
        fundamentals = nas.fundamentals(symbols)
        issue = "Im NAS fehlen aktuelle Fundamentalkennzahlen für diesen Ticker."
    except (URLError, OSError, ValueError):
        fundamentals = {}
        issue = "Die Fundamentaldaten auf dem NAS sind momentan nicht abrufbar."
    stocks = []
    for symbol in symbols:
        rows = nas_rows(histories.get(symbol))
        raw = fundamentals.get(symbol)
        raw = raw if isinstance(raw, dict) else {}
        if not rows and (not raw or not identify):
            continue
        profile = {}
        if identify:
            try:
                profile = next((row for row in nas.search(symbol)
                                if isinstance(row, dict) and row.get("symbol") == symbol), {})
            except (URLError, OSError, ValueError):
                pass
        info = nas_info(raw, profile)
        series = nas_price_series(symbol, rows, info.get("currency"), bucket)
        if not raw:
            series["issues"].append(issue)
        stocks.append({"symbol": symbol, **series, "info": info,
                       "statements": {}, "news": [],
                       "nas_primary": True, "quote_source": "xang auf NAS",
                       "fundamentals_source": "xang auf NAS" if raw else None})
    return stocks


@lru_cache(maxsize=96)
def statement_profile(symbol, cutoff, bucket):
    if "." not in symbol:
        try:
            sec = load_sec_statements(symbol, date.fromisoformat(cutoff), bucket * 600)
        except (URLError, OSError, ValueError, TypeError, KeyError):
            sec = {}
        if sec.get("income") or sec.get("quarterly"):
            return {"statements": sec, "statements_source": "SEC EDGAR",
                    "info": {"financialCurrency": "USD"}, "statement_date_limit": cutoff}
    ticker = yf.Ticker(symbol)
    statements = {name: {day: row for day, row in values.items() if day <= cutoff}
                  for name, values in load_statements(ticker).items()}
    currency = None
    isin = None
    if any(statements.values()):
        try:
            info = ticker.get_info()
            currency, isin = info.get("financialCurrency"), info.get("isin")
        except Exception:
            pass
    statements, currency, official = merge_filings(symbol, cutoff, statements, currency, isin)
    return {"statements": statements, "statements_source": statement_source(statements, official),
            "info": {"financialCurrency": currency} if currency else {}, "statement_date_limit": cutoff}


def with_statements(result, bucket):
    cutoff = result["prices"][-1][0]
    profile = statement_profile(result["symbol"], cutoff, bucket)
    return {**result, **profile, "info": {**result["info"], **profile["info"]}}


@lru_cache(maxsize=32)
def stock_extra(symbol, bucket, cutoff, currency, history_only=False):
    ticker = yf.Ticker(symbol)
    issues = []
    rows = []
    info = {}
    try:
        candidate = [row for row in load_history(ticker, "max") if row[0] <= cutoff]
        price_currency = ticker.history_metadata.get("currency")
        if candidate and candidate[-1][0] == cutoff and price_currency and (not currency or currency == price_currency):
            rows = candidate
            info["currency"] = price_currency
        else:
            issues.append("Die Yahoo-Kursreihe passt nicht zum NAS-Stichtag oder zur Notierungswährung.")
    except Exception:
        issues.append("Die ergänzende Yahoo-Kursreihe ist momentan nicht verfügbar.")
    statements = {} if history_only else load_statements(ticker)
    if any(statements.values()):
        try:
            company = ticker.get_info()
            if company.get("financialCurrency"):
                info["financialCurrency"] = company["financialCurrency"]
        except Exception:
            pass
    if not history_only and not statements["income"]:
        issues.append("Yahoo Finance hat keine Jahres-GuV geliefert.")
    return {"symbol": symbol, "prices": rows, "info": info, "statements": statements, "issues": issues,
            "price_source": "Yahoo Finance" if rows else None,
            "statements_source": "Yahoo Finance" if any(statements.values()) else None}


class Handler(BaseHTTPRequestHandler):
    nas = None

    # The path prefix the page runs under: "" when started by hand (port 8766,
    # root), "/qualitaet" when it hangs behind the dashboard. Every path is
    # stripped once at the top, so the same route comparison as before applies
    # everywhere after that.
    base_path = ""

    def inner_path(self, path):
        """The path without the base path, or ``None`` if it does not match."""
        if not self.base_path:
            return path
        prefix = self.base_path
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
        return None

    def asset(self, path):
        """The facade's fonts.css and woff2 files; the same files the funnel uses.

        A missing file returns ``(None, None)`` and the caller answers 404 - a FileNotFoundError
        here would close the socket without a status line.
        """
        if path == "/fonts.css":
            if not FONT_CSS.is_file():
                return None, None
            return FONT_CSS.read_bytes(), "text/css; charset=utf-8"
        name = path[len("/fonts/"):]
        if not FONT_NAME.fullmatch(name) or not (FONT_DIR / name).is_file():
            return None, None
        return (FONT_DIR / name).read_bytes(), "font/woff2"

    def page(self):
        """index.html mit eingespieltem Basispfad.

        Die Seite bleibt sonst byte-gleich: genau ein Block kommt direkt hinter
        das oeffnende ``<html>``-Tag (der Browser legt ihn in den Kopf), damit
        das Skript seine Datenabfragen unter demselben Pfadanfang baut, unter dem
        die Seite geladen wurde. Ohne Basispfad ist der Wert leer - und damit
        verhaelt sich alles wie vorher.

        Gesucht wird das Tag, nicht ``<head>``: die Datei hat keinen eigenen
        Kopf-Block, und ein Suchwort aus dem Fliesstext traefe die falsche
        Stelle (genau das ist beim ersten Anlauf passiert - der Block landete in
        einem Kommentar des Skripts).
        """
        html = (ROOT / "index.html").read_text(encoding="utf-8").replace(ASSET_BASE, "")
        html = html.encode()
        start = html.find(b"<html")
        ende = html.find(b">", start) if start >= 0 else -1
        if start < 0 or ende < 0:
            return html
        # json.dumps leaves "</" alone, so a base path carrying "</script>" would end
        # the block early. Escaping "<" as \u003c is the same string to JavaScript.
        escaped = json.dumps(self.base_path).replace("<", "\\u003c")
        injection = (f"<script>window.AKT_BASE_PATH={escaped}</script>").encode()
        return html[:ende + 1] + injection + html[ende + 1:]

    def reply_asset(self, content, content_type):
        """One font file: held for a week, because its content does not change."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reply(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, status, value):
        self.reply(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self):
        target = urlsplit(self.path)
        path = self.inner_path(target.path)
        if path in ("/", "/index.html"):
            self.reply(200, self.page(), "text/html; charset=utf-8")
            return
        if path and (path == "/fonts.css" or path.startswith("/fonts/")):
            content, content_type = self.asset(path)
            if content is None:
                self.json(404, {"error": "Adresse nicht gefunden."})
            else:
                # Not through reply(): it sets no-store and would make the browser fetch the
                # six font files again on every request.
                self.reply_asset(content, content_type)
            return
        if path not in ("/api/search", "/api/stock", "/api/stock-batch", "/api/stock-extra", "/api/statements",
                        "/api/compare", "/api/xang", "/api/ranking"):
            self.json(404, {"error": "Adresse nicht gefunden."})
            return
        params = parse_qs(target.query)
        bucket = int(time() // 900)
        query = params.get("q", [""])[0].strip()
        try:
            if path == "/api/xang":
                if not SYMBOL.fullmatch(query.upper()):
                    raise ValueError("Bitte einen gültigen Börsenticker eingeben.")
                if self.nas is None:
                    self.json(200, {"status": "disabled"})
                    return
                try:
                    self.json(200, self.nas.snapshot(query.upper()))
                except (URLError, ValueError, OSError):
                    self.json(502, {"error": "NAS-Snapshot momentan nicht abrufbar."})
                return
            if path == "/api/search":
                if not 1 <= len(query) <= 100:
                    raise ValueError("Bitte einen Namen, Ticker oder eine ISIN eingeben.")
                self.json(200, search(query, self.nas))
                return
            if path == "/api/ranking":
                # The weighted list needs prices only, and it brings its own universe: the generic
                # ticker check below would reject the empty q of a universe request.
                universe = params.get("universe", [""])[0].strip()
                own = checked_symbols([part for part in
                                       re.split(r"[,\s;]+", params.get("q", [""])[0].upper())
                                       if part])
                available = 0
                if universe:
                    symbols, available = capped_symbols(ranking.load_universe(universe))
                else:
                    symbols = own
                if not symbols:
                    # An empty universe file is not a bad request: the list says so.
                    raise ValueError("Das Universum ist leer." if universe
                                     else "Bitte ein Universum oder einen bis zweihundert "
                                          "Ticker angeben.")
                index_symbol = (params.get("index", ["^GSPC"])[0] or "^GSPC").upper()
                if not re.fullmatch(r"[A-Z0-9.^=\-]{1,24}", index_symbol):
                    raise ValueError("Der Vergleichsindex ist ungültig.")
                periods = int(params.get("periods", [str(ranking.PERIODS)])[0])
                if not 60 <= periods <= 2500:
                    raise ValueError("Die Zahl der Perioden muss zwischen 60 und 2500 liegen.")
                enter_ranking()
                try:
                    fetch, index_rows = ranking_series(index_symbol)
                    answer = ranking.rank_list(symbols, fetch, index_rows, periods)
                    if available:
                        # Said out loud: the list worked through a part of the file, not all of it.
                        answer["listed"] = available
                    self.json(200, answer)
                finally:
                    leave_ranking()
                return
            symbols = list(dict.fromkeys(query.upper().split(",")))
            maximum = 15 if path == "/api/stock-batch" else 5
            if len(symbols) > maximum or any(not SYMBOL.fullmatch(s) for s in symbols):
                raise ValueError(f"Bitte bis zu {maximum} gültige Börsenticker eingeben.")
            if path == "/api/statements":
                if len(symbols) != 1:
                    raise ValueError("Bitte genau einen Ticker auswählen.")
                cutoff = date.fromisoformat(params.get("cutoff", [""])[0]).isoformat()
                self.json(200, statement_profile(symbols[0], cutoff, int(time() // 600)))
            elif path == "/api/stock-extra":
                if len(symbols) != 1:
                    raise ValueError("Bitte genau eine Aktie auswählen.")
                if self.nas is None:
                    raise ValueError("Die Ergänzung setzt ein NAS-Kursprofil voraus.")
                cutoff = date.fromisoformat(params.get("cutoff", [""])[0]).isoformat()
                currency = params.get("currency", [""])[0]
                if currency and not re.fullmatch(r"[A-Z]{3}|GBp|ZAc", currency):
                    raise ValueError("Die Notierungswährung ist ungültig.")
                self.json(200, stock_extra(symbols[0], bucket, cutoff, currency,
                                           params.get("history_only") == ["1"]))
            elif path == "/api/stock":
                if len(symbols) != 1:
                    raise ValueError("Bitte genau eine Aktie auswählen.")
                cached = nas_stocks(self.nas, symbols, identify=True, bucket=bucket) if self.nas is not None else []
                if cached:
                    result = cached[0]
                    if not result["prices"]:
                        result.update(prices=prices(symbols[0], bucket), price_source="Yahoo Finance",
                                      quote_source="Yahoo Finance",
                                      issues=["Keine NAS-Tageskurse für diesen Ticker."])
                    self.json(200, result)
                else:
                    result = stock(symbols[0], bucket)
                    if self.nas is not None:
                        result = {**result, "issues": ["Keine NAS-Tageskurse für diesen Ticker.", *result["issues"]]}
                    self.json(200, result)
            elif path == "/api/stock-batch":
                if self.nas is not None:
                    results = nas_stocks(self.nas, symbols, bucket=bucket)
                    if results and params.get("statements") == ["1"]:
                        statement_bucket = int(time() // 600)
                        def enrich(item):
                            try:
                                return with_statements(item, statement_bucket)
                            except (URLError, OSError, ValueError):
                                return item
                        with ThreadPoolExecutor(max_workers=min(3, len(results))) as pool:
                            results = list(pool.map(enrich, results))
                    self.json(200, {"stocks": results})
                    return

                def get_stock(symbol):
                    try:
                        return stock(symbol, bucket)
                    except Exception:
                        return None

                with ThreadPoolExecutor(max_workers=min(3, len(symbols))) as pool:
                    self.json(200, {"stocks": [item for item in pool.map(get_stock, symbols) if item is not None]})
            else:
                histories = self.nas.histories(symbols) if self.nas is not None else None
                nas_series = {symbol: nas_rows(histories.get(symbol)) for symbol in symbols} if histories is not None else {}
                flagged = [symbol for symbol, rows in nas_series.items() if price_jumps(rows)]
                fundamentals = {}
                if flagged:
                    try:
                        fundamentals = self.nas.fundamentals(flagged)
                    except (URLError, OSError, ValueError):
                        pass

                def get_prices(symbol):
                    try:
                        if histories is not None:
                            rows = nas_series[symbol]
                            if not rows:
                                return {"symbol": symbol, "error": "Kursreihe fehlt im NAS-Bestand."}
                            raw = fundamentals.get(symbol)
                            info = nas_info(raw, {}) if isinstance(raw, dict) else {}
                            return {"symbol": symbol, **nas_price_series(symbol, rows, info.get("currency"), bucket)}
                        return {"symbol": symbol, "prices": prices(symbol, bucket), "price_source": "Yahoo Finance"}
                    except Exception:
                        return {"symbol": symbol, "error": "Kursreihe fehlt."}

                with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
                    self.json(200, {"series": list(pool.map(get_prices, symbols))})
        except HTTPError as error:
            message = ("Die NAS-Sitzung ist abgelaufen. Bitte die Aktienanalyse neu starten und erneut anmelden."
                       if error.code in (401, 403) else "Der NAS-Scanner konnte die Anfrage nicht beantworten.")
            error.close()
            self.json(502, {"error": message})
        except ValueError as error:
            self.json(400, {"error": str(error)})
        except Exception:
            self.json(502, {"error": "Die Datenquelle ist nicht erreichbar oder liefert für diesen Ticker derzeit keine Daten."})


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Lokale Aktienanalyse starten.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--nas", metavar="URL",
                        help="Stock-Scanner-Adresse im Heimnetz "
                             "(Standard: AKTIENANALYSE_NAS_URL oder http://192.168.178.10:8089)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address to listen on (default 127.0.0.1; inside a container "
                             "0.0.0.0, otherwise the service is unreachable from outside)")
    parser.add_argument("--base-path", default="", metavar="PATH",
                        help="Path prefix the page runs under, e.g. /qualitaet "
                             "(empty = root, as when started by hand)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port muss zwischen 1 und 65535 liegen.")
    base_path = args.base_path.strip()
    if base_path:
        if not base_path.startswith("/") or base_path.startswith("//") or ".." in base_path:
            parser.error("--base-path must start with exactly one / and must not contain ..")
        base_path = base_path.rstrip("/")
    Handler.base_path = base_path

    # Without --nas the environment variable applies; when that is empty too, the app runs
    # without the NAS as before. The default lives in exactly one place, nas_scanner.py.
    nas_url = args.nas or os.environ.get("AKTIENANALYSE_NAS_URL") or None
    if nas_url:
        from nas_scanner import NasScanner

        try:
            nas = NasScanner(nas_url)
            Handler.nas = nas
            nas.connect()
            print("NAS-Datenquelle verbunden.", flush=True)
        except (URLError, ValueError, OSError, EOFError) as error:
            # The scanner is optional: a failed sign-in must not stop the service.
            print(f"NAS-Verbindung fehlgeschlagen ({error}). Es geht ohne NAS weiter.",
                  flush=True)

    with ThreadingHTTPServer((args.host, args.port), Handler) as server:
        print(f"Aktienanalyse öffnen unter http://{args.host}:{args.port}{base_path}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
