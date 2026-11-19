"""Load comparable US-GAAP statements from SEC company facts."""

import json
import math
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction
from functools import lru_cache
from itertools import chain
from threading import Lock
from time import monotonic, sleep
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

INCOME = {
    "totalrevenue": ("RevenueFromContractWithCustomerExcludingAssessedTax",
                     "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    "operatingincome": ("OperatingIncomeLoss",),
    "grossprofit": ("GrossProfit",),
    "netincome": ("NetIncomeLoss",),
    "dilutedeps": ("EarningsPerShareDiluted",),
    "dilutedaverageshares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "pretaxincome": ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",),
    "taxprovision": ("IncomeTaxExpenseBenefit",),
    "interestexpense": ("InterestExpense", "InterestExpenseNonOperating"),
}
BALANCE = {
    "totalassets": ("Assets",),
    "stockholdersequity": ("StockholdersEquity",),
    "currentassets": ("AssetsCurrent",),
    "currentliabilities": ("LiabilitiesCurrent",),
    "longtermdebt": ("LongTermDebtNoncurrent",),
    "totaldebt": ("DebtAndCapitalLeaseObligations",),
    "cashandcashequivalents": ("CashAndCashEquivalentsAtCarryingValue",),
}
CASH = {
    "operatingcashflow": ("NetCashProvidedByUsedInOperatingActivities",),
    "commonstockissuance": ("ProceedsFromIssuanceOfCommonStock",),
}
UNITS = {"dilutedeps": "USD/shares", "dilutedaverageshares": "shares"}
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
SEC_LOCK = Lock()
LAST_REQUEST = 0.0
MAX_SIZE = 32 * 1024 * 1024
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
EPS_TAG, SHARES_TAG = INCOME["dilutedeps"][0], INCOME["dilutedaverageshares"][0]
MONTHS = {month: str(i).zfill(2) for i, month in enumerate(
    "January February March April May June July August September October November December".split(), 1)}
REPORT_DATE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b")
FETCH_ERRORS = (URLError, OSError, ValueError, TypeError, KeyError)


def request_bytes(url, accept):
    global LAST_REQUEST
    agent = os.environ.get("AKTIENANALYSE_SEC_USER_AGENT", "Aktienanalyse/8")
    request = Request(url, headers={"User-Agent": agent, "Accept": accept})
    with SEC_LOCK:
        sleep(max(0, .25 - (monotonic() - LAST_REQUEST)))
        LAST_REQUEST = monotonic()
    with urlopen(request, timeout=7) as response:
        raw = response.read(MAX_SIZE + 1)
    if len(raw) > MAX_SIZE:
        raise ValueError("SEC-Antwort zu groß.")
    return raw


def request_json(url):
    return json.loads(request_bytes(url, "application/json"))


@lru_cache(maxsize=128)
def request_html(url):
    return request_bytes(url, "text/html")


@lru_cache(maxsize=2)
def ticker_index(day):
    data = request_json("https://www.sec.gov/files/company_tickers.json")
    return {item["ticker"].upper(): int(item["cik_str"]) for item in data.values()}


def concept_rows(facts, aliases, unit, cutoff, period, split_bases=(), evidence=None):
    """Return the latest reported value at each exact period end."""
    chosen = {}
    accepted_units = (unit, unit.replace("/", "-per-")) if "/" in unit else (unit,)
    for preference, alias in enumerate(aliases):
        units = facts.get(alias, {}).get("units", {})
        for item in chain.from_iterable(units.get(name, []) for name in accepted_units):
            if item.get("form") not in FORMS:
                continue
            try:
                end = date.fromisoformat(item["end"])
                filed = date.fromisoformat(item["filed"])
                value = item["val"]
                if not isinstance(value, (int, float)) or not math.isfinite(value) or end > cutoff or filed > cutoff:
                    continue
                start = date.fromisoformat(item["start"]) if "start" in item else None
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if period == "instant":
                if start is not None:
                    continue
            else:
                days = (end - start).days + 1 if start is not None else 0
                lower, upper = (345, 385) if period == "annual" else (72, 110)
                if not lower <= days <= upper:
                    continue
            priority = (filed, -preference)
            if end not in chosen or priority > chosen[end][0]:
                factor = split_factor(split_bases, filed) if alias in (EPS_TAG, SHARES_TAG) else 1
                adjusted = value if factor == 1 else (value / factor if alias == EPS_TAG else value * factor)
                chosen[end] = (priority, start, adjusted, item.get("accn"), factor, value)
    if evidence is not None:
        for end, ((filed, _), start, _, accession, factor, raw) in chosen.items():
            if accession and re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                evidence[end.isoformat()] = {
                    "period_start": start.isoformat() if start else None,
                    "period_end": end.isoformat(), "filed": filed.isoformat(),
                    "unit": unit, "reported_value": raw, "split_factor": factor,
                    "url": f"{SEC_ARCHIVES}/{int(accession[:10])}/{accession.replace('-', '')}/{accession}-index.htm",
                }
    return {end: (start, value) for end, (_, start, value, _, _, _) in chosen.items()}


def split_factor(split_bases, filed):
    return math.prod(ratio for first_report, ratio in split_bases if filed < first_report)


def observed_split_bases(facts, cutoff):
    """Find a change of share basis in like-for-like SEC comparatives."""
    filed_values = defaultdict(dict)
    for tag, units in ((EPS_TAG, ("USD/shares", "USD-per-shares")), (SHARES_TAG, ("shares",))):
        for unit in units:
            for item in facts.get(tag, {}).get("units", {}).get(unit, ()):
                try:
                    filed = date.fromisoformat(item["filed"])
                    start, end = (date.fromisoformat(item[key]) for key in ("start", "end"))
                    value = item["val"]
                    if item["form"] in FORMS and start <= end <= filed <= cutoff and math.isfinite(value):
                        filed_values[(start, end)][(filed, item["accn"], tag)] = value
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue

    transitions = defaultdict(list)
    for period, rows in filed_values.items():
        comparable = sorted((filed, accession, count, rows[(filed, accession, EPS_TAG)])
                            for (filed, accession, tag), count in rows.items()
                            if tag == SHARES_TAG and count > 0 and (filed, accession, EPS_TAG) in rows)
        for before, after in zip(comparable, comparable[1:]):
            old_date, _, old_shares, old_eps = before
            new_date, _, new_shares, new_eps = after
            if old_date == new_date or min(abs(old_eps), abs(new_eps)) < .05:
                continue
            ratio = Fraction(new_shares / old_shares).limit_denominator(10)
            if (ratio > 0 and (ratio >= 1.2 or ratio <= 1 / 1.2)
                    and math.isclose(new_shares, old_shares * ratio, rel_tol=.0001)
                    and math.isclose(new_eps, old_eps / ratio, abs_tol=.011)):
                transitions[ratio].append((old_date, new_date, period))
    changes = set()
    for ratio, pairs in transitions.items():
        while pairs:
            edge = min(new for _, new, _ in pairs)
            covered = [(old, new, period) for old, new, period in pairs if old < edge <= new]
            if len({period for _, _, period in covered}) >= 2:
                changes.add((edge, float(ratio)))
                pairs = [(old, new, period) for old, new, period in pairs if old >= edge]
            else:
                pairs = [(old, new, period) for old, new, period in pairs if new > edge]
    # Conflicting evidence for the same boundary is left unused.
    return sorted(change for change in changes if sum(d == change[0] for d, _ in changes) == 1)


def pack_rows(series, dates):
    return {end.isoformat(): {key: values[end][1] for key, values in series.items() if end in values}
            for end in sorted(dates)}


def parse_statements(payload, cutoff, q4_release=None):
    facts = payload.get("facts", {}).get("us-gaap", {})
    split_bases = observed_split_bases(facts, cutoff)
    provenance = {"income": {}, "quarterly": {}}

    def series(mapping, period):
        channel = {"annual": "income", "quarter": "quarterly"}.get(period)
        return {key: concept_rows(facts, tags, UNITS.get(key, "USD"), cutoff, period,
                                  split_bases, provenance[channel] if channel and key == "dilutedeps" else None)
                for key, tags in mapping.items()}

    annual = series(INCOME, "annual")
    quarterly = series(INCOME, "quarter")
    # Identify the fiscal year by reported revenue, or net income for filers
    # without a supported consolidated revenue tag.
    fiscal_ends = annual["totalrevenue"] or annual["netincome"]
    annual_dates = set(sorted(fiscal_ends)[-10:])
    for key in ("totalrevenue", "operatingincome", "grossprofit", "netincome"):
        for end in annual_dates:
            if end in quarterly[key] or end not in annual[key]:
                continue
            start, amount = annual[key][end]
            first_three = sorted((d, s, v) for d, (s, v) in quarterly[key].items()
                                 if s is not None and start <= s <= d < end)
            if len(first_three) != 3:
                continue
            _, first_start, _ = first_three[0]
            last_end = first_three[-1][0]
            if (start == first_start and 72 <= (end - last_end).days <= 110 and
                    all(s == prev + timedelta(days=1)
                        for (prev, _, _), (_, s, _) in zip(first_three, first_three[1:]))):
                quarterly[key][end] = (last_end + timedelta(days=1),
                                       amount - sum(v for _, _, v in first_three))
    quarter_dates = set(quarterly["totalrevenue"]) | set(quarterly["netincome"])
    if q4_release:
        for end in sorted(annual_dates, reverse=True)[:3]:
            if end in quarterly["dilutedeps"]:
                continue
            report = q4_release(end)
            if report:
                value, filed, url = report
                period = quarterly["totalrevenue"].get(end) or quarterly["netincome"].get(end)
                start = period[0] if period else None
                factor = split_factor(split_bases, filed)
                quarterly["dilutedeps"][end] = (start, value / factor)
                provenance["quarterly"][end.isoformat()] = {
                    "period_start": start.isoformat() if start else None, "period_end": end.isoformat(),
                    "filed": filed.isoformat(), "unit": "USD/shares", "reported_value": value,
                    "split_factor": factor, "url": url, "source": "earnings_release",
                }
    quarter_dates = sorted(quarter_dates | set(quarterly["dilutedeps"]))[-12:]
    balance = pack_rows(series(BALANCE, "instant"), annual_dates)
    cash = pack_rows(series(CASH, "annual"), annual_dates)
    result = {"income": pack_rows(annual, annual_dates), "quarterly": pack_rows(quarterly, quarter_dates),
            "balance": {d: row for d, row in balance.items() if row},
            "cash": {d: row for d, row in cash.items() if row}}
    for channel, entries in provenance.items():
        available = {day: {"dilutedeps": source} for day, source in entries.items() if day in result[channel]}
        if available:
            result.setdefault("provenance", {})[channel] = available
    return result


def release_eps(html, end):
    """Read the reported current-quarter diluted EPS from an income table."""
    amounts = set()
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        rows = [[" ".join(cell.stripped_strings).replace("\u00a0", " ").strip()
                 for cell in row.find_all(["td", "th"], recursive=False)]
                for row in table.find_all("tr") if row.find_parent("table") is table]
        rows = [[cell for cell in row if cell] for row in rows if row]
        header = " ".join(" ".join(row) for row in rows[:10])
        dates = REPORT_DATE.findall(header)
        quarter = re.search(r"\b(?:quarter|three months)\s+ended\b", header, re.I)
        if not dates or not quarter:
            continue
        month, day, year = dates[0]
        if f"{year}-{MONTHS[month]}-{int(day):02}" != end.isoformat():
            continue
        annual = re.search(r"\b(?:year|twelve months)\s+ended\b", header, re.I)
        if annual and annual.start() < quarter.start():
            continue
        labels = [re.sub(r"\s+", " ", row[0].lower().rstrip(":")) for row in rows if row]
        if (not any(label in ("net income", "net loss", "consolidated net income", "net income (loss)",
                             "net earnings", "net sales", "total net sales") for label in labels)
                or not any(re.fullmatch(r"basic(?: (?:earnings|loss) per (?:common )?share)?(?: \(\d+\))?", label)
                           for label in labels)):
            continue
        eps_section = False
        for row, label in zip((r for r in rows if r), labels):
            if label not in ("basic", "diluted"):
                eps_section = label in ("earnings per share", "earnings per common share",
                                        "net income per share", "net loss per share")
            if not re.fullmatch(r"diluted(?: (?:earnings|loss) per (?:common )?share)?(?: \(\d+\))?", label):
                continue
            if label == "diluted" and not eps_section:
                continue
            # Ignore loss markers, ambiguous column layouts and unlabeled currencies.
            cells = [cell.replace(" ", "").replace("−", "-") for cell in row[1:]]
            if not any("$" in cell for cell in cells) and not re.search(r"\b(?:USD|U\.S\. dollars)\b", header, re.I):
                continue
            numbers = [cell.replace("$", "") for cell in cells if cell != "$"]
            if len(numbers) != len(dates):
                continue
            if numbers and re.fullmatch(r"-?\d+\.\d+|\(\d+\.\d+\)", numbers[0]):
                amounts.add(float(numbers[0].replace("(", "-").replace(")", "")))
    return amounts.pop() if len(amounts) == 1 else None


@lru_cache(maxsize=32)
def recent_filings(cik, day):
    return request_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")["filings"]["recent"]


def find_q4_release(cik, end, cutoff, day):
    try:
        recent = recent_filings(cik, day)
    except FETCH_ERRORS:
        return None
    candidates = []
    for accession, form, day_filed, items in zip(*(recent.get(key, ()) for key in
                                                  ("accessionNumber", "form", "filingDate", "items"))):
        if form not in ("8-K", "8-K/A") or "2.02" not in re.split(r"\s*,\s*", (items or "").strip()):
            continue
        try:
            filed = date.fromisoformat(day_filed)
        except (TypeError, ValueError):
            continue
        if end < filed <= min(cutoff, end + timedelta(days=120)) and re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            candidates.append((filed, accession))
    for filed, accession in sorted(candidates, reverse=True)[:4]:
        root = f"{SEC_ARCHIVES}/{cik}/{accession.replace('-', '')}/"
        try:
            listing = BeautifulSoup(request_html(f"{root}{accession}-index.htm"), "html.parser")
            for row in listing.find_all("tr"):
                cells = row.find_all("td", recursive=False)
                if len(cells) < 4 or cells[3].get_text(strip=True).upper() not in ("EX-99", "EX-99.1"):
                    continue
                link = cells[2].find("a", href=True)
                url = urljoin(root, link["href"]) if link else ""
                if re.fullmatch(re.escape(root) + r"[\w.-]+\.html?", url, re.I):
                    value = release_eps(request_html(url), end)
                    if value is not None:
                        return value, filed, url
        except FETCH_ERRORS:
            continue
    return None


@lru_cache(maxsize=96)
def load_sec_statements(symbol, cutoff, bucket):
    cik = ticker_index(bucket // 86400).get(symbol)
    if cik is None:
        return {}
    payload = request_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    if payload.get("cik") != cik:
        return {}
    return parse_statements(payload, cutoff, lambda end: find_q4_release(cik, end, cutoff, bucket // 86400))
