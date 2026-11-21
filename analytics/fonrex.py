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
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import store

BASE = os.environ.get("AKTIENANALYSE_FONREX_URL", "http://fonrex-api:5000").rstrip("/")
TIMEOUT = float(os.environ.get("AKTIENANALYSE_FONREX_TIMEOUT_S", "8"))
ESEF_MAX_AGE_DAYS = 90
RISK_MAX_AGE_DAYS = 7

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
NO_REPORTS = "Fonrex hat für diesen Titel keine Abschlüsse."


def available() -> bool:
    """Whether a key is configured at all - without one every call would end in a 401."""
    return bool(os.environ.get("AKTIENANALYSE_FONREX_KEY", "").strip())


def _get(path, params=None):
    """One answer from Fonrex, or None. Never raises: a page must survive a silent source."""
    if not available():
        return None
    url = BASE + path + ("?" + urlencode(params) if params else "")
    request = Request(url, headers={"X-API-KEY": os.environ["AKTIENANALYSE_FONREX_KEY"].strip(),
                                    "User-Agent": "paperbroker-analytics/1"})
    try:
        with urlopen(request, timeout=TIMEOUT) as answer:
            payload = answer.read()
        return json.loads(payload)
    except (HTTPError, URLError, OSError, ValueError, TypeError):
        return None


def _cached(key, max_age_days, read):
    """The stored answer while it is fresh, otherwise one request and a write."""
    with closing(store.connect()) as conn:
        stored = store.read_fonrex(conn, key, max_age_h=max_age_days * 24)
    if stored is not None:
        return stored
    payload = read()
    if payload is not None:
        store.write_fonrex(key, payload)
    return payload


def esef_sections(ticker, cutoff=None):
    """The annual figures Fonrex holds for a European issuer, in the shape of the page.

    Returns ``(sections, currency, reason)``. ``sections`` is empty when there is nothing to add -
    either because Fonrex has no reports for this issuer (every German one) or because it could not
    be asked. The reason says which of the two it was.
    """
    if not available():
        return {}, None, NO_KEY
    payload = _cached(f"esef:{ticker}", ESEF_MAX_AGE_DAYS,
                      lambda: _get(f"/esef/{ticker}/statements", {"years": 6}))
    years = (payload or {}).get("years") or []
    if not years:
        return {}, None, NO_REPORTS if payload is not None else UNREACHABLE
    currency = None
    sections = {"income": {}, "balance": {}, "cash": {}}
    for year in years:
        day = (year.get("period_end") or "")[:10]
        if len(day) != 10 or (cutoff and day > cutoff):
            continue
        currency = currency or year.get("currency")
        if year.get("currency") and currency and year.get("currency") != currency:
            continue
        figures = year.get("figures") or {}
        for section, mapping in FIGURE_MAP.items():
            row = sections[section].setdefault(day, {})
            for source, target in mapping.items():
                value = (figures.get(source) or {}).get("value")
                if isinstance(value, (int, float)):
                    row[target] = round(float(value), 4 if target in NOT_MONEY else 2)
    sections = {name: rows for name, rows in sections.items() if rows}
    return sections, currency, None if sections else NO_REPORTS


def risk(ticker, isin=None):
    """The three-year risk figures of one instrument.

    Prefers the ISIN: the catalogue's ticker is wrong often enough (measured: XONA for Exxon, CCC3
    for Coca-Cola) that a ticker-only call would fail where the paper is perfectly well known.
    Without an ISIN the ticker is still tried, so a US listing keeps its risk figures.
    """
    if not available() or not ticker:
        return None
    key = f"risk:{isin}" if isin else f"risk:{ticker}"
    payload = _cached(key, RISK_MAX_AGE_DAYS,
                      lambda: _get("/risk/" + ticker.replace("/", ""), {"isin": isin} if isin else None))
    return payload if isinstance(payload, dict) and payload.get("annualized_volatility") is not None else None


def notes(ticker, isin=None, reason=None):
    """What Fonrex contributed or why it did not - one entry for the data-gap block."""
    if reason == NO_KEY:
        return []
    if reason is None and not risk(ticker, isin):
        reason = UNREACHABLE
    if reason is None:
        return []
    return [{"block": "Fonrex", "source": "Fonrex (ESEF, Risiko)", "have": 0, "need": 1,
             "reason": reason}]
