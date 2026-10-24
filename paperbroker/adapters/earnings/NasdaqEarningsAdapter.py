"""Earnings date source: Nasdaq's public JSON endpoints, plus a bounded cache.

Why Nasdaq and not Yahoo
------------------------
Measured on the deployment host (2026-10-24): Yahoo's ``quoteSummary`` answered
HTTP 429 for the crumb handshake, i.e. without an account and a session dance it
is not usable. Nasdaq's ``api.nasdaq.com`` answers both endpoints used here
without a key, an account or a cookie:

* ``/api/analyst/<SYMBOL>/earnings-date?assetclass=stocks`` - the next date, the
  release session in plain text and a marker that separates an estimated date
  ("is expected* to report") from a stated one,
* ``/api/calendar/earnings?date=YYYY-MM-DD`` - a structured row per reporting
  company (``time-pre-market`` / ``time-after-hours`` / ``time-not-supplied``),
  used only as a cross-check.

No paid API, no access key, no external account is introduced.

What the source cannot do
-------------------------
Measured limits, kept visible instead of papered over:

* Nasdaq labels future dates as estimates ("expected*", "is estimated ... derived
  from an algorithm"). A ``confirmed`` date is therefore the exception, not the
  rule - the adapter never promotes an estimate.
* The calendar endpoint is a daily list; asking "when does X report" would need a
  window scan, so it is only ever consulted for the date the first endpoint named.
* The endpoints carry no timezone. Only the session (before/after the open) is
  read, never a clock time.

Cache and freshness
-------------------
Two different limits, on purpose:

* ``ttl_seconds`` (6 h) decides when the source is asked again. Earnings dates
  move rarely, but vendors revise them; six hours keeps a same-day revision
  visible without hammering the source on every page load.
* ``freshness_limit_seconds`` (48 h) decides whether a stored proof may still
  AUTHORISE anything. 48 h survives a market-closed weekend (Friday check stays
  valid on Sunday) while three missed days turn the record ``stale`` - and a
  stale record blocks, it does not warn.

A failed live query never turns an old proof into a new one: ``source_checked_at_utc``
keeps the time of the last REAL check and the record carries ``live_query_failed``.
"""

try:
    import fcntl  # POSIX only; the deployed image is Linux
except ImportError:       # pragma: no cover - exercised by patching fcntl to None
    fcntl = None          # Windows: the in-process lock still serialises threads
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

from ...earnings_calendar import (
    CONFIRMATION_UNKNOWN,
    CONFIRMATIONS,
    FLAG_CACHE_TIMESTAMP_FUTURE,
    FLAG_CACHE_USED,
    FLAG_DATE_IN_PAST,
    FLAG_ESTIMATED,
    FLAG_LIVE_FAILED,
    FLAG_SESSION_UNKNOWN,
    FLAG_SOURCE_NOT_CONFIGURED,
    SCHEMA_VERSION,
    SESSION_UNKNOWN,
    SESSIONS,
    STATUS_FRESH,
    STATUS_INVALID,
    STATUS_STALE,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    earnings_body,
    merge_calendar_check,
    parse_nasdaq_calendar_row,
    parse_nasdaq_earnings_date,
)

SOURCE_NAME = "nasdaq_earnings_date"
DATE_URL = "https://api.nasdaq.com/api/analyst/{symbol}/earnings-date?assetclass=stocks"
CALENDAR_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json",
}

DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_FRESHNESS_LIMIT_SECONDS = 48 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 6.0
DEFAULT_CROSS_CHECK_TIMEOUT_SECONDS = 3.0
DEFAULT_CACHE_PATH = ".cache/earnings.json"
# One entry per ticker; the route accepts any 1-6 letter symbol, so an
# unthrottled public caller could otherwise grow the file without bound.
MAX_CACHE_ENTRIES = 512
# Serialises the read-modify-write inside one process (threads) - flock alone
# would not help two threads of the same process, because the lock is per open
# file description and both would take it.
_CACHE_LOCK = threading.Lock()


class EarningsSourceError(Exception):
    """A failed source query, with a stable code for the JSON contract."""

    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self):
        return {"code": self.code, "message": self.message, "http_status": self.status}


def _utc_now(now_fn):
    wert = now_fn()
    if isinstance(wert, datetime):
        return wert.astimezone(UTC) if wert.tzinfo else wert.replace(tzinfo=UTC)
    # arrow and anything else with .to("utc").datetime
    return wert.to("utc").datetime.replace(tzinfo=UTC)


def _iso(wert):
    return wert.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def urllib_transport(url, headers, timeout):
    """(status, body) instead of raising on HTTP status - one place decides."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class EarningsCache:
    """One JSON file, one entry per ticker, written atomically.

    A corrupt or unreadable file is a cache MISS, never an exception: a broken
    cache may cost a live query, it may not take the screener down.
    """

    def __init__(self, path=None, max_entries=MAX_CACHE_ENTRIES):
        self.path = path or DEFAULT_CACHE_PATH
        self.max_entries = max(1, int(max_entries))

    def _read_all(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                roh = json.load(fh)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError, ValueError):
            return {}
        if not isinstance(roh, dict) or not isinstance(roh.get("entries"), dict):
            return {}
        return roh["entries"]

    def get(self, ticker):
        entry = self._read_all().get(ticker)
        if not isinstance(entry, dict):
            return None
        if not self._valid(entry):
            return None
        return entry

    @staticmethod
    def _valid(entry):
        datum = entry.get("next_earnings_date")
        if datum is not None:
            try:
                datetime.strptime(datum, "%Y-%m-%d")
            except (TypeError, ValueError):
                return False
        if entry.get("release_session") not in SESSIONS:
            return False
        if entry.get("date_confirmation") not in CONFIRMATIONS:
            return False
        try:
            datetime.strptime(entry.get("source_checked_at_utc") or "", "%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return False
        return True

    def put(self, ticker, entry):
        """Read-modify-write, serialised and atomic.

        Two writers must not lose each other's entry: the read, the bounding and
        the replace happen under one lock (threads) plus an exclusive ``flock``
        on a sibling lock file (other processes). ``os.replace`` keeps readers
        seeing either the old or the new file - never half of one.
        """
        verzeichnis = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(verzeichnis, exist_ok=True)
        except OSError:
            return False
        sperre = self.path + ".lock"
        try:
            with _CACHE_LOCK, open(sperre, "a", encoding="utf-8") as sperr_datei:
                if fcntl is not None:
                    fcntl.flock(sperr_datei.fileno(), fcntl.LOCK_EX)
                try:
                    self._write_locked(ticker, entry, verzeichnis)
                finally:
                    if fcntl is not None:
                        fcntl.flock(sperr_datei.fileno(), fcntl.LOCK_UN)
        except OSError:
            # An unwritable cache must not fail the request; the answer is still
            # correct, it just cannot be reused.
            return False
        return True

    def _write_locked(self, ticker, entry, verzeichnis):
        eintraege = dict(self._read_all())
        eintraege[ticker] = entry
        eintraege = self._bounded(eintraege)
        nutzlast = {"schema_version": SCHEMA_VERSION, "entries": eintraege}
        fd, tmp = tempfile.mkstemp(prefix=".earnings-", suffix=".tmp", dir=verzeichnis)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(nutzlast, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _bounded(self, eintraege):
        """Keep the most recently CHECKED entries; drop the oldest beyond the cap.

        Oldest by ``source_checked_at_utc`` (the only timestamp every valid
        entry must carry), so a cap hit costs the least useful proof first.
        """
        if len(eintraege) <= self.max_entries:
            return eintraege
        geordnet = sorted(
            eintraege.items(),
            key=lambda kv: str((kv[1] or {}).get("source_checked_at_utc") or ""),
            reverse=True,
        )
        return dict(geordnet[: self.max_entries])


class NullEarningsAdapter:
    """The default when no source is configured: says so, invents nothing."""

    source = None

    def __init__(self, now_fn=None):
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def get_earnings(self, ticker):
        jetzt = _utc_now(self._now_fn)
        body = earnings_body(
            ticker,
            {
                "next_earnings_date": None,
                "release_session": SESSION_UNKNOWN,
                "date_confirmation": CONFIRMATION_UNKNOWN,
                "source": None,
                "source_checked_at_utc": None,
                "retrieved_at_utc": _iso(jetzt),
                "status": STATUS_UNKNOWN,
                "quality_flags": [FLAG_SOURCE_NOT_CONFIGURED],
            },
            cache={"state": "none", "stored_at_utc": None, "age_seconds": None,
                   "ttl_seconds": None, "freshness_limit_seconds": None},
        )
        return body


class NasdaqEarningsAdapter:
    """Live Nasdaq query with a bounded, atomic cache (see module docstring)."""

    source = SOURCE_NAME

    def __init__(
        self,
        transport=None,
        now_fn=None,
        cache=None,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        freshness_limit_seconds=DEFAULT_FRESHNESS_LIMIT_SECONDS,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        cross_check=True,
        cross_check_timeout_seconds=DEFAULT_CROSS_CHECK_TIMEOUT_SECONDS,
    ):
        self._transport = transport or urllib_transport
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._cache = cache if cache is not None else EarningsCache()
        self.ttl_seconds = int(ttl_seconds)
        self.freshness_limit_seconds = int(freshness_limit_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.cross_check = bool(cross_check)
        # The cross-check is optional (a failure is tolerated), so it must not
        # be able to double the cold-path latency: it gets its own, shorter cap.
        self.cross_check_timeout_seconds = float(cross_check_timeout_seconds)

    # -- source access ---------------------------------------------------- #
    def _get_json(self, url, timeout=None):
        try:
            status, body = self._transport(url, dict(_HEADERS), self.timeout_seconds if timeout is None else timeout)
        except Exception as exc:  # noqa: BLE001 - every transport failure is one class here
            raise EarningsSourceError("source_unreachable", f"{type(exc).__name__}: {exc}") from exc
        if status != 200:
            raise EarningsSourceError("http_error", f"the source answered HTTP {status}", status)
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise EarningsSourceError("invalid_json", "the source answer is not JSON") from exc

    # -- public ------------------------------------------------------------ #
    def get_earnings(self, ticker):
        symbol = (ticker or "").strip().upper()
        jetzt = _utc_now(self._now_fn)
        gespeichert = self._cache.get(symbol)
        alter = None
        if gespeichert is not None:
            try:
                geprueft = datetime.strptime(gespeichert["source_checked_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                alter = (jetzt - geprueft).total_seconds()
            except (TypeError, ValueError):
                gespeichert, alter = None, None

        if gespeichert is not None and alter is not None and 0 <= alter < self.ttl_seconds:
            return self._from_cache(symbol, gespeichert, alter, jetzt, state="hit", live_error=None)

        # A cache entry from the future (clock moved backwards) is not trusted as
        # fresh; it is re-queried like an expired one.
        try:
            neu = self._query(symbol, jetzt)
        except EarningsSourceError as exc:
            if gespeichert is not None and alter is not None:
                return self._from_cache(symbol, gespeichert, alter, jetzt, state="stale_fallback", live_error=exc)
            return earnings_body(
                symbol,
                {
                    "next_earnings_date": None,
                    "release_session": SESSION_UNKNOWN,
                    "date_confirmation": CONFIRMATION_UNKNOWN,
                    "source": SOURCE_NAME,
                    "source_checked_at_utc": None,
                    "retrieved_at_utc": _iso(jetzt),
                    "status": STATUS_UNAVAILABLE,
                    "quality_flags": [FLAG_LIVE_FAILED],
                },
                cache={"state": "miss", "stored_at_utc": None, "age_seconds": None,
                       "ttl_seconds": self.ttl_seconds,
                       "freshness_limit_seconds": self.freshness_limit_seconds,
                       "error": exc.as_dict()},
            )

        eintrag = {
            "next_earnings_date": neu["next_earnings_date"],
            "release_session": neu["release_session"],
            "date_confirmation": neu["date_confirmation"],
            "source": SOURCE_NAME,
            "source_checked_at_utc": _iso(jetzt),
            "quality_flags": list(neu.get("quality_flags") or []),
            "raw": neu.get("raw"),
        }
        geschrieben = self._cache.put(symbol, eintrag)
        return earnings_body(
            symbol,
            {
                "next_earnings_date": neu["next_earnings_date"],
                "release_session": neu["release_session"],
                "date_confirmation": neu["date_confirmation"],
                "source": SOURCE_NAME,
                "source_checked_at_utc": _iso(jetzt),
                "retrieved_at_utc": _iso(jetzt),
                "status": neu["status"],
                "quality_flags": list(neu.get("quality_flags") or []),
            },
            cache={"state": "live" if geschrieben else "live_unpersisted",
                   "stored_at_utc": _iso(jetzt) if geschrieben else None,
                   "age_seconds": 0,
                   "ttl_seconds": self.ttl_seconds,
                   "freshness_limit_seconds": self.freshness_limit_seconds},
        )

    def _query(self, symbol, jetzt):
        """One live answer, normalised - the only place that touches the source."""
        payload = self._get_json(DATE_URL.format(symbol=symbol))
        geparst = parse_nasdaq_earnings_date(payload)
        flags = []
        if geparst["parse_status"] == "no_date":
            return {
                "next_earnings_date": None,
                "release_session": SESSION_UNKNOWN,
                "date_confirmation": CONFIRMATION_UNKNOWN,
                "status": STATUS_UNKNOWN,
                "quality_flags": flags,
                "raw": geparst.get("raw"),
            }
        if geparst["parse_status"] != "ok":
            return {
                "next_earnings_date": None,
                "release_session": SESSION_UNKNOWN,
                "date_confirmation": CONFIRMATION_UNKNOWN,
                "status": STATUS_INVALID,
                "quality_flags": flags + ["source_answer_invalid"],
                "raw": geparst.get("raw"),
            }

        record = {
            "next_earnings_date": geparst["next_earnings_date"],
            "release_session": geparst["release_session"],
            "date_confirmation": geparst["date_confirmation"],
            "source": SOURCE_NAME,
            "source_checked_at_utc": _iso(jetzt),
            "retrieved_at_utc": _iso(jetzt),
            "status": STATUS_FRESH,
            "quality_flags": flags,
        }
        if record["date_confirmation"] == "estimated":
            record["quality_flags"].append(FLAG_ESTIMATED)
        if record["release_session"] == SESSION_UNKNOWN:
            record["quality_flags"].append(FLAG_SESSION_UNKNOWN)

        if self.cross_check:
            try:
                kalender = self._get_json(
                    CALENDAR_URL.format(date=record["next_earnings_date"]),
                    timeout=self.cross_check_timeout_seconds,
                )
                pruefung = parse_nasdaq_calendar_row(kalender, symbol, record["next_earnings_date"])
            except EarningsSourceError:
                pruefung = None
            record, _flag = merge_calendar_check(record, pruefung)
        if record["status"] == STATUS_FRESH and record["next_earnings_date"] < jetzt.date().isoformat():
            record["quality_flags"] = sorted(set(record["quality_flags"] + [FLAG_DATE_IN_PAST]))
        record["quality_flags"] = sorted(set(record["quality_flags"]))
        record["raw"] = geparst.get("raw")
        return record

    def _from_cache(self, symbol, eintrag, alter, jetzt, *, state, live_error):
        """A stored proof, labelled with its age - never as a live confirmation."""
        flags = list(eintrag.get("quality_flags") or [])
        flags.append(FLAG_CACHE_USED)
        if live_error is not None:
            flags.append(FLAG_LIVE_FAILED)
        # Two-sided on purpose: a negative age means the clock moved backwards
        # (or the file was tampered with). Such a proof is not fresh - it must
        # not authorise, and the live query already failed by the time we are here.
        if alter < 0:
            status = STATUS_STALE
            flags.append(FLAG_CACHE_TIMESTAMP_FUTURE)
        elif alter <= self.freshness_limit_seconds:
            status = STATUS_FRESH
        else:
            status = STATUS_STALE
        cache_block = {
            "state": state,
            "stored_at_utc": eintrag.get("source_checked_at_utc"),
            "age_seconds": int(alter),
            "ttl_seconds": self.ttl_seconds,
            "freshness_limit_seconds": self.freshness_limit_seconds,
        }
        if live_error is not None:
            cache_block["error"] = live_error.as_dict()
        return earnings_body(
            symbol,
            {
                "next_earnings_date": eintrag.get("next_earnings_date"),
                "release_session": eintrag.get("release_session") or SESSION_UNKNOWN,
                "date_confirmation": eintrag.get("date_confirmation") or CONFIRMATION_UNKNOWN,
                "source": eintrag.get("source") or SOURCE_NAME,
                "source_checked_at_utc": eintrag.get("source_checked_at_utc"),
                "retrieved_at_utc": _iso(jetzt),
                "status": status,
                "quality_flags": sorted(set(flags)),
            },
            cache=cache_block,
        )
