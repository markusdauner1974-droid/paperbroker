"""Fonrex as a supplementary source for the stock analysis.

Why this exists
---------------
The criteria are built on the numbers of the original reports, and that order does not change. Two
things are missing there, though:

* An official annual series for a European issuer whose package the collector could not read.
  Measured on 2026-11-22: ASML fell back to five Yahoo years while Fonrex holds six official ESEF
  years with twenty-five figures. For German issuers Fonrex has nothing - its own provider module
  states that ``filings.xbrl.org`` carries no German filing - so nothing is gained there, and
  nothing is claimed.
* Any risk figure at all. ``/risk`` returns the annualised volatility, the largest drawdown and the
  annualised return over three years, addressed by ISIN, so the broken exchange in the catalogue
  does not matter.

What is deliberately not taken
------------------------------
Prices: ``/quote`` is Yahoo with a fifteen-minute delay and ``/eod`` is bounded by what Fonrex has
stored (SAP returned 253 points even for ``period=5y``). Fundamentals: they come from thirteen
scrapers, and a number whose origin cannot be named does not belong in a criterion.

Every answer is cached in SQLite, and every failure is a named reason, never an exception: a page
that cannot reach Fonrex looks exactly like a page that never asked.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import store

BASE = os.environ.get("AKTIENANALYSE_FONREX_URL", "http://fonrex-api:5000").rstrip("/")
TIMEOUT = float(os.environ.get("AKTIENANALYSE_FONREX_TIMEOUT_S", "8"))
ESEF_MAX_AGE_DAYS = 90
RISK_MAX_AGE_DAYS = 7
# A service that is down stays down for a while. Without this every cold profile blocked its request
# thread on two eight-second timeouts, once per symbol.
DOWN_MINUTES = 5
DOWN_KEY = "|down"

# The ticker reaches the request path, so it is checked here as well as in the caller: the page's
# symbol pattern allows dots, and ".." would walk to another endpoint.
SYMBOL_RE = re.compile(r"^[A-Za-z0-9^=\-][A-Za-z0-9.^=\-]{0,23}$")
# The key is sent as a header. Over an internal name or a private address that is a short hop inside
# one Docker network; to a public host it would cross the internet in clear text.
PRIVATE_HOST_RE = re.compile(
    r"^(localhost|[a-z0-9_-]+|127\.[0-9.]+|10\.[0-9.]+|192\.168\.[0-9.]+"
    r"|172\.(1[6-9]|2[0-9]|3[01])\.[0-9.]+)$")

SYMBOL_INVALID = "Der Tickersymbol ist für diese Abfrage nicht zulässig."


class _HttpFailed:
    """The service answered, just not for this resource (404, 500, unreadable body).

    Kept apart from a transport failure: one missing report must not mark the whole service dead.
    """

    def __repr__(self):
        return "HTTP_FAILED"


HTTP_FAILED = _HttpFailed()
INSECURE_TARGET = "Fonrex ist nur über eine unverschlüsselte Adresse erreichbar; der Schlüssel bleibt zurück."

# Fonrex names its figures after the IFRS concepts; the page reads the vocabulary of the annual
# statements. Only what was measured to exist is mapped - no figure is invented from another.
FIGURE_MAP = {
    "income": {
        "revenue": "totalrevenue",
        "net_income": "netincome",
        "gross_profit": "grossprofit",
        "operating_income": "ebit",
        "interest_expense": "interestexpense",
        "tax_provision": "incometaxexpense",
        "eps_basic": "basiceps",
        "eps_diluted": "dilutedeps",
        "rd_expense": "researchdevelopment",
        "sga_expense": "sellinggeneraladministrative",
        "shares_basic": "basicaverageshares",
        "shares_diluted": "dilutedaverageshares",
    },
    "balance": {
        "total_assets": "totalassets",
        "total_equity": "stockholdersequity",
        "long_term_debt": "longtermdebt",
        "cash_and_equivalents": "cashandcashequivalents",
        "inventory": "inventory",
        "accounts_receivable": "netreceivables",
        "goodwill": "goodwill",
        "intangible_assets": "intangibleassets",
    },
    "cash": {
        "operating_cashflow": "totalcashfromoperatingactivities",
        "investing_cashflow": "totalcashfrominvestingactivities",
        "financing_cashflow": "totalcashfromfinancingactivities",
        "dividends_paid": "dividendspaid",
        "capex": "capitalexpenditures",
        "stock_repurchases": "repurchaseofcapitalstock",
    },
}
# Per-share and share-count figures keep their decimals; everything else is a currency amount.
NOT_MONEY = {"basiceps", "dilutedeps", "basicaverageshares", "dilutedaverageshares"}

UNREACHABLE = "Fonrex ist nicht erreichbar."
NO_KEY = "Kein Zugangsschlüssel für Fonrex hinterlegt."
# Measured 2026-11-22: Fonrex serves German issuers with quotes, technicals, risk and a Yahoo
# mirror of the statements - what it does not have are the ESEF reports. filings.xbrl.org itself
# returns count 0 for SAP's and Einhell's LEI, so the gap is the index, not the query.
NO_REPORTS = ("Fonrex führt zu diesem Emittenten keine ESEF-Berichte; Kennzahlen, Kurse und Risiko "
              "liefert es sehr wohl.")


def available() -> bool:
    """Whether a key is configured at all - without one every call would end in a 401."""
    return bool(os.environ.get("AKTIENANALYSE_FONREX_KEY", "").strip())


def permitted_target():
    """Whether the key may be sent to the configured address at all."""
    parts = urlsplit(BASE)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and bool(PRIVATE_HOST_RE.match(parts.hostname or ""))


def symbol_ok(ticker):
    """A ticker that is safe to place in a request path."""
    return bool(ticker) and bool(SYMBOL_RE.match(ticker)) and ".." not in ticker


def _get(path, params=None):
    """One answer from Fonrex, or None. Never raises: a page must survive a silent source."""
    if not available() or not permitted_target():
        return None
    url = BASE + path + ("?" + urlencode(params) if params else "")
    request = Request(url, headers={"X-API-KEY": os.environ["AKTIENANALYSE_FONREX_KEY"].strip(),
                                    "User-Agent": "paperbroker-analytics/1"})
    try:
        with urlopen(request, timeout=TIMEOUT) as answer:
            payload = answer.read()
    except HTTPError:
        return HTTP_FAILED
    except (URLError, OSError):
        return None
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return HTTP_FAILED


def _cached(key, max_age_days, read):
    """The stored answer while it is fresh, otherwise one request and a write.

    A failed look leaves a mark for a few minutes, so a service that is down costs one timeout and
    not one per symbol and bucket. The mark is only for a real transport failure: when nothing was
    even attempted - no key, an address the key may not travel to - there is nothing to remember.
    """
    if not available() or not permitted_target():
        return None
    with closing(store.connect()) as conn:
        stored = store.read_fonrex(conn, key, max_age_h=max_age_days * 24)
        down = store.read_fonrex(conn, DOWN_KEY, max_age_h=DOWN_MINUTES / 60)
    if stored is not None:
        return stored          # a warm answer survives even while the mark is up
    if down is not None:
        return None
    payload = read()
    if payload is None:
        store.write_fonrex(DOWN_KEY, {"down": True})
    elif isinstance(payload, dict):
        # Only an object is worth keeping: an answer of another shape is an anomaly, and caching it
        # would repeat the same unreadable answer for ninety days.
        store.write_fonrex(key, payload)
    return payload


def _value(figures, name):
    """One figure out of a year, or None - the answer may not have the shape promised."""
    entry = figures.get(name) if isinstance(figures, dict) else None
    value = entry.get("value") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def esef_sections(ticker, cutoff=None):
    """The annual figures Fonrex holds for a European issuer, in the shape of the page.

    Returns ``(sections, currency, reason)``. ``sections`` is empty when there is nothing to add -
    either because Fonrex has no reports for this issuer (every German one) or because it could not
    be asked. The reason says which of the two it was.
    """
    if not available():
        return {}, None, NO_KEY
    if not permitted_target():
        return {}, None, INSECURE_TARGET
    if not symbol_ok(ticker):
        return {}, None, SYMBOL_INVALID
    payload = _cached(f"esef:{ticker}", ESEF_MAX_AGE_DAYS,
                      lambda: _get(f"/esef/{quote(ticker, safe='')}/statements", {"years": 6}))
    if payload is HTTP_FAILED:
        # 404 or a body that is not JSON: this issuer has nothing under that address.
        return {}, None, NO_REPORTS
    if payload is not None and not isinstance(payload, dict):
        # An answer of the wrong shape is not "no reports" - it is a source we cannot read.
        return {}, None, UNREACHABLE
    years = (payload or {}).get("years")
    years = [year for year in years if isinstance(year, dict)] if isinstance(years, list) else []
    if not years:
        return {}, None, NO_REPORTS if payload is not None else UNREACHABLE
    currency = None
    sections = {"income": {}, "balance": {}, "cash": {}}
    for year in years:
        day = str(year.get("period_end") or "")[:10]
        if len(day) != 10 or (cutoff and day > cutoff):
            continue
        year_currency = year.get("currency")
        currency = currency or year_currency
        if year_currency and currency and year_currency != currency:
            continue
        figures = year.get("figures")
        for section, mapping in FIGURE_MAP.items():
            row = {}
            for source, target in mapping.items():
                value = _value(figures, source)
                if value is not None:
                    row[target] = round(value, 4 if target in NOT_MONEY else 2)
            # A year that carried none of the concepts we know must not become a phantom year: the
            # gap block would list it as a business year without revenue.
            if row:
                sections[section][day] = {**sections[section].get(day, {}), **row}
    sections = {name: rows for name, rows in sections.items() if rows}
    return sections, currency, None if sections else NO_REPORTS


def risk(ticker, isin=None):
    """The three-year risk figures of one instrument.

    Prefers the ISIN: the catalogue's ticker is wrong often enough (measured: XONA for Exxon, CCC3
    for Coca-Cola) that a ticker-only call would fail where the paper is perfectly well known.
    Without an ISIN the ticker is still tried, so a US listing keeps its risk figures.
    """
    if not available() or not symbol_ok(ticker):
        return None
    key = f"risk:{isin}" if isin else f"risk:{ticker}"
    payload = _cached(key, RISK_MAX_AGE_DAYS,
                      lambda: _get("/risk/" + quote(ticker, safe=""), {"isin": isin} if isin else None))
    return payload if isinstance(payload, dict) and payload.get("annualized_volatility") is not None else None


def notes(ticker, isin=None, reason=None):
    """Why Fonrex could not add years - one entry for the data-gap block.

    Only a reason from the report path is reported here. A missing risk figure stays visible by its
    absent cells; turning it into "unreachable" would call a source dead that had just delivered.
    """
    if reason in (None, NO_KEY):
        return []
    return [{"block": "Fonrex", "source": "Fonrex (ESEF, Risiko)", "have": 0, "need": 1,
             "reason": reason}]
