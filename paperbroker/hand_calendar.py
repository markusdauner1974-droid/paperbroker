"""Hand-curated report dates: the gap funnel's gap filler, never its source.

Why this module exists
----------------------
Finnhub answers ``None`` when its 270-day window holds no report date. For a
title like that the funnel stops at the first stage, and a title that may well
report in six weeks disappears from the list. This module stores a date a HUMAN
typed after reading an announcement - nothing is scraped, nothing is guessed.

The three rules that shape the file
-----------------------------------
* **A curated date never overrides a source date.** The funnel asks Finnhub
  first and only looks here when the source has nothing. A curated entry that
  competes with Finnhub would make the page disagree with the calendar it claims
  to follow.
* **An expired entry is not a date.** ``date <= scan day`` means the report is
  over (or today, which the funnel cannot trade either), so the entry reads
  ``expired`` and the title behaves exactly like one without a date - the badge
  says why, the funnel does not pretend.
* **A broken file is an empty file.** This is a hand-maintained artefact in a
  mounted cache directory; a half-written or wrongly edited file must not break
  a scan. Reading tolerates, writing is atomic (tmp file + ``os.replace``) and
  serialised under one lock, so two saves cannot interleave.

Who writes the file
-------------------
Only the POST endpoint (``routes/hand_earnings.py``). There is no other writer,
and no code path here invents an entry: the date always arrives as text from a
request.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

#: Where the curated dates live. The mounted cache directory is the default
#: (the same one the next-earnings cache uses), the working directory is the
#: fallback for a run outside a container - the same two-path pattern as
#: ``next_earnings.py``.
DEFAULT_PATH = "/app/.cache/hand_earnings.json"
FALLBACK_PATH = ".cache/hand_earnings.json"
ENV_PATH = "HAND_EARNINGS_PATH"

FILE_VERSION = 1

#: How many entries the file may hold. Hand calendar entries are a handful per
#: year, so a cap is not a limit on the feature - it is the bound that keeps the
#: ONE write path from being used to grow the file without end (each save
#: rewrites the whole document).
MAX_STORED_SYMBOLS = 500

#: The window a curated date may live in. 270 days is Finnhub's own window: a
#: date further out would be a promise the funnel cannot measure anyway.
MAX_DAYS_AHEAD = 270

STATUS_CURATED = "curated"
STATUS_EXPIRED = "expired"

#: Rejection codes of the POST endpoint. They are stable and part of the
#: answer's contract - the page turns them into a sentence through i18n.
CODE_INVALID_SYMBOL = "invalid_symbol"
CODE_INVALID_DATE = "invalid_date"
CODE_PAST_DATE = "past_date"
CODE_TOO_FAR_OUT = "too_far_out"
CODE_TOO_MANY = "too_many_entries"

#: A US ticker: one to six capital letters. Deliberately strict - this value
#: becomes a key of a file and a filename-adjacent token, not a free-text field.
_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}$")
#: ISO date, nothing else. ``datetime.fromisoformat`` would also accept
#: "2026-12-10T08:00", which is not what the field offers.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HandCalendarError(Exception):
    """A rejected write, carrying the stable code the answer repeats."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self):
        return {"status": "rejected", "code": self.code, "message": self.message}


def default_path():
    """``DEFAULT_PATH`` when its directory exists, else the fallback."""
    override = os.environ.get(ENV_PATH)
    if override:
        return override
    parent = os.path.dirname(DEFAULT_PATH)
    return DEFAULT_PATH if os.path.isdir(parent) else FALLBACK_PATH


def valid_symbol(symbol):
    """``True`` for "A".."ZZZZZZ" - uppercase, one to six letters."""
    return isinstance(symbol, str) and bool(_SYMBOL_RE.match(symbol))


def parse_date(value):
    """A ``date`` from a strict ISO string, or ``None``.

    ``None`` is the honest answer for everything that is not exactly
    ``YYYY-MM-DD``: a timestamp is not a date, and guessing its first ten
    characters would silently accept a format the field never sends.
    """
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def classify(entry, day):
    """``curated`` or ``expired`` for one stored entry at ``day``.

    ``date <= day`` is expired, not "reports today": the funnel needs an expiry
    STRICTLY after the report, and a report on the scan day is already behind
    the close that gap measures. Treating both the same keeps one rule.
    """
    if not isinstance(entry, dict):
        return None
    day_of_report = parse_date(entry.get("date"))
    if day_of_report is None:
        return None
    return STATUS_EXPIRED if day_of_report <= day else STATUS_CURATED


class HandCalendar:
    """The curated dates behind one file, read often and written rarely.

    Reads are served from memory (the funnel asks per title, in workers); a
    write replaces the in-memory map under the same lock that guards the file,
    so a running scan sees either the old file or the new one, never a half
    written pair.
    """

    def __init__(self, path=None, now_fn=None):
        self._path = path if path is not None else default_path()
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._entries = None
        self._file_stamp = None

    # --- reading ------------------------------------------------------- #
    @property
    def path(self):
        return self._path

    def _stamp(self):
        """``(inode, mtime_ns, size)`` of the file, or ``None`` when it is absent.

        The stamp is what makes the in-memory map safe: another PROCESS may have
        written the file (a second worker's POST, or a human editing it by hand),
        and a cache that never re-reads would serve the old dates until a restart.
        """
        try:
            stat = os.stat(self._path)
        except OSError:
            return None
        # Der INODE ist der wichtige Teil: geschrieben wird per os.replace, jede
        # neue Fassung ist also ein neuer Inode. mtime allein kollidiert, wenn
        # zwei Fassungen innerhalb der Zeitgranularitaet entstehen UND gleich
        # lang sind - genau dieser Fall ist hier aufgetreten.
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _file_changed(self):
        return self._stamp() != self._file_stamp

    #: How often a read is retried when a writer interleaves.
    READ_ATTEMPTS = 3

    def _load(self):
        """Read the file and its stamp as ONE pair -> ``(entries, stamp)``.

        ``_read`` and ``_stamp`` are two syscalls, so another process can replace
        the file between them. Recording a stamp that belongs to the NEWER file
        while holding the OLDER content would make the cache look fresh forever -
        the one failure a stat-based cache must never have. The pair is therefore
        only accepted when the stamp is identical before and after the read.

        If a writer keeps interleaving, the data comes back with ``None`` as its
        stamp: the next call re-reads instead of pinning a pair nobody can vouch
        for. A stale read for THIS call is unavoidable; a stale cache is not.
        """
        stamp = self._stamp()
        data = {}
        for _versuch in range(self.READ_ATTEMPTS):
            data = self._read()
            jetzt = self._stamp()
            if jetzt == stamp:
                return data, stamp
            stamp = jetzt
        return data, None

    def _read(self):
        """The file as ``{symbol: entry}`` - unreadable means empty, with a log line."""
        if not self._path:
            return {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # A hand-edited file is not a broken scan: say so and run without it.
            logger.warning("hand earnings unreadable (%s: %s)", type(exc).__name__, exc)
            return {}
        rows = payload.get("symbols") if isinstance(payload, dict) else None
        entries = {}
        for symbol, row in (rows or {}).items():
            if not isinstance(row, dict) or parse_date(row.get("date")) is None:
                continue
            clean = str(symbol).strip().upper()
            if not valid_symbol(clean):
                continue
            entries[clean] = {
                "date": row["date"],
                "source": str(row.get("source") or "").strip(),
                "notiert_am": str(row.get("notiert_am") or "").strip(),
            }
        return entries

    def reload(self):
        """Re-read the file unconditionally and remember its new stamp."""
        with self._lock:
            self._entries, self._file_stamp = self._load()
        return self._entries

    def entries(self):
        """The loaded map - re-read whenever the file changed on disk.

        A read costs one ``os.stat``; that is what keeps a second worker's write
        (or a hand edit) visible to this one. The map is only rebuilt when the
        stamp actually moved, so a scan of thirty titles pays thirty stats and
        zero parses.
        """
        with self._lock:
            if self._entries is None or self._file_changed():
                self._entries, self._file_stamp = self._load()
            return dict(self._entries)

    def entry_for(self, symbol, day):
        """The curated fact for ``symbol`` at ``day``, or ``None``.

        ``None`` means "this title has no curated date" - not "the file is
        unreadable"; an unreadable file yields the same ``None`` because a
        missing date is the state the funnel already handles.
        """
        clean = str(symbol or "").strip().upper()
        entry = self.entries().get(clean)
        if entry is None:
            return None
        status = classify(entry, day)
        if status is None:
            return None
        return {
            "status": status,
            "curated_date": entry["date"],
            "source": entry["source"] or None,
            "notiert_am": entry["notiert_am"] or None,
        }

    def curated_date(self, symbol, day):
        """The curated report date as a ``date``, but only while it is valid."""
        entry = self.entry_for(symbol, day)
        if entry is None or entry["status"] != STATUS_CURATED:
            return None
        return parse_date(entry["curated_date"])

    # --- writing ------------------------------------------------------- #
    def _today(self):
        if self._now_fn is None:
            return datetime.now().date()
        stamp = self._now_fn()
        return stamp.date() if isinstance(stamp, datetime) else stamp

    @contextlib.contextmanager
    def _exclusive(self):
        """Exclusive lock across PROCESSES - ``self._lock`` only orders threads.

        Two workers of the same app each hold their own ``HandCalendar``; the
        read-modify-write of ``save()`` would otherwise interleave across them and
        one save would silently drop the other's entry. The sidecar file is only a
        handle: the kernel releases the lock when the process dies, so a crash
        cannot leave a stale lock behind.
        """
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(f"{self._path}.lock", "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write(self, entries):
        """Write the whole map atomically - tmp file in the same directory first."""
        body = {"version": FILE_VERSION, "symbols": dict(sorted(entries.items()))}
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temporary = f"{self._path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=1, sort_keys=True)
        # ``os.replace`` is the atomic step: a reader sees the old file or the
        # new one, never a truncated one - the file is hand-edited, not backed
        # by a database.
        os.replace(temporary, self._path)

    def save(self, symbol, date_text, *, source="", day=None):
        """Validate and store one entry -> ``{"status": "saved", "entry": ...}``.

        Raises ``HandCalendarError`` with a stable code for anything the file
        may not hold. ``day`` is the scan day the window is measured from; the
        clock answers it when it is not passed in.
        """
        clean = str(symbol or "").strip().upper()
        if not valid_symbol(clean):
            raise HandCalendarError(CODE_INVALID_SYMBOL,
                                    "a symbol is one to six capital letters")
        day_of_report = parse_date(date_text)
        if day_of_report is None:
            raise HandCalendarError(CODE_INVALID_DATE, "the date is not YYYY-MM-DD")
        heute = day if day is not None else self._today()
        if day_of_report < heute:
            raise HandCalendarError(CODE_PAST_DATE, "the date lies before the scan day")
        if day_of_report > heute + timedelta(days=MAX_DAYS_AHEAD):
            raise HandCalendarError(
                CODE_TOO_FAR_OUT,
                f"the date lies more than {MAX_DAYS_AHEAD} days ahead")

        eintrag = {
            "date": day_of_report.isoformat(),
            # The source is a short label ("IR-Pressemitteilung"), never a URL
            # and never free text from a page - the endpoint owns its shape.
            "source": str(source or "").strip()[:80],
            "notiert_am": heute.isoformat(),
        }
        with self._lock, self._exclusive():
            # Unter dem Prozess-Lock wird der Stand NEU gelesen, wenn ein anderer
            # Worker ihn bewegt hat: sonst ueberschreibt jede Speicherung die
            # Eintraege, die zwischen unseren Aufrufen dazugekommen sind.
            if self._entries is None or self._file_changed():
                self._entries = self._read()
            entries = dict(self._entries)
            # Die Obergrenze gilt nur fuer ein NEUES Symbol: ein bestehender
            # Eintrag darf immer korrigiert werden, auch wenn die Datei voll ist.
            if clean not in entries and len(entries) >= MAX_STORED_SYMBOLS:
                raise HandCalendarError(
                    CODE_TOO_MANY,
                    f"the file already holds {MAX_STORED_SYMBOLS} entries")
            entries[clean] = eintrag
            self._write(entries)
            # Nach dem Schreiben wird der Zwischenspeicher VERWORFEN statt
            # gesetzt: zwischen unserem Schreiben und einem Stempel koennte ein
            # anderer Prozess schreiben, und wir haetten dann unseren (aelteren)
            # Stand mit dem neueren Stempel gepaart. Ein Lesevorgang mehr pro
            # Speicherung ist billiger als ein Cache, der luegt.
            self._entries = None
            self._file_stamp = None
        return {"status": "saved", "symbol": clean, "entry": dict(eintrag)}
