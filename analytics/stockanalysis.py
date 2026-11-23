"""The quarterly figures the provider keeps longer than twelve months.

Yahoo hands over five or six quarters for a European paper, and none at all for L'Oréal. A trailing
twelve month comparison needs eight, because it puts one twelve month window against the window
twelve months before it. stockanalysis.com publishes twenty quarters - five years - as a plain
table, without a key and without a quota.

What is used here:

* ``series(symbol)`` - the quarterly revenue and earnings per share, newest first, from the store
  while it is younger than a week, otherwise fetched once and stored.
* ``twelve_month_growth(series)`` - the growth of the trailing twelve months against the twelve
  months before, or nothing when the series does not carry two complete windows.

No figure is guessed. A window that is short, has a hole, or mixes quarters with half years is
reported as absent.
"""

from __future__ import annotations

import html as html_module
import json
import re
import urllib.error
import urllib.request
from contextlib import closing
from datetime import date
from threading import Lock, Thread
from time import monotonic

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
#: The page weighs about 280 kB. A cap keeps a wrong answer from filling the process.
MAX_BYTES = 2_000_000
#: One page, one budget: the socket timeout bounds a single read, not the whole call, so the total is
#: checked as well. Three seconds covers the measured 1,6 to 2,0 seconds; anything slower belongs in
#: the background, not in a waiting request.
FETCH_BUDGET_S = 3.0
#: A quarter's figures do not change once reported; a week keeps the page fresh without asking twice.
MAX_AGE_H = 7 * 24
BASE = "https://stockanalysis.com"

#: Our ticker suffix to the exchange path the site uses. The empty suffix is a US listing.
EXCHANGES = {
    ".DE": "etr", ".F": "fra", ".PA": "epa", ".AS": "ams", ".BR": "bru", ".LS": "lis",
    ".MC": "bme", ".MI": "bit", ".L": "lon", ".SW": "swx", ".VI": "vie", ".ST": "sto",
    ".CO": "cph", ".OL": "osl", ".HE": "hel", ".IR": "ise", ".WA": "wse", ".PR": "prg",
}
US = ""

ROW_REVENUE = "Revenue"
ROW_EPS = "EPS (Diluted)"
DATE_TEXT = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")
MONTHS = {name: number for number, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


class SourceUnavailable(Exception):
    """The site did not answer, or answered with something that is not the table."""


def exchange_for(symbol: str) -> str | None:
    """The exchange path for one of our tickers, or None when the suffix is unknown.

    ``BRK.B`` is not a Frankfurt listing: the single letter after the dot is a share class, and the
    site serves it as ``/stocks/brk.b/``. Anything else unknown stays refused rather than guessed.
    """
    upper = symbol.upper()
    if "." not in upper:
        return US
    stem, suffix = upper.rsplit(".", 1)
    if f".{suffix}" in EXCHANGES:
        return EXCHANGES[f".{suffix}"]
    if len(suffix) == 1 and suffix.isalpha() and "." not in stem:
        return US
    return None


def url_for(symbol: str) -> str | None:
    exchange = exchange_for(symbol)
    if exchange is None:
        return None
    base = symbol.upper()
    if exchange:
        path = f"/quote/{exchange}/{base.split('.')[0]}/financials/income-statement/?p=quarterly"
    else:
        path = f"/stocks/{base.lower()}/financials/income-statement/?p=quarterly"
    return BASE + path


def number(text: str):
    """A table cell as a number: thousands separators dropped, a dash means no figure."""
    cleaned = html_module.unescape(text or "").replace("\u2212", "-").strip()
    if cleaned in ("", "-", "--", "—"):
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()").replace(",", "").replace(" ", "")
    try:
        value = float(cleaned.rstrip("%"))
    except ValueError:
        return None
    return -value if negative else value


def cells(row: str) -> list[str]:
    found = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in found]


def parse(page: str) -> list[dict]:
    """The quarters of an income statement page, newest first.

    A row is kept when both its period end and its revenue column can be read; the order of the
    table is the order of the site, newest first.
    """
    rows = [cells(row) for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)]
    rows = [row for row in rows if row]
    if not rows:
        return []
    labels = rows[0][1:]
    ends = []
    for row in rows:
        if row and row[0].lower().startswith("period ending"):
            ends = row[1:]
            break
    revenue = next((row[1:] for row in rows if row[0] == ROW_REVENUE), None)
    eps = next((row[1:] for row in rows if row[0] == ROW_EPS), None)
    if revenue is None and eps is None:
        return []
    # Every column is read by its position. A header with a colspan, an extra leading cell or a note
    # column would shift the figures against their dates without ever raising - a growth figure that
    # looks right and is not. Rows that do not line up are refused instead of matched.
    width = len(labels)
    for row in (ends, revenue, eps):
        if row is not None and len(row) != width:
            return []
    periods = []
    for index in range(len(labels)):
        stamp = ends[index] if index < len(ends) else ""
        match = DATE_TEXT.search(stamp or "")
        if not match or match.group(1) not in MONTHS:
            continue
        day = date(int(match.group(3)), MONTHS[match.group(1)], int(match.group(2)))
        periods.append({
            "period_end": day.isoformat(),
            "label": labels[index] if index < len(labels) else "",
            "revenue": number(revenue[index]) if revenue and index < len(revenue) else None,
            "eps": number(eps[index]) if eps and index < len(eps) else None,
        })
    periods.sort(key=lambda row: row["period_end"], reverse=True)
    return periods


def fetch(symbol: str, budget_s: float = FETCH_BUDGET_S) -> list[dict]:
    """The page as published, read once, within a budget.

    The socket timeout bounds one read, not the call: a body that trickles in passes each operation
    and still takes minutes. So the whole call is clocked and abandoned when it overstays, and the
    body is capped before it is decoded.
    """
    address = url_for(symbol)
    if not address:
        raise SourceUnavailable(f"kein Börsenplatz für {symbol}")
    if budget_s <= 0:
        raise SourceUnavailable("kein Zeitbudget im Sammelabruf")
    started = monotonic()
    request = urllib.request.Request(address, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=budget_s) as answer:
            body = answer.read(MAX_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as error:
        raise SourceUnavailable(str(error)) from error
    if len(body) > MAX_BYTES:
        raise SourceUnavailable(f"Antwort über {MAX_BYTES} Bytes")
    if monotonic() - started > budget_s:
        raise SourceUnavailable(f"über {budget_s:.1f} s gebraucht")
    periods = parse(body.decode("utf-8", "replace"))
    if not periods:
        raise SourceUnavailable(f"keine Tabelle für {symbol}")
    return periods


def series(symbol: str, store_module=None, path=None,
           budget_s: float = FETCH_BUDGET_S) -> list[dict]:
    """The quarters of a ticker, from the store while fresh, otherwise fetched once."""
    if store_module is None:
        return fetch(symbol, budget_s)
    with closing(store_module.connect(path)) as conn:
        stored = store_module.read_quarterly_source(conn, symbol, MAX_AGE_H)
    if stored:
        return stored
    periods = fetch(symbol, budget_s)
    store_module.write_quarterly_source(symbol, periods, path)
    return periods


#: One fetch per ticker at a time: a page view, a reload and a second tab ask for the same series.
_PENDING: set[str] = set()
_PENDING_LOCK = Lock()


def refresh_in_background(symbol: str, store_module, path=None,
                          budget_s: float = FETCH_BUDGET_S * 4) -> bool:
    """Load a series after the answer was sent, so no request ever waits on a slow source.

    Returns whether a load was started. A second call for the same ticker while one runs does
    nothing - the queue would otherwise grow with every reload.
    """
    with _PENDING_LOCK:
        if symbol in _PENDING:
            return False
        _PENDING.add(symbol)

    def work():
        try:
            periods = fetch(symbol, budget_s)
            if periods:
                store_module.write_quarterly_source(symbol, periods, path)
        except Exception:
            pass
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(symbol)

    Thread(target=work, daemon=True, name=f"quarters-{symbol}").start()
    return True


def _step_days(periods: list[dict]) -> int | None:
    """The distance between two neighbouring periods, in days."""
    if len(periods) < 2:
        return None
    first = date.fromisoformat(periods[0]["period_end"])
    second = date.fromisoformat(periods[1]["period_end"])
    return (first - second).days


def _window(periods: list[dict], start: int, count: int, step: int):
    """One twelve month window of ``count`` periods, or None when it does not hold together.

    Four quarters span three intervals, not twelve months of calendar - 4 x 91 days is the year.
    So the window is checked against the step it was built from, not against 365 days.
    """
    chunk = periods[start:start + count]
    if len(chunk) != count:
        return None
    days = [date.fromisoformat(row["period_end"]) for row in chunk]
    steps = [(days[index] - days[index + 1]).days for index in range(len(days) - 1)]
    if any(not 0.6 * step <= distance <= 1.4 * step for distance in steps):
        return None
    return chunk


def twelve_month_growth(periods: list[dict]) -> dict:
    """The growth of the trailing twelve months, against the twelve months before.

    Returns a dict with ``revenue``, ``eps``, ``periods`` and ``step_days``; a value is None when the
    series does not carry it. Nothing is filled in from a shorter window: a twelve month figure
    built from nine months reads as a fact and is not one.
    """
    answer = {"revenue": None, "eps": None, "periods": len(periods), "step_days": None}
    step = _step_days(periods)
    if step is None:
        return answer
    answer["step_days"] = step
    count = 4 if step < 130 else 2
    now = _window(periods, 0, count, step)
    before = _window(periods, count, count, step)
    if not now or not before:
        return answer

    def total(chunk, key):
        values = [row.get(key) for row in chunk]
        if any(value is None for value in values):
            return None
        return sum(values)

    for key in ("revenue", "eps"):
        present, earlier = total(now, key), total(before, key)
        if present is None or earlier is None or earlier <= 0:
            continue
        answer[key] = present / earlier - 1
    return answer


def growth(symbol: str, store_module=None, path=None,
           budget_s: float = FETCH_BUDGET_S) -> dict:
    """``twelve_month_growth`` for one ticker, with the source named.

    A series that is not in the store yet is fetched within the budget; when that does not fit, the
    load continues in the background and the answer says so instead of waiting.
    """
    try:
        periods = series(symbol, store_module, path, budget_s)
    except SourceUnavailable:
        if store_module is not None:
            refresh_in_background(symbol, store_module, path)
        return {"revenue": None, "eps": None, "periods": 0, "step_days": None,
                "source": "stockanalysis.com", "newest": None,
                "reason": "Quartalsreihe wird im Hintergrund geladen"}
    answer = twelve_month_growth(periods)
    answer["source"] = "stockanalysis.com"
    answer["newest"] = periods[0]["period_end"] if periods else None
    return answer


def summary(periods: list[dict]) -> str:
    """A short line for the log, without a table."""
    return json.dumps({"periods": len(periods), "newest": periods[0]["period_end"] if periods else None},
                      ensure_ascii=False)
