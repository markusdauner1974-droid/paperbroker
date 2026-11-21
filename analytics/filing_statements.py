"""Discover filings by identifier and read their original Inline XBRL tags."""

import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from threading import Lock
from time import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, is_zipfile

import store

API = "https://api.financialreports.eu"
CDN = "cdn.financialreports.eu"
SIZE_LIMIT = 24 * 1024 * 1024
IX = "{http://www.xbrl.org/2013/inlineXBRL}"
INSTANCE = "{http://www.xbrl.org/2003/instance}"
EU_MARKETS = {
    "DE": "DE", "F": "DE", "BE": "DE", "DU": "DE", "HM": "DE", "MU": "DE",
    "PA": "FR", "AS": "NL", "BR": "BE", "MC": "ES", "MI": "IT", "VI": "AT",
    "HE": "FI", "ST": "SE", "CO": "DK", "OL": "NO", "LS": "PT", "IR": "IE",
    "L": "GB", "SW": "CH", "WA": "PL", "PR": "CZ", "BD": "HU", "AT": "GR",
}
INCOME = {
    "Revenue": "totalrevenue",
    "GrossProfit": "grossprofit",
    "ProfitLossFromOperatingActivities": "operatingincome",
    "ProfitLossAttributableToOwnersOfParent": "netincome",
    "ProfitLossBeforeTax": "pretaxincome",
    "IncomeTaxExpenseContinuingOperations": "taxprovision",
    # Earnings per share belong here: the report tags them per share class, so the
    # basic and the diluted figure stay apart instead of being mixed.
    "BasicEarningsLossPerShare": "basiceps",
    "DilutedEarningsLossPerShare": "dilutedeps",
    # Some issuers report one figure for both classes when no dilutive instruments
    # exist. It is then the same number for basic and diluted, so it may fill the
    # diluted line; the label on the page stays the diluted one.
    "BasicAndDilutedEarningsLossPerShare": "dilutedeps",
}
BALANCE = {
    "Assets": "totalassets", "EquityAttributableToOwnersOfParent": "stockholdersequity",
    "NumberOfSharesOutstanding": "sharesoutstanding",
    "CurrentAssets": "currentassets", "CurrentLiabilities": "currentliabilities",
    "CashAndCashEquivalents": "cashandcashequivalents",
}
CASH = {"CashFlowsFromUsedInOperatingActivities": "operatingcashflow"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = build_opener(NoRedirect())


def read_bytes(url, key=None):
    headers = {"User-Agent": "Aktienanalyse/12", "Accept": "application/json" if key else "*/*"}
    if key:
        headers["X-API-Key"] = key
    with OPENER.open(Request(url, headers=headers), timeout=6) as response:
        payload = response.read(SIZE_LIMIT + 1)
    if len(payload) > SIZE_LIMIT:
        raise ValueError("Datei zu groß")
    return payload


def listing(path, **query):
    key = os.environ.get("AKTIENANALYSE_FILINGS_KEY", "").strip()
    url = f"{API}/{path}/?{urlencode(query)}"
    result = json.loads(read_bytes(url, key))
    if isinstance(result, dict):
        result = result.get("results")
    return result if isinstance(result, list) else []


def document_files(payload):
    archive = BytesIO(payload)
    if not is_zipfile(archive):
        yield payload
        return
    with ZipFile(archive) as z:
        members = [item for item in z.infolist()
                   if item.filename.lower().endswith((".xhtml", ".html", ".htm"))
                   and item.file_size <= SIZE_LIMIT]
        if not members or len(members) > 12 or sum(item.file_size for item in members) > 2 * SIZE_LIMIT:
            return
        for member in members:
            # The declared file_size is a claim made by the archive. Read with a cap
            # so a member that lies cannot expand into memory unbounded.
            with z.open(member) as handle:
                data = handle.read(SIZE_LIMIT + 1)
            if len(data) > SIZE_LIMIT:
                return
            yield data


def number(element):
    fmt = element.get("format", "").split(":")[-1]
    text = "".join(element.itertext()).strip().replace("−", "-")
    text = re.sub(r"[\s\u00a0\u202f]", "", text)
    if fmt in ("numcommadecimal", "num-comma-decimal"):
        text = text.replace(".", "").replace(",", ".")
    elif fmt in ("numdotdecimal", "num-dot-decimal"):
        text = text.replace(",", "")
    elif fmt == "fixed-zero":
        text = "0"
    elif fmt or "," in text:
        return None
    if not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", text):
        return None
    try:
        scale = int(element.get("scale", "0"))
        if not -12 <= scale <= 12:
            return None
        value = Decimal(text).scaleb(scale)
        return -value if element.get("sign") == "-" else value
    except (ValueError, InvalidOperation):
        return None


def report_facts(payload, entity, cutoff):
    # Dimensions are intentionally excluded. They may describe a segment or share class.
    facts = defaultdict(set)
    for document in document_files(payload):
        if re.search(rb"<!ENTITY\b", document, re.I):
            continue
        try:
            parser = ET.iterparse(BytesIO(document), events=("start-ns", "end"))
            namespaces = defaultdict(set)
            for event, value in parser:
                if event == "start-ns":
                    namespaces[value[0]].add(value[1])
            root = parser.root
        except ET.ParseError:
            continue
        ifrs = {prefix for prefix, urls in namespaces.items() if len(urls) == 1
                and re.fullmatch(r"https?://(?:xbrl\.ifrs\.org|xbrl\.iasb\.org)/.*/ifrs-full",
                                 next(iter(urls)))}
        units = {}
        for unit in root.iter(INSTANCE + "unit"):
            if len(unit) == 1 and unit[0].tag == INSTANCE + "measure":
                prefix, _, currency = (unit[0].text or "").strip().partition(":")
                if namespaces.get(prefix) == {"http://www.xbrl.org/2003/iso4217"} and re.fullmatch("[A-Z]{3}", currency):
                    units[unit.get("id")] = currency
                continue
            # Amounts per share are divided units: currency over shares. Without this
            # the earnings per share of European reports never arrive, because the
            # figure is tagged per share class and the unit is not plain currency.
            numerator = unit.findtext(f"{INSTANCE}divide/{INSTANCE}unitNumerator/{INSTANCE}measure", "")
            denominator = unit.findtext(f"{INSTANCE}divide/{INSTANCE}unitDenominator/{INSTANCE}measure", "")
            prefix, _, currency = numerator.strip().partition(":")
            if (namespaces.get(prefix) == {"http://www.xbrl.org/2003/iso4217"}
                    and re.fullmatch("[A-Z]{3}", currency)
                    and (denominator or "").strip().rpartition(":")[2] == "shares"):
                units[unit.get("id")] = currency
        contexts = {}
        for context in root.iter(INSTANCE + "context"):
            identifier = context.findtext(f"{INSTANCE}entity/{INSTANCE}identifier", "").strip()
            if identifier != entity or any(node.tag in (INSTANCE + "segment", INSTANCE + "scenario")
                                           for node in context.iter()):
                continue
            period = context.find(INSTANCE + "period")
            if period is None:
                continue
            start = period.findtext(INSTANCE + "startDate")
            end = period.findtext(INSTANCE + "endDate") or period.findtext(INSTANCE + "instant")
            try:
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 if start else 0
                if date.fromisoformat(end) > cutoff or days < 0:
                    continue
            except (TypeError, ValueError):
                continue
            contexts[context.get("id")] = (start, end, days)
        for item in root.iter(IX + "nonFraction"):
            prefix, _, tag = item.get("name", "").partition(":")
            ctx = contexts.get(item.get("contextRef"))
            currency = units.get(item.get("unitRef"))
            if (prefix not in ifrs or not ctx or not currency or item.get("continuedAt")
                    or item.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in ("true", "1")):
                continue
            start, end, days = ctx
            if (days == 0 and tag not in BALANCE) or (days and tag not in (INCOME | CASH)):
                continue
            value = number(item)
            if value is not None:
                facts[(start, end, days, currency, tag)].add(value)
    return {key: values.pop() for key, values in facts.items() if len(values) == 1}


def add_cumulative_quarters(candidates):
    cumulative = defaultdict(list)
    for (start, end, days, currency, tag), result in candidates.items():
        if tag in INCOME and 70 <= days <= 385:
            cumulative[start, currency, tag].append((end, days, result))
    derived = {}
    for (_start, currency, tag), entries in cumulative.items():
        entries.sort()
        for (before_end, before_days, before), (end, days, after) in zip(entries, entries[1:], strict=False):
            if not 70 <= days - before_days <= 110:
                continue
            quarter_start = (date.fromisoformat(before_end) + timedelta(days=1)).isoformat()
            key = (quarter_start, end, days - before_days, currency, tag)
            if key in candidates:
                continue
            filed = max(before[0], after[0])
            derived[key] = (filed, after[1], after[2] - before[2], before[1])
    return derived


def tagged_statements(documents, entity, cutoff, expected_currency):
    """Assemble the statements from documents that were already parsed once."""
    candidates = {}
    for filed, url, facts in documents:
        for fact, value in facts.items():
            if fact not in candidates:
                candidates[fact] = (filed, url, value, None)
    candidates.update(add_cumulative_quarters(candidates))
    currencies = {fact[3] for fact in candidates}
    currency = expected_currency if expected_currency else (next(iter(currencies)) if len(currencies) == 1 else None)
    if not currency:
        return {}, None

    rows = {"income": {}, "quarterly": {}, "balance": {}, "cash": {}, "provenance": {}}
    periods = defaultdict(set)
    for start, end, days, unit, tag in candidates:
        if unit == currency and tag == "Revenue" and (70 <= days <= 110 or 345 <= days <= 385):
            periods[end, "income" if days >= 345 else "quarterly"].add(start)
    annual_ends = {end for (end, section), starts in periods.items() if section == "income" and len(starts) == 1}
    for (start, end, days, unit, tag), (filed, url, value, previous_url) in candidates.items():
        if unit != currency:
            continue
        section = "income" if 345 <= days <= 385 else "quarterly" if 70 <= days <= 110 else None
        if tag in BALANCE and not start and end in annual_ends:
            section, key = "balance", BALANCE[tag]
        elif section and periods[end, section] == {start}:
            if tag in CASH and section == "income":
                section, key = "cash", CASH[tag]
            elif tag in INCOME:
                key = INCOME[tag]
            else:
                continue
        else:
            continue
        amount = float(value)
        if not math.isfinite(amount):
            continue
        rows[section].setdefault(end, {})[key] = amount
        rows["provenance"].setdefault(section, {}).setdefault(end, {})[key] = {
            "source": "ixbrl_original", "url": url, "filed": filed,
            "unit": unit, "period_start": start, "period_end": end,
        }
        if previous_url:
            rows["provenance"][section][end][key]["previous_url"] = previous_url
    return ({key: value for key, value in rows.items() if value}, currency) if rows["provenance"] else ({}, None)


def valid_link(url, hostname):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == hostname and not parsed.fragment


def stored_statements(symbol, currency):
    """What the store already knows about this symbol, while it is fresh enough.

    Fetching costs credits, so a second look at the same company must not look
    anything up again.
    """
    try:
        with closing(store.connect()) as conn:
            sections = store.read_figures(conn, symbol)
            if not (sections.get("income") or sections.get("balance")):
                return {}, None
            identity = store.company(conn, symbol)
            provenance = {section: {day: {key: {"source": "ixbrl_original", "stored": True}
                                          for key in row}
                                    for day, row in block.items()}
                          for section, block in sections.items()}
            return {**sections, "provenance": provenance}, identity.get("currency") or currency
    except (sqlite3.Error, OSError):
        return {}, None


def remember_statements(symbol, isin, currency, tagged):
    """Write what the collector learned, so the next look costs nothing.

    A missing store is not worth an error: the page still shows the figures it just read. A
    TypeError is not in that list - it means the call and the store disagree about their
    arguments, and swallowing it would let the collector pay for every page view again.
    """
    try:
        with closing(store.connect()) as conn:
            store.remember_company(conn, symbol, isin=isin, currency=currency)
            store.write_figures(conn, symbol, tagged, source="ixbrl_original", currency=currency)
            filings = []
            for block in (tagged.get("provenance") or {}).values():
                for day, row in block.items():
                    for entry in row.values():
                        if not isinstance(entry, dict) or not entry.get("url"):
                            continue
                        released = entry.get("filed") or ""
                        filings.append((int(str(day)[:4]), "IXBRL", entry["url"], str(released)))
            if filings:
                store.remember_filings(conn, symbol, filings)
    except (sqlite3.Error, OSError, ValueError):
        pass


_LOCKS = {}
_LOCKS_GUARD = Lock()
_LOCKS_MAX = 256


def symbol_lock(symbol):
    """One lock per symbol, so two page views of the same share never both pay.

    The map is bounded, because the symbol comes from the request. Only locks that
    nobody holds are dropped: evicting a lock that is in use would hand the next
    caller a different object and two threads would enter the paid path together.
    """
    with _LOCKS_GUARD:
        if symbol in _LOCKS:
            return _LOCKS[symbol]
        _LOCKS[symbol] = Lock()
        while len(_LOCKS) > _LOCKS_MAX:
            frei = next((key for key, lock in _LOCKS.items()
                         if key != symbol and not lock.locked()), None)
            if frei is None:
                break
            del _LOCKS[frei]
        return _LOCKS[symbol]


def issuer_identifier(payload):
    """The identifier the report carries itself - the LEI for European issuers.

    It is inside the file, so a stored filing list can be read again without
    asking the provider who the issuer is (and paying for the answer).
    """
    for document in document_files(payload):
        found = re.search(rb"<[\w.-]*:identifier[^>]*>\s*([^<\s]{1,64})\s*<", document)
        if found:
            return found.group(1).decode("ascii", "replace")
        # Another archive member may carry the identifier; keep looking instead of
        # giving up on the first file that has none.
    return ""


def stored_filings(symbol):
    """The filing list that was already paid for, while it is young enough."""
    try:
        with closing(store.connect()) as conn:
            return store.filings(conn, symbol)
    except (sqlite3.Error, OSError):
        return []


# Only machine-readable packages carry the figures. A filing list of one issuer mixes ad-hoc
# notices, videos and SEC cover forms in with the reports; those yield no tagged facts at all.
REPORT_PERIODS = ("FY", "H1", "9M", "Q1", "Q2", "Q3", "Q4")
PACKAGE_EXTENSIONS = {"ZIP", "XBRI", "XBRL", "XHTML"}
# The annual list is one paginated request; taking only its first page stopped at three annual
# packages and left the fifth and sixth year unread.
ANNUAL_PACKAGES = 100


def report_rows(company_id, cutoff, fiscal_period=None, size=100):
    query = dict(company=company_id, ordering="-release_datetime",
                 release_datetime_to=cutoff + "T23:59:59Z", page_size=size, view="full")
    if fiscal_period:
        query["fiscal_period"] = fiscal_period
    return listing("filings", **query)


def filing_entries(symbol, cutoff, isin, refresh=False):
    """Where the reports are: the stored list when it exists, the paid one otherwise."""
    stored = [] if refresh else stored_filings(symbol)
    if stored:
        limit = date.fromisoformat(cutoff)
        entries = []
        for item in stored:
            year, address = item[0], item[2]
            released = item[3] if len(item) > 3 else ""
            if released:
                try:
                    # A report released after the cut-off must not be read: it would
                    # carry knowledge the price series cannot have. The paid branch
                    # has always refused these; the stored branch has to as well.
                    if date.fromisoformat(released[:10]) > limit:
                        continue
                except ValueError:
                    continue
            entries.append((address, str(year), released or ""))
        return "", entries
    ticker, _, market = symbol.rpartition(".")
    country = EU_MARKETS.get(market)
    if not country:
        return "", []
    identity = {"isin": isin} if isin and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) else {
        "ticker": f"{ticker}.{country}"}
    companies = listing("companies", **identity, page_size=2)
    if len(companies) != 1:
        return "", []
    company = companies[0]
    company_id, lei = company.get("id"), company.get("lei", "")
    if type(company_id) is not int or not re.fullmatch(r"[A-Z0-9]{20}", lei or ""):
        return "", []
    if "isin" in identity and isin not in company.get("isins", ()):
        return "", []
    # The annual reports first: they carry a full fiscal year each, and the newest ad-hoc notices
    # of a US-registered issuer are cover forms whose XBRL names a different entity. Reading only
    # the five newest filings therefore produced no figures at all.
    candidates = (report_rows(company_id, cutoff, "FY", ANNUAL_PACKAGES)
                  + report_rows(company_id, cutoff, None, 60))
    entries = []
    seen = set()
    for item in candidates:
        try:
            filed = date.fromisoformat(item["release_datetime"][:10])
            url = item.get("document") or item.get("document_url")
            if (filed > date.fromisoformat(cutoff) or item.get("company", {}).get("id") != company_id
                    or not valid_link(url, CDN)
                    or not urlsplit(url).path.startswith("/financialreports/media/filings/")
                    or (item.get("fiscal_period") or "") not in REPORT_PERIODS
                    or (item.get("file_extension") or "").upper() not in PACKAGE_EXTENSIONS
                    or url in seen):
                continue
            seen.add(url)
            entries.append((url, item.get("period_ending_date") or url, filed.isoformat()))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return lei, entries


# Six fiscal years need six packages; each annual ESEF package also carries the year before it.
DOCUMENTS_READ = 10
REPORTED_YEARS_MIN = 6
DOCUMENTS_TRIED = 14


def collect_statements(symbol, cutoff, currency, isin, refresh=False):
    """Download the original packages and read the tags the issuers set themselves."""
    try:
        lei, entries = filing_entries(symbol, cutoff, isin, refresh=refresh)
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, None
    documents = []
    seen = set()
    attempts = 0
    years = set()
    limit = date.fromisoformat(cutoff)
    for url, tag, filed in entries:
        if len(documents) >= DOCUMENTS_READ or attempts >= DOCUMENTS_TRIED:
            break
        if tag in seen or not valid_link(url, CDN):
            continue
        seen.add(tag)
        attempts += 1
        try:
            payload = read_bytes(url)
        except (OSError, ValueError):
            continue
        if not lei:
            # No company lookup was needed, so the report names its issuer itself.
            lei = issuer_identifier(payload)
        if not re.fullmatch(r"[A-Z0-9]{20}", lei or ""):
            return {}, None
        try:
            facts = report_facts(payload, lei, limit)
        except (BadZipFile, OSError, RuntimeError, ValueError):
            continue
        if not facts:
            # Ad-hoc notices, cover forms and videos carry no tagged figures; they do not count
            # against the reading budget either.
            continue
        documents.append((filed, url, facts))
        years |= {end for (start, end, days, unit, tag_name) in facts
                  if tag_name == "Revenue" and 345 <= days <= 385 and (not currency or unit == currency)}
        if len(years) >= REPORTED_YEARS_MIN:
            # Enough years to answer the question; every further package is bandwidth.
            break
    if not documents:
        return {}, None
    try:
        tagged, report_currency = tagged_statements(documents, lei, limit, currency)
    except (BadZipFile, OSError, RuntimeError, ValueError):
        return {}, None
    if tagged:
        remember_statements(symbol, isin, report_currency, tagged)
    return tagged, report_currency


def filings_available():
    """Whether the original reports may be asked for at all - the key decides."""
    return bool(os.environ.get("AKTIENANALYSE_FILINGS_KEY", "").strip())


def reported_years(sections):
    """How many fiscal years the section actually carries a revenue figure for."""
    return sum(1 for row in (sections.get("income") or {}).values()
               if app_finite(row.get("totalrevenue")) is not None)


def app_finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def merge_blocks(stored, collected):
    """What the reports say now, plus the years only the store still holds."""
    merged = {"provenance": collected.get("provenance") or {}}
    for section in ("income", "quarterly", "balance", "cash"):
        rows = {day: dict(row) for day, row in (stored.get(section) or {}).items()}
        for day, row in (collected.get(section) or {}).items():
            rows[day] = {**(rows.get(day) or {}), **row}
        if rows:
            merged[section] = rows
    return merged


@lru_cache(maxsize=24)
def filing_statements(symbol, cutoff, currency, bucket, isin=None):
    """Look up a unique issuer; load only its published, tagged original files."""
    if not os.environ.get("AKTIENANALYSE_FILINGS_KEY", "").strip():
        return {}, None
    cached, cached_currency = stored_statements(symbol, currency)
    if cached and reported_years(cached) >= REPORTED_YEARS_MIN:
        return cached, cached_currency
    # A store filled by an older collector can hold fewer years than the reports offer. It is
    # then not an answer but a starting point: pay once, collect, and keep the years it alone had.
    # lru_cache does not merge two cold callers: without this lock two page views at
    # the same moment would both pay for the same listing. The store is checked
    # again inside the lock, so only the first caller pays and the rest read.
    with symbol_lock(symbol):
        cached, cached_currency = stored_statements(symbol, currency)
        if cached and reported_years(cached) >= REPORTED_YEARS_MIN:
            return cached, cached_currency
        collected, report_currency = collect_statements(symbol, cutoff, currency, isin)
        # A stored filing list from an older collector can be shorter than the reports offer.
        # Paying for the listing once more is the only way to learn whether it is.
        if reported_years(collected or {}) < REPORTED_YEARS_MIN:
            fresh, fresh_currency = collect_statements(symbol, cutoff, currency, isin, refresh=True)
            if reported_years(fresh or {}) > reported_years(collected or {}):
                collected, report_currency = fresh, fresh_currency or report_currency
        if cached and collected:
            collected, report_currency = merge_blocks(cached, collected), report_currency or cached_currency
        return (collected or cached), (report_currency or cached_currency)


def merge_filings(symbol, cutoff, statements, currency, isin=None):
    tagged, report_currency = filing_statements(symbol, cutoff, currency, int(time() // 600), isin)
    if not tagged:
        return statements, currency, False
    result = dict(statements) if currency == report_currency else {}
    for section in ("income", "quarterly", "balance", "cash"):
        result[section] = dict(result.get(section, {}))
        for day, row in tagged.get(section, {}).items():
            result[section][day] = {**result[section].get(day, {}), **row}
    result["provenance"] = tagged["provenance"]
    return result, report_currency, True
