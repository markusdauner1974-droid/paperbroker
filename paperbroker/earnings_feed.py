"""The announcement feed next to a title without a report date: read, show, stop.

What this is
------------
Google News publishes an RSS endpoint (``application/xml``) that can be asked
for a search term. For a title whose report date the calendar does not know,
this module asks that endpoint for "«company name» schedules earnings" and
returns the first item whose HEADLINE reads like a scheduling announcement -
title and link, nothing else.

What this is not
----------------
* **No article is fetched.** The link in the answer is for a human to open in a
  browser; the module never requests the page behind it, never extracts a date
  from a sentence and never works around a consent wall or a bot shield. The
  feed is the boundary, and it stays the boundary.
* **No date is derived.** Even a headline that contains a date is not turned
  into an entry: the curated date comes from the person reading the page
  (``hand_calendar.py``), never from a parser here.
* **No retry.** One request, four seconds, then "no signal" - the funnel runs
  this beside thirty other titles and may not wait for a news site.

Failure behaviour
-----------------
Every failure (transport, HTTP status, unparsable XML, an empty name) is
``None`` plus a log line. A signal is a convenience; a scan never fails because
a feed is down.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

#: The official RSS search endpoint of Google News. No key, no login, one
#: documented content type - and the reason this feature can exist without
#: touching an article page.
FEED_URL = "https://news.google.com/rss/search"
SOURCE_NAME = "google_news_rss"

DEFAULT_TTL_SECONDS = 12 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_CACHE_PATH = "/app/.cache/earnings_feed.json"
FALLBACK_CACHE_PATH = ".cache/earnings_feed.json"
ENV_CACHE_PATH = "EARNINGS_FEED_CACHE_PATH"
CACHE_VERSION = 1

#: The query carries the COMPANY NAME, not only the ticker: "ITG schedules
#: earnings" finds press releases, "ITG" alone finds everything about a ticker.
QUERY_TEMPLATE = '"{name}" schedules earnings'

#: A headline has to promise a DATE to be worth showing. Both halves are needed
#: ("schedules" + "results" matches, "quarterly results announced" does not) -
#: an analysis piece about the last quarter matches neither half.
_SCHEDULE_WORDS = (
    "schedules", "schedule", "scheduled", "to report", "will report",
    "will announce", "to announce", "sets date", "set date", "announces date",
    "conference call", "webcast",
)
_REPORT_WORDS = (
    "earnings", "results", "webcast", "conference call", "financial",
    "quarter", "fiscal", "q1", "q2", "q3", "q4",
)


class FeedError(Exception):
    """A failed query - never raised out of ``signal()``, only out of the parts."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def default_cache_path():
    override = os.environ.get(ENV_CACHE_PATH)
    if override:
        return override
    parent = os.path.dirname(DEFAULT_CACHE_PATH)
    return DEFAULT_CACHE_PATH if os.path.isdir(parent) else FALLBACK_CACHE_PATH


def query_url(name):
    """The feed URL for one company name."""
    query = QUERY_TEMPLATE.format(name=(name or "").strip())
    return f"{FEED_URL}?{urllib.parse.urlencode({'q': query, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})}"


def looks_like_schedule(title):
    """``True`` when a headline promises a report date rather than discussing one.

    Case-insensitive, and deliberately generous on the verb side: "to report",
    "will report", "schedules", "sets date" all appear in real announcements.
    The noun side is what keeps analyses out.
    """
    text = str(title or "").lower()
    if not text:
        return False
    return (any(word in text for word in _SCHEDULE_WORDS)
            and any(word in text for word in _REPORT_WORDS))


def _safe_link(link):
    """The link only if it is an ``http(s)`` address, else ``None``.

    A feed is third-party text. ``rel="noopener"`` does not stop a
    ``javascript:`` or ``data:`` URI from running in this page's origin, so the
    scheme is checked HERE - on the server, once - and the page only ever
    receives an address it may turn into a link.
    """
    text = str(link or "").strip()
    return text if text[:7].lower() == "http://" or text[:8].lower() == "https://" else None


def parse_feed(xml_text):
    """``{"title", "link"}`` of the first announcing item, or ``None``.

    Only ``channel/item`` is read; a feed without one (or without titles) is
    "no signal". An item whose link is not ``http(s)`` is skipped like a
    malformed one - the headline alone is not worth showing as a link.
    """
    if isinstance(xml_text, bytes):
        try:
            xml_text = xml_text.decode("utf-8", "replace")
        except Exception:                       # noqa: BLE001 - a decode is a guess
            return None
    if not isinstance(xml_text, str) or not xml_text.strip():
        return None
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return None
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = _safe_link(item.findtext("link"))
        if not title or not link:
            continue
        if looks_like_schedule(title):
            return {"title": title, "link": link}
    return None


def requests_transport(url, headers, timeout):
    """``(status, body)`` - an HTTP status is returned, never raised."""
    response = requests.get(url, headers=headers, timeout=timeout)
    return response.status_code, response.content


@dataclass(frozen=True)
class SignalEntry:
    """One remembered answer: the signal (or ``None``) and when it was asked."""

    title: str | None
    link: str | None
    fetched_at: datetime


class EarningsFeedAdapter:
    """Feed signals per company name, behind a 12 h file cache.

    ``transport`` and the clock come in through the constructor so a test can
    drive it without network and without sleeping - the same shape as the
    next-earnings adapter next door.
    """

    def __init__(self, transport=None, cache_path=None, ttl_seconds=DEFAULT_TTL_SECONDS,
                 now_fn=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
        self._transport = transport or requests_transport
        self._cache_path = cache_path if cache_path is not None else default_cache_path()
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._now_fn = now_fn
        self._timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()

    # --- cache --------------------------------------------------------- #
    def _clock(self):
        return self._now_fn() if self._now_fn is not None else datetime.now()

    def _read_cache(self):
        if not self._cache_path:
            return {}
        try:
            with open(self._cache_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("earnings feed cache unreadable (%s: %s)",
                           type(exc).__name__, exc)
            return {}
        rows = payload.get("signals") if isinstance(payload, dict) else None
        entries = {}
        for key, row in (rows or {}).items():
            if not isinstance(row, dict):
                continue
            try:
                fetched = datetime.fromisoformat(str(row.get("fetched_at")).replace("Z", "+00:00"))
            except ValueError:
                continue
            entries[str(key)] = SignalEntry(title=row.get("title"), link=row.get("link"),
                                            fetched_at=fetched)
        return entries

    def _fresh(self, entry, clock):
        try:
            age = (clock - entry.fetched_at).total_seconds()
        except TypeError:            # naive vs aware - a stale entry, not a crash
            return False
        return 0 <= age < self._ttl_seconds

    def _write_cache(self, entries):
        """Best effort: the cache is an accelerator, not the answer."""
        if not self._cache_path:
            return
        body = {
            "version": CACHE_VERSION,
            "source": SOURCE_NAME,
            "signals": {
                key: {"title": entry.title, "link": entry.link,
                      "fetched_at": entry.fetched_at.isoformat()}
                for key, entry in entries.items()
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
            logger.warning("earnings feed cache not written (%s: %s)",
                           type(exc).__name__, exc)

    # --- the query ----------------------------------------------------- #
    def _ask(self, name):
        """``(title, link)`` or ``(None, None)`` - one request, no retry."""
        headers = {"Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
                   "User-Agent": "paperbroker (hand calendar feed reader)"}
        started = time.monotonic()
        try:
            status, body = self._transport(query_url(name), headers, self._timeout_seconds)
        except Exception as exc:                # noqa: BLE001 - a source failure is a state
            logger.info("earnings feed unreachable for %s (%s)", name, type(exc).__name__)
            return None, None
        elapsed = time.monotonic() - started
        if status != 200:
            logger.info("earnings feed answered %s for %s after %.1fs", status, name, elapsed)
            return None, None
        signal = parse_feed(body)
        logger.info("earnings feed for %s: %s in %.1fs", name,
                    "signal" if signal else "no signal", elapsed)
        if signal is None:
            return None, None
        return signal["title"], signal["link"]

    def signal(self, name, *, use_cache=True):
        """``{"title", "link"}`` for ``name``, or ``None``. Never raises.

        ``None`` covers both "the feed holds no scheduling announcement" and
        "the feed could not be read": the page shows the same sentence, and the
        log line names which case it was.
        """
        clean = " ".join(str(name or "").split())
        if len(clean) < 2:
            return None
        key = clean.lower()
        clock = self._clock()
        if use_cache:
            with self._lock:
                entry = self._read_cache().get(key)
            if entry is not None and self._fresh(entry, clock):
                return None if not entry.title else {"title": entry.title,
                                                    "link": entry.link}
        title, link = self._ask(clean)
        with self._lock:
            entries = self._read_cache()
            entries[key] = SignalEntry(title=title, link=link, fetched_at=clock)
            self._write_cache(entries)
        return None if not title else {"title": title, "link": link}
