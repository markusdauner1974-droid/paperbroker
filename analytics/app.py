import json
import math
import os
import re
import socket
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic, time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen
from xml.etree.ElementTree import ParseError

import fonrex
import ranking
import stockanalysis
import store
import yfinance as yf
from filing_statements import REPORTED_YEARS_MIN, filings_available, merge_filings, reported_years
from sec_statements import load_sec_statements
from statement_export import export_gaps, statement_export
from yfinance.exceptions import YFTickerMissingError

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


class UnknownSymbol(Exception):
    """The source does not know this ticker - not the same as an unreachable source."""

    def __init__(self, symbol, candidates=None):
        self.symbol = symbol
        self.candidates = list(candidates or [])
        self.suggestion = self.candidates[0]["symbol"] if self.candidates else None
        if self.suggestion:
            message = (f"Den Ticker {symbol} kennt die Datenquelle nicht. "
                       f"Gemeint ist vermutlich {self.suggestion}.")
        else:
            message = f"Den Ticker {symbol} kennt die Datenquelle nicht."
        super().__init__(message)


#: The class names that mean "no such instrument", kept for a module that cannot import them.
UNKNOWN_TYPE_NAMES = ("YFTickerMissingError", "YFTzMissingError", "YFPricesMissingError")

#: Words only Yahoo's own answers use. They are the fallback, not the rule: a proxy or scanner page
#: can carry any sentence in its body, and "Quote not found" alone was too broad to tell them apart.
UNKNOWN_MARKERS = ("possibly delisted", "Quote not found for symbol")


def unknown_symbol(error) -> bool:
    """Whether the failure says "no such ticker" rather than "no answer".

    Measured: an unknown Yahoo ticker comes back as ``YFTzMissingError: possibly delisted; no timezone
    found``, a delisted one as ``YFPricesMissingError`` - both under ``YFTickerMissingError``. A
    timeout or a refused connection is neither, and telling them apart matters: one is a typo the
    reader can correct, the other is an outage nobody can.

    The class decides, not the text. An error page from a proxy can quote any sentence, so a message
    match alone answered 404 for an outage - and told the reader to check a ticker that was fine.
    """
    if YFTickerMissingError is not None and isinstance(error, YFTickerMissingError):
        return True
    # An older embedded analytics module, or a copy re-raised through a wrapper, keeps the name only.
    if type(error).__name__ in UNKNOWN_TYPE_NAMES:
        return True
    text = str(error)
    return any(marker in text for marker in UNKNOWN_MARKERS)


def symbol_candidates(symbol) -> list[dict]:
    """The tickers the source does know for a typo, best first - never one without a reason.

    Measured: "BASF.DE" answers nothing while the search names BASF SE as BAS.DE, "SIE" was answered
    with ENR.DE (Siemens Energy) by a rule that only looked for the stem inside any name, and
    "NESN" got nothing although the search knows NESN.SW.

    Two signals, weighed: the search's own order, which is a relevance ranking, and how the row
    relates to what was typed. A stem that is a ticker stem beats a name that merely contains it,
    which is what keeps "SIE" away from Siemens Energy.
    """
    typed = (symbol or "").strip()
    stem = typed.split(".")[0].lower()
    suffix = typed.split(".")[1].lower() if "." in typed else ""
    if len(stem) < 2:
        return []
    try:
        rows = yahoo_matches(stem)
    except Exception:
        return []
    scored = []
    for rank, row in enumerate(rows):
        candidate = row["symbol"]
        if candidate.upper() == typed.upper():
            continue
        name = (row.get("name") or "").lower()
        same_stem = candidate.split(".")[0].lower() == stem
        starts = name.startswith(stem)
        contains = stem in name
        if not (same_stem or starts or contains):
            # A row without any relation to what was typed is not a suggestion: the search's own
            # order alone would have offered "Something Else AG" for "XYZQ".
            continue
        score = max(0.0, 2.6 - 0.3 * rank)          # the search ranks by relevance
        if same_stem:
            score += 3.0                            # a ticker stem, not a substring
        if starts:
            score += 1.4
        elif contains:
            score += 0.5
        if suffix and candidate.split(".")[-1].lower() == suffix:
            score += 3.0                            # the venue the reader asked for
        scored.append((score, rank, candidate, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [{"symbol": candidate, "name": row.get("name") or candidate,
             "exchange": row.get("exchange") or ""}
            for _, _, candidate, row in scored[:3]]


def symbol_suggestion(symbol):
    """The best ticker for a typo, or ``None``."""
    candidates = symbol_candidates(symbol)
    return candidates[0]["symbol"] if candidates else None


#: A ticker the source did not know stays unknown for a while. The lookup costs a search of up to
#: eight seconds, the endpoint takes its ticker from the request, and a mistyped ticker comes back
#: with every reload of the page - so the verdict is kept, not the network call.
UNKNOWN_TTL_S = 3600
UNKNOWN_PLACES = 256
_UNKNOWN: dict[str, tuple[float, list]] = {}
_UNKNOWN_GUARD = Lock()


def unknown_verdict(symbol):
    """The candidates for a ticker known to be unknown, or ``None`` when it has to be looked up."""
    now = monotonic()
    with _UNKNOWN_GUARD:
        remembered = _UNKNOWN.get(symbol)
        if remembered and now - remembered[0] < UNKNOWN_TTL_S:
            return remembered[1]
        if remembered:
            _UNKNOWN.pop(symbol, None)
    return None


def remember_unknown(symbol, candidates):
    """Keep the verdict so the next reload of the same typo costs nothing."""
    now = monotonic()
    with _UNKNOWN_GUARD:
        if len(_UNKNOWN) >= UNKNOWN_PLACES:
            _UNKNOWN.pop(next(iter(_UNKNOWN)))
        _UNKNOWN[symbol] = (now, list(candidates or []))


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
    # A ticker already known to be unknown answers from memory: no price attempt, no search. Without
    # this every reload of a mistyped page cost a failed download plus a search of up to eight
    # seconds, and a caller could ask for as many different typos as it liked.
    remembered = unknown_verdict(symbol)
    if remembered is not None:
        raise UnknownSymbol(symbol, remembered)
    ticker = yf.Ticker(symbol)
    try:
        rows = prices(symbol, bucket)
    except Exception as error:
        # A typo in the ticker is the reader's to correct, an outage is nobody's: the two must not
        # arrive as the same 502.
        if unknown_symbol(error):
            candidates = symbol_candidates(symbol)
            remember_unknown(symbol, candidates)
            raise UnknownSymbol(symbol, candidates) from error
        raise
    info = {}
    issues = []
    try:
        raw = ticker.get_info()
        text_keys = ("shortName", "longName", "currency", "financialCurrency", "sector", "isin",
                     "industry", "exchange", "fullExchangeName", "quoteType")
        # Every field the page prints must be listed here - a figure Yahoo has but this tuple does
        # not mention reaches the page as undefined and is shown as a dash. `priceToBook` was missing
        # for exactly that reason: L'Oreal has 6.84 at the source and the header showed nothing.
        number_keys = ("marketCap", "trailingPE", "forwardPE", "priceToSalesTrailing12Months",
                       "revenueGrowth", "earningsQuarterlyGrowth", "grossMargins",
                       "profitMargins", "returnOnEquity", "totalDebt", "totalRevenue",
                       "operatingMargins", "targetMeanPrice", "recommendationMean",
                       "numberOfAnalystOpinions", "trailingEps", "dividendRate",
                       "priceToBook", "dividendYield", "bookValue", "pegRatio")
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



def long_quarterly_growth(symbol, statements, budget_s=stockanalysis.FETCH_BUDGET_S,
                          periods=stockanalysis.UNFETCHED):  # list | None | UNFETCHED
    """The twelve month growth from the longer quarterly series, or None.

    The provider hands over five or six quarters for a European paper; the second source keeps
    twenty. Asked only when the page could not build the figure itself, and never fatally: a source
    that is down or slow leaves the row open and says so, it does not hold up the page. The wait is
    capped by ``budget_s``; whatever does not fit is loaded afterwards and named as loading.
    """
    if consecutive_quarters(statements.get("quarterly") or {}) >= QUARTER_WINDOW:
        return None
    try:
        answer = stockanalysis.growth(symbol, store_module=store, budget_s=budget_s,
                                      periods=periods)
    except Exception:
        return {"revenue": None, "eps": None, "reason": "Quartalsreihe nicht abrufbar"}
    if answer.get("revenue") is None and answer.get("eps") is None and "reason" not in answer:
        answer["reason"] = "Quartalsreihe zu kurz"
    return answer


#: What the second source publishes per quarter. Net income is not among it, so a quarter that
#: only comes from there carries a dash in that column instead of a made-up figure.
#: (key on the source's period, key in a statement row, label for the page, factor to whole units).
#: Revenue and operating income are published in millions, earnings per share is not - scaling it
#: turned 1,71 into 1 710 000.
SECOND_SOURCE_FIELDS = (("revenue", "totalrevenue", "Umsatz", stockanalysis.MILLIONS),
                        ("operating_income", "operatingincome", "Betriebsergebnis",
                         stockanalysis.MILLIONS),
                        ("eps", "dilutedeps", "verwässertes EPS", 1))


def absent_in_row(row, key):
    """Whether a reported field counts as missing.

    The provider sends 0.0 for diluted EPS on quarters that clearly earned money - measured on SAP
    Q3 2026: net income 2 249 Mio against 1 157 Mio shares, published EPS 1,94. A zero there is a
    defect of the feed, not a figure, and the second source is allowed to complete it.
    """
    value = finite(row.get(key))
    if value is None:
        return True
    return key == "dilutedeps" and value == 0 and (finite(row.get("netincome")) or 0) > 0


def second_source_needed(statements) -> bool:
    """Whether the provider's own quarters cannot carry the twelve month figures.

    Two cases. One is visible: fewer than eight consecutive quarters, and the table stops after a
    year. The other is quiet - Yahoo leaves the diluted EPS of a US fiscal fourth quarter empty, so
    the page's four-quarter sums turn null although a dozen quarters are there. Both are answered by
    the same second source, and it only ever fills what is missing.
    """
    quarterly = statements.get("quarterly") or {}
    if consecutive_quarters(quarterly) < QUARTER_WINDOW:
        return True
    newest = sorted(quarterly, reverse=True)[:QUARTER_WINDOW]
    return any(absent_in_row(quarterly[day], row_key)
               for day in newest
               for _, row_key, _, _ in SECOND_SOURCE_FIELDS)


def second_source_quarters(symbol, statements, budget_s=stockanalysis.FETCH_BUDGET_S,
                           periods=stockanalysis.UNFETCHED):  # list | None | UNFETCHED
    """The quarter table extended by the second source - gaps only, never an overwrite.

    Yahoo hands over five or six quarters for a European paper and none at all for L'Oréal, so the
    table stopped after a year. The second source keeps twenty quarters of revenue, diluted EPS and
    operating income. A quarter Yahoo already reports stays untouched except where it left a field
    empty; net income is never invented.

    Returns the quarters and a note for the page, or ``(quarters, None)`` when nothing was added.
    The rows are copied first: ``stock()`` is cached, and a row edited in place would carry the
    second source into every later caller.
    """
    quarterly = {day: dict(row) for day, row in (statements.get("quarterly") or {}).items()}
    if not quarterly and not statements.get("income"):
        # A scanner row without any statement is served from the local snapshot; asking a second
        # source there would turn a deliberately local path into a network call.
        return quarterly, None
    if not second_source_needed(statements):
        return quarterly, None
    if isinstance(periods, stockanalysis.Unfetched):
        try:
            periods = stockanalysis.series(symbol, store_module=store, budget_s=budget_s)
        except Exception:
            # A source that is down or slow leaves the table as it was; the TTM rows already name the
            # reason, and an exception here would cost the whole page.
            return quarterly, None
    if periods is None:
        return quarterly, None
    if stockanalysis.quarter_step(periods) is None:
        # No quarterly reporting: there is no quarter to add. The half-year rows stay out.
        return quarterly, None
    added, filled = [], []
    for period in periods:
        day = period.get("period_end")
        if not day:
            continue
        values = []
        for source_key, row_key, _, factor in SECOND_SOURCE_FIELDS:
            amount = finite(period.get(source_key))
            if amount is not None:
                values.append((row_key, amount * factor))
        if not values:
            continue
        row = quarterly.get(day)
        if row is None:
            quarterly[day] = dict(values)
            added.append(day)
            continue
        missing = [(key, value) for key, value in values if absent_in_row(row, key)]
        if missing:
            row.update(missing)
            filled.append(day)
    if not added and not filled:
        return quarterly, None
    return quarterly, {"source": "stockanalysis.com",
                       "added": sorted(added, reverse=True),
                       "filled": sorted(filled, reverse=True),
                       "fields": [label for _, _, label, _ in SECOND_SOURCE_FIELDS]}


def with_second_source(symbol, result):
    """The answer plus the twelve month growth and the extended quarter table.

    One place for both dispatchers: the delivered one in this file and the embedded one under
    paperbroker/routes answer the same endpoint, and a step only one of them takes shows up as a
    missing column. The growth is read before the table grows - the page then builds its own
    figures from the longer series and keeps the second source as a fallback.
    """
    statements = result.get("statements") or {}
    # One request, one fetch: both steps below read the same series. Fetching inside each of them
    # asked the source twice and, when it was slow, waited out the budget twice.
    needs = second_source_needed(statements)
    periods = stockanalysis.UNFETCHED
    if needs:
        try:
            periods = stockanalysis.series(symbol, store_module=store,
                                           budget_s=stockanalysis.FETCH_BUDGET_S)
        except Exception:
            # Nothing is queued twice: refresh_in_background does nothing while a load for the same
            # ticker runs. The next view then has the rows instead of asking again.
            stockanalysis.refresh_in_background(symbol, store)
            periods = None
    growth = long_quarterly_growth(symbol, statements, periods=periods)
    quarterly, note = second_source_quarters(symbol, statements, periods=periods)
    if note is None:
        return {**result, "ttm_growth": growth}
    return {**result, "ttm_growth": growth, "quarterly_source": note,
            "statements": {**statements, "quarterly": quarterly}}


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
            "reason": ("Ohne acht aufeinanderfolgende Quartale mit Umsatz kann die Seite das "
                       "TTM-Wachstum nicht selbst rechnen; es kommt dann aus der zweiten Quelle "
                       "oder bleibt offen."),
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


def statement_source(statements, official, sec_source=None, quarterly_export=False,
                     fonrex_years=False):
    if official and quarterly_export:
        return ("XBRL-Originalberichte · Quartale aus der Standardauswertung der Berichte · "
                "Yahoo Finance für verfügbare Ergänzungen")
    if official:
        return "XBRL-Originalberichte · Yahoo Finance für verfügbare Ergänzungen"
    if fonrex_years:
        return ("Jahre aus den ESEF-Berichten über Fonrex · Quartale aus der Standardauswertung · "
                "Yahoo Finance für verfügbare Ergänzungen")
    if sec_source:
        return sec_source if any(statements.values()) else None
    return "Yahoo Finance" if any(statements.values()) else None


# Venues that merely resell a paper another exchange prices: Frankfurt, Stuttgart, Hamburg and the
# other German regional boards, plus the OTC market. Their volume is a fraction of the home venue's,
# so they are never the listing a company is valued at.
RESALE_CODES = {"FRA", "STU", "HAM", "BER", "DUS", "MUN", "HAN", "PNK", "OTC"}
RESALE_NAMES = {"frankfurt", "stuttgart", "hamburg", "berlin", "düsseldorf", "dusseldorf",
                "münchen", "muenchen", "hannover", "otc markets", "otc", "xetra frankfurt"}

# ASCII and typographic apostrophes alike become a separator, so one spelling cannot make two rows.
APOSTROPHES = ("'" + chr(0x2018) + chr(0x2019) + chr(0x02BC) + "`"
               + chr(0x00B4))
APOSTROPHE_TO_SPACE = {ord(character): " " for character in APOSTROPHES}

# Words that name a legal form, not a company. Stripped before two listings are compared.
# Only legal forms belong here. "Group" and "Holdings" are part of a name and often the only word
# that tells two companies apart - "X Group" and "X Holdings" must not become one row.
LEGAL_FORMS = {"sa", "s", "a", "ag", "se", "nv", "n", "v", "spa", "sp", "ab", "asa", "oyj", "plc",
               "inc", "corp", "co", "kgaa", "gmbh", "na", "societe", "anonyme", "adr", "adrs", "o"}


def company_key(name):
    """One key per company, so six listings of L'Oreal become one row.

    Accents, punctuation and the legal form are dropped: "L'Oréal S.A.", "L'Oreal S.A. (ADRs)" and
    "L'Oreal SA" all end up as "oreal". A leading "The" goes too - "The Home Depot" and "Home Depot"
    are one company, and a listing that adds or drops the article must not become a second row. A
    name that leaves nothing behind keeps its own text, so two unknown companies never collapse.
    """
    # The apostrophe is turned into a space before the accents are folded away: dropping it instead
    # would leave "loreal" beside "l oreal" - the same company in two rows.
    text = str(name or "").translate(APOSTROPHE_TO_SPACE)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    text = text.replace("(", " ").replace(")", " ")
    words = [word for word in re.split(r"[^a-z0-9]+", text) if word]
    # "the" only as the first word: a "The" in the middle is part of the name.
    if words and words[0] == "the":
        words = words[1:]
    # Every word that is not a legal form stays, including a short one: "Firma 0" and "Firma 1"
    # differ by a single character, and dropping it would fold two companies into one row.
    kept = [word for word in words if word not in LEGAL_FORMS]
    return " ".join(kept) or str(name or "").strip().casefold()


def is_resale(listing):
    """Whether this listing only resells a price another exchange makes."""
    code = str(listing.get("exchange_code") or "").upper()
    name = str(listing.get("exchange") or "").strip().casefold()
    return code in RESALE_CODES or name in RESALE_NAMES


LEAD_LOOKUPS = 3    # a search must answer in a second, not in ten


def lead_index(listings, facts):
    """Which of a company's listings carries the price.

    Decided by currency, not by rank: the price belongs to the venue that trades in the currency the
    company reports in. Measured 2026-11-22, Yahoo ranks NASDAQ first for ASML (USD) and the NYSE
    first for SAP (USD) - taking those would put a dollar price beside euro statements, and an FX
    error the reader cannot see. Where no listing matches the reporting currency, the first that is
    not a regional resale board or the OTC market stands; if every listing is one, the first stays.
    """
    facts = list(facts) + [{}] * max(0, len(listings) - len(facts))
    reporting = next((fact.get("reporting_currency") or fact.get("currency")
                       for fact in facts[:LEAD_LOOKUPS] if fact.get("reporting_currency")
                       or fact.get("currency")), "")
    for index, listing in enumerate(listings[:LEAD_LOOKUPS]):
        code = facts[index].get("exchange_code", "")
        if (reporting and facts[index].get("currency") == reporting
                and not is_resale({**listing, "exchange_code": code})):
            return index
    for index, listing in enumerate(listings):
        if not is_resale({**listing, "exchange_code": facts[index].get("exchange_code", "")}):
            return index
    return 0


# How many listings may be asked from Yahoo at the same time - per request and, through the gate
# below, in the whole process: four page requests at once must not multiply the lookups into four
# times six. A search holds a slot only while it waits for Yahoo.
FACT_WORKERS = 6
FACT_GATE = BoundedSemaphore(FACT_WORKERS)
# `Ticker.get_info()` takes no timeout of its own. A stalled connection would hold a thread - and a
# gate slot - for good, and with four request threads that is the whole server. Two guards: every
# lookup is awaited with this limit, and sockets created from here on give up by themselves.
FACT_TIMEOUT_S = 8

socket.setdefaulttimeout(FACT_TIMEOUT_S)


def gated_facts(symbol, bucket):
    """listing_facts behind the process-wide gate."""
    with FACT_GATE:
        return listing_facts(symbol, bucket)


@lru_cache(maxsize=256)
def listing_facts(symbol, bucket):
    """Currency, daily volume and ISIN of one listing - three figures, no statements.

    Deliberately not `stock()`: a search must answer in a second, and loading balance sheets for
    every hit would take ten.
    """
    try:
        raw = yf.Ticker(symbol).get_info() or {}
    except Exception:
        return {}
    return {"currency": raw.get("currency") or "", "volume": finite(raw.get("averageVolume")),
            "exchange_code": raw.get("exchange") or "", "isin": raw.get("isin") or "",
            "reporting_currency": raw.get("financialCurrency") or "",
            "country": raw.get("country") or "",
            "name": raw.get("longName") or raw.get("shortName") or ""}


# Wikidata records the ISIN of a company as claim P946. It is the only free source that names it for
# a European listing: Yahoo holds no ISIN (`isin` is always null, and `Ticker.isin` answers with a
# foreign paper's number - measured: FR0011149590 for L'Oreal instead of FR0000120321).
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COUNTRY_ISIN_PREFIX = {
    "france": "FR", "germany": "DE", "netherlands": "NL", "belgium": "BE", "italy": "IT",
    "spain": "ES", "portugal": "PT", "austria": "AT", "switzerland": "CH", "sweden": "SE",
    "denmark": "DK", "norway": "NO", "finland": "FI", "ireland": "IE", "poland": "PL",
    "united kingdom": "GB", "united states": "US", "canada": "CA", "japan": "JP",
    "australia": "AU", "hong kong": "HK", "china": "CN", "brazil": "BR", "india": "IN",
    "south africa": "ZA", "south korea": "KR", "taiwan": "TW", "singapore": "SG",
    "mexico": "MX", "turkey": "TR", "israel": "IL", "new zealand": "NZ", "luxembourg": "LU",
    "greece": "GR", "hungary": "HU", "czechia": "CZ", "czech republic": "CZ", "russia": "RU",
}
ISIN_FORM = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
# A legal form in the query keeps Wikidata from finding the company: "L'Oréal S.A." returns nothing,
# "L'Oréal" returns the right entity. The suffix is peeled off one step at a time.
LEGAL_SUFFIX = re.compile(
    r"[\s,]+(s\.?a\.?|s\.?e\.?|a\.?g\.?|n\.?v\.?|b\.?v\.?|s\.?p\.?a\.?|plc|inc\.?|corp\.?|"
    r"co\.?|kgaa|gmbh|ab|asa|oyj|na|societe anonyme)\.?$", re.IGNORECASE)


def isin_search_terms(name):
    """The name as given, then with its legal form peeled off - the order Wikidata answers in."""
    terms = []
    current = str(name or "").strip()
    while current and current not in terms and len(terms) < 4:
        terms.append(current)
        current = LEGAL_SUFFIX.sub("", current).strip()
    return terms


WIKIDATA_PAUSE_S = 300     # after a 429 the service is left alone for five minutes
_wikidata_blocked_until = 0.0


def _wikidata(params):
    """One answer from Wikidata, or None. A search must never fail because a helper is silent.

    The service answers 429 with a Retry-After when asked too often. That is respected: after a
    refusal nothing is asked for five minutes, and the page simply shows no ISIN meanwhile.
    """
    global _wikidata_blocked_until
    if time() < _wikidata_blocked_until:
        return None
    url = WIKIDATA_API + "?" + "&".join(f"{key}={quote(str(value))}" for key, value in params.items())
    request = Request(url, headers={"User-Agent": "paperbroker-analytics/1 (stock analysis page)"})
    try:
        with urlopen(request, timeout=4) as answer:
            return json.loads(answer.read())
    except HTTPError as refusal:
        if refusal.code == 429:
            _wikidata_blocked_until = time() + WIKIDATA_PAUSE_S
        return None
    except (URLError, OSError, ValueError):
        return None


def company_isin(name, country, path=None):
    """The ISIN of a company, accepted only when its country prefix matches the company's country.

    Without that check a name match alone is not evidence: "SAP" also finds other entities, and an
    ISIN pointing at the wrong paper would be worse than no ISIN at all. Two letters must agree, and
    the form must be exact - anything else returns "".
    """
    prefix = COUNTRY_ISIN_PREFIX.get(str(country or "").strip().casefold(), "")
    if not prefix or not name:
        return ""
    key = company_key(name)
    with closing(store.connect(path)) as conn:
        stored = store.read_company_isin(conn, key)
    if stored:
        return stored
    for term in isin_search_terms(name):
        found = _wikidata({"action": "wbsearchentities", "format": "json", "language": "de",
                           "uselang": "de", "type": "item", "limit": 1, "search": term})
        hits = (found or {}).get("search") or []
        if not hits or not hits[0].get("id"):
            continue
        entity = _wikidata({"action": "wbgetentities", "format": "json", "props": "claims",
                            "ids": hits[0]["id"]})
        claims = (((entity or {}).get("entities") or {}).get(hits[0]["id"]) or {}).get("claims") or {}
        for claim in claims.get("P946") or []:
            value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or "")
            if isinstance(value, str) and ISIN_FORM.fullmatch(value) and value.startswith(prefix):
                store.write_company_isin(key, value, path)
                return value
    return ""


# Wikidata allows few requests and refuses the rest with 429 - asking in parallel makes it refuse
# more. So they are asked one at a time and never past the deadline: the answer is stored, the next
# search continues where this one stopped.
ISIN_DEADLINE_S = 2.0
# Yahoo's search carries an eight-second timeout of its own. A second question is only asked while
# the first answer is still cheap: past this, the reader would wait for two slow answers in a row.
UMLAUT_RETRY_BUDGET_S = 3.0


def verified_isin(raw, name, country, path=None):
    """An ISIN from any source, but only after the same check: form, and country prefix.

    Yahoo's `isin` is not trusted on its own - measured, it answers AR0725224551 for ASML where
    NL0010273215 is right. A raw value is taken only when it passes; otherwise the verified lookup
    decides, and an empty string is better than a wrong number.
    """
    prefix = COUNTRY_ISIN_PREFIX.get(str(country or "").strip().casefold(), "")
    if prefix and isinstance(raw, str) and ISIN_FORM.fullmatch(raw) and raw.startswith(prefix):
        return raw
    return company_isin(name, country, path)


def symbol_root(symbol):
    """The paper's own code without the venue: "9PDA.DU" -> "9PDA", "AAPL" -> "AAPL"."""
    return (symbol or "").split(".")[0].upper()


# The German boards Yahoo codes with a suffix: Xetra, Frankfurt, Düsseldorf, Munich, Stuttgart,
# Berlin, Hamburg, Hanover. A paper traded on two of them carries the same local code on both.
GERMAN_BOARDS = {"DE", "F", "DU", "MU", "SG", "BE", "HM", "HA"}


def board_of(symbol):
    """The venue suffix of a Yahoo symbol: "9PDA.DU" -> "DU", "AAPL" -> ""."""
    return (symbol or "").rsplit(".", 1)[-1].upper() if "." in (symbol or "") else ""


def merge_german_boards(groups):
    """A paper on two German boards is one company, even when the two sources name it differently.

    Measured on "pdd": Yahoo answered PDD (NASDAQ) with 9PDA.F (Frankfurt) behind it, and the same
    answer carried 9PDA.DU (Düsseldorf) as a second company called "Pinduoduo Inc". Neither carries
    an ISIN, so the ISIN merge cannot join them.

    Only German boards are joined by their code, and only against a board that already sits behind
    another company: "9PDA" is the local code of PDD, while "SAP.TO" belongs to Saputo Inc. and must
    not be folded into SAP SE, whose own listings share the root "SAP".
    """
    roots = {}
    for group in groups:
        for listing in group.get("listings", [])[1:]:
            if board_of(listing["symbol"]) in GERMAN_BOARDS:
                roots.setdefault(symbol_root(listing["symbol"]), group)
    kept = []
    for group in groups:
        listings = group.get("listings", [])
        target = None
        if len(listings) == 1 and board_of(listings[0]["symbol"]) in GERMAN_BOARDS:
            target = roots.get(symbol_root(listings[0]["symbol"]))
        if target is None or target is group:
            kept.append(group)
            continue
        known = {listing["symbol"] for listing in target["listings"]}
        target["listings"] += [listing for listing in listings if listing["symbol"] not in known]
    return kept


def _order_listings(group):
    """The home listing first, then the German boards, then the rest - the order the chips show.

    The home listing carries the numbers; a German board is the one a reader here can actually
    trade; everything else is a foreign reseller.
    """
    listings = group.get("listings", [])
    if len(listings) < 2:
        return
    lead = listings[0]
    rest = sorted(listings[1:], key=lambda listing: (board_of(listing["symbol"]) not in GERMAN_BOARDS,
                                                     listing["symbol"]))
    group["listings"] = [lead] + rest


def merge_same_isin(rows):
    """Two rows with the same ISIN are one security, so they become one row.

    Measured: the search for "apple" answered AAPL (NASDAQ) and APC.DE (Germany) side by side, both
    US0378331005. The ISIN arrives after the grouping, so it is the second chance to notice.
    """
    by_isin = {}
    order = []
    for row in rows:
        isin = row.get("isin") or ""
        if not isin:
            order.append(row)
            continue
        if isin not in by_isin:
            by_isin[isin] = row
            order.append(row)
            continue
        kept = by_isin[isin]
        if (row.get("volume") or 0) > (kept.get("volume") or 0):
            # The liquid listing carries the row: its venue, its currency, its volume.
            for field in ("symbol", "name", "exchange", "currency", "volume"):
                kept[field] = row[field]
        known = {listing["symbol"] for listing in kept["listings"]}
        kept["listings"] += [listing for listing in row["listings"] if listing["symbol"] not in known]
    return order


def group_matches(matches, bucket, lookup=True, path=None, deadline=None):
    """One entry per company, its other listings kept behind it.

    A share and its ADR are the same company in another unit, and four German boards resell the
    Paris price. Showing six rows of "L'Oréal S.A." leaves the reader to guess which one the numbers
    belong to - the home listing is the answer, and the rest follow on request.
    """
    groups = {}
    order = []
    for match in matches:
        key = company_key(match.get("name") or match.get("symbol") or "")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(match)
    work = []
    asked = set()
    for key in order:
        for listing in groups[key][:LEAD_LOOKUPS]:
            if listing["symbol"] in asked:
                continue    # one question per symbol: two groups can share a listing
            asked.add(listing["symbol"])
            work.append((key, listing))
    # Six at a time: enough to keep a search short, few enough that Yahoo does not answer with 429.
    # The same symbol is asked once - two groups can carry the same listing.
    if work:
        # Not a `with` block: its exit waits for every task, which would undo the limit below. A
        # listing that did not answer in time shows no figures instead of holding the search.
        pool = ThreadPoolExecutor(max_workers=FACT_WORKERS)
        try:
            running = {item[1]["symbol"]: pool.submit(gated_facts, item[1]["symbol"], bucket)
                       for item in work}
            answers = []
            for item in work:
                try:
                    answers.append(running[item[1]["symbol"]].result(timeout=FACT_TIMEOUT_S))
                except Exception:
                    answers.append({})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        by_symbol = {item[1]["symbol"]: answer for item, answer in zip(work, answers, strict=True)}
    else:
        by_symbol = {}

    result = []
    pending = []        # (row index, raw isin, name, country) - filled after the loop, in parallel
    for key in order:
        listings = groups[key]
        # The scanner hands the figures with its rows; asking Yahoo for them anyway would make a
        # search wait on a second source that has nothing to add.
        if lookup:
            # Looked up by symbol, not by position: one shared symbol is asked once, so the answers
            # are compact and would otherwise sit beside the wrong listing.
            facts = [by_symbol.get(listing["symbol"], {}) for listing in listings[:LEAD_LOOKUPS]]
            facts += [{}] * (len(listings) - len(facts))
        else:
            facts = [{field: listing[field] for field in ("currency", "volume", "exchange_code",
                                                          "reporting_currency", "country", "isin")
                      if listing.get(field) not in (None, "")} for listing in listings]
        index = lead_index(listings, facts)
        lead = listings[index]
        lead_facts = facts[index]
        result.append({"symbol": lead["symbol"],
                       "name": lead.get("name") or lead_facts.get("name") or lead["symbol"],
                       # A venue the source did not name is better than an empty column: the code
                       # from Yahoo ("GER", "NMS") at least says where the price comes from.
                       "exchange": lead.get("exchange") or lead_facts.get("exchange_code") or "",
                       "currency": lead_facts.get("currency", ""),
                       "volume": lead_facts.get("volume"),
                       "isin": "",
                       "listings": [{"symbol": listing["symbol"],
                                     "exchange": listing.get("exchange") or ""}
                                    for listing in listings]})
        if lookup:
            pending.append((len(result) - 1, lead_facts.get("isin"), result[-1]["name"],
                            lead_facts.get("country") or ""))
    if pending:
        # Every row is offered to the lookup: a stored answer costs a millisecond, and only the
        # uncached ones can take time. The deadline stops those - the next search picks up the rest,
        # because what was answered is stored. A caller that fills several lists passes one deadline
        # along, so the budget is spent once, not once per list.
        deadline = deadline if deadline is not None else monotonic() + ISIN_DEADLINE_S
        for index, raw, name, country in pending:
            if monotonic() > deadline:
                break
            result[index]["isin"] = verified_isin(raw, name, country, path)
    return merge_same_isin(result)


def scanner_matches(query, nas):
    """The scanner's own rows - symbol, name, the venue it knows. Nothing else.

    Useful for a paper Yahoo does not find, useless as a row on its own: it carries no currency, no
    volume and no ISIN, and two spellings of one company become two rows.
    """
    if nas is None or len(query) > 50:
        return []
    matches = []
    seen = set()
    try:
        rows = nas.search(query)
    except (URLError, OSError, ValueError, EOFError):
        # A failing scanner must not take the search down: Yahoo answers alone.
        return []
    for row in rows:
        symbol = row.get("symbol", "") if isinstance(row, dict) else ""
        if (not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or symbol in seen
                or row.get("industry") == "Exchange Traded Fund"):
            continue
        seen.add(symbol)
        matches.append({"symbol": symbol, "name": row.get("name") or symbol,
                        "exchange": row.get("exchange") or row.get("market") or ""})
    return matches


def yahoo_matches(query):
    """Every quote Yahoo knows for the name, one entry per symbol."""
    matches = []
    seen = set()
    for row in yf.Search(query, max_results=20, news_count=0, timeout=8).quotes:
        symbol = row.get("symbol", "")
        if row.get("quoteType") != "EQUITY" or not SYMBOL.fullmatch(symbol) or symbol in seen:
            continue
        seen.add(symbol)
        matches.append({"symbol": symbol,
                        "name": row.get("longname") or row.get("shortname") or symbol,
                        "exchange": row.get("exchDisp") or row.get("exchange") or ""})
    return matches


def plain_query(query):
    """The same name without umlauts: "Münchener Rück" -> "Muenchener Rueck".

    Measured against Yahoo's search: the umlaut spelling of a German company returns nothing
    ("Münchener Rück", "Müller"), while the transliteration finds it (seven rows, MUV2.DE first).
    The umlaut form is asked first, so a name Yahoo does know keeps its exact answer.
    """
    return (query or "").translate({ord("ä"): "ae", ord("ö"): "oe", ord("ü"): "ue",
                                    ord("Ä"): "Ae", ord("Ö"): "Oe", ord("Ü"): "Ue",
                                    ord("ß"): "ss"})


def search(query, nas=None, bucket=None):
    """One row per company, whichever source knows it.

    Yahoo and the scanner are asked at the same time: one after the other would make every search
    wait for the scanner's answer before Yahoo is even asked, and the scanner has an 8 s timeout of
    its own. Yahoo carries the currency, the volume and the other venues; the scanner adds papers
    Yahoo did not return, and a scanner row whose symbol already sits among a company's venues is
    that venue, not a second company - measured, "asml" produced a bare "ASML" row beside the
    complete "ASML.AS" one.
    """
    normalized = query.upper()
    fallback = [SAP] if normalized in {"SAP", "SAP SE", "SAP.DE", "DE0007164600", "716460"} else []
    failure = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        scanner_job = pool.submit(scanner_matches, query, nas)
        started = monotonic()
        try:
            found = yahoo_matches(query)
            yahoo_answered = True
        except Exception as error:
            found, yahoo_answered, failure = [], False, error
        if yahoo_answered and not found:
            # A German name with umlauts is often unknown to Yahoo's search in that spelling:
            # measured, "Münchener Rück" returns nothing while "Muenchener Rueck" returns seven rows.
            #
            # The retry is only worth it when the first question came back quickly. A Yahoo that
            # needed seconds for its empty answer would need them again, and the reader would wait
            # twice for one search (review finding).
            plain = plain_query(query)
            if plain != query and monotonic() - started < UMLAUT_RETRY_BUDGET_S:
                try:
                    found = yahoo_matches(plain)
                except Exception:
                    pass          # the umlaut form answered; a failing retry adds nothing
        scanned = scanner_job.result()
    # The ISIN budget starts once the sources have answered: a slow scanner or search would
    # otherwise eat the budget before the first lookup and every row would come back without ISIN.
    deadline = monotonic() + ISIN_DEADLINE_S

    if not yahoo_answered and not scanned:
        # The log has to name the reason: without it "offline" is a guess, and the reader cannot
        # tell a blocked feed from a Yahoo that answered with nothing.
        print(f"Namenssuche: Yahoo antwortete nicht ({type(failure).__name__}: {failure}).",
              flush=True)
        return {"matches": fallback,
                "notice": "Die Namenssuche ist zurzeit offline. Ein Börsenticker kann direkt geladen werden."}
    rows = found if yahoo_answered else []
    if fallback and SAP["symbol"] not in {row["symbol"] for row in rows}:
        rows = fallback + rows
    groups = group_matches(rows, bucket, deadline=deadline) if rows else []
    known = {listing["symbol"] for group in groups for listing in group["listings"]}
    extra = [row for row in scanned if row["symbol"] not in known]
    if extra:
        groups += group_matches(extra, bucket, deadline=deadline)
    # The two sources are grouped apart, so the same security can still sit in both: measured,
    # "apple" answered AAPL from Yahoo and APC.DE from the scanner, the same ISIN twice.
    groups = merge_same_isin(groups)
    # A second German board of a paper already in the list is a venue of it, not a second company:
    # measured, "pdd" showed PDD with 9PDA.F and, next to it, "Pinduoduo Inc" with 9PDA.DU.
    groups = merge_german_boards(groups)
    for group in groups:
        _order_listings(group)
    if not groups:
        # The sources answered and know nothing by that name - that is not an outage, and calling it
        # one sends the reader looking for a bug that is not there.
        #
        # With the SAP fallback in hand the notice must not claim that nothing was found while the
        # row sits right below it (review finding). The row is the answer; only the hint stays.
        hint = "Ein Börsenticker lässt sich direkt eingeben."
        return {"matches": fallback,
                "notice": hint if fallback else
                          f"Keine Quelle kennt eine Aktie namens „{query}“. {hint}"}
    # The label says who answered: a fallback row must not be sold as a Yahoo answer.
    source = [label for label, used in (("xang auf NAS", scanned),
                                        ("Yahoo Finance", yahoo_answered and found)) if used]
    return {"matches": groups, "source": " + ".join(source)}


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


EXPORT_BLOCK = "Standardauswertung"
CURRENCY_MISMATCH = "Die Quelle nennt eine andere Währung als der Abschluss."


def export_notes(symbol):
    """Why the standardised statements are missing, in the wording of the data-gap block."""
    notes = []
    for _year, _item, reason in export_gaps(symbol)[:3]:
        notes.append({"block": EXPORT_BLOCK, "source": "FinancialFilings", "have": 0, "need": 1,
                      "reason": reason})
    return notes


RISK_WINDOW_DAYS = 756          # three trading years
RISK_MIN_RETURNS = 61           # below three months a volatility is a guess
# The number of closes is not a span: a paper listed five months ago can collect 62 of them. The
# label says three years, so the series has to cover them - 2.5 years leaves room for holidays.
RISK_MIN_SPAN_DAYS = int(2.5 * 365)


def spans_the_years(risk, minimum_days=RISK_MIN_SPAN_DAYS):
    """Whether a provided risk figure really covers the years its label claims.

    Asked of the secondary source as well: its answer carries `years: 3` while the window it reports
    ran from 2025-11-21 to 2026-11-20 - one year behind a three-year label.
    """
    try:
        start = date.fromisoformat(str(risk["start"])[:10])
        end = date.fromisoformat(str(risk["end"])[:10])
    except (KeyError, TypeError, ValueError):
        return False
    return (end - start).days >= minimum_days


def price_risk(prices):
    """Volatility, largest drawdown and annualised return from the daily closes on the page.

    Computed here rather than asked for: the page already holds the series, and a secondary source
    that keeps daily prices for only a fraction of the papers (measured 7 of 14) would leave every
    other header without the two figures - or with a window that is not the three years its label
    claims.
    """
    # A price row is [day, close, ...] with more fields behind it - take the two that matter and
    # leave the rest alone, so a wider row later does not break the series.
    closes = [(row[0], float(row[1])) for row in (prices or [])[-RISK_WINDOW_DAYS:]
              if isinstance(row, (list, tuple)) and len(row) > 1 and row[1]]
    if len(closes) < RISK_MIN_RETURNS + 1:
        return None
    returns = [math.log(closes[i][1] / closes[i - 1][1])
               for i in range(1, len(closes)) if closes[i - 1][1] > 0 and closes[i][1] > 0]
    if len(returns) < RISK_MIN_RETURNS:
        return None
    try:
        span = (date.fromisoformat(closes[-1][0]) - date.fromisoformat(closes[0][0])).days
    except (TypeError, ValueError):
        return None
    if span < RISK_MIN_SPAN_DAYS:
        return None
    peak, peak_day = closes[0][1], closes[0][0]
    drawdown, trough_day, trough_peak_day = 0.0, closes[0][0], closes[0][0]
    for day, value in closes:
        if value > peak:
            peak, peak_day = value, day
        if value / peak - 1 < drawdown:
            # The peak and the low belong together: storing only the day of the low would pair it
            # with a later, higher peak that never saw the drop.
            drawdown, trough_day, trough_peak_day = value / peak - 1, day, peak_day
    years = len(returns) / 252
    total = closes[-1][1] / closes[0][1]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return {"annualized_volatility": round(math.sqrt(variance) * math.sqrt(252), 6),
            "annualized_return": round(total ** (1 / years) - 1, 6) if years > 0 and total > 0 else None,
            "max_drawdown": {"value": round(drawdown, 6), "peak": trough_peak_day, "trough": trough_day},
            "returns": len(returns), "start": closes[0][0], "end": closes[-1][0],
            "source": "eigene Kurse"}


def same_fiscal_year(one, other):
    """Whether two period ends describe the same business year.

    A retailer closing on 2021-01-03 and a report ending 2020-12-31 are the same year. Keeping both
    would count it twice and shift every growth figure built on the series.
    """
    try:
        return abs((date.fromisoformat(one) - date.fromisoformat(other)).days) <= 45
    except (ValueError, TypeError):
        return False


def merge_fonrex(statements, extra):
    """Fill the annual figures the collector could not read; never overwrite a tagged value.

    Fonrex reads the same ESEF filings, so wherever paperbroker has a figure it stays. What arrives
    here are years that would otherwise be Yahoo years or missing altogether - and a year that is
    already there under a slightly different period end is enriched, not added a second time.
    """
    sections = ("income", "quarterly", "balance", "cash")
    # Everything else stays as it was - above all the provenance map merge_filings attached, which
    # the page reads to mark a figure as coming from the original reports (index.html:1345).
    result = dict(statements or {})
    for name in sections:
        rows = result.get(name)
        result[name] = {day: dict(row) for day, row in rows.items()} if isinstance(rows, dict) else {}
    for section in ("income", "balance", "cash"):
        for day, row in (extra.get(section) or {}).items():
            rows = result[section]
            twin = next((known for known in rows if same_fiscal_year(known, day)), None)
            target = rows.setdefault(twin or day, {})
            for item, value in row.items():
                target.setdefault(item, value)
    return {name: rows for name, rows in result.items() if rows or name not in sections}


def merge_export(statements, export):
    """Fill what the original reports and the aggregates left open; never overwrite them.

    The XLSX is a standardised re-reading of the same reports, so where a tagged value exists it
    stays. The quarterly figures are the exception in practice: no other source has them, so the
    derived quarters arrive here.
    """
    result = {name: {day: dict(row) for day, row in (statements.get(name) or {}).items()}
              for name in ("income", "quarterly", "balance", "cash")}
    for section in ("income", "quarterly", "balance", "cash"):
        for day, row in (export.get(section) or {}).items():
            target = result.setdefault(section, {}).setdefault(day, {})
            for item, value in row.items():
                target.setdefault(item, value)
    if statements.get("provenance"):
        result["provenance"] = statements["provenance"]
    return result


@lru_cache(maxsize=96)
def statement_profile(symbol, cutoff, bucket):
    if "." not in symbol:
        try:
            sec = load_sec_statements(symbol, date.fromisoformat(cutoff), bucket * 600)
        except (URLError, OSError, ValueError, TypeError, KeyError):
            sec = {}
        if sec.get("income") or sec.get("quarterly"):
            return {"statements": sec, "statements_source": "SEC EDGAR",
                    "info": {"financialCurrency": "USD"}, "statement_date_limit": cutoff,
                    "risk": fonrex.risk(symbol),
                    "data_gaps": statement_gaps(sec, "SEC EDGAR", False, filings_available())}
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
    # Fonrex only fills what the original reports could not deliver. For a German issuer it has
    # nothing (its own provider module says filings.xbrl.org carries no German filing), for a Dutch
    # or French one it has the official years - measured: ASML six against five from Yahoo.
    fonrex_reason = None
    # Asked when the reports are missing altogether or when there are too few of them: four years
    # give a different score from six, and the block should say whether the gap could be closed.
    if not official or reported_years(statements) < REPORTED_YEARS_MIN:
        extra, extra_currency, fonrex_reason = fonrex.esef_sections(symbol, cutoff)
        if extra and (not currency or not extra_currency or extra_currency == currency):
            statements = merge_fonrex(statements, extra)
            currency = currency or extra_currency
        else:
            fonrex_reason = fonrex_reason or CURRENCY_MISMATCH
    supplement = {}
    if filings_available():
        try:
            supplement, export_currency = statement_export(symbol, cutoff, currency, bucket, isin)
            if export_currency and currency and export_currency != currency:
                supplement = {}
        except (ParseError, URLError, OSError, ValueError, TypeError, KeyError):
            supplement = {}
    if supplement:
        statements = merge_export(statements, supplement)
    gaps = statement_gaps(statements, None, official, filings_available())
    gaps.extend(export_notes(symbol))
    gaps.extend(fonrex.notes(symbol, isin, fonrex_reason))
    return {"statements": statements,
            "risk": fonrex.risk(symbol, isin),
            "statements_source": statement_source(statements, official,
                                                  quarterly_export=bool((supplement or {}).get("quarterly")),
                                                  fonrex_years=official is False and not fonrex_reason),
            "info": {"financialCurrency": currency} if currency else {}, "statement_date_limit": cutoff,
            "data_gaps": gaps}


# The two sources rarely close the same day: the scanner on the NAS has Friday's close while
# Yahoo still ends on Thursday. Demanding the same day threw the whole eighteen-year series away -
# and with it the ten-year performance and every dividend figure. A week covers a weekend and a
# holiday; anything older is a genuinely stale series and still refuses.
CUTOFF_TOLERANCE_DAYS = 7


def recent_enough(last_day, cutoff):
    """Whether a series ending on last_day still belongs to this cutoff."""
    try:
        distance = (date.fromisoformat(cutoff) - date.fromisoformat(last_day)).days
    except (ValueError, TypeError):
        return False
    return 0 <= distance <= CUTOFF_TOLERANCE_DAYS


def with_statements(result, bucket):
    cutoff = result["prices"][-1][0]
    profile = statement_profile(result["symbol"], cutoff, bucket)
    return {**result, **profile, "info": {**result["info"], **profile["info"]}}


@lru_cache(maxsize=32)
def _stock_extra_cached(symbol, bucket, cutoff, currency, history_only=False):
    ticker = yf.Ticker(symbol)
    issues = []
    rows = []
    info = {}
    try:
        candidate = [row for row in load_history(ticker, "max") if row[0] <= cutoff]
        price_currency = ticker.history_metadata.get("currency")
        if candidate and recent_enough(candidate[-1][0], cutoff) and price_currency \
                and (not currency or currency == price_currency):
            rows = candidate
            info["currency"] = price_currency
        elif candidate:
            issues.append("Die Yahoo-Kursreihe endet zu früh für den NAS-Stichtag oder nennt "
                          "eine andere Notierungswährung.")
        else:
            issues.append("Die ergänzende Yahoo-Kursreihe ist momentan nicht verfügbar.")
    except Exception:
        issues.append("Die ergänzende Yahoo-Kursreihe ist momentan nicht verfügbar.")
    # The same way in as the non-NAS path: SEC for US issuers and the original reports for
    # everything else. Loading Yahoo directly here skipped both - which is why a key never helped
    # on an instance whose prices come from the scanner.
    # statement_profile counts its bucket in ten-minute steps (it multiplies by 600 for the
    # daily SEC keys); this function's own bucket rotates every fifteen minutes.
    profile = {} if history_only else statement_profile(symbol, cutoff, int(time() // 600))
    statements = profile.get("statements") or {}
    info = {**info, **(profile.get("info") or {})}
    if not history_only and not (statements.get("income") or {}):
        issues.append("Yahoo Finance hat keine Jahres-GuV geliefert.")
    return {"symbol": symbol, "prices": rows, "info": info, "statements": statements, "issues": issues,
            "price_source": "Yahoo Finance" if rows else None,
            # The page's own closes give the three years its label promises; the secondary source
            # is the fallback for a series too short to say anything.
            "risk": (price_risk(rows)
                     or (profile.get("risk") if spans_the_years(profile.get("risk") or {}) else None)),
            "data_gaps": profile.get("data_gaps") or [],
            "statements_source": profile.get("statements_source")}


_EXTRA_GUARD = Lock()
_EXTRA_LOCKS = {}


def stock_extra(symbol, bucket, cutoff, currency, history_only=False):
    """The Yahoo supplement of one ticker, downloaded once for concurrent callers.

    ``lru_cache`` alone does not coalesce concurrent cold calls: N requests for the same
    ticker would each run ``yf.Ticker(...).history(period="max")`` beside each other, which
    is the concurrency hole ``prices()`` closes with its per-key Flight. The page now asks
    for the supplement on every NAS load (one view, the complete one), so the same ticker
    can be asked for twice in a row - by two tabs, a reload, or a reader clicking the
    ticker again. The lock is per key, so different tickers still load in parallel.
    """
    key = (symbol, bucket, cutoff, currency, history_only)
    with _EXTRA_GUARD:
        flight = _EXTRA_LOCKS.setdefault(key, Flight())
        flight.waiting += 1
    try:
        with flight.lock:
            # Inside the lock the cache decides: the first caller downloads, the others find
            # their answer there and spend no request.
            return _stock_extra_cached(symbol, bucket, cutoff, currency, history_only)
    finally:
        with _EXTRA_GUARD:
            flight.waiting -= 1
            if flight.waiting <= 0:
                _EXTRA_LOCKS.pop(key, None)


# The cache helpers stay reachable under the public name: the NAS check list clears the
# cache between its cases, and a test that swaps the cache would otherwise test nothing.
stock_extra.cache_clear = _stock_extra_cached.cache_clear  # type: ignore[attr-defined]
stock_extra.cache_info = _stock_extra_cached.cache_info  # type: ignore[attr-defined]


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
                self.json(200, search(query, self.nas, int(time() // 600)))
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
                else:
                    result = stock(symbols[0], bucket)
                    if self.nas is not None:
                        result = {**result, "issues": ["Keine NAS-Tageskurse für diesen Ticker.", *result["issues"]]}
                # Read here, not inside stock(): the second source is not part of the cached payload.
                # A new dict, because the object stock() returns belongs to the cache - writing into it
                # would hand every later caller the answer of this one.
                result = with_second_source(symbols[0], result)
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
        except UnknownSymbol as error:
            # 404, not 502: the source answered, it just does not know this ticker. The candidates
            # travel with it, so the page can offer them instead of leaving the reader at a dead end.
            self.json(404, {"error": str(error), "suggestion": error.suggestion,
                            "suggestions": error.candidates})
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
