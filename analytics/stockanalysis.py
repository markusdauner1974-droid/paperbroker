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
from threading import Event, Lock, Thread
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
ROW_OPERATING = "Operating Income"
#: The site publishes its tables in millions; the page works in whole currency units.
MILLIONS = 1_000_000
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
    operating = next((row[1:] for row in rows if row[0] == ROW_OPERATING), None)
    if revenue is None and eps is None:
        return []
    # Every column is read by its position. A header with a colspan, an extra leading cell or a note
    # column would shift the figures against their dates without ever raising - a growth figure that
    # looks right and is not. Rows that do not line up are refused instead of matched.
    width = len(labels)
    for row in (ends, revenue, eps, operating):
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
            "operating_income": (number(operating[index])
                                 if operating and index < len(operating) else None),
        })
    periods.sort(key=lambda row: row["period_end"], reverse=True)
    return periods


#: Bytes per read. A body is taken in pieces so the clock can be looked at between them; the socket
#: timeout alone bounds one read, so a trickle that never stalls long enough would run forever.
CHUNK_BYTES = 65_536


def read_body(answer, deadline: float) -> bytes:
    """The whole body, in pieces, abandoned once the deadline has passed.

    ``read`` on an HTTP answer blocks until it has the number of bytes asked for, so a large piece
    would wait out the trickle anyway. ``read1`` returns what has arrived, which is what makes the
    clock between the pieces worth looking at.
    """
    take = getattr(answer, "read1", None) or answer.read
    pieces = []
    total = 0
    while True:
        if monotonic() > deadline:
            raise SourceUnavailable("Zeitbudget während des Lesens überschritten")
        piece = take(CHUNK_BYTES)
        if not piece:
            return b"".join(pieces)
        total += len(piece)
        if total > MAX_BYTES:
            raise SourceUnavailable(f"Antwort über {MAX_BYTES} Bytes")
        pieces.append(piece)


def fetch(symbol: str, budget_s: float = FETCH_BUDGET_S) -> list[dict]:
    """The page as published, read once, within a budget.

    The socket timeout bounds one read, not the call: a body that trickles in passes each operation
    and still takes minutes. So the body is taken in pieces and the clock is looked at between them,
    which abandons the call at its deadline and refuses an oversized answer while it is still being
    read.
    """
    address = url_for(symbol)
    if not address:
        raise SourceUnavailable(f"kein Börsenplatz für {symbol}")
    if budget_s <= 0:
        raise SourceUnavailable("kein Zeitbudget im Sammelabruf")
    started = monotonic()
    deadline = started + budget_s
    request = urllib.request.Request(address, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=budget_s) as answer:
            body = read_body(answer, deadline)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as error:
        raise SourceUnavailable(str(error)) from error
    if monotonic() > deadline:
        raise SourceUnavailable(f"über {budget_s:.1f} s gebraucht")
    periods = parse(body.decode("utf-8", "replace"))
    if not periods:
        raise SourceUnavailable(f"keine Tabelle für {symbol}")
    return periods


#: The fetch the page waits for, one per ticker: the leader fetches, the others read afterwards.
_INFLIGHT: dict[str, Event] = {}
_INFLIGHT_LOCK = Lock()


def _read(symbol, store_module, path):
    with closing(store_module.connect(path)) as conn:
        return store_module.read_quarterly_source(conn, symbol, MAX_AGE_H)


def _current(stored, store_module) -> bool:
    # A series stored before the operating income was read carries none of it and is still fresh by
    # age, so age alone would keep the two financing rows empty for a week after the deploy. The
    # parser version is written on every row, so it marks exactly those rows for a re-read and
    # leaves a series read today from a page without an operating income (a bank) alone.
    return bool(stored) and store_module.rows_are_current(stored)


def series(symbol: str, store_module=None, path=None,
           budget_s: float = FETCH_BUDGET_S) -> list[dict]:
    """The quarters of a ticker, from the store while fresh, otherwise fetched once."""
    if store_module is None:
        return fetch(symbol, budget_s)
    stored = _read(symbol, store_module, path)
    if _current(stored, store_module):
        return stored
    # One fetch per ticker at a time: a page view, a reload and a second tab ask for the same
    # series, and a cold or week-stale store would otherwise run one fetch each - every one of
    # them holding a request thread and up to MAX_BYTES for the whole budget. The background
    # loads have a cap; the ones the page waits for need to share as well. Only the leader
    # fetches, the others read what it stored.
    with _INFLIGHT_LOCK:
        flight = _INFLIGHT.get(symbol)
        leader = flight is None
        if leader:
            flight = Event()
            _INFLIGHT[symbol] = flight
    if not leader:
        # Bounded: the leader gives up at its own budget, and a waiter must never outlive it - a
        # name lookup can outlast the socket timeout, so the wait gets a second on top.
        finished = flight.wait(budget_s + 1.0)
        fetched = _read(symbol, store_module, path)
        if _current(fetched, store_module):
            return fetched
        if not finished:
            # The leader is still working; nothing has failed yet. Saying "no result" here would
            # report a failure that has not happened.
            raise SourceUnavailable(f"geteilter Abruf für {symbol} noch nicht abgeschlossen")
        # Nothing usable came out of the shared fetch. An empty list would look like "this paper has
        # no quarters" and the caller would label the row "Quartalsreihe zu kurz" - a wrong reason
        # that also skips the recovery. The failure is the same one the leader saw, so it is raised
        # the same way: growth() then starts the background load and names it.
        raise SourceUnavailable(f"geteilter Abruf für {symbol} ohne Ergebnis")
    # The running fetch also counts as a pending load: otherwise growth() would start a second one
    # for the same ticker the moment a waiter gives up, and two requests would run side by side.
    counted = False
    with _PENDING_LOCK:
        if len(_PENDING) < MAX_PENDING:
            _PENDING.add(symbol)
            counted = True
    try:
        periods = fetch(symbol, budget_s)
        store_module.write_quarterly_source(symbol, periods, path)
        return periods
    finally:
        with _PENDING_LOCK:
            if counted:
                _PENDING.discard(symbol)
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(symbol, None)
        flight.set()


#: One fetch per ticker at a time: a page view, a reload and a second tab ask for the same series.
_PENDING: set[str] = set()
_PENDING_LOCK = Lock()
#: And a fixed number of loads at once. The ticker comes from the request, so a caller can ask for as
#: many distinct series as it likes; without a cap every one of them would own a thread, a socket and
#: up to MAX_BYTES until its budget runs out.
MAX_PENDING = 8


def refresh_in_background(symbol: str, store_module, path=None,
                          budget_s: float = FETCH_BUDGET_S * 4) -> bool:
    """Load a series after the answer was sent, so no request ever waits on a slow source.

    Returns whether a load was started. A second call for the same ticker while one runs does
    nothing - the queue would otherwise grow with every reload.
    """
    with _PENDING_LOCK:
        if symbol in _PENDING or len(_PENDING) >= MAX_PENDING:
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
    answer = {"revenue": None, "eps": None, "periods": len(periods), "step_days": None,
              "revenue_total": None, "operating_income_total": None}
    step = _step_days(periods)
    if step is None:
        return answer
    answer["step_days"] = step
    count = 4 if step < 130 else 2
    now = _window(periods, 0, count, step)
    before = _window(periods, count, count, step)
    if not now:
        return answer

    def total(chunk, key):
        values = [row.get(key) for row in chunk]
        if any(value is None for value in values):
            return None
        return sum(values)

    for key in ("revenue", "eps"):
        if before is None:
            continue
        present, earlier = total(now, key), total(before, key)
        if present is None or earlier is None or earlier <= 0:
            continue
        answer[key] = present / earlier - 1
    # The two sums the page divides by a balance sheet figure. Kept apart from the growth: a window
    # can be too short for a comparison and still carry a usable twelve month total.
    for key, name in (("revenue", "revenue_total"), ("operating_income", "operating_income_total")):
        present = total(now, key)
        answer[name] = present * MILLIONS if present is not None else None
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
