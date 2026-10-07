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
from collections import OrderedDict
import re
import math
import arrow
import requests

from .QuoteAdapter import QuoteAdapter
from ...quotes import Quote, OptionQuote
from ...assets import asset_factory, Option, Asset, Call, Put


class CboeNotFoundError(Exception):
    """Unknown symbol or empty chain in the CBOE feed."""
    pass


class CboeRequestError(Exception):
    """The CBOE feed rejected the request (blocked / throttled / down)."""
    pass


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/131.0.0.0 Safari/537.36")

_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # leap-day-safe upper bound


def _num(value, scale=1.0):
    """CBOE value -> float (scaled); missing/null -> None. 0 stays 0."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v * scale


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
        """OCC symbol -> (underlying, 'YYYY-MM-DD', 'call'/'put', strike)."""
        m = _OCC.match(symbol)
        if not m:
            # adjusted/mini contracts and other non-standard symbols: skip
            return None
        underlying, yymmdd, cp, strike8 = m.groups()
        # fast string decode of YYMMDD - real-calendar month/day table
        # (no arrow in the hot path: this runs per contract)
        mm, dd = int(yymmdd[2:4]), int(yymmdd[4:6])
        if not (1 <= mm <= 12) or not (1 <= dd <= _DAYS_IN_MONTH[mm - 1]):
            return None  # impossible dates like 21-18 or 30 February
        expiration = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        return underlying, expiration, "call" if cp == "C" else "put", int(strike8) / 1000.0

    def _quote_from_contract(self, contract, quote_date, underlying_price):
        parsed = self._parse_occ(contract.get("option", ""))
        if parsed is None:
            return None
        underlying, expiration, option_type, strike = parsed

        oc_symbol = contract["option"]
        asset_symbol = None
        try:
            asset = asset_factory(oc_symbol)
            if isinstance(asset, Option):
                asset_symbol = asset
        except Exception:
            asset = None
        if asset is None:
            # asset_factory could not build a proper Option (rare symbol
            # variants) - fall back to a manually constructed Option
            underlying_asset = Asset(underlying)
            if option_type == "call":
                asset = Call(underlying=underlying_asset, option_type=option_type,
                             strike=strike, expiration_date=expiration)
            else:
                asset = Put(underlying=underlying_asset, option_type=option_type,
                            strike=strike, expiration_date=expiration)

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
        except ValueError:
            # asset_factory throws when an 9+ char symbol is not valid OCC
            raise CboeNotFoundError(f"No asset for {asset!r}")
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
        for contract in chain["options"]:
            parsed = self._parse_occ(contract.get("option", ""))
            if parsed is None:
                continue
            if exp is not None and parsed[1] != exp:
                continue
            q = self._quote_from_contract(contract, quote_date, chain["current_price"])
            if q is not None:
                q.quote_timestamp = chain["timestamp"]
                out.append(q)
        return out

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
        return sorted(dates)