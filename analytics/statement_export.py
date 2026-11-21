"""The provider's standardised statements as an XLSX, read into our sections.

The export costs no credits and carries an annual and an interim sheet per statement, so the
quarterly figures the other sources lack are reachable from the reports themselves. It is asked
once per symbol and kept; a page view never waits twice for the same file. The labels of the
provider are ignored - every figure is stored under its period end, because a third party's
quarter label can be off by a year and still look plausible.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from contextlib import closing
from datetime import date, datetime
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile

import store
from openpyxl import load_workbook

API = "https://api.financialreports.eu"
EXPORT = "https://financialfilings.com/de/companies/api/company/{company}/financials/export.xlsx"
SIZE_LIMIT = 8_000_000
TIMEOUT = 12

# One download per symbol and week. The figures are stored, so a second look costs nothing; a
# provider that answers with 429 must not be asked again on every cold cache key.
# Ten exports a day are allowed, so a weekly refresh of a whole universe is out of reach. A quarter
# only changes four times a year, so a quarter of a year is a fair lifetime for one reading.
EXPORT_MAX_AGE_DAYS = 90
EXPORT_PER_DAY = 10
# After a failed attempt the provider is left alone for this long. Without it every cold cache
# key would knock again, and a provider that answers 429 stays angry.
EXPORT_RETRY_MINUTES = 30
FOUR_QUARTERS = 8

BUDGET_REASON = "Das Tagesbudget von zehn Auswertungen ist verbraucht; morgen wieder."
ANNUAL = {"income": "Income (Annual)", "balance": "Balance (Annual)", "cash": "Cash Flow (Annual)"}
INTERIM = {"income": "Income (Interim)", "balance": "Balance (Interim)", "cash": "Cash Flow (Interim)"}

# The provider's line items, mapped onto the names the rest of the app already uses. Only items
# that have a counterpart there are read; everything else stays in the file.
ITEMS = {
    "Total Revenues": "totalrevenue",
    "Cost Of Revenue": "costofrevenue",
    "Gross Profit": "grossprofit",
    "Selling General & Admin Exp": "sellinggeneraladmin",
    "Operating Income": "operatingincome",
    "EBIT": "ebit",
    "EBITDA": "ebitda",
    "EBT Excl Unusual Items": "pretaxincome",
    "Income Tax Expense": "taxprovision",
    "Net Income": "netincome",
    "Basic EPS": "basiceps",
    "Diluted EPS": "dilutedeps",
    "Cash And Equivalents": "cashandcashequivalents",
    "Total Current Assets": "currentassets",
    "Total Current Liabilities": "currentliabilities",
    "Total Non-Current Liabilities": "totalnoncurrentliabilities",
    "Total Liabilities": "totalliabilities",
    "Total Equity": "stockholdersequity",
    "Total Assets": "totalassets",
    "Tangible Book Value": "tangiblebookvalue",
    "Total Debt": "totaldebt",
    "Net Debt": "netdebt",
    "Cash from Ops": "operatingcashflow",
    "Capital Expenditure": "capitalexpenditure",
    "Cash from Investing": "investingcashflow",
    "Cash from Financing": "financingcashflow",
    "Total Dividends Paid": "dividendspaid",
    "Cash Interest Paid": "interestpaid",
}
# Per-share figures and ratios are not money and must not be scaled by the unit of the sheet.
NOT_MONEY = {"basiceps", "dilutedeps", "effectivetaxrate", "payoutratio", "returnoninvestment"}
MONEY_SCALES = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}
CUMULATIVE = ("FY", "H1", "9M")
QUARTER_END = {"Q1": "03-31", "H1": "06-30", "Q2": "06-30", "9M": "09-30", "Q3": "09-30", "FY": "12-31",
               "Q4": "12-31"}


def money_scale(units):
    """The scale the sheet declares on the cover, e.g. 'Millions, except per-share figures'."""
    text = (units or "").lower()
    for name, factor in MONEY_SCALES.items():
        if name.rstrip("s") in text:
            return factor
    return 1.0


def _cell_rows(sheet):
    rows = list(sheet.iter_rows(values_only=True))
    for index, row in enumerate(rows):
        if row and isinstance(row[0], str) and row[0].strip().lower() == "line item":
            return index, rows
    return None, rows


def _period_kind(label):
    """The reporting span a column names, e.g. 'H1 2024' -> ('H1', 2024)."""
    match = re.fullmatch(r"\s*(Q[1-4]|H1|9M|FY)\s*(\d{4})\s*", str(label or ""))
    return (match.group(1), int(match.group(2))) if match else (None, None)


def read_export(payload):
    """Every sheet of the export as periods, currencies and figures; labels are not trusted."""
    book = load_workbook(io.BytesIO(payload), data_only=True, read_only=True)
    cover = {}
    if "Cover" in book.sheetnames:
        for row in book["Cover"].iter_rows(values_only=True):
            values = [cell for cell in row if cell not in (None, "")]
            if len(values) >= 2 and isinstance(values[0], str):
                cover[values[0].strip().lower()] = values[1]
    scale = money_scale(cover.get("units"))
    report = {"annual": {}, "interim": {}, "cover": cover, "gaps": []}
    for blocks, sheets in (("annual", ANNUAL), ("interim", INTERIM)):
        for name in sheets.values():
            if name not in book.sheetnames:
                continue
            head, rows = _cell_rows(book[name])
            if head is None:
                continue
            labels, ends, currencies, sources = rows[head][1:], None, None, {}
            for row in rows[head + 1:]:
                if not row or not isinstance(row[0], str):
                    continue
                key = row[0].strip()
                if key.lower() == "period end":
                    ends = row[1:]
                elif key.lower() == "currency":
                    currencies = row[1:]
                elif key.lower() == "source filing":
                    for position in range(len(labels)):
                        if position + 1 < len(row) and row[position + 1]:
                            sources[position] = str(row[position + 1])
            if not ends:
                continue
            for index, label in enumerate(labels):
                span, year = _period_kind(label)
                end = ends[index] if index < len(ends) else None
                day = (end.date().isoformat() if isinstance(end, datetime)
                       else end.isoformat() if isinstance(end, date)
                       else str(end or "")[:10])
                if not span or not day:
                    continue
                currency = (currencies[index] if currencies and index < len(currencies) else None) or ""
                for row in rows[head + 1:]:
                    if not row or not isinstance(row[0], str):
                        continue
                    item = ITEMS.get(row[0].strip())
                    value = row[index + 1] if index + 1 < len(row) else None
                    if not item or not isinstance(value, (int, float)) or isinstance(value, bool):
                        continue
                    amount = float(value) if item in NOT_MONEY else float(value) * scale
                    bucket = report[blocks].setdefault(day, {"span": span, "year": year,
                                                             "currency": currency, "items": {},
                                                             "sources": {}})
                    bucket["items"][item] = amount
                    if sources.get(index):
                        bucket["sources"][item] = str(sources[index])
    book.close()
    return report


def quarters_from_interim(report, cutoff=None):
    """Single quarters from the cumulative interim columns - only where the chain closes.

    Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M. A year whose own annual column contradicts the
    interim chain is dropped instead of guessed: the provider's columns disagree with each other
    often enough to make that check worth its cost.
    """
    spans = {}
    for day, block in report["interim"].items():
        if cutoff and day > cutoff:
            continue
        spans.setdefault(block["year"], {})[block["span"]] = block
    annual = {}
    for block in report["annual"].values():
        annual[block["year"]] = block
    quarters = {}
    gaps = []
    for year, by_span in sorted(spans.items()):
        items = set()
        for block in by_span.values():
            items |= set(block["items"])
        if "Q1" not in by_span or "H1" not in by_span:
            continue
        own_year = annual.get(year)
        for item in sorted(items):
            chain = {"Q1": by_span["Q1"]["items"].get(item)}
            cumulative = {span: by_span.get(span, {}).get("items", {}).get(item) for span in ("H1", "9M", "FY")}
            # Some issuers publish no year-end column among the interim sheets. The annual sheet
            # is then the published year, and Q4 = year - 9M is still a subtraction of two
            # reported figures rather than an estimate.
            if cumulative["FY"] is None:
                cumulative["FY"] = (own_year or {}).get("items", {}).get(item)
            if chain["Q1"] is None or cumulative["H1"] is None:
                continue
            currency = by_span["Q1"]["currency"]
            if any(block["currency"] not in ("", currency) for block in by_span.values()):
                gaps.append((f"{year}", item, "Die Spalten nennen verschiedene Währungen."))
                continue
            derived = {"Q1": chain["Q1"], "Q2": cumulative["H1"] - chain["Q1"]}
            if cumulative["9M"] is not None:
                derived["Q3"] = cumulative["9M"] - cumulative["H1"]
            if cumulative["9M"] is not None and cumulative["FY"] is not None:
                derived["Q4"] = cumulative["FY"] - cumulative["9M"]
            # The cross-check needs a complete chain. A missing fourth quarter is a gap of the
            # source, not a contradiction - the quarters that exist are derived from published
            # cumulative figures and stay.
            published = (own_year or {}).get("items", {}).get(item)
            if "Q4" in derived:
                parts = [derived.get(q) for q in ("Q1", "Q2", "Q3", "Q4")]
                year_total = sum(part for part in parts if part is not None)
                if published is not None and abs(year_total - published) > abs(published) * 0.005:
                    gaps.append((f"{year}", item, "Die Quartale ergeben nicht das Jahr des Berichts."))
                    continue
            for span, amount in derived.items():
                if amount is None:
                    continue
                day = f"{year}-{QUARTER_END[span]}"
                if cutoff and day > cutoff:
                    continue
                quarters.setdefault(day, {})[item] = round(amount, 4 if item in NOT_MONEY else 2)
    return quarters, gaps


def sections_from_export(report, cutoff=None):
    """What the export contributes: the annual columns and the derived quarters."""
    sections = {}
    for section in ANNUAL:
        for day, block in report["annual"].items():
            if cutoff and day > cutoff:
                continue
            rows = {item: value for item, value in block["items"].items() if _belongs(section, item)}
            if rows:
                sections.setdefault(section, {})[day] = rows
    quarters, gaps = quarters_from_interim(report, cutoff)
    income_quarters = {day: {item: value for item, value in row.items() if _belongs("income", item)}
                       for day, row in quarters.items()}
    income_quarters = {day: row for day, row in income_quarters.items() if row}
    if income_quarters:
        sections.setdefault("quarterly", {}).update(income_quarters)
    return sections, gaps


INCOME_ITEMS = set(ITEMS.values()) - {"cashandcashequivalents", "totalcurrentassets",
                                     "totalcurrentliabilities", "totalnoncurrentliabilities",
                                     "totalliabilities", "stockholdersequity", "totalassets",
                                     "tangiblebookvalue", "totaldebt", "netdebt",
                                     "operatingcashflow", "capitalexpenditure", "investingcashflow",
                                     "financingcashflow", "dividendspaid", "interestpaid"}


def _belongs(section, item):
    return (item in INCOME_ITEMS) if section in ("income", "quarterly") else (item not in INCOME_ITEMS)


def company_id(symbol, isin=None):
    """The provider's own number for an issuer, asked once and then remembered."""
    key = os.environ.get("AKTIENANALYSE_FILINGS_KEY", "").strip()
    if not key:
        return None
    stored = store.stored_provider(symbol)
    if stored:
        return stored
    ticker, _, market = symbol.rpartition(".")
    query = {"isin": isin} if isin and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) else {
        "ticker": f"{ticker}.{market}" if market else ticker}
    try:
        url = f"{API}/companies/?{urlencode({**query, 'page_size': 2})}"
        with urlopen(Request(url, headers={"X-API-Key": key, "Accept": "application/json"}), timeout=TIMEOUT) as r:
            import json
            found = json.load(r).get("results") or []
    except (HTTPError, URLError, OSError, ValueError):
        return None
    if len(found) != 1 or not isinstance(found[0].get("id"), int):
        return None
    store.remember_provider(symbol, found[0]["id"])
    return found[0]["id"]


def download(company):
    """The export for one company, straight from the provider. One attempt, no retry storm."""
    key = os.environ.get("AKTIENANALYSE_FILINGS_KEY", "").strip()
    if not key:
        return b""
    url = EXPORT.format(company=company)
    try:
        with urlopen(Request(url, headers={"X-API-Key": key, "User-Agent": "paperbroker/1"}), timeout=TIMEOUT) as r:
            payload = r.read(SIZE_LIMIT + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return b""
    return payload if len(payload) <= SIZE_LIMIT else b""


def _stored(symbol):
    """What the store already holds, so a second look never downloads again."""
    with closing(store.connect()) as conn:
        sections = store.read_figures(conn, symbol, max_age_h=EXPORT_MAX_AGE_DAYS * 24,
                                      sections=("income", "quarterly", "balance", "cash"))
    return sections, None


@lru_cache(maxsize=64)
def statement_export(symbol, cutoff, currency, bucket=0, isin=None):
    """The export's contribution for one symbol, downloaded at most once a week."""
    if not os.environ.get("AKTIENANALYSE_FILINGS_KEY", "").strip():
        return {}, None
    stored, stored_currency = _stored(symbol)
    if store.export_is_fresh(symbol, EXPORT_MAX_AGE_DAYS):
        return stored, stored_currency or currency
    company = company_id(symbol, isin)
    if not company:
        return stored, stored_currency or currency
    if store.export_attempted_within(symbol, EXPORT_RETRY_MINUTES):
        return stored, (stored_currency or currency) if stored else None
    if not store.budget_take(EXPORT_PER_DAY):
        # The provider allows ten files a day. A page view must not spend the last one on a
        # failure, and it must say why the figures are missing instead of staying silent.
        store.remember_export(symbol, company, "", [("", "", BUDGET_REASON)])
        return stored, (stored_currency or currency) if stored else None
    payload = download(company)
    if not payload:
        store.budget_release()
        store.remember_export(symbol, company, "", [("", "", "Der Abruf ist fehlgeschlagen.")])
        return stored, (stored_currency or currency) if stored else None
    try:
        report = read_export(payload)
    except (BadZipFile, OSError, ValueError, KeyError, TypeError):
        store.budget_release()
        return stored, (stored_currency or currency) if stored else None
    sections, gaps = sections_from_export(report, cutoff)
    if not sections:
        return stored, (stored_currency or currency) if stored else None
    report_currency = next((block["currency"] for block in report["annual"].values() if block["currency"]), None)
    report_currency = report_currency or next((block["currency"] for block in report["interim"].values()
                                               if block["currency"]), None)
    if currency and report_currency and currency != report_currency:
        return stored, (stored_currency or currency) if stored else None
    with closing(store.connect()) as conn:
        store.write_figures(conn, symbol, sections, source="xlsx_export", currency=report_currency)
    store.remember_export(symbol, company, hashlib.sha256(payload).hexdigest(), gaps)
    merged, merged_currency = _stored(symbol)
    return merged or sections, report_currency or merged_currency or currency


def export_gaps(symbol):
    """What the export itself could not close, as stored when the file was read."""
    return store.stored_export_gaps(symbol)
