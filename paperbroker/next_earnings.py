"""The next earnings date of ONE title, from Finnhub's calendar endpoint.

Why Finnhub and not Nasdaq
--------------------------
``adapters/earnings/NasdaqEarningsAdapter.py`` reads its date from
``api.nasdaq.com/api/analyst/<SYMBOL>/earnings-date``. Measured on 2026-11-13:
that endpoint answers HTTP 200 for every symbol and carries NO date for any of
them (``earningsDate: null`` across the whole verified sample), so a scan that
needs the next report cannot be built on it. Finnhub's
``/api/v1/calendar/earnings`` answers the date for a symbol inside a date window
and needs nothing but the key.

The answer is UNSORTED
----------------------
Measured 2026-11-14 with this exact client, ``symbol=CTVA``: the rows came back
as ``2027-05-03`` first and ``2027-02-01`` second - a caller that takes the
first row gets the wrong quarter. The rule is therefore explicit: keep every
row of the queried symbol, drop what lies before ``today`` and take the
EARLIEST remaining date (``parse_next_earnings``). Nothing here trusts an order.

A key that is absent is not a date
----------------------------------
Without ``FINNHUB_API_KEY`` in the environment there is no query at all, and
``get_next_earnings`` raises ``NextEarningsError("no_api_key")`` instead of
returning ``None``: "we could not ask" and "the title reports on no day" are
different statements, and a scan that merges them would report a hole as a
fact about the company. A title inside the window with no row is the ordinary
``None`` answer ("no date known"), never a guessed date.

Cache
-----
One JSON file, one entry per symbol, TTL 12 h (``DEFAULT_TTL_SECONDS``): report
dates move rarely but vendors revise them, so a same-day revision stays visible
while the 30 gap candidates of one scan do not cost 30 queries on every reload.
The cache holds ``null`` too - a title the source knows nothing about is
remembered as such for the same 12 h instead of being asked again immediately.
A cache that cannot be read or written is a LOG line, never an error: the file
is an accelerator, not a source of truth. Writes are serialised because the
scan calls this adapter from several threads.

The token never leaves this module in a message
-----------------------------------------------
The key travels as the ``token`` query parameter (as the API requires) and is
therefore part of the URL ``requests`` sees - so no exception text is ever
forwarded: an error carries a stable ``code``, the HTTP status and a sentence
that names the failure class, not the request.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import requests

SOURCE_NAME = "finnhub_earnings_calendar"
EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
#: The environment variable the key is read from, spelled once.
ENV_API_KEY = "FINNHUB_API_KEY"
#: How far ahead the window reaches: 270 days cover three report cycles plus a
#: margin, so a title whose next date is a year out still answers instead of
#: looking like "no date".
DEFAULT_WINDOW_DAYS = 270
#: 12 h - long enough that one scan day does not query the same title twice,
#: short enough that a revision lands the same day.
DEFAULT_TTL_SECONDS = 12 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 8.0
#: The cache lives beside the other source caches (the compose stack mounts
#: ``cboe_cache`` at ``/app/.cache``). A checkout that has no ``/app`` falls back
#: to the working directory, so the adapter is usable outside the image too.
DEFAULT_CACHE_PATH = "/app/.cache/next_earnings.json"
FALLBACK_CACHE_PATH = ".cache/next_earnings.json"
CACHE_VERSION = 1

# --- why a query failed ---------------------------------------------------- #
CODE_NO_API_KEY = "no_api_key"
CODE_TRANSPORT_ERROR = "transport_error"
CODE_UNAUTHORIZED = "unauthorized"
CODE_THROTTLED = "throttled"
CODE_HTTP_ERROR = "http_error"
CODE_INVALID_PAYLOAD = "invalid_payload"

logger = logging.getLogger(__name__)


class NextEarningsError(Exception):
    """A failed or impossible query, with a stable code the caller can act on.

    ``status`` is the HTTP status when there was an answer, ``None`` when the
    request was never made (no key) or never landed (transport). The message is
    a sentence for a log or a JSON field and contains no URL, no query and no
    token.
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


def default_cache_path():
    """``/app/.cache`` when that directory exists, else the local ``.cache``."""
    parent = os.path.dirname(DEFAULT_CACHE_PATH)
    return DEFAULT_CACHE_PATH if os.path.isdir(parent) else FALLBACK_CACHE_PATH


def resolve_token(token=None):
    """The key to use: an explicitly passed one, else the environment.

    ``None`` means "read the environment"; an explicit string (including the
    empty one) is taken as it is. That distinction lets a test state "there is
    no key" without depending on the machine it runs on.
    """
    if token is not None:
        return token.strip()
    return (os.environ.get(ENV_API_KEY) or "").strip()


def _as_date(value):
    """``"2027-02-01"`` / ``date`` / ``datetime`` -> ``date``, else ``None``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _clock(now_fn):
    """The adapter's clock as a timezone-aware UTC ``datetime``."""
    value = (now_fn or (lambda: datetime.now(UTC)))()
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    inner = getattr(value, "datetime", None)          # arrow.Arrow
    if isinstance(inner, datetime):
        return inner if inner.tzinfo is not None else inner.replace(tzinfo=UTC)
    raise TypeError(f"now_fn must return a datetime, not {type(value).__name__}")


def calendar_rows(payload):
    """``earningsCalendar`` as a list of mappings, or an empty list.

    ``None`` (a title the source knows nothing about) and a missing key are both
    an empty answer; the caller decides whether that is acceptable.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("earningsCalendar")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def parse_next_earnings(payload, symbol, today):
    """The EARLIEST report date at or after ``today`` for ``symbol``, or ``None``.

    The answer is unsorted (see the module docstring), so every row is read and
    the minimum wins - never the first one. A row of another symbol, a date
    before ``today`` and a date that does not parse are all skipped: a scan must
    not be moved to a quarter that is already gone, and a broken row must cost
    that row only.
    """
    wanted = (symbol or "").strip().upper()
    if not wanted:
        return None
    floor = _as_date(today)
    if floor is None:
        raise ValueError(f"today is not a date: {today!r}")
    earliest = None
    for row in calendar_rows(payload):
        if str(row.get("symbol") or "").strip().upper() != wanted:
            continue
        day = _as_date(row.get("date"))
        if day is None or day < floor:
            continue
        if earliest is None or day < earliest:
            earliest = day
    return earliest


def requests_transport(url, headers, timeout):
    """``(status, body)`` - an HTTP status is RETURNED, never raised, as elsewhere."""
    response = requests.get(url, headers=headers, timeout=timeout)
    return response.status_code, response.content


@dataclass(frozen=True)
class CacheEntry:
    """One remembered answer: the date (or ``None``) and when it was asked."""

    next_earnings_date: date | None
    fetched_at: datetime

    def is_fresh(self, clock, ttl_seconds):
        age = (clock - self.fetched_at).total_seconds()
        return 0 <= age < ttl_seconds


class NextEarningsAdapter:
    """Next report date per symbol from Finnhub, behind a 12 h file cache.

    The adapter owns no network and no clock of its own: both come in through
    the constructor, so a test can drive it without either. Every method is safe
    to call from several threads - the cache file is read and written under one
    lock.
    """

    def __init__(self, token=None, transport=None, cache_path=None,
                 ttl_seconds=DEFAULT_TTL_SECONDS, now_fn=None,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 window_days=DEFAULT_WINDOW_DAYS):
        self._token = token
        self._transport = transport or requests_transport
        self._cache_path = cache_path if cache_path is not None else default_cache_path()
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._now_fn = now_fn
        self._timeout_seconds = float(timeout_seconds)
        self._window_days = max(1, int(window_days))
        self._lock = threading.Lock()

    # --- cache --------------------------------------------------------- #
    def _read_cache(self):
        """The cache file as ``{symbol: CacheEntry}`` - an unreadable file is empty."""
        if not self._cache_path:
            return {}
        try:
            with open(self._cache_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # A broken cache is not a broken source: log it, ask the source.
            logger.warning("next earnings cache unreadable (%s: %s)",
                           type(exc).__name__, exc)
            return {}
        rows = payload.get("symbols") if isinstance(payload, dict) else None
        entries = {}
        for symbol, row in (rows or {}).items():
            if not isinstance(row, dict):
                continue
            fetched = None
            if isinstance(row.get("fetched_at"), str):
                try:
                    fetched = datetime.fromisoformat(
                        row["fetched_at"].replace("Z", "+00:00"))
                except ValueError:
                    fetched = None
            if fetched is None:
                continue
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=UTC)
            entries[str(symbol).strip().upper()] = CacheEntry(
                next_earnings_date=_as_date(row.get("next_earnings_date")),
                fetched_at=fetched,
            )
        return entries

    def _write_cache(self, entries):
        """Persist the cache - best effort, never fatal (it is an accelerator)."""
        if not self._cache_path:
            return
        body = {
            "version": CACHE_VERSION,
            "source": SOURCE_NAME,
            "symbols": {
                symbol: {
                    "next_earnings_date": (None if entry.next_earnings_date is None
                                           else entry.next_earnings_date.isoformat()),
                    "fetched_at": entry.fetched_at.astimezone(UTC)
                                        .isoformat().replace("+00:00", "Z"),
                }
                for symbol, entry in entries.items()
            },
        }
        try:
            parent = os.path.dirname(self._cache_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            temporary = f"{self._cache_path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(temporary, self._cache_path)
        except OSError as exc:
            logger.warning("next earnings cache not written (%s: %s)",
                           type(exc).__name__, exc)

    def _lookup(self, symbol, clock):
        with self._lock:
            entry = self._read_cache().get(symbol)
        if entry is not None and entry.is_fresh(clock, self._ttl_seconds):
            return entry.next_earnings_date, True
        return None, False

    def _remember(self, symbol, value, clock):
        with self._lock:
            entries = self._read_cache()
            entries[symbol] = CacheEntry(next_earnings_date=value, fetched_at=clock)
            self._write_cache(entries)

    # --- the query ----------------------------------------------------- #
    def _url(self, symbol, today, token):
        window = {
            "from": today.isoformat(),
            "to": (today + timedelta(days=self._window_days)).isoformat(),
            "symbol": symbol,
            "token": token,
        }
        return f"{EARNINGS_URL}?{urllib.parse.urlencode(window)}"

    def get_next_earnings(self, symbol, *, use_cache=True):
        """The next report date of ``symbol`` at or after today, or ``None``.

        ``None`` is a fact about the title ("the source lists no date in the
        window"); a missing key, a transport failure, a refusal or an unreadable
        answer raise ``NextEarningsError`` with their own code, because those
        are facts about the QUERY. ``use_cache=False`` asks the source even if a
        fresh entry exists (the TTL has to be provable).
        """
        clean = (symbol or "").strip().upper()
        if not clean:
            raise NextEarningsError("invalid_symbol", "an empty symbol has no earnings date")
        clock = _clock(self._now_fn)
        today = clock.date()
        if use_cache:
            cached, fresh = self._lookup(clean, clock)
            if fresh:
                return cached

        token = resolve_token(self._token)
        if not token:
            # No key is a configuration fact, not a title without a date: the
            # caller must be able to tell "we did not ask" from "there is none".
            raise NextEarningsError(
                CODE_NO_API_KEY,
                f"{ENV_API_KEY} is not set - the next earnings date cannot be read",
            )

        url = self._url(clean, today, token)
        headers = {"Accept": "application/json"}
        try:
            status, body = self._transport(url, headers, self._timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - one class per failed request
            # Deliberately no exception text: requests embeds the URL (and with
            # it the token) in its messages.
            raise NextEarningsError(
                CODE_TRANSPORT_ERROR,
                f"the earnings calendar was not reachable ({type(exc).__name__})",
            ) from exc
        if status in (401, 403):
            raise NextEarningsError(
                CODE_UNAUTHORIZED,
                "the earnings calendar refused the key (HTTP 401/403)", status,
            )
        if status == 429:
            raise NextEarningsError(
                CODE_THROTTLED,
                "the earnings calendar throttled the request (HTTP 429)", status,
            )
        if status != 200:
            raise NextEarningsError(
                CODE_HTTP_ERROR, f"the earnings calendar answered HTTP {status}", status,
            )
        try:
            payload = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray))
                                 else body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise NextEarningsError(
                CODE_INVALID_PAYLOAD, "the earnings calendar answer is not JSON", status,
            ) from exc

        value = parse_next_earnings(payload, clean, today)
        # ``None`` is cached too: a title the source knows nothing about is
        # remembered as such instead of being asked again on the next reload.
        self._remember(clean, value, clock)
        return value
