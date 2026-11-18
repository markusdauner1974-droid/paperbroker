"""Hand-curated report dates: the gap funnel's gap filler, never its source.

Why this module exists
----------------------
Finnhub answers ``None`` when its 270-day window holds no report date. For a
title like that the funnel stops at the first stage, and a title that may well
report in six weeks disappears from the list. This module holds a date a HUMAN
typed after reading an announcement - nothing is scraped, nothing is guessed.

Nothing here touches the disk
-----------------------------
The entries live in this process's memory, in one plain dict. No file, no lock
file, no temporary copy, no cache directory - a save cannot fail because a
mounted volume is not writable, and there is nothing to clean up afterwards.

The price is stated plainly: a restart empties the calendar. That is accepted
on purpose. The entries are a handful per year, the funnel works without them
(the title simply stays in the exclusions), and a redeploy is the natural
moment to re-read an announcement anyway. What a restart does NOT cost is a
wrong answer: without an entry the title is treated exactly like a title whose
report date nobody knows.

The three rules that shape the store
------------------------------------
* **A curated date never overrides a source date.** The funnel asks Finnhub
  first and only looks here when the source has nothing. A curated entry that
  competes with Finnhub would make the page disagree with the calendar it claims
  to follow.
* **An expired entry is not a date.** ``date <= scan day`` means the report is
  over (or today, which the funnel cannot trade either), so the entry reads
  ``expired`` and the title behaves exactly like one without a date - the badge
  says why, the funnel does not pretend. Expired entries are also dropped from
  memory on the next access: there is no reason to carry them, and nothing
  else would ever remove them.
* **One lock, one dict.** Reads happen per title inside the scan's workers, so a
  save replaces the dict under a lock instead of mutating it in place: a running
  scan sees the old calendar or the new one, never a half-applied one. There is
  no second process to coordinate with - the app runs as one.

Who writes it
-------------
Only the POST endpoint (``routes/hand_earnings.py``). No code path here invents
an entry: the date always arrives as text from a request.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

#: How many entries the store may hold. A hand calendar has a handful per year;
#: the cap is not a limit on the feature, it is the bound that keeps the one
#: write path from letting the dict grow without end.
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
#: becomes a key and is echoed in a badge, not a free-text field.
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
    """The curated dates, held in memory for the life of this process.

    Reads are served from a dict the funnel asks per title (in scan workers); a
    write swaps in a new dict under the lock, so a running scan never sees a
    half-applied calendar.
    """

    def __init__(self, now_fn=None):
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._entries = {}

    # --- reading ------------------------------------------------------- #
    def __len__(self):
        with self._lock:
            return len(self._entries)

    def entries(self):
        """A COPY of the store - callers may hold it while a save happens."""
        with self._lock:
            return {symbol: dict(entry) for symbol, entry in self._entries.items()}

    def entry_for(self, symbol, day):
        """The curated fact for ``symbol`` at ``day``, or ``None``.

        ``None`` means "this title has no curated date" - the state the funnel
        already handles, so nothing else has to know about this store.
        """
        clean = str(symbol or "").strip().upper()
        with self._lock:
            entry = self._entries.get(clean)
        if entry is None:
            return None
        status = classify(entry, day)
        if status is None:
            return None
        return {
            "status": status,
            "curated_date": entry["date"],
            "source": entry.get("source") or None,
            "notiert_am": entry.get("notiert_am") or None,
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

    def _pruned(self, entries, day):
        """``entries`` without the reports that are already behind us.

        Nothing else would ever remove them: an expired entry is not shown, not
        needed and not wanted, and holding it would only keep a dead symbol
        around until the next restart.
        """
        return {symbol: entry for symbol, entry in entries.items()
                if classify(entry, day) == STATUS_CURATED}

    def save(self, symbol, date_text, *, source="", day=None):
        """Validate and store one entry -> ``{"status": "saved", "entry": ...}``.

        Raises ``HandCalendarError`` with a stable code for anything the store
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
        with self._lock:
            entries = self._pruned(self._entries, heute)
            # Die Obergrenze gilt nur fuer ein NEUES Symbol: ein bestehender
            # Eintrag darf immer korrigiert werden, auch wenn der Speicher voll ist.
            if clean not in entries and len(entries) >= MAX_STORED_SYMBOLS:
                raise HandCalendarError(
                    CODE_TOO_MANY,
                    f"the calendar already holds {MAX_STORED_SYMBOLS} entries")
            entries[clean] = eintrag
            self._entries = entries
        logger.info("hand earnings saved: %s -> %s", clean, eintrag["date"])
        return {"status": "saved", "symbol": clean, "entry": dict(eintrag)}

    def delete(self, symbol):
        """Forget one entry -> ``True`` when there was something to forget.

        The page reaches this by clearing the date field: an entry that is being
        corrected or is simply not wanted any more must be removable without a
        restart and without a second button.
        """
        clean = str(symbol or "").strip().upper()
        if not valid_symbol(clean):
            raise HandCalendarError(CODE_INVALID_SYMBOL,
                                    "a symbol is one to six capital letters")
        with self._lock:
            entries = dict(self._entries)
            vorhanden = entries.pop(clean, None) is not None
            self._entries = entries
        if vorhanden:
            logger.info("hand earnings deleted: %s", clean)
        return vorhanden

    def clear(self):
        """Drop everything - used by tests and by a caller that wants a reset."""
        with self._lock:
            self._entries = {}
