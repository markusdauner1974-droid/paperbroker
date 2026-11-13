"""
    CBOE delayed-quotes adapter for the public keyless CBOE feed.

    Endpoint: https://cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json
    (verified 2026-10-07, keyless, requires a full browser User-Agent)

    Units verified against live AAPL data (3568 contracts):
      - iv   is DECIMAL (median 0.309)  -> stored as percent (x100)
      - delta/gamma/vega/theta/rho are RAW (|delta| 0..1) -> stored x100
      - 0 values are legitimate (illiquid contracts): 0 stays 0, only
        missing/null becomes None.

    Caching: one fetch per underlying, shared by get_expiration_dates and
    get_options. TTL refreshes when the CBOE timestamp changes (the feed
    updates roughly every 15 minutes); a bounded LRU keeps memory in check.
"""
import logging
import math
import random
import re
import threading
import time
from collections import OrderedDict
from datetime import date

import arrow
import requests

from ...assets import _DAYS_IN_MONTH, Asset, Call, Option, Put, asset_factory, parse_occ
from ...quotes import OptionQuote, Quote
from .QuoteAdapter import QuoteAdapter

# --- why a chain call failed ----------------------------------------------- #
# Stable machine-readable classes. A caller may show the class (and its own
# German sentence) in a 200 body; the exception text stays a log detail, because
# a requests message carries URLs and socket details.
CHAIN_BLOCKED = "blocked"          # 403: the feed refuses this client
CHAIN_THROTTLED = "throttled"      # 429: too many requests
CHAIN_NOT_LISTED = "not_listed"    # 404: no chain for this underlying
CHAIN_UNREACHABLE = "unreachable"  # timeout, connection error, 5xx
CHAIN_MALFORMED = "malformed"      # 200, but the payload cannot be read
CHAIN_UNUSABLE = "unusable"        # 200, the chain was read, no row was usable
CHAIN_REASONS = (CHAIN_BLOCKED, CHAIN_THROTTLED, CHAIN_NOT_LISTED,
                 CHAIN_UNREACHABLE, CHAIN_MALFORMED, CHAIN_UNUSABLE)


class CboeChainError(Exception):
    """A chain call that failed, carrying the class and the attempt count.

    ``reason`` is the part a caller may repeat: it names WHAT happened, so a
    scan can say "throttled" instead of "could not be read". ``attempts`` is how
    many requests the answer really cost - a retried call is not one call.
    """

    reason = CHAIN_UNREACHABLE

    def __init__(self, message="", reason=None, attempts=1):
        super().__init__(message)
        if reason is not None:
            self.reason = reason
        self.attempts = attempts


class CboeNotFoundError(CboeChainError):
    """Unknown symbol or empty chain in the CBOE feed (no chain listed)."""

    reason = CHAIN_NOT_LISTED


class CboeRequestError(CboeChainError):
    """The CBOE feed rejected the request (blocked / throttled / down)."""

    reason = CHAIN_UNREACHABLE


class CboeUnusableChainError(CboeRequestError):
    """The feed delivered a chain, but no contract of it could be built.

    Deliberately NOT a ``CboeNotFoundError``: the symbol IS known and the
    chain WAS read. A handler that classifies only by the base class would
    otherwise tell the caller "unknown symbol" for a source that answered
    with data it could not map (measured: every row refused by the option
    constructor -> ``except CboeNotFoundError`` -> 404 "unknown symbol").

    It IS a ``CboeRequestError`` because from the caller's point of view the
    chain could not be read at all - but its own ``reason`` says why, and
    every handler must check this class BEFORE the base class.
    """

    reason = CHAIN_UNUSABLE


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")


def _num(value, scale=1.0):
    """CBOE value -> float (scaled); missing/null -> None. 0 stays 0."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        # D8: NaN AND +-inf are not data. A non-finite price used to slip
        # through and reached the JSON body as Infinity/NaN, which strict
        # JSON (RFC 8259) cannot parse. Missing stays None, zero stays zero.
        return None
    return v * scale


logger = logging.getLogger(__name__)


def _is_real_date(value):
    """True when the date really exists (29 February only in a leap year)."""
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


class ChainResult(list):
    """The chain as an ordinary list, plus how many rows were unusable.

    A list subclass on purpose: callers index and count the result and the
    JSON echo serialises it as an array - all of that stays unchanged
    (measured 2026-10-12), so no existing assertion had to be touched.
    """

    def __init__(self, items=(), skipped=0, attempts=1):
        super().__init__(items)
        self.skipped = skipped
        #: How many requests the chain cost. One for a single fetch that was
        #: not retried, more when the feed throttled or broke; 0 for a cache
        #: hit, which spends no request at all.
        self.attempts = attempts


class RetryBudget:
    """Thread-safe token bucket that pays for retry sleeps, in second units.

    A backoff is not free: it is spent on the worker thread, so eight symbols
    that keep failing add roughly 1.7 s each to the scan's wall clock
    (measured 2026-11-16: a 19.8 s scan with 8 ``fetch_failed`` rows). The
    bucket caps the total wait per adapter, shared by every worker that uses
    it, because all of them talk to the same source.

    ``take`` is all-or-nothing on purpose. When the budget is spent the caller
    must NOT retry: an immediate second request while the source is already
    throttling costs that source more than the retry could win, and it would
    turn a throttle into a burst. Reporting the failure with its own reason is
    the honest answer; the next scan run starts with a full bucket.

    The clock is injectable (tests pin it); refill is monotonic, so a clock
    that jumps backwards cannot mint budget.
    """

    def __init__(self, capacity_seconds=2.0, refill_per_second=2.0,
                 clock=time.monotonic):
        self._capacity = max(0.0, float(capacity_seconds))
        self._refill_per_second = max(0.0, float(refill_per_second))
        self._clock = clock
        self._tokens = self._capacity
        self._stamp = self._clock()
        self._lock = threading.Lock()

    def take(self, seconds):
        """Reserve ``seconds`` of wait: ``True`` if granted, ``False`` if not.

        A denied request spends nothing - the caller must not sleep and must
        not retry.
        """
        seconds = max(0.0, float(seconds))
        with self._lock:
            now = self._clock()
            elapsed = now - self._stamp
            if elapsed > 0:
                self._tokens = min(self._capacity,
                                   self._tokens + elapsed * self._refill_per_second)
                self._stamp = now
            if self._tokens < seconds:
                return False
            self._tokens -= seconds
            return True

    @property
    def available(self):
        """Seconds of wait left right now (after refill) - for reporting."""
        with self._lock:
            now = self._clock()
            elapsed = now - self._stamp
            if elapsed > 0:
                self._tokens = min(self._capacity,
                                   self._tokens + elapsed * self._refill_per_second)
                self._stamp = now
            return self._tokens


class CBOEQuoteAdapter(QuoteAdapter):
    """
    QuoteAdapter for the keyless CBOE delayed-quotes feed.

    Greeks are passed through as adapter-supplied values
    (OptionQuote(greeks_source='adapter')) - the feed IV surface is
    authoritative for the screener; no model recompute.

    quote_timestamp on returned quotes carries the CBOE feed timestamp
    (staleness visible downstream).
    """

    MAX_CACHE_UNDERLYINGS = 64

    #: Backoff before the second and the third attempt. A 429 asks for less
    #: traffic, not for a different request, so waiting is the only useful
    #: reaction; the jitter keeps parallel workers from retrying in lockstep.
    #: The wait is paid from ``retry_budget`` - see RetryBudget.
    RETRY_BACKOFF_SECONDS = (0.4, 1.2)

    def __init__(self, timeout_seconds=30, session=None, now_fn=None,
                 sleep_fn=None, backoff_seconds=RETRY_BACKOFF_SECONDS,
                 retry_budget=None):
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": _BROWSER_UA, "Accept": "application/json"})
        self._now_fn = now_fn if now_fn is not None else arrow.utcnow
        # Injected in tests: the retry policy is what is under test, not the
        # wall clock.
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._backoff_seconds = tuple(backoff_seconds)
        # One bucket per adapter, shared by every worker thread that uses it:
        # the waits add up on the scan's wall clock, so they need a common cap.
        self._retry_budget = retry_budget if retry_budget is not None else RetryBudget()
        # underlying -> {"timestamp": str, "current_price": float, "options": [...]}
        self._cache = OrderedDict()
        self._cache_ttl_seconds = 900  # bounded; refreshed on timestamp change

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _reason_for_status(status):
        """``(reason, retryable)`` for a non-200 answer.

        403 is a block, 429 a throttle, 404 an unlisted symbol. Retrying a block
        or an unlisted symbol cannot change the answer - only the throttle and a
        failing feed (5xx, read like an unreachable connection) are repeated.
        Anything else is an unexpected answer and is reported unreachable
        without a retry: guessing that a 400 might go away is not a policy.
        """
        if status == 403:
            return CHAIN_BLOCKED, False
        if status == 429:
            return CHAIN_THROTTLED, True
        if status == 404:
            return CHAIN_NOT_LISTED, False
        if isinstance(status, int) and 500 <= status < 600:
            return CHAIN_UNREACHABLE, True
        return CHAIN_UNREACHABLE, False

    def _wait_before_retry(self, versuch):
        """Pay for the backoff of attempt ``versuch`` - if the budget allows.

        Returns ``True`` when the wait was granted and slept, ``False`` when
        the shared retry budget is spent. In that case the caller must not
        sleep AND must not retry: repeating the request immediately while the
        source is already throttling or refusing costs the source more than
        the retry could win. The failure goes out at once, with its own reason
        and the attempts it really took.
        """
        pause = self._backoff_seconds[versuch - 1] + random.uniform(0.0, 0.1)
        if not self._retry_budget.take(pause):
            return False
        self._sleep(pause)
        return True

    def _fetch(self, underlying_symbol):
        """Fetch + cache the full chain for an underlying (LRU + timestamp TTL).

        Failures are CLASSIFIED, not concatenated: ``blocked`` (403),
        ``throttled`` (429), ``not_listed`` (404), ``unreachable`` (timeout,
        connection error, 5xx) and ``malformed`` (200 with an unreadable body).
        Only ``throttled``, ``unreachable`` and 5xx get two further attempts,
        with the 0.4 s / 1.2 s backoff above - and only while the adapter's
        shared ``retry_budget`` can pay for the wait; once it is spent the
        failure leaves at once instead of retrying under throttling. A block
        and an unlisted symbol are answers and are not repeated. Each attempt
        is counted in ``attempts`` on the error and in the cache entry, because
        a retried call is not one source call.

        Concurrency is deliberately NOT touched here. A single 429 halves the
        scan's worker count only if the adapter can see the run it is part of -
        it cannot, and a guess at a shared pool would be worse than the
        documented limit. The scan keeps its concurrency; this retry spreads the
        same load over time instead.
        """
        key = underlying_symbol
        cached = self._cache.get(key)
        now = arrow.get(self._now_fn())
        if cached is not None:
            age = (now - cached["fetched_at"]).total_seconds()
            if age < self._cache_ttl_seconds:
                # move to most-recent position (LRU touch)
                self._cache.move_to_end(key)
                # A cache hit spends no request: it must not count as one.
                return dict(cached, attempts=0)

        url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{key}.json"
        versuch = 0
        while True:
            versuch += 1
            try:
                resp = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as e:
                fehler = CboeRequestError(
                    f"CBOE feed unreachable for {key}: {e}",
                    reason=CHAIN_UNREACHABLE, attempts=versuch,
                )
                if versuch <= len(self._backoff_seconds) and self._wait_before_retry(versuch):
                    continue
                raise fehler from e

            status = getattr(resp, "status_code", None)
            if status == 200:
                break

            reason, wiederholbar = self._reason_for_status(status)
            if reason == CHAIN_NOT_LISTED:
                fehler = CboeNotFoundError(key, reason=reason, attempts=versuch)
            else:
                fehler = CboeRequestError(f"CBOE feed HTTP {status} for {key}",
                                          reason=reason, attempts=versuch)
            if wiederholbar and versuch <= len(self._backoff_seconds) \
                    and self._wait_before_retry(versuch):
                continue
            raise fehler

        try:
            payload = resp.json()
            data = payload["data"]
            options = data["options"]
            current_price = data["current_price"]
            timestamp = payload.get("timestamp")
        except (ValueError, KeyError, TypeError) as e:
            # TypeError catches {"data": null} (None is not subscriptable)
            raise CboeRequestError(f"CBOE feed malformed response for {key}: {e}",
                                   reason=CHAIN_MALFORMED, attempts=versuch) from e

        if not isinstance(data, dict) or not isinstance(options, list):
            raise CboeRequestError(f"CBOE feed 'data'/'options' malformed for {key}",
                                   reason=CHAIN_MALFORMED, attempts=versuch)

        for entry_ in options:
            # each contract must be a dict; 'option' must be a string when present
            if not isinstance(entry_, dict) or (
                "option" in entry_ and not isinstance(entry_["option"], str)
            ):
                raise CboeRequestError(f"CBOE feed malformed option entry for {key}",
                                       reason=CHAIN_MALFORMED, attempts=versuch)

        try:
            current_price = float(current_price) if current_price is not None else None
        except (TypeError, ValueError) as e:
            raise CboeRequestError(f"CBOE feed invalid current_price for {key}: {e!r}",
                                   reason=CHAIN_MALFORMED, attempts=versuch) from e

        entry = {
            "timestamp": timestamp,
            "current_price": current_price,
            "options": options,
            "fetched_at": now,
            # What this entry cost the feed; carried to ChainResult below.
            "attempts": versuch,
        }
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > self.MAX_CACHE_UNDERLYINGS:
            self._cache.popitem(last=False)
        return entry

    @staticmethod
    def _parse_occ(symbol):
        """OCC symbol -> (underlying, 'YYYY-MM-DD', 'call'/'put', strike) or None.

        Thin delegate: the single OCC implementation lives in assets.parse_occ.
        Kept as a method because callers use it as a class attribute
        (CBOEQuoteAdapter._parse_occ) and the adapter calls it via self.
        """
        return parse_occ(symbol)

    def _quote_from_contract(self, contract, quote_date, underlying_price):
        parsed = self._parse_occ(contract.get("option", ""))
        if parsed is None:
            return None
        underlying, expiration, option_type, strike = parsed

        oc_symbol = contract["option"]
        # B-9: no silent fallback. The old fallback built the option by hand
        # and passed an `option_type` argument that Call/Put do not accept, so
        # every contract that reached it died with a TypeError (measured
        # 2026-10-12) and took the whole chain down with it. A contract that
        # cannot be built now raises and is skipped by the per-contract
        # boundary in get_options instead.
        asset = asset_factory(oc_symbol)

        q = OptionQuote(
            quote_date=quote_date,
            asset=asset,
            price=_num(contract.get("last_trade_price")),
            bid=_num(contract.get("bid")) or 0.0,
            ask=_num(contract.get("ask")) or 0.0,
            bid_size=_num(contract.get("bid_size")) or 0,
            ask_size=_num(contract.get("ask_size")) or 0,
            delta=_num(contract.get("delta"), scale=100.0),
            iv=_num(contract.get("iv"), scale=100.0),
            gamma=_num(contract.get("gamma"), scale=100.0),
            vega=_num(contract.get("vega"), scale=100.0),
            theta=_num(contract.get("theta"), scale=100.0),
            rho=_num(contract.get("rho"), scale=100.0),
            underlying_price=underlying_price,
            open_interest=_num(contract.get("open_interest")),
            volume=_num(contract.get("volume")),
            greeks_source='adapter',
        )
        # expose feed metadata
        q.feed_symbol = oc_symbol
        q.theo = _num(contract.get("theo"))
        q.last_trade_price = _num(contract.get("last_trade_price"))
        q.last_trade_time = contract.get("last_trade_time")

        # quote_factory-style mid price if last trade is missing but bid/ask exist
        if q.price is None and q.bid + q.ask != 0.0:
            q.price = (q.bid + q.ask) / 2
        return q

    @staticmethod
    def _as_date(value):
        """Accept 'YYYY-MM-DD' / date-like / 'YYMMDD' / 'YYYYMMDD' -> 'YYYY-MM-DD' (or None).

        NOTE: string decoding is table-based, not arrow-based - old arrow
        versions silently return the raw string for unknown formats.
        """
        if value is None:
            return None
        # NOTE: str has .format too - '261016'.format("YYYY-MM-DD") returns
        # the raw string (no braces -> args ignored). Type-check FIRST.
        if not isinstance(value, str) and hasattr(value, "format"):  # arrow/ date-like
            return value.format("YYYY-MM-DD")
        s = str(value)
        if re.match(r"^\d{6}$", s):
            mm, dd = int(s[2:4]), int(s[4:6])
            if 1 <= mm <= 12 and 1 <= dd <= _DAYS_IN_MONTH[mm - 1]:
                return f"20{s[0:2]}-{s[2:4]}-{s[4:6]}"
            return None
        if re.match(r"^\d{8}$", s):
            yy, mm, dd = int(s[0:4]), int(s[4:6]), int(s[6:8])
            if 2020 <= yy <= 2040 and 1 <= mm <= 12 and 1 <= dd <= _DAYS_IN_MONTH[mm - 1]:
                return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
            return None
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return s
        return None

    # ------------------------------------------------------------------ #
    # QuoteAdapter interface
    # ------------------------------------------------------------------ #

    def get_quote(self, asset):
        """Latest quote for a stock or an option (OCC symbol or Option)."""
        try:
            a = asset_factory(asset) if not isinstance(asset, Option) else asset
        except ValueError as err:
            # asset_factory throws when an 9+ char symbol is not valid OCC
            raise CboeNotFoundError(f"No asset for {asset!r}") from err
        if a is None:
            raise CboeNotFoundError(f"No asset for {asset!r}")
        if isinstance(a, Option):
            underlying = a.underlying.symbol if hasattr(a.underlying, "symbol") else str(a.underlying)
            chain = self._fetch(underlying)
            quote_date = chain["timestamp"] or arrow.get(self._now_fn()).format("YYYY-MM-DD HH:mm:ss")
            for contract in chain["options"]:
                if contract.get("option") == a.symbol:
                    q = self._quote_from_contract(contract, quote_date, chain["current_price"])
                    q.quote_timestamp = chain["timestamp"]
                    return q
            raise CboeNotFoundError(f"Option {a.symbol} not in {underlying} chain")

        # stock quote: from the underlying block
        chain = self._fetch(a.symbol)
        q = Quote(
            quote_date=chain["timestamp"] or arrow.get(self._now_fn()).format("YYYY-MM-DD HH:mm:ss"),
            asset=a,
            price=chain["current_price"],
        )
        q.quote_timestamp = chain["timestamp"]
        return q

    def get_options(self, underlying_asset=None, expiration_date=None):
        """All OptionQuotes for an underlying, optionally filtered by expiration."""
        underlying = underlying_asset.upper() if isinstance(underlying_asset, str) \
            else underlying_asset.symbol
        chain = self._fetch(underlying)
        quote_date = chain["timestamp"] or arrow.get(self._now_fn()).format("YYYY-MM-DD HH:mm:ss")

        exp = self._as_date(expiration_date)
        if expiration_date is not None and exp is None:
            raise ValueError(f"expiration_date not parseable: {expiration_date!r}")

        out = []
        skipped = 0
        for contract in chain["options"]:
            parsed = self._parse_occ(contract.get("option", ""))
            if parsed is None:
                continue
            if exp is not None and parsed[1] != exp:
                continue
            # B-16: one malformed row must cost exactly that row. Without this
            # boundary a single contract that the parser accepts but the option
            # constructor cannot build killed the whole request (measured:
            # 4314 quotes -> 0). The boundary is deliberately broad because
            # CboeNotFoundError is not a ValueError (measured MRO), so a narrow
            # tuple would lose coverage as soon as the row fails elsewhere.
            try:
                q = self._quote_from_contract(contract, quote_date, chain["current_price"])
            except Exception as exc:  # noqa: BLE001 - boundary by design
                skipped += 1
                logger.warning("get_options(%s): skipping %r (%s: %s)", underlying,
                               contract.get("option"), type(exc).__name__, exc)
                continue
            if q is not None:
                q.quote_timestamp = chain["timestamp"]
                out.append(q)
        if skipped:
            logger.warning("get_options(%s): %d of %d contracts skipped",
                           underlying, skipped, len(chain["options"]))
        if skipped and not out:
            # never let a fully unusable chain look like "no candidates" - and
            # never call it "not listed": the feed did deliver a chain, the
            # adapter just could not build a single contract from it. The
            # attempts the chain cost belong to that answer. The class is its
            # own one (not CboeNotFoundError), so no handler can degrade this
            # to "unknown symbol" by checking the base class.
            raise CboeUnusableChainError(
                f"all {len(chain['options'])} contracts of {underlying} unusable",
                attempts=int(chain.get("attempts", 1) or 1))
        return ChainResult(out, skipped=skipped, attempts=int(chain.get("attempts", 1) or 0))

    def get_option_chain_snapshot(self, underlying_symbol):
        """Raw, unfiltered view of the cached feed entry for one underlying.

        The returned dict carries the source rows exactly as the feed delivered
        them - missing bid/ask stays missing instead of becoming 0, and a row
        whose option symbol cannot be interpreted is still present. Nothing is
        mapped through OptionQuote, so no value is scaled or substituted here.

        Deliberately NOT applied: expiration window, open-interest or volume
        minimums, spread bounds, IV bands, trend, score, budget, costs and the
        age limit. Choosing an underlying is a data request, not a quality
        filter - the caller decides.

        Failures keep the existing contract unchanged: CboeNotFoundError for a
        symbol the source does not know, CboeRequestError for a timeout, an
        outage, HTTP throttling or an unreadable payload - and
        CboeUnusableChainError (a CboeRequestError, never a CboeNotFoundError)
        for a chain that arrived but yielded no buildable contract.
        """
        entry = self._fetch(underlying_symbol)
        records = entry.get("options")
        return {
            "underlying_symbol": underlying_symbol,
            "source_timestamp_raw": entry.get("timestamp"),
            "current_price": entry.get("current_price"),
            # A copy of the list: the cache keeps its own order and length even
            # if a caller reorders or slices what it got.
            "records": list(records) if isinstance(records, list) else [],
            "fetched_at": entry.get("fetched_at"),
        }
    def get_expiration_dates(self, underlying_asset=None):
        """Sorted 'YYYY-MM-DD' strings available for an underlying."""
        underlying = underlying_asset.upper() if isinstance(underlying_asset, str) \
            else underlying_asset.symbol
        chain = self._fetch(underlying)
        dates = set()
        for contract in chain["options"]:
            parsed = self._parse_occ(contract.get("option", ""))
            if parsed:
                dates.add(parsed[1])
        # B-16: the leap-day-safe table accepts 29 February in every year, but
        # the option constructor refuses it in a non-leap year (measured) and
        # such a date took the whole screen down before a date was even picked.
        # Offer only dates that really exist; a real 29 February (leap year)
        # stays in the list.
        return sorted(d for d in dates if _is_real_date(d))
