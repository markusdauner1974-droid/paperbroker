"""The closing gap after an earnings release: measure it, then pick what to trade.

Why this module exists
----------------------
The existing earnings scan positions BEFORE the numbers (exit on the last
session before the release). The opposite trade needs the opposite input: the
close of the reaction session compared with the close of the session before it.
Nothing in the repo computed that, and nothing selected candidates by the size
of the move. This module does exactly those two things and nothing else - it
places no order, prices no option and knows no chain.

Source, measured (2026-11-12)
-----------------------------
``GET https://api.nasdaq.com/api/quote/<SYMBOL>/historical?assetclass=stocks&fromdate=YYYY-MM-DD&limit=N``
answers HTTP 200 without a key, an account or a cookie and carries
``data.tradesTable.rows[]`` with ``date`` ("11/11/2026"), ``open``, ``high``,
``low``, ``close`` ("$173.73") and ``volume`` ("339,214,800"), newest row
first. 151 titles at 8 in parallel: 45 s, every answer HTTP 200.

Yahoo's chart API is NOT used: it answered HTTP 429 after four calls. The
stdlib client is not used for the default transport either - api.nasdaq.com
refuses the Python stdlib stack (measured on 2026-11-11, see
``adapters/earnings/NasdaqEarningsAdapter.py``), which is why the default
transport is ``requests`` with ``urllib`` kept as the fallback.

What the source cannot do, and what this module therefore does not pretend
--------------------------------------------------------------------------
* A symbol with a dot (``PBR.A``) delivers no usable history at all. It is
  REFUSED before any request (``unsupported_symbol``): a silent empty answer
  would look like "no gap" instead of "not measurable".
* Rows are neither sorted nor repaired. They come in answer order (newest
  first) and a row with a missing or unparseable field is DROPPED, never
  filled with 0 - a 0 close would produce a -100 % gap that looks real.
* ``compute_gap`` answers ``None`` when the reaction session or the session
  before it is absent from the rows. "No gap" and "gap of zero" are different
  statements and the difference is the point of the whole scan.

Two stages, deliberately separated
----------------------------------
``compute_gap`` measures ONE title against ONE session. ``select_candidates``
ranks a whole day's measurements by the size of the move, after a turnover
hurdle that removes what cannot be traded: measured on 2026-11-11, the
largest gaps of the day belonged to penny stocks (SLXN +23.6 % on 0.9 M USD,
UCL +21.7 % at a 0.23 USD price) - a gap you cannot get filled is not a
signal. Both stages are pure functions over plain data so a caller can test
the day's selection without a network.

From a calendar day to that list
--------------------------------
``reaction_day_rows`` decides WHO is asked: every title the calendar listed on
the previous day (its session is usually gone, so the row says
``session_uncertain``) plus the ``before_market`` titles of the reaction day.
``fetch_gaps`` asks in parallel, because 220 titles serially is not a scan but a
wait. ``scan_gaps`` runs both and keeps every intermediate row and count, so a
day that yields three candidates can be explained instead of just accepted.

Clock
-----
``NasdaqHistoryAdapter`` accepts ``now_fn`` like its earnings sibling. This
slice has nothing to age - the close of a past session never changes, and the
injected cache is per ``(symbol, fromdate, limit)`` on purpose - so the clock
is accepted for a uniform adapter shape and is not consulted yet.
"""

from __future__ import annotations

import json
import math
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import requests

HISTORY_URL = (
    "https://api.nasdaq.com/api/quote/{symbol}/historical"
    "?assetclass=stocks&fromdate={fromdate}&limit={limit}"
)
# Measured with this exact string (see the order): the Chrome UA is answered
# with HTTP 200, `Mozilla/5.0` short form works through requests as well but
# the longer one is what was measured for THIS endpoint.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
}
DEFAULT_TIMEOUT_SECONDS = 6.0
# The source returns at most ``limit`` rows counted FROM ``fromdate`` onward -
# and it returns the NEWEST of them. The limit, not the fromdate, closes the
# asked window, so a count below the number of sessions the lookback holds drops
# exactly the reaction day the run is about. ``DEFAULT_LIMIT`` is the floor of
# the count; what a run actually asks for is ``LOOKBACK_LIMIT`` below.
DEFAULT_LIMIT = 32
DEFAULT_THRESHOLD_PCT = 2.0
DEFAULT_MIN_TURNOVER_USD = 5_000_000.0
DEFAULT_MAX_CANDIDATES = 30
DEFAULT_MAX_WORKERS = 8
# Measured: 151 titles at 8 in parallel took 45 s against the live endpoint. The
# window is 30 calendar days because the source rows are daily sessions - a
# reaction day plus its reference close always fits, and a wider window would
# only buy rows nobody reads.
GAP_LOOKBACK_DAYS = 30
# The row count a run asks for: the lookback window in the source's own terms.
# ``fromdate`` is ``reaction - GAP_LOOKBACK_DAYS``, so the distance the count has
# to bridge is exactly the lookback: 30 calendar days hold at most 23 sessions
# (weekends and holidays cut the rest), and the +5 is room, not a guess. It is
# derived from the WINDOW, never from today - an answer must not change with the
# clock - and it stays above ``DEFAULT_LIMIT`` so a narrow default can never
# truncate the day that was asked for.
LOOKBACK_LIMIT = max(DEFAULT_LIMIT, GAP_LOOKBACK_DAYS + 5)
# The shared cache of the endpoint keeps one entry per (symbol, fromdate, limit).
# 5000 entries cover several days of the whole calendar and stay small in RAM;
# a past close never changes, so entries are never aged out - only displaced.
DEFAULT_CACHE_ENTRIES = 5000

# The three session tokens a calendar row carries, spelled here rather than
# imported: this module deliberately imports nothing from earnings_calendar /
# earnings_scan (see the slice-1 decision), and the values are part of the
# calendar's JSON shape, not of its code. A row with anything else - including
# a missing session - is "unknown" and therefore uncertain.
SESSION_BEFORE = "before_market"
SESSION_AFTER = "after_market"
SESSION_UNKNOWN = "unknown"

GAP_UP = "up"
GAP_DOWN = "down"
GAP_NONE = "none"
GAP_DIRECTIONS = (GAP_UP, GAP_DOWN, GAP_NONE)

STRATEGY_BULL_CALL_DEBIT = "bull_call_debit"
STRATEGY_BEAR_PUT_DEBIT = "bear_put_debit"
# The spread follows the direction of the gap: a gap up is traded with calls,
# a gap down with puts. "none" has no strategy - and no candidate.
STRATEGY_FOR_GAP = {GAP_UP: STRATEGY_BULL_CALL_DEBIT, GAP_DOWN: STRATEGY_BEAR_PUT_DEBIT}

# Nasdaq symbols are letters, digits and a few separators; the dot is allowed by
# the exchange but not by this source (see the module docstring), so it is
# rejected on its own path to keep the reason distinguishable.
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^-]{1,12}$")
_UNUSABLE_NUMBERS = frozenset({"N/A", "NA", "--", "-", "NAN", "NULL", "NONE"})


class GapSourceError(Exception):
    """A failed source query, with a stable code the caller can act on.

    Own class on purpose: importing the earnings adapter's error would couple
    two sources that fail for different reasons and are fixed independently.
    """

    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self):
        return f"{self.code}: {self.message}"

    def as_dict(self):
        return {"code": self.code, "message": self.message, "http_status": self.status}


@dataclass(frozen=True)
class GapBar:
    """One session of the source, as delivered - immutable on purpose.

    A bar is a measured fact, not a mutable record: nothing downstream may
    "correct" a close. A volume of 0 is allowed (a halted session), a missing
    volume is not (such a row never becomes a bar).
    """

    session_date: date
    open: float
    close: float
    volume: float


@dataclass(frozen=True)
class GapPoint:
    """The gap of ONE session against the session before it."""

    reaction_date: date
    prev_close: float
    close: float
    volume: float
    gap_pct: float


@dataclass(frozen=True)
class Candidate:
    """One title that passed the threshold and the turnover hurdle."""

    symbol: str
    gap_pct: float
    direction: str
    strategy: str | None
    turnover_usd: float
    close: float
    volume: float


# --- parsing --------------------------------------------------------------- #
def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _number(value):
    """A finite float from ``"$1,234.50"`` / ``"339,214,800"`` / ``12.5``, else ``None``.

    ``None`` is the only "unusable" answer: a missing field must never be read
    as 0, because 0 is a real price and a real volume.
    """
    if isinstance(value, bool):                 # bool is an int; "True" is no price
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = _text(value).replace("$", "").replace(",", "").replace(" ", "")
    if not text or text.upper() in _UNUSABLE_NUMBERS:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _session_date(value):
    """``"11/11/2026"`` -> ``date(2026, 11, 11)``, or ``None``."""
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None


def _history_rows(payload):
    """``data.tradesTable.rows`` as a list of mappings, or an empty list.

    Missing rows are an EMPTY answer, not an error: Nasdaq answers a Sunday or
    a title it does not cover with a rows list that is absent or null. Whether
    that is acceptable is the caller's decision (``fetch_daily`` names it
    ``no_data``).
    """
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    table = data.get("tradesTable")
    if not isinstance(table, Mapping):
        return []
    rows = table.get("rows")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _bar_from_row(row):
    """One bar, or ``None`` when any of its fields is missing or unparseable."""
    session = _session_date(row.get("date"))
    opened = _number(row.get("open"))
    closed = _number(row.get("close"))
    volume = _number(row.get("volume"))
    if session is None or opened is None or closed is None or volume is None:
        return None
    return GapBar(session_date=session, open=opened, close=closed, volume=volume)


def parse_nasdaq_history(payload):
    """The bars of a Nasdaq history answer, in ANSWER order (newest first).

    Order is kept, never re-sorted: the source already sorts, and a caller that
    re-sorted would silently accept a source that changed its mind. Rows with a
    missing or unparseable field are dropped, one by one, so a single bad row
    costs that row and not the day.
    """
    bars = []
    for row in _history_rows(payload):
        bar = _bar_from_row(row)
        if bar is not None:
            bars.append(bar)
    return bars


# --- transport ------------------------------------------------------------- #
def urllib_transport(url, headers, timeout):
    """``(status, body)`` instead of raising on HTTP status - one place decides."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def requests_transport(url, headers, timeout):
    """``(status, body)`` via ``requests`` - the default for this host.

    Same contract as ``urllib_transport``: an HTTP status is RETURNED, not
    raised, so exactly one place classifies it. Only a transport failure (DNS,
    connect, read timeout) raises, and ``fetch_daily`` names it
    ``transport_error``.
    """
    response = requests.get(url, headers=headers, timeout=timeout)
    return response.status_code, response.content


def _normalise_symbol(symbol):
    """The symbol in source form, or ``GapSourceError`` when it cannot be asked.

    Refused BEFORE any request: a dotted symbol (``PBR.A``) is answered with an
    unusable history, and an empty one would query the bare endpoint. Both
    would come back as "no rows", i.e. as a gap that merely could not be
    measured - the caller must be able to tell those apart.
    """
    text = _text(symbol).upper()
    if not text:
        raise GapSourceError("unsupported_symbol", "an empty symbol has no history")
    if "." in text:
        raise GapSourceError(
            "unsupported_symbol",
            f"'{text}' contains a dot; this source answers no usable history for it",
        )
    if not _SYMBOL_PATTERN.match(text):
        raise GapSourceError("unsupported_symbol", f"'{text}' is not a symbol this source serves")
    return text


def _iso_day(value):
    """A ``date``/ISO string rebuilt as ``YYYY-MM-DD`` - only parsed dates reach the URL."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise GapSourceError(
            "invalid_fromdate", f"'{text}' is not an ISO date (YYYY-MM-DD)"
        ) from exc


class BoundedCache(dict):
    """A mapping with a hard ceiling for the shared history cache.

    ``NasdaqHistoryAdapter`` accepts any mutable mapping, so the bound lives
    here instead of inside the adapter: a caller that shares one adapter across
    requests needs a store that cannot grow without end, and a caller that wants
    no caching still passes ``None``.

    Eviction is FIFO by insertion order - the oldest entry leaves when a new one
    would exceed ``max_entries``. There is no TTL on purpose: the close of a past
    session never changes, so age is not a reason to forget; pressure is. A
    second assignment for an existing key refreshes the value in place and does
    NOT make it young, because insertion order is the whole policy here.

    Writes are serialised: the endpoint's fetch pool calls the shared adapter
    from several threads, and an eviction is a read-then-delete - two of them
    interleaving could delete the wrong entry or raise. A read of a key that is
    evicted at that exact moment is the caller's concern (``dict.get`` is a
    single lookup and answers ``None``, which the adapter treats as a miss).
    """

    def __init__(self, max_entries=DEFAULT_CACHE_ENTRIES, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_entries = max(0, int(max_entries))
        self._lock = threading.Lock()
        while len(self) > self.max_entries:
            del self[next(iter(self))]

    def __setitem__(self, key, value):
        with self._lock:
            if key in self:
                # Order is the policy: refreshing a value does not make it young.
                super().__setitem__(key, value)
                return
            super().__setitem__(key, value)
            while len(self) > self.max_entries:
                # The oldest key is the first one in insertion order - and it is
                # never the key just written, unless the ceiling is 0.
                del self[next(iter(self))]


class NasdaqHistoryAdapter:
    """Daily closes from Nasdaq, with an optional caller-supplied cache.

    The cache is any mutable mapping: one entry per ``(symbol, fromdate, limit)``.
    There is no built-in store and no TTL - a past session's close never
    changes, and a caller that wants a bound supplies a mapping that has one.
    Without ``cache`` every call queries - nothing is remembered behind the
    caller's back.
    """

    timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    def __init__(self, transport=None, cache=None, now_fn=None):
        self._transport = transport or requests_transport
        self._cache = cache
        # Accepted for the same shape as the earnings adapter; not consulted in
        # this slice (see the module docstring).
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def fetch_daily(self, symbol, fromdate, limit=DEFAULT_LIMIT):
        """The daily bars from ``fromdate`` on, newest first - at most ``limit``.

        The window is only as wide as ``limit`` allows: ``fromdate`` opens it and
        the count closes it, so the caller must pass ``LOOKBACK_LIMIT`` for a
        gap run or the reaction day falls out of its own question (see
        ``LOOKBACK_LIMIT``).

        Every failure is a ``GapSourceError`` with a stable code: the caller
        must be able to say WHY a title has no gap instead of showing 0 %.
        """
        symbol = _normalise_symbol(symbol)
        day = _iso_day(fromdate)
        count = max(1, int(limit))
        key = (symbol, day, count)
        if self._cache is not None:
            # One lookup, not "in" then "[]": a shared cache may evict the key
            # between the two, and a KeyError here would cost the title a gap.
            cached = self._cache.get(key)
            if cached is not None:
                return list(cached)

        url = HISTORY_URL.format(symbol=symbol, fromdate=day, limit=count)
        try:
            status, body = self._transport(url, dict(_HEADERS), self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - every transport failure is one class here
            raise GapSourceError("transport_error", f"{type(exc).__name__}: {exc}") from exc
        if status != 200:
            raise GapSourceError("http_error", f"the source answered HTTP {status}", status)
        if not body:
            raise GapSourceError("no_data", "the source answered an empty body", status)
        try:
            payload = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise GapSourceError("invalid_payload", "the source answer is not JSON", status) from exc

        bars = parse_nasdaq_history(payload)
        if not bars:
            raise GapSourceError("no_data", "the source answered no usable history rows", status)
        if self._cache is not None:
            self._cache[key] = list(bars)
        return bars


# --- measurement and selection --------------------------------------------- #
def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def compute_gap(bars, reaction_date):
    """The gap of ``reaction_date`` against the nearest session before it.

    ``None`` when either side is missing or the previous close is not positive:
    a gap needs two real closes. A missing day is NOT a gap of 0 - that value
    would pass every threshold check as a "small move" and hide the hole.
    """
    reaction = _as_date(reaction_date)
    if reaction is None:
        return None
    current = next((bar for bar in bars if bar.session_date == reaction), None)
    if current is None:
        return None

    previous = None
    for bar in bars:
        if bar.session_date < reaction and (previous is None or bar.session_date > previous.session_date):
            previous = bar
    # A non-positive previous close makes the ratio meaningless (division by 0,
    # or a sign that would invert the gap) - no gap rather than an invented one.
    if previous is None or previous.close <= 0:
        return None

    gap_pct = round((current.close - previous.close) / previous.close * 100, 2)
    return GapPoint(
        reaction_date=reaction,
        prev_close=previous.close,
        close=current.close,
        volume=current.volume,
        gap_pct=gap_pct,
    )


def classify_gap(gap_pct, threshold_pct):
    """``"up"`` / ``"down"`` / ``"none"`` - both thresholds inclusive."""
    if gap_pct >= threshold_pct:
        return GAP_UP
    if gap_pct <= -threshold_pct:
        return GAP_DOWN
    return GAP_NONE


def strategy_for_direction(direction):
    """The spread that follows the direction, or ``None`` for ``"none"``."""
    return STRATEGY_FOR_GAP.get(direction)


def _field(row, name):
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def select_candidates(
    rows,
    threshold_pct=DEFAULT_THRESHOLD_PCT,
    min_turnover_usd=DEFAULT_MIN_TURNOVER_USD,
    max_candidates=DEFAULT_MAX_CANDIDATES,
):
    """The tradable gaps of a day, biggest move first.

    A title qualifies when its move clears ``threshold_pct`` AND its session
    turnover (``close * volume``) reaches ``min_turnover_usd``: the biggest gaps
    of a day are regularly penny stocks that cannot be filled. Sorting is by
    ``abs(gap_pct)`` descending, ties by ``symbol`` ascending, so the result is
    stable and reproducible.

    Never padded: fewer qualifying titles means fewer candidates. ``max_candidates``
    is a cap, not a target.
    """
    candidates: list[Candidate] = []
    for row in rows or ():
        symbol = _text(_field(row, "symbol")).upper()
        gap_pct = _number(_field(row, "gap_pct"))
        close = _number(_field(row, "close"))
        volume = _number(_field(row, "volume"))
        if not symbol or gap_pct is None or close is None or volume is None:
            continue
        direction = classify_gap(gap_pct, threshold_pct)
        if direction == GAP_NONE:
            continue
        turnover = close * volume
        if turnover < min_turnover_usd:
            continue
        candidates.append(
            Candidate(
                symbol=symbol,
                gap_pct=gap_pct,
                direction=direction,
                strategy=strategy_for_direction(direction),
                turnover_usd=turnover,
                close=close,
                volume=volume,
            )
        )
    candidates.sort(key=lambda candidate: (-abs(candidate.gap_pct), candidate.symbol))
    return candidates[: max(0, int(max_candidates))]


# --- the day: candidate set, fetch, scan ----------------------------------- #
@dataclass(frozen=True)
class GapFetchResult:
    """What a parallel fetch of one reaction day produced.

    ``rows`` are the measured titles in input order, ``failures`` names every
    other symbol with the code that stopped it. The two lists are disjoint on
    purpose: a title is either measured or explained, never both and never
    silently missing.
    """

    rows: list[dict]
    failures: dict[str, str]


@dataclass(frozen=True)
class GapScanResult:
    """One whole reaction day: who was a candidate, who had a gap, who is left."""

    candidates: list[Candidate]
    candidate_rows: list[dict]
    gap_rows: list[dict]
    failures: dict[str, str]
    counts: dict[str, int]


def _calendar_iso(value, name):
    """The ISO string of a calendar day - ``ValueError``, never a guessed day.

    A wrong date silently shifted by a day would put the whole scan on the
    wrong session, and nothing downstream could tell.
    """
    day = _as_date(value)
    if day is None:
        raise ValueError(f"{name} is not a date: {value!r}")
    return day.isoformat()


def _calendar_row(row, earnings_date, session_uncertain):
    """One calendar row in the shape the scan works with - a NEW dict, never a copy.

    ``session_uncertain`` is set here and not inferred later: the source deletes
    past sessions, so "no session" must travel with the row as a doubt, not be
    read downstream as "after the close".
    """
    return {
        "symbol": _text(_field(row, "symbol")).upper(),
        "name": _text(_field(row, "name")),
        "session": _text(_field(row, "session")) or SESSION_UNKNOWN,
        "session_uncertain": bool(session_uncertain),
        "earnings_date": earnings_date,
        "market_cap": _field(row, "market_cap"),
    }


def reaction_day_rows(previous_rows, day_rows, *, previous_date, day_date):
    """The titles a reaction day can be traded on, and nothing else.

    The set is the union of two statements:

    * EVERY title the calendar listed on the previous day - those are the
      releases that happened after that session closed. Their session is
      usually gone (``unknown``), so the row carries ``session_uncertain``
      instead of pretending to know when the numbers came out,
    * only the ``before_market`` titles of the reaction day itself: a title
      releasing after the close of that day has no gap YET.

    A symbol listed on both days appears ONCE, with the reaction day's row -
    that is the one with a known session. Nothing is merged field by field: a
    half-and-half row would look like evidence nobody has. Titles without a
    symbol are dropped, and the order is stable: reaction day first, then the
    previous day, each in input order.
    """
    day_iso = _calendar_iso(day_date, "day_date")
    previous_iso = _calendar_iso(previous_date, "previous_date")

    rows = []
    seen = set()
    for row in day_rows or ():
        if _text(_field(row, "session")) != SESSION_BEFORE:
            continue
        candidate = _calendar_row(row, day_iso, session_uncertain=False)
        if not candidate["symbol"] or candidate["symbol"] in seen:
            continue
        seen.add(candidate["symbol"])
        rows.append(candidate)

    for row in previous_rows or ():
        known = _text(_field(row, "session"))
        candidate = _calendar_row(
            row,
            previous_iso,
            session_uncertain=known not in (SESSION_BEFORE, SESSION_AFTER),
        )
        if not candidate["symbol"] or candidate["symbol"] in seen:
            continue
        seen.add(candidate["symbol"])
        rows.append(candidate)

    return rows


def _fetch_one(source, symbol, reaction, fromdate, limit=LOOKBACK_LIMIT):
    """``(symbol, gap, failure_code)`` - exactly one of gap/code is set.

    No exception leaves this function: the caller runs it in a pool, and one
    title that fails must not cost the other 219. An adapter that breaks its own
    contract (anything but ``GapSourceError``) is named ``unexpected_error``
    rather than being allowed to take the pool down.
    """
    try:
        bars = source.fetch_daily(symbol, fromdate, limit)
        gap = compute_gap(bars, reaction)
    except GapSourceError as exc:
        return symbol, None, exc.code
    except Exception:  # noqa: BLE001 - one broken symbol must not cost the day
        # An adapter that breaks its own contract still gets a STABLE code, so a
        # caller can switch on it without meeting a class name one day.
        return symbol, None, "unexpected_error"
    if gap is None:
        # The rows came back but the reaction session is not among them: the
        # title could not be measured, it did not "not move".
        return symbol, None, "no_reaction_bar"
    return symbol, gap, None


def fetch_gaps(symbols, reaction_date, *, adapter=None, max_workers=DEFAULT_MAX_WORKERS,
               deadline_seconds=None):
    """Measure every symbol against ``reaction_date`` - in parallel, one by one.

    Serial fetching is not an option: the measured day had 220 titles, and the
    answer for one title is independent of every other, so the pool is the
    whole point. ``rows`` keeps the order of ``symbols`` (the selection sorts
    later, on purpose), and every symbol that produced no gap ends up in
    ``failures`` with the code that stopped it - ``unsupported_symbol``,
    ``http_error``, ``no_data``, ``no_reaction_bar`` or ``transport_error``.

    ``deadline_seconds`` is the whole run's budget, not a per-title timeout
    (the adapter has its own). ``None`` waits for every title, which is the
    original behaviour. With a deadline, the titles that are not finished when
    it expires answer ``failures[symbol] = "deadline_exceeded"`` instead of
    being dropped: the day stays countable, and a request cannot occupy a
    worker for as long as the slowest batch of fetches. Nothing is raised and
    nothing is capped - every symbol is still ASKED, only the waiting ends.
    """
    reaction = _as_date(reaction_date)
    if reaction is None:
        raise GapSourceError("invalid_fromdate", f"{reaction_date!r} is not a date")
    fromdate = reaction - timedelta(days=GAP_LOOKBACK_DAYS)
    # The count is derived from the window this run asks for, not from the
    # default: the source answers the NEWEST ``limit`` rows from ``fromdate``
    # on, so a count that does not span the whole lookback silently drops the
    # reaction day out of its own question. ``reaction - fromdate`` IS the
    # lookback; the +5 is room for the sessions the calendar does not expect
    # (holidays, half days). Deliberately independent of "today": the same day
    # must answer the same way whenever it is scanned.
    limit = max(DEFAULT_LIMIT, (reaction - fromdate).days + 5)
    source = adapter or NasdaqHistoryAdapter()

    wanted = [_text(symbol).upper() for symbol in symbols or ()]
    if not wanted:
        return GapFetchResult(rows=[], failures={})

    frist = None if deadline_seconds is None else max(0.0, float(deadline_seconds))
    pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
    paare = []
    try:
        # One future per symbol, kept in input order: the rows below stay stable
        # without a sort, exactly like ``pool.map`` did.
        paare = [(symbol, pool.submit(_fetch_one, source, symbol, reaction, fromdate, limit))
                 for symbol in wanted]
        fertig, _offen = wait([future for _symbol, future in paare], timeout=frist)
    finally:
        # Waiting for the pool on an expired deadline would undo the deadline:
        # the unanswered titles are already accounted for as failures, so the
        # running fetches finish in the background and are not awaited.
        pool.shutdown(wait=frist is None, cancel_futures=frist is not None)

    rows = []
    failures = {}
    for symbol, future in paare:
        if future not in fertig:
            failures[symbol] = "deadline_exceeded"
            continue
        symbol, gap, code = future.result()
        if code is not None or gap is None:
            failures[symbol] = code if code is not None else "no_reaction_bar"
            continue
        rows.append({"symbol": symbol, "gap_pct": gap.gap_pct, "close": gap.close, "volume": gap.volume})
    return GapFetchResult(rows=rows, failures=failures)


def _counts(candidate_rows, gap_rows, candidates, failures, threshold_pct, min_turnover_usd):
    """The numbers of a run, computed from the rows themselves - not from hopes.

    ``turnover_passed`` counts titles that cleared BOTH hurdles, because that is
    what the word promises: a title that moved 10 % on 0.4 M USD did not pass
    the turnover hurdle.
    """
    over_threshold = 0
    turnover_passed = 0
    for row in gap_rows:
        gap_pct = _number(row.get("gap_pct"))
        close = _number(row.get("close"))
        volume = _number(row.get("volume"))
        if gap_pct is None or classify_gap(gap_pct, threshold_pct) == GAP_NONE:
            continue
        over_threshold += 1
        if close is not None and volume is not None and close * volume >= min_turnover_usd:
            turnover_passed += 1
    return {
        "candidate_rows": len(candidate_rows),
        "gaps_computed": len(gap_rows),
        "over_threshold": over_threshold,
        "turnover_passed": turnover_passed,
        "selected": len(candidates),
        "failures": len(failures),
    }


def scan_gaps(
    previous_rows,
    day_rows,
    reaction_date,
    *,
    previous_date,
    threshold_pct=DEFAULT_THRESHOLD_PCT,
    min_turnover_usd=DEFAULT_MIN_TURNOVER_USD,
    max_candidates=DEFAULT_MAX_CANDIDATES,
    adapter=None,
    max_workers=DEFAULT_MAX_WORKERS,
):
    """One reaction day from the calendar rows to the trading candidates.

    Three steps, each already testable on its own: build the candidate set,
    fetch the gaps in parallel, select. The result keeps the intermediate
    stages - ``candidate_rows`` before the gap check, ``gap_rows`` after it -
    so a caller can show WHY a day produced three candidates instead of thirty,
    and ``counts`` says the same in numbers.
    """
    candidate_rows = reaction_day_rows(
        previous_rows,
        day_rows,
        previous_date=previous_date,
        day_date=reaction_date,
    )
    fetched = fetch_gaps(
        [row["symbol"] for row in candidate_rows],
        reaction_date,
        adapter=adapter,
        max_workers=max_workers,
    )
    candidates = select_candidates(
        fetched.rows,
        threshold_pct=threshold_pct,
        min_turnover_usd=min_turnover_usd,
        max_candidates=max_candidates,
    )
    counts = _counts(
        candidate_rows,
        fetched.rows,
        candidates,
        fetched.failures,
        threshold_pct,
        min_turnover_usd,
    )
    return GapScanResult(
        candidates=candidates,
        candidate_rows=candidate_rows,
        gap_rows=fetched.rows,
        failures=fetched.failures,
        counts=counts,
    )
