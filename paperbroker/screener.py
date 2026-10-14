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
import math
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
    # D12: position size. The pair numbers above stay PER UNIT (per share); these
    # turn them into a position. fees is the TOTAL for the position, as the broker
    # charges it; currency is reported, not converted (no exchange rate here).
    multiplier: int | None = 100               # 100 shares per US equity option contract
    quantity: int | None = 1                   # contracts
    fees: float | None = 0.0                   # total fees for the position, in USD
    budget: float | None = None                # hard upper bound for the position debit
    pair_max_width: float | None = None        # D11: widest strike distance for a spread pair;
                                               # None = adjacent strikes only (fast mode)
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
        """Reject a contradictory or unusable criteria set with a readable reason.

        The text goes straight to the operator (page and API), so every message
        names the field, the expectation and the value found - the same style
        the request parser in server.py already uses. Every violation is
        reported, not only the first: one round trip has to be enough.
        """
        bad = []
        if self.iv_pref is not None and self.iv_pref not in ("high", "low"):
            bad.append(f"iv_pref: einer von high, low erwartet (ist: '{self.iv_pref}').")
        if self.max_spread_pct is not None and self.max_spread_pct < 0:
            bad.append(f"max_spread_pct: muss ≥ 0 sein (ist: {self.max_spread_pct}).")
        if self.max_spread_abs is not None and self.max_spread_abs < 0:
            bad.append(f"max_spread_abs: muss ≥ 0 sein (ist: {self.max_spread_abs}).")
        if self.pair_max_width is not None and self.pair_max_width < 0:
            bad.append(f"pair_max_width: muss ≥ 0 sein (ist: {self.pair_max_width}).")
        if self.min_oi is not None and self.min_oi < 0:
            bad.append(f"min_oi: muss ≥ 0 sein (ist: {self.min_oi}).")
        if self.min_volume is not None and self.min_volume < 0:
            bad.append(f"min_volume: muss ≥ 0 sein (ist: {self.min_volume}).")
        if self.min_bid_size is not None and self.min_bid_size < 0:
            bad.append(f"min_bid_size: muss ≥ 0 sein (ist: {self.min_bid_size}).")
        if self.min_ask_size is not None and self.min_ask_size < 0:
            bad.append(f"min_ask_size: muss ≥ 0 sein (ist: {self.min_ask_size}).")
        if self.max_quote_age_min is not None and self.max_quote_age_min < 0:
            bad.append(f"max_quote_age_min: muss ≥ 0 sein "
                       f"(ist: {self.max_quote_age_min}).")
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            bad.append(f"dte_min: muss ≤ dte_max sein "
                       f"(ist: {self.dte_min} > {self.dte_max}).")
        if self.iv_min is not None and self.iv_max is not None and self.iv_min > self.iv_max:
            bad.append(f"iv_min: muss ≤ iv_max sein "
                       f"(ist: {self.iv_min} > {self.iv_max}).")
        # D16: the position size is real money, so it gets the same treatment as every
        # other field. None stays allowed and means "no position is computed" - a SET
        # value has to be usable. The browser's min="1"/min="0" are convenience, not a
        # check: the server takes the values from the request and from the API.
        if self.multiplier is not None and (not isinstance(self.multiplier, int)
                                            or self.multiplier < 1):
            bad.append(f"multiplier: muss eine positive ganze Zahl sein "
                       f"(ist: {self.multiplier}).")
        if self.quantity is not None and (not isinstance(self.quantity, int)
                                          or self.quantity < 1):
            bad.append(f"quantity: muss eine positive ganze Zahl sein "
                       f"(ist: {self.quantity}).")
        if self.fees is not None and self.fees < 0:
            bad.append(f"fees: darf nicht negativ sein (ist: {self.fees}).")
        if self.budget is not None and self.budget < 0:
            bad.append(f"budget: darf nicht negativ sein (ist: {self.budget}).")
        for wname in ("weight_spread", "weight_oi", "weight_iv"):
            w = getattr(self, wname)
            if w is not None and w < 0:
                bad.append(f"{wname}: muss ≥ 0 sein (ist: {w}).")
        if bad:
            raise ValueError("; ".join(bad))


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
    if bid is None or ask is None:
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return None  # D8: infinity is not a price
    if ask <= 0 or bid <= 0:
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
            dropped.append((q, "iv=0 illiquide"))
            continue
        if q.open_interest is not None and q.open_interest == 0:
            dropped.append((q, "oi=0 illiquide"))
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
            dropped.append((q, "Spread: kein Geld-/Briefkurs"))
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
                dropped.append((q, f"absoluter Spread {abs_sp:.2f}$ > {abs_bound:.2f}$ (Mitte {mid:.2f})"))
            else:
                dropped.append((q, f"Spread {sp:.1f}% > {criteria.max_spread_pct}%"))
    return kept, dropped


def filter_oi(quotes, criteria: ScreenerCriteria):
    kept, dropped = [], []
    for q in quotes:
        oi = q.open_interest
        if oi is None:
            if criteria.require_oi:
                dropped.append((q, "OI fehlt"))
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
                dropped.append((q, "Volumen fehlt"))
            else:
                kept.append(q)
            continue
        if criteria.min_volume is not None and v <= criteria.min_volume:
            dropped.append((q, f"Volumen {v} <= {criteria.min_volume}"))
        else:
            kept.append(q)
    return kept, dropped


def filter_iv(quotes, criteria: ScreenerCriteria):
    """Direction-agnostic band only. Strategy decides preference."""
    kept, dropped = [], []
    for q in quotes:
        iv = q.iv
        if iv is None:
            dropped.append((q, "IV fehlt"))
            continue
        if criteria.iv_min is not None and iv < criteria.iv_min:
            dropped.append((q, f"IV {iv:.0f} < {criteria.iv_min}"))
            continue
        if criteria.iv_max is not None and iv > criteria.iv_max:
            dropped.append((q, f"IV {iv:.0f} > {criteria.iv_max}"))
            continue
        kept.append(q)
    return kept, dropped


def filter_dte(quotes, criteria: ScreenerCriteria, now_fn=None):
    kept, dropped = [], []
    # D8: the window is measured against the clock of THIS scan, not against the day
    # the feed stamped the quote. The stored field keeps the data's own maturity (the
    # Greeks are priced with it); filter, expiration picker and header now share ONE
    # basis - the app passes its clock, exactly like the picker already did.
    # imported here on purpose: the module keeps arrow a local import (see filter_stale).
    import arrow

    clock = now_fn or arrow.get
    for q in quotes:
        asset = getattr(q, "asset", None)
        d = asset.get_days_to_expiration(clock()) if asset is not None else q.days_to_expiration
        if d is None:
            dropped.append((q, "DTE fehlt"))
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
            dropped.append((q, "Größe unter Minimum"))
    return kept, dropped


# Clock skew is seconds, not minutes. A quote claiming to be more than a
# minute in the FUTURE is a broken feed, not a fast clock - and the plain
# `age_s > max_age_s` check could never see it, because a future timestamp
# makes the age negative. Shared with the freshness lamp so both sides read
# one rule instead of two.
FUTURE_TOLERANCE_S = 60


def filter_stale(quotes, criteria: ScreenerCriteria, now_fn=None):
    """max_quote_age_min uses quote_timestamp. Fail-closed: when the guard
    is enabled, a quote whose age cannot be established (missing or
    unparseable timestamp) is dropped with a distinct reason - unknown
    freshness must not silently pass a 30-minute guard.

    B-17: the clock call is checked SEPARATELY. A failing now_fn() is an
    internal error, not an unreadable timestamp - the reason text is shown
    on the page, so it must not lie. Measured 2026-10-10: a caller passing
    an arrow object instead of a function got 'unparseable timestamp' for
    contracts although all 104 carried a valid timestamp.

    Slice J: a timestamp AFTER the clock yields a NEGATIVE age, so the plain
    `age_s > max_age_s` check let every such quote through - measured: +1 s,
    +1 day and 9999-12-31 were all kept with no reason, while the lamp called
    the chain "frisch". More than FUTURE_TOLERANCE_S ahead is therefore
    dropped and named as such; the tolerance covers clock skew of seconds and
    nothing else. With the guard switched off nothing is checked here.
    """
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
            dropped.append((q, "Aktualität unbekannt (kein Zeitstempel)"))
            continue
        try:
            now_v = now_fn()
            if now_v is None:
                raise ValueError("clock returned None")
            if getattr(now_v, 'tzinfo', None) is not None or isinstance(now_v, arrow.Arrow):
                now_naive = arrow.get(now_v).to('UTC').naive
            elif isinstance(now_v, datetime):
                now_naive = now_v
            else:
                raise TypeError(f"unsupported clock value: {type(now_v).__name__}")
        except Exception:
            # internal error, NOT an unreadable timestamp (B-17)
            dropped.append((q, "Aktualität unbekannt (Uhrfehler)"))
            continue
        try:
            # normalize both sides to UTC, THEN strip the offset: arrow
            # timestamps carry UTC, callers may pass aware arrows in any
            # timezone, .naive alone drops the offset WITHOUT conversion
            # (a 14:35-04:00 clock read against a 14:30 UTC stamp would
            # look 5 min old instead of 5 h 5 min) - so to('UTC') first
            ts_naive = arrow.get(str(ts).replace(' ', 'T')).to('UTC').naive
            age_s = (now_naive - ts_naive).total_seconds()
        except Exception:
            dropped.append((q, "Aktualität unbekannt (Zeitstempel unlesbar)"))
            continue
        if age_s < -FUTURE_TOLERANCE_S:
            # the wording keeps the rounding of the stale branch below: the
            # existing edge (30 min + 1 s reads "stale 30 min") must not move
            dropped.append((q, f"Zeitstempel in der Zukunft (+{(-age_s)/60:.0f} Min.)"))
        elif age_s > max_age_s:
            dropped.append((q, f"veraltet: {age_s/60:.0f} Min."))
        else:
            kept.append(q)
    return kept, dropped


# ---------------------------------------------------------------------- #
# orchestrator
# ---------------------------------------------------------------------- #

_TREND_LABEL = {"up": "steigend", "down": "fallend", "flat": "neutral"}


def _trend_label(direction):
    """German label for a trend direction; unknown values pass through.

    Lives here and not in server.py because the hint reasons in screen()
    are the first consumer; the page badge imports the same words, so both
    surfaces cannot drift apart.
    """
    return _TREND_LABEL.get(direction, direction)


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
    quotes, d = filter_dte(quotes, criteria, now_fn=now_fn); dropped_all += d
    quotes, d = filter_sizes(quotes, criteria);         dropped_all += d

    # D10: the IV component is a RANK inside the scored set, not a position inside an
    # optional band. Measured: with iv_pref set but no band every quote scored 0.5, so
    # 10 % IV and 60 % IV tied even at weight_iv = 1 - the preference did nothing. The
    # band (iv_min/iv_max) keeps its job as a filter; it no longer doubles as the scale.
    ivs = sorted({q.iv for q in quotes if q.iv is not None})

    def _iv_rank(value):
        if len(ivs) < 2:
            return 0.5      # one value (or none) carries no ranking information
        return ivs.index(value) / (len(ivs) - 1)

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
        # D10: IV stays NEUTRAL unless a preference is set (None -> no influence on the
        # ranking, as before). With a preference the score is the rank inside the scored
        # set - always effective, no band required:
        #   'high' -> higher IV ranks better (premium selling)
        #   'low'  -> lower IV ranks better (debit strategies)
        # The band (iv_min/iv_max) remains a FILTER and no longer sets the scale.
        iv_score = 0.5
        if criteria.iv_pref in ("high", "low") and iv is not None:
            iv_score = _iv_rank(iv)
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
                reasons.append(f"Trend-Symbol ({trend.symbol}) passt nicht")
            elif trend.direction == 'up' and q.asset.option_type == 'call':
                reasons.append("Trend steigend passt (Call)")
            elif trend.direction == 'down' and q.asset.option_type == 'put':
                reasons.append("Trend fallend passt (Put)")
            elif trend.direction == 'flat' or trend.direction not in ('up', 'down'):
                reasons.append("Trend neutral")
            else:
                reasons.append(f"Trend {_trend_label(trend.direction)} passt nicht "
                               f"zu {q.asset.option_type}")

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

# ---------------------------------------------------------------------- #
# Ausbau-Slice B: vertical call spreads from the raw chain.
#
# A vertical is a property of the CHAIN, not of the screened candidate
# list: the eight single-option filters do not preserve strike adjacency.
# Measured on four live chains (2026-10-10): the default criteria left
# 0 candidates and therefore 0 pairs on every chain, while the raw chains
# carried 102/104/300/182 adjacent call pairs; with the age guard off only
# 7 candidates -> 5 pairs survived. So the pairs are formed FIRST and the
# per-leg rules run afterwards - the same contract screen() uses when it
# reports the FIRST filter that rejects a contract.
#
# Puts are deliberately not built here: no quote fixture in the suite
# carries a put, so a put rule could not be pinned by a test. The rule
# would read: long the HIGHER strike, breakeven = short strike - debit.
# ---------------------------------------------------------------------- #



@dataclass
class VerticalPair:
    """One vertical spread in its trade direction: long is the leg that is bought.

    A call debit spread buys the LOWER strike, a put debit spread buys the HIGHER one;
    the builder decides which leg lands in `long`, and every derived number follows it.

    Every derived number comes from ONE rounded debit, so the page and the
    JSON cannot drift apart: 5.1 - 2.4 is 2.6999999999999997 in binary
    floating point, and the source is the single place to round it.
    """

    long: OptionQuote
    short: OptionQuote

    @property
    def symbol(self):
        return f"{self.long.asset.symbol}/{self.short.asset.symbol}"

    @property
    def expiration(self):
        return self.long.asset.expiration_date

    @property
    def strike_width(self):
        # D12: a distance, not a signed difference. For a call spread the short leg is
        # the higher one, for a put spread the long leg is - without abs() the width of
        # every put pair came out negative and the pair rule (0 < debit < width) would
        # have dropped them all.
        return round(abs(self.short.asset.strike - self.long.asset.strike), 2)

    @property
    def net_debit(self):
        """Immediately executable cost: buy the long leg at ask, sell the
        short leg at bid. Rule 1 guarantees both sides are quoted, so no
        missing-side fallback is needed here."""
        return round(self.long.ask - self.short.bid, 2)

    @property
    def max_loss(self):
        # a debit spread loses at most what it costs - no second computation
        return self.net_debit

    @property
    def max_profit(self):
        return round(self.strike_width - self.net_debit, 2)

    @property
    def breakeven(self):
        # D12: a call debit spread breaks even at lower + debit, a put debit spread at
        # higher - debit. The long leg carries the direction, the sign follows from it.
        if getattr(self.long.asset, "option_type", "call") == "put":
            return round(self.long.asset.strike - self.net_debit, 2)
        return round(self.long.asset.strike + self.net_debit, 2)

    @property
    def debit_width_ratio(self):
        width = self.strike_width
        return round(self.net_debit / width, 4) if width else None

    @property
    def debit_ge_width(self):
        """Derived marker, never stored: at or above the width the spread
        cannot make money. Measured on the raw chains: 316 of 560 cleanly
        quoted call pairs - the most common case, so it stays visible
        instead of being filtered away."""
        return self.net_debit >= self.strike_width

    def position_debit(self, criteria=None):
        """D12: what this position costs - multiplier x quantity x debit, plus fees.

        None when the position size is not set (or the debit is not a number): a missing
        size must not silently read as zero.
        """
        if criteria is None or criteria.multiplier is None or criteria.quantity is None:
            return None
        debit = self.net_debit
        if debit is None:
            return None
        return round(criteria.multiplier * criteria.quantity * debit + (criteria.fees or 0.0), 2)

    def as_dict(self, criteria=None):
        """The JSON shape - built from the same attributes the page reads."""
        return {
            "symbol": self.symbol,
            "expiration": self.expiration,
            "long_symbol": self.long.asset.symbol,
            "short_symbol": self.short.asset.symbol,
            "long_strike": self.long.asset.strike,
            "short_strike": self.short.asset.strike,
            "strike_width": self.strike_width,
            # D12: per-unit value AND the position it would be. The criteria come in as
            # an argument: a pair is built from two legs and does not own the settings.
            "multiplier": None if criteria is None else criteria.multiplier,
            "quantity": None if criteria is None else criteria.quantity,
            "fees": None if criteria is None else criteria.fees,
            "budget": None if criteria is None else criteria.budget,
            "currency": "USD",
            "position_debit": self.position_debit(criteria),
            "net_debit": self.net_debit,
            "max_loss": self.max_loss,
            "max_profit": self.max_profit,
            "breakeven": self.breakeven,
            "debit_width_ratio": self.debit_width_ratio,
            "debit_ge_width": self.debit_ge_width,
        }


def form_vertical_pairs(chain, option_type="call", max_width=None):
    """Adjacent-strike pairs per (underlying, expiration) from the RAW chain.

    The iteration order IS the output order: groups ascending by
    (underlying, expiration, option_type), strikes ascending inside a group.
    Nothing is sorted afterwards, so the order is reproducible without any
    scoring rule.
    """
    groups = {}
    for quote in chain or []:
        asset = getattr(quote, "asset", None)
        if asset is None or getattr(asset, "option_type", None) != option_type:
            continue
        key = (getattr(asset.underlying, "symbol", None),
               getattr(asset, "expiration_date", None),
               option_type)
        groups.setdefault(key, []).append(quote)
    def _paar(lower, upper):
        # D12: the pair order IS the trade direction. A call debit spread buys the LOWER
        # strike, a put debit spread buys the HIGHER one - the comment above has said so
        # all along, the code did not do it.
        if option_type == "put":
            return VerticalPair(long=upper, short=lower)
        return VerticalPair(long=lower, short=upper)

    pairs = []
    for key in sorted(groups):
        legs = sorted(groups[key], key=lambda q: q.asset.strike)
        if max_width is None:
            # fast mode (default): adjacent strikes only
            pairs.extend(_paar(lower, upper)
                         for lower, upper in zip(legs, legs[1:], strict=False))
            continue
        # D11: every pair inside an explicitly bounded width. Measured on a live chain
        # (442 contracts): 220 adjacent pairs, 2880 inside 25 $ - both under 1 ms, so
        # the wider search costs nothing. What grows is the NUMBER of results, which is
        # why this is opt-in: an illiquid contract between two good legs no longer hides
        # the wider spread (review finding 4). Every pair still runs through the same
        # leg and pair checks as the list.
        for i, lower in enumerate(legs):
            for upper in legs[i + 1:]:
                breite = float(upper.asset.strike) - float(lower.asset.strike)
                if breite > max_width:
                    break
                pairs.append(_paar(lower, upper))
    return pairs


def leg_drop_reason(leg, criteria, now_fn=None):
    """First reason the SINGLE-OPTION chain rejects this leg, with the leg named.

    D7: the spread list and the option list above it must never disagree about
    the same contract. So the leg runs through exactly the chain screen() runs,
    in exactly its order, and is reported with the first reason that chain
    produces. Before D7 this function had fixed rules of its own (OI 500, IV > 0)
    and ignored the form entirely.

    One reason per leg, not a list - the reader sees the shape the single-option
    drop list already uses ("strike 270.0 call: OI 40 < 500").
    """
    _kept, dropped = screen([leg], criteria, now_fn=now_fn)
    if not dropped:
        return None
    return f"strike {leg.asset.strike} {leg.asset.option_type}: {dropped[0][1]}"


def vertical_spreads(chain, criteria, option_type="call", now_fn=None):
    """(kept pairs, dropped pairs with reason) under the SAME criteria as the list.

    D7: the spread list used to run fixed leg rules of its own (both sides
    quoted, OI >= 500, IV > 0) and ignored the form, so a user could read a
    spread as the result of filters it never passed. The list and this section
    are now filtered by one rule set, produced in this one place so the page and
    the JSON cannot drift apart.

    Pairs first, rules afterwards: filtering the legs before pairing would break
    strike adjacency and change the result. Measured on the owning fixture:
    pairs-first gives 4 pairs / 1 kept / 3 dropped, filter-first gives 3 pairs /
    1 kept / 2 dropped.

    The maturity window is a gate for the WHOLE section: both legs always carry
    the same expiration, and this function is called per (underlying, expiry), so
    an expiry outside the window must leave the section empty - and say why.
    """
    kept, dropped = [], []
    # The maturity window is a gate for the WHOLE section: both legs of a pair
    # always carry the same expiration. It is applied per quote (the field
    # filter_dte reads), so a chain carrying several expiries is handled too -
    # only quotes inside the window take part in pairing.
    if criteria.dte_min is not None or criteria.dte_max is not None:
        gefiltert = [q for q in chain or []
                     if getattr(q, "days_to_expiration", None) is not None
                     and (criteria.dte_min is None
                          or q.days_to_expiration >= criteria.dte_min)
                     and (criteria.dte_max is None
                          or q.days_to_expiration <= criteria.dte_max)]
        if (chain or []) and not gefiltert:
            tage = sorted({q.days_to_expiration for q in chain
                           if getattr(q, "days_to_expiration", None) is not None})
            return [], [(None, f"Verfall: Laufzeit {tage} Tage außerhalb "
                              f"[{criteria.dte_min}, {criteria.dte_max}]")]
        chain = gefiltert
    for pair in form_vertical_pairs(chain, option_type=option_type,
                                   max_width=criteria.pair_max_width):
        if criteria.budget is not None:
            groesse = pair.position_debit(criteria)
            if groesse is not None and groesse > criteria.budget:
                dropped.append((pair, f"Position {groesse:.2f} USD > Budget {criteria.budget:.2f} USD"))
                continue
        if pair.net_debit <= 0 or pair.net_debit >= pair.strike_width:
            # PAIR-level rule (both legs are quoted by now). Debit <= 0 means the
            # higher strike's bid reaches the lower strike's ask - an incoherent
            # quote pair. Debit >= width means the spread cannot make money at
            # expiry at all (measured live: 6 of 54 SPY November spreads, the
            # review's 768/769 among them): its break-even would sit ABOVE the
            # upper strike, outside the payoff range, and a "distance to
            # break-even" built from it is meaningless. Such a construction is
            # not a result.
            dropped.append((pair, f"Debit {pair.net_debit} außerhalb "
                                  f"(0, {pair.strike_width}) - kein Gewinn möglich"))
            continue
        reason = (leg_drop_reason(pair.long, criteria, now_fn)
                  or leg_drop_reason(pair.short, criteria, now_fn))
        if reason is None:
            kept.append(pair)
        else:
            dropped.append((pair, reason))
    return kept, dropped
