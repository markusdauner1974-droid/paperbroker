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
import re
from collections import OrderedDict
from datetime import date

import arrow
import requests

from ...assets import _DAYS_IN_MONTH, Asset, Call, Option, Put, asset_factory, parse_occ
from ...quotes import OptionQuote, Quote
from .QuoteAdapter import QuoteAdapter


class CboeNotFoundError(Exception):
    """Unknown symbol or empty chain in the CBOE feed."""


class CboeRequestError(Exception):
    """The CBOE feed rejected the request (blocked / throttled / down)."""


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

    def __init__(self, items=(), skipped=0):
        super().__init__(items)
        self.skipped = skipped


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

    def __init__(self, timeout_seconds=30, session=None, now_fn=None):
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": _BROWSER_UA, "Accept": "application/json"})
        self._now_fn = now_fn if now_fn is not None else arrow.utcnow
        # underlying -> {"timestamp": str, "current_price": float, "options": [...]}
        self._cache = OrderedDict()
        self._cache_ttl_seconds = 900  # bounded; refreshed on timestamp change

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _fetch(self, underlying_symbol):
        """Fetch + cache the full chain for an underlying (LRU + timestamp TTL)."""
        key = underlying_symbol
        cached = self._cache.get(key)
        now = arrow.get(self._now_fn())
        if cached is not None:
            age = (now - cached["fetched_at"]).total_seconds()
            if age < self._cache_ttl_seconds:
                # move to most-recent position (LRU touch)
                self._cache.move_to_end(key)
                return cached

        url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{key}.json"
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.RequestException as e:
            raise CboeRequestError(f"CBOE feed unreachable for {key}: {e}") from e

        if resp.status_code == 404:
            raise CboeNotFoundError(key)
        if resp.status_code != 200:
            raise CboeRequestError(f"CBOE feed HTTP {resp.status_code} for {key}")

        try:
            payload = resp.json()
            data = payload["data"]
            options = data["options"]
            current_price = data["current_price"]
            timestamp = payload.get("timestamp")
        except (ValueError, KeyError, TypeError) as e:
            # TypeError catches {"data": null} (None is not subscriptable)
            raise CboeRequestError(f"CBOE feed malformed response for {key}: {e}") from e

        if not isinstance(data, dict) or not isinstance(options, list):
            raise CboeRequestError(f"CBOE feed 'data'/'options' malformed for {key}")

        for entry_ in options:
            # each contract must be a dict; 'option' must be a string when present
            if not isinstance(entry_, dict) or (
                "option" in entry_ and not isinstance(entry_["option"], str)
            ):
                raise CboeRequestError(f"CBOE feed malformed option entry for {key}")

        try:
            current_price = float(current_price) if current_price is not None else None
        except (TypeError, ValueError) as e:
            raise CboeRequestError(f"CBOE feed invalid current_price for {key}: {e!r}") from e

        entry = {
            "timestamp": timestamp,
            "current_price": current_price,
            "options": options,
            "fetched_at": now,
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
            # never let a fully unusable chain look like "no candidates"
            raise CboeNotFoundError(
                f"all {len(chain['options'])} contracts of {underlying} unusable")
        return ChainResult(out, skipped=skipped)

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
        outage, HTTP throttling or an unreadable payload.
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
