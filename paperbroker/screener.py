"""
    Options screener (Phase 4).

    Filters an OptionQuote chain into tradeable candidates using
    configurable criteria, following the debate-panel design:

    - pure filter functions + screen() orchestrator
    - ScreenerCriteria dataclass (None = filter disabled)
    - tri-state data semantics: 0 = illiquid (excluded), None = missing
      (default: excluded with reason 'missing'), criteria None = skipped
    - spread relative to mid (%), plus optional absolute spread floor
      (stabilizes penny options where mid -> 0)
    - IV filters are direction-agnostic; the strategy decides whether
      high IV (theta_sell) or low IV (momentum_buy) is preferred
    - TrendProvider protocol decides call/put side only, never IV direction
    - staleness guard via quote_timestamp (configurable max age)

    Units follow the adapter conventions: prices in dollars, IV/greeks
    stored as percentages (CBOE decimal 0.25 -> stored 25).
"""
from dataclasses import dataclass, field, fields
from typing import Optional, Protocol

from .quotes import OptionQuote

_PENNY_MID_THRESHOLD = 1.0  # $ mid at/below which the absolute spread bound applies


@dataclass
class ScreenerCriteria:
    """Filter criteria. A field set to None disables that filter.

    Data semantics in quotes: 0 = legitimate zero (illiquid, excluded),
    None = missing data (excluded with reason; see require_* flags).
    """

    # liquidity / trading cost
    max_spread_pct: float | None = 10.0        # (ask-bid)/mid*100 upper bound
    max_spread_abs: float | None = 0.10        # ask-bid bound in $ (penny zone only)
    min_oi: int | None = 500                   # open interest floor
    min_volume: int | None = 0                 # volume floor (0 = only require > 0)
    min_bid_size: float | None = None          # disabled by default (data verified present)
    min_ask_size: float | None = None          # but configurable on demand

    # valuation band on stored IV (percentage scale: 25 == 25 %)
    iv_min: float | None = None                # e.g. 20 -> IV >= 20
    iv_max: float | None = None                # e.g. 60 -> IV <= 60
    # strategy's IV preference, used ONLY for ranking (not filtering):
    #   'high' = premium selling / 'low' = debit strategies / None = neutral
    iv_pref: str | None = None

    # calendar window
    dte_min: int | None = 7                    # exclude 0-DTE (gamma/pin risk)
    dte_max: int | None = 45

    # staleness guard on quote_timestamp (minutes); None = no check
    max_quote_age_min: int | None = 30

    # tri-state handling
    require_oi: bool = True                       # missing OI -> exclude
    require_volume: bool = False                  # missing volume -> skip filter

    # composite score weights (all None -> sort by spread_pct only)
    weight_spread: float | None = 0.5
    weight_oi: float | None = 0.3
    weight_iv: float | None = 0.2

    def validate(self):
        bad = []
        if self.iv_pref is not None and self.iv_pref not in ("high", "low"):
            bad.append('iv_pref in ("high", "low", None)')
        if self.max_spread_pct is not None and self.max_spread_pct < 0:
            bad.append("max_spread_pct >= 0")
        if self.max_spread_abs is not None and self.max_spread_abs < 0:
            bad.append("max_spread_abs >= 0")
        if self.min_oi is not None and self.min_oi < 0:
            bad.append("min_oi >= 0")
        if self.min_volume is not None and self.min_volume < 0:
            bad.append("min_volume >= 0")
        if self.max_quote_age_min is not None and self.max_quote_age_min < 0:
            bad.append("max_quote_age_min >= 0")
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            bad.append("dte_min <= dte_max")
        if self.iv_min is not None and self.iv_max is not None and self.iv_min > self.iv_max:
            bad.append("iv_min <= iv_max")
        for wname in ("weight_spread", "weight_oi", "weight_iv"):
            w = getattr(self, wname)
            if w is not None and w < 0:
                bad.append(f"{wname} >= 0")
        if bad:
            raise ValueError(f"ScreenerCriteria invalid: {'; '.join(bad)}")


class TrendProvider(Protocol):
    """Trend signal interface - Phase 4 defines the contract only.

    Implementations supply (direction, as_of) for a symbol; direction is
    used by the screener to pick the option side (call for rising,
    put for falling). Never used for IV filtering - IV preference is a
    strategy decision, not a trend decision.
    """

    def get_trend(self, symbol: str):  # -> Optional[TrendSignal]
        ...


@dataclass
class TrendSignal:
    symbol: str
    direction: str                 # 'up' | 'down' | 'flat'
    as_of: str | None = None    # signal timestamp (staleness visible)


@dataclass
class ScreenResult:
    quote: OptionQuote
    score: float
    spread_pct: float
    reasons: list = field(default_factory=list)   # why it passed/priority hints


# ---------------------------------------------------------------------- #
# pure filter helpers - each returns (kept: list[OptionQuote],
#                                   dropped: list[tuple[OptionQuote, str]])
# ---------------------------------------------------------------------- #

def _spread_pct(q: OptionQuote):
    bid, ask = q.bid, q.ask
    if bid is None or ask is None or ask <= 0 or bid <= 0:
        return None
    if bid > ask:
        return None  # crossed market: nonsensical quote, not a candidate
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100


def filter_data_complete(quotes):
    """Tri-state gate: 0 IV / 0 OI = illiquid (out); None = missing (out
    when required, else passed through to later filters)."""
    kept, dropped = [], []
    for q in quotes:
        if q.iv is not None and q.iv == 0:
            dropped.append((q, "iv=0 illiquid"))
            continue
        if q.open_interest is not None and q.open_interest == 0:
            dropped.append((q, "oi=0 illiquid"))
            continue
        kept.append(q)
    return kept, dropped


def filter_spread(quotes, criteria: ScreenerCriteria):
    """Relative bound always; absolute bound only acts as penny-guard
    (mid <= 1 $) so expensive contracts (AAPL spreads of 3 $ are normal
    and well below 10 %) are not wiped out by a dollar cap."""
    kept, dropped = [], []
    for q in quotes:
        sp = _spread_pct(q)
        if sp is None:
            dropped.append((q, "spread: no bid/ask"))
            continue
        abs_sp = (q.ask - q.bid)
        mid = (q.bid + q.ask) / 2
        penny_zone = mid <= _PENNY_MID_THRESHOLD
        abs_bound = criteria.max_spread_abs if penny_zone else None
        ok = True
        if criteria.max_spread_pct is not None and sp > criteria.max_spread_pct:
            ok = False
        if ok and abs_bound is not None and abs_sp > abs_bound:
            ok = False
        if ok:
            kept.append(q)
        else:
            if abs_bound is not None and abs_sp > abs_bound:
                dropped.append((q, f"penny-spread {abs_sp:.2f}$ > {abs_bound:.2f}$ (mid {mid:.2f})"))
            else:
                dropped.append((q, f"spread {sp:.1f}% > {criteria.max_spread_pct}%"))
    return kept, dropped


def filter_oi(quotes, criteria: ScreenerCriteria):
    kept, dropped = [], []
    for q in quotes:
        oi = q.open_interest
        if oi is None:
            if criteria.require_oi:
                dropped.append((q, "missing OI"))
            else:
                kept.append(q)
            continue
        if criteria.min_oi is not None and oi < criteria.min_oi:
            dropped.append((q, f"OI {oi} < {criteria.min_oi}"))
        else:
            kept.append(q)
    return kept, dropped


def filter_volume(quotes, criteria: ScreenerCriteria):
    kept, dropped = [], []
    for q in quotes:
        v = q.volume
        if v is None:
            if criteria.require_volume:
                dropped.append((q, "missing volume"))
            else:
                kept.append(q)
            continue
        if criteria.min_volume is not None and v <= criteria.min_volume:
            dropped.append((q, f"volume {v} <= {criteria.min_volume}"))
        else:
            kept.append(q)
    return kept, dropped


def filter_iv(quotes, criteria: ScreenerCriteria):
    """Direction-agnostic band only. Strategy decides preference."""
    kept, dropped = [], []
    for q in quotes:
        iv = q.iv
        if iv is None:
            dropped.append((q, "missing IV"))
            continue
        if criteria.iv_min is not None and iv < criteria.iv_min:
            dropped.append((q, f"IV {iv:.0f} < {criteria.iv_min}"))
            continue
        if criteria.iv_max is not None and iv > criteria.iv_max:
            dropped.append((q, f"IV {iv:.0f} > {criteria.iv_max}"))
            continue
        kept.append(q)
    return kept, dropped


def filter_dte(quotes, criteria: ScreenerCriteria):
    kept, dropped = [], []
    for q in quotes:
        d = q.days_to_expiration
        if d is None:
            dropped.append((q, "missing DTE"))
            continue
        if criteria.dte_min is not None and d < criteria.dte_min:
            dropped.append((q, f"DTE {d} < {criteria.dte_min}"))
            continue
        if criteria.dte_max is not None and d > criteria.dte_max:
            dropped.append((q, f"DTE {d} > {criteria.dte_max}"))
            continue
        kept.append(q)
    return kept, dropped


def filter_sizes(quotes, criteria: ScreenerCriteria):
    kept, dropped = [], []
    for q in quotes:
        ok = True
        if criteria.min_bid_size is not None and (q.bid_size is None or q.bid_size == 0 or q.bid_size < criteria.min_bid_size):
            ok = False
        if criteria.min_ask_size is not None and (q.ask_size is None or q.ask_size == 0 or q.ask_size < criteria.min_ask_size):
            ok = False
        if ok:
            kept.append(q)
        else:
            dropped.append((q, "size below minimum"))
    return kept, dropped


def filter_stale(quotes, criteria: ScreenerCriteria, now_fn=None):
    """max_quote_age_min uses quote_timestamp. Fail-closed: when the guard
    is enabled, a quote whose age cannot be established (missing or
    unparseable timestamp) is dropped with a distinct reason - unknown
    freshness must not silently pass a 30-minute guard.

    B-17: the clock call is checked SEPARATELY. A failing now_fn() is an
    internal error, not an unreadable timestamp - the reason text is shown
    on the page, so it must not lie. Measured 2026-10-10: a caller passing
    an arrow object instead of a function got 'unparseable timestamp' for
    67 contracts although all 104 carried a valid timestamp."""
    from datetime import datetime

    import arrow
    if criteria.max_quote_age_min is None:
        return quotes, []
    if now_fn is None:
        now_fn = lambda: arrow.get()  # noqa: E731
    max_age_s = criteria.max_quote_age_min * 60
    kept, dropped = [], []
    for q in quotes:
        ts = getattr(q, "quote_timestamp", None)
        if ts is None:
            dropped.append((q, "freshness unknown (no quote_timestamp)"))
            continue
        try:
            now_v = now_fn()
        except Exception:
            # internal error, NOT an unreadable timestamp (B-17)
            dropped.append((q, "freshness unknown (clock error)"))
            continue
        try:
            # normalize both sides to UTC, THEN strip the offset: arrow
            # timestamps carry UTC, callers may pass aware arrows in any
            # timezone, .naive alone drops the offset WITHOUT conversion
            # (a 14:35-04:00 clock read against a 14:30 UTC stamp would
            # look 5 min old instead of 5 h 5 min) - so to('UTC') first
            ts_naive = arrow.get(str(ts).replace(' ', 'T')).to('UTC').naive
            if getattr(now_v, 'tzinfo', None) is not None or isinstance(now_v, arrow.Arrow):
                now_naive = arrow.get(now_v).to('UTC').naive
            else:
                now_naive = now_v
            age_s = (now_naive - ts_naive).total_seconds()
        except Exception:
            dropped.append((q, "freshness unknown (unparseable timestamp)"))
            continue
        if age_s > max_age_s:
            dropped.append((q, f"stale {age_s/60:.0f} min"))
        else:
            kept.append(q)
    return kept, dropped


# ---------------------------------------------------------------------- #
# orchestrator
# ---------------------------------------------------------------------- #

def screen(quotes, criteria: ScreenerCriteria, trend=None, now_fn=None):
    """Filter OptionQuote list -> List[ScreenResult], scored + sorted.

    trend: optional TrendSignal - used only to annotate the option side,
    never for IV filtering.
    """
    criteria.validate()
    # ORDER MATTERS for the drop reasons: a contract is reported with the
    # FIRST filter that rejects it (data -> stale -> spread -> oi -> volume
    # -> iv -> dte -> sizes). The page shows those reasons, so this order is
    # part of the user-visible contract.
    dropped_all = []
    quotes, d = filter_data_complete(quotes);           dropped_all += d
    quotes, d = filter_stale(quotes, criteria, now_fn); dropped_all += d
    quotes, d = filter_spread(quotes, criteria);        dropped_all += d
    quotes, d = filter_oi(quotes, criteria);            dropped_all += d
    quotes, d = filter_volume(quotes, criteria);        dropped_all += d
    quotes, d = filter_iv(quotes, criteria);            dropped_all += d
    quotes, d = filter_dte(quotes, criteria);           dropped_all += d
    quotes, d = filter_sizes(quotes, criteria);         dropped_all += d

    results = []
    for q in quotes:
        sp = _spread_pct(q)
        spread_p = sp if sp is not None else 0.0
        # scoring: normalized 0..1 components (weights default spread/oi/iv)
        spread_score = 0.0 if criteria.max_spread_pct else 0.5
        if criteria.max_spread_pct:
            spread_score = max(0.0, 1.0 - spread_p / criteria.max_spread_pct)
        oi = q.open_interest or 0
        oi_score = 0.0
        if criteria.min_oi:
            oi_score = min(1.0, oi / (criteria.min_oi * 4)) if oi > 0 else 0.0
        iv = q.iv
        # IV stays NEUTRAL in the score: a band (iv_min/iv_max) passed as
        # filter says nothing about preference. High-IV-vs-low-IV ranking
        # is a strategy decision; inject strategy via iv_pref:
        #   'high' -> higher IV scores better (premium selling)
        #   'low'  -> lower IV scores better (debit strategies)
        # None (default) -> IV does not influence ranking.
        iv_score = 0.5
        if criteria.iv_pref in ("high", "low") and iv is not None:
            if criteria.iv_min is not None and criteria.iv_max is not None and criteria.iv_max > criteria.iv_min:
                iv_score = (iv - criteria.iv_min) / (criteria.iv_max - criteria.iv_min)
                iv_score = min(1.0, max(0.0, iv_score))
                if criteria.iv_pref == "low":
                    iv_score = 1.0 - iv_score
        w_sp = criteria.weight_spread if criteria.weight_spread is not None else 0
        w_oi = criteria.weight_oi if criteria.weight_oi is not None else 0
        w_iv = criteria.weight_iv if criteria.weight_iv is not None else 0
        norm = w_sp + w_oi + w_iv
        score = (w_sp * spread_score + w_oi * oi_score + w_iv * iv_score) / norm if norm else 0.0

        reasons = []
        if sp is not None:
            reasons.append(f"spread {sp:.1f}%")
        if oi:
            reasons.append(f"OI {oi}")
        if iv is not None:
            reasons.append(f"IV {iv:.0f}")
        if trend is not None:
            # side matching only for the trend's underlying symbol and
            # a real direction; 'flat'/unknown = neutral annotation
            if trend.symbol and q.asset.underlying.symbol != trend.symbol:
                reasons.append(f"trend symbol mismatch ({trend.symbol})")
            elif trend.direction == 'up' and q.asset.option_type == 'call':
                reasons.append("trend up match (call)")
            elif trend.direction == 'down' and q.asset.option_type == 'put':
                reasons.append("trend down match (put)")
            elif trend.direction == 'flat' or trend.direction not in ('up', 'down'):
                reasons.append(f"trend neutral ({trend.direction})")
            else:
                reasons.append(f"trend {trend.direction} vs {q.asset.option_type} mismatch")

        results.append(ScreenResult(quote=q, score=round(score, 4),
                                    spread_pct=round(spread_p, 2), reasons=reasons))
    results.sort(key=lambda r: (-r.score, r.spread_pct, r.quote.asset.symbol))
    return results, dropped_all


class OptionScreener:
    """Facade for reuse: build once (criteria), screen many chains."""

    def __init__(self, criteria: ScreenerCriteria = None):
        self.criteria = criteria or ScreenerCriteria()

    def screen(self, quotes, trend=None, now_fn=None):
        return screen(quotes, self.criteria, trend=trend, now_fn=now_fn)
