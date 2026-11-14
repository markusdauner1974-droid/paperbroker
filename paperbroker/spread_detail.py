"""One debit spread, in numbers: payoff, today's model value, scenarios.

Why this module exists
----------------------
The funnel answers WHICH spread a gap candidate would trade and what it costs.
That is enough to choose it and not enough to look at it: a reader wants the two
legs with their quotes, the four break-even numbers of the position, the profit
and loss for every price between half and 1.7 times the spot, and what happens
to the position when the underlying moves in ten-percent steps.

Everything here is arithmetic over data a caller supplies. A quote is read with
``getattr`` and a missing value stays ``None`` - never a zero, never a guess.
There is no chart library and no numpy: the curve is a list of points, and the
picture is drawn from that list by the page (SVG), not here.

The conventions are the repo's
------------------------------
* prices in dollars per share, greeks in the percentage convention of the
  chain (``delta=38.0`` means 0.38 per share, ``iv=42.51`` means 42.51 %),
* one spread is one contract pair: 100 shares, multiplier ``MULTIPLIER``,
* ``net_debit = ask(long) - bid(short)`` - the price one PAYS, not the mid,
* reward/risk here is ``max_profit / max_loss``, the plain ratio of the
  position; the funnel's R/R is a different number (a percentage return) and is
  not re-derived from this one.

The two curves are two different questions
------------------------------------------
``pl_expiry`` is exact: the payout of the pair at expiration minus the debit.
``pl_today`` is a MODEL value: both legs priced with Black-Scholes at the
shifted spot, each with the implied volatility the chain shows for THAT leg
(r = ``RATE``, q = ``DIVIDEND``), minus the debit. At the current spot it is
slightly negative, because the debit was paid at the ask/bid and the model
prices the mid - that difference IS the half-spread the trade costs.
"""

from __future__ import annotations

import math

#: Risk-free rate used by the model curve. The chain's own price is not
#: re-derived from it; it only prices the UNSEEN points of today's curve.
RATE = 0.04
#: Dividend yield of the model. Zero because this repo has no dividend feed and
#: a made-up 2 % would move every point of the "today" curve silently.
DIVIDEND = 0.0
#: One spread is 100 shares.
MULTIPLIER = 100

#: The scenario ladder of the agreed contract, in percent of the spot.
SCENARIO_CHANGES = (-30, -20, -10, 0, 10, 20)
#: The price axis: 50 % .. 170 % of the spot.
SERIES_FROM = 0.5
SERIES_TO = 1.7
#: 161 grid points; the spot is added as its own point, so the answer carries
#: 162 - inside the agreed 120..200 and dense enough to read as a curve.
SERIES_POINTS = 161
#: Below this many DISTINCT prices the 2-decimal grid has collapsed (a sub-dollar
#: spot), and the points are rebuilt with four decimals instead of shipping a
#: curve of duplicates.
SERIES_MIN_DISTINCT = 120

CALL = "call"
PUT = "put"

#: Days per year for the model. Calendar days: an option's clock does not stop
#: on weekends, and the chain's own DTE counts them the same way.
DAYS_PER_YEAR = 365.0


class SpreadDetailError(ValueError):
    """A spread that cannot be described, named by the code the route answers.

    ``code`` is the machine-readable reason (``leg_not_listed``,
    ``invalid_spread``); ``message`` is the sentence a human reads. No value is
    ever filled in to keep the answer at 200: half a spread is not a spread.
    """

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _number(source, name):
    """One finite float of ``source`` (attribute or key), else ``None``."""
    value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round2(value):
    """Two decimals, or ``None`` - the same rule as the funnel's rows."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 2)


def _int_or_none(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number)


# --- the model ------------------------------------------------------------- #
def norm_cdf(x):
    """Standard normal CDF, from ``math.erf`` - one call, no table, no library."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x):
    """Standard normal density."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def intrinsic(option_type, spot, strike):
    """What one option is worth right now if it expired this second."""
    if option_type == PUT:
        return max(strike - spot, 0.0)
    return max(spot - strike, 0.0)


def black_scholes(option_type, spot, strike, years, vol, rate=RATE, dividend=DIVIDEND):
    """The model price of ONE option per share.

    ``years`` is the time to expiration in years and ``vol`` the volatility as a
    FRACTION (0.4251 for 42.51 %). At or past expiration - and for a vol of zero,
    which is the same statement about certainty - the answer is the intrinsic
    value, because that is what the formula converges to and dividing by
    ``vol * sqrt(T)`` would not.
    """
    if spot is None or strike is None:
        return None
    if strike <= 0 or spot <= 0:
        return None
    if years is None or years <= 0 or not vol or vol <= 0:
        return intrinsic(option_type, spot, strike)
    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * vol * vol) * years) / root
    d2 = d1 - root
    discount_r = math.exp(-rate * years)
    discount_q = math.exp(-dividend * years)
    if option_type == PUT:
        return strike * discount_r * norm_cdf(-d2) - spot * discount_q * norm_cdf(-d1)
    return spot * discount_q * norm_cdf(d1) - strike * discount_r * norm_cdf(d2)


def model_greeks(option_type, spot, strike, years, vol, rate=RATE, dividend=DIVIDEND):
    """The four greeks of one option in the repo's percentage convention.

    ``delta`` and ``gamma`` are multiplied by 100 like the chain's own values
    (per 100 shares), ``vega`` is the change per ONE volatility point and
    ``theta`` the change per CALENDAR day - both per spread, so they can stand
    beside the adapter's numbers without a second unit. A greek that the model
    cannot compute (no time, no vol, no price) is ``None``, not zero.
    """
    if spot is None or strike is None or spot <= 0 or strike <= 0:
        return {"delta": None, "gamma": None, "vega": None, "theta": None}
    if years is None or years <= 0 or not vol or vol <= 0:
        # At expiration the position is its payout: delta is +/-1 inside the
        # money and 0 outside, gamma/vega/theta are zero because nothing is
        # left to react to. Written out instead of returning the last model
        # value, which would describe an option that no longer exists.
        if option_type == PUT:
            delta = -1.0 if spot < strike else 0.0
        else:
            delta = 1.0 if spot > strike else 0.0
        return {"delta": round(delta * 100.0, 2), "gamma": 0.0, "vega": 0.0, "theta": 0.0}

    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * vol * vol) * years) / root
    d2 = d1 - root
    discount_q = math.exp(-dividend * years)
    discount_r = math.exp(-rate * years)
    density = norm_pdf(d1)

    if option_type == PUT:
        delta = -discount_q * norm_cdf(-d1)
        theta_annual = (-spot * discount_q * density * vol / (2.0 * math.sqrt(years))
                        + rate * strike * discount_r * norm_cdf(-d2)
                        - dividend * spot * discount_q * norm_cdf(-d1))
    else:
        delta = discount_q * norm_cdf(d1)
        theta_annual = (-spot * discount_q * density * vol / (2.0 * math.sqrt(years))
                        - rate * strike * discount_r * norm_cdf(d2)
                        + dividend * spot * discount_q * norm_cdf(d1))

    return {
        "delta": round(delta * 100.0, 2),
        "gamma": round(discount_q * density / (spot * root) * 100.0, 2),
        # Per volatility POINT and per 100 shares: S * phi(d1) * sqrt(T).
        "vega": round(spot * discount_q * density * math.sqrt(years), 2),
        "theta": round(theta_annual * MULTIPLIER / DAYS_PER_YEAR, 2),
    }


# --- the position ---------------------------------------------------------- #
def _strike(quote):
    strike = _number(quote, "strike")
    if strike is None:
        strike = _number(getattr(quote, "asset", None), "strike")
    return strike


def _leg_quote_number(quote, name, side):
    value = _number(quote, name)
    if value is None:
        raise SpreadDetailError(
            "leg_not_listed",
            f"Der {side}-Leg hat keinen lesbaren Wert für {name}.",
        )
    return value


def spread_numbers(long_quote, short_quote, option_type):
    """The four numbers of the position, from the two quotes alone.

    ``net_debit`` is what one PAYS: the ask of the bought leg minus the bid of
    the sold one. The width is the distance of the strikes, so the maximum
    profit is what is left of the width after the debit - never below zero, and
    ``None`` for a pair whose width is zero (that is not a spread).
    """
    long_strike = _strike(long_quote)
    short_strike = _strike(short_quote)
    if long_strike is None or short_strike is None:
        raise SpreadDetailError("leg_not_listed", "Ein Leg hat keinen Strike.")
    ask_long = _leg_quote_number(long_quote, "ask", "long")
    bid_short = _leg_quote_number(short_quote, "bid", "short")
    net_debit = ask_long - bid_short
    if net_debit <= 0:
        raise SpreadDetailError(
            "invalid_spread",
            "Der Spread kostet nichts - Ask des Long-Legs liegt nicht über dem "
            "Bid des Short-Legs.",
        )
    width = abs(long_strike - short_strike)
    if width <= 0:
        raise SpreadDetailError("invalid_spread", "Beide Legs haben denselben Strike.")
    max_loss = net_debit * MULTIPLIER
    max_profit = (width - net_debit) * MULTIPLIER
    if option_type == PUT:
        breakeven = long_strike - net_debit
    else:
        breakeven = long_strike + net_debit
    return {
        "net_debit": _round2(net_debit),
        "width": _round2(width),
        "max_loss": _round2(max_loss),
        "max_profit": _round2(max_profit),
        "breakeven": _round2(breakeven),
        "rr": _round2(max_profit / max_loss) if max_loss > 0 else None,
    }


def expiry_value(option_type, price, long_strike, short_strike):
    """What the PAIR pays at expiration, per share, before the debit.

    One expression, two signs: a call spread is worth ``max(S-Kl,0) -
    max(S-Ks,0)``, a put spread ``max(Kl-S,0) - max(Ks-S,0)``. Writing both out
    would be two places where the sign can drift apart.
    """
    if option_type == PUT:
        return max(long_strike - price, 0.0) - max(short_strike - price, 0.0)
    return max(price - long_strike, 0.0) - max(price - short_strike, 0.0)


def expiry_pl(option_type, price, long_strike, short_strike, net_debit):
    """Profit/loss of one spread at expiration, in dollars."""
    return (expiry_value(option_type, price, long_strike, short_strike) - net_debit) * MULTIPLIER


def today_pl(option_type, price, long_strike, short_strike, net_debit,
             iv_long, iv_short, years):
    """Profit/loss of one spread TODAY at ``price``, from the model.

    Each leg is priced with ITS OWN implied volatility, because the two legs of
    a skew are not the same contract: using one vol for both would draw a curve
    the market does not quote. ``None`` when either vol is missing - a model
    curve without a volatility is not a model curve.
    """
    if iv_long is None or iv_short is None:
        return None
    if iv_long <= 0 or iv_short <= 0:
        return None
    lang = black_scholes(option_type, price, long_strike, years, iv_long / 100.0)
    kurz = black_scholes(option_type, price, short_strike, years, iv_short / 100.0)
    if lang is None or kurz is None:
        return None
    return (lang - kurz - net_debit) * MULTIPLIER


def position_greeks(long_quote, short_quote):
    """Long leg minus short leg, in the chain's own convention.

    ``delta``/``gamma``/``vega``/``theta`` are read as the adapter delivers
    them (already per 100 shares) and subtracted; a greek that only one leg
    carries stays ``None``, because a difference from a guess is a guess.
    """
    out = {}
    for name in ("delta", "gamma", "vega", "theta", "rho"):
        links = _number(long_quote, name)
        rechts = _number(short_quote, name)
        out[name] = None if links is None or rechts is None else _round2(links - rechts)
    return out


def _model_position_greeks(option_type, price, long_strike, short_strike, years,
                           iv_long, iv_short):
    """The position's model greeks at one price, or ``None`` per greek."""
    if iv_long is None or iv_short is None or iv_long <= 0 or iv_short <= 0:
        return {"delta": None, "gamma": None, "vega": None, "theta": None}
    lang = model_greeks(option_type, price, long_strike, years, iv_long / 100.0)
    kurz = model_greeks(option_type, price, short_strike, years, iv_short / 100.0)
    out = {}
    for name in ("delta", "gamma", "vega", "theta"):
        links, rechts = lang[name], kurz[name]
        out[name] = None if links is None or rechts is None else _round2(links - rechts)
    return out


# --- the shape ------------------------------------------------------------- #
def series_prices(spot):
    """The price axis of the chart: 50 % .. 170 % of the spot, plus the spot.

    The spot is added as its own point even when it does not fall on the grid
    (it never does at 161 points), because the marker of the chart has to sit on
    a real point of the curve. Duplicates are dropped: a curve with two points
    at the same price is a vertical line, not information.
    """
    if spot is None or spot <= 0:
        return []
    low = SERIES_FROM * spot
    high = SERIES_TO * spot
    step = (high - low) / (SERIES_POINTS - 1)

    def grid(digits):
        prices = [round(low + step * index, digits) for index in range(SERIES_POINTS)]
        prices.append(round(spot, digits))
        return sorted(set(prices))

    prices = grid(2)
    if len(prices) < SERIES_MIN_DISTINCT:
        # A sub-dollar spot: two decimals collapse the grid into a handful of
        # prices, so the same axis is built with four. The series stays a curve.
        prices = grid(4)
    return prices


def build_detail(*, symbol, name, day, strategy, strategy_label, expiration,
                 option_type, long_quote, short_quote, spot, spot_source):
    """The whole answer of one spread: legs, numbers, today, expiry, scenarios.

    ``day`` is the scanned reaction day and ``expiration`` the listed series the
    spread trades; their distance is ``dte`` in CALENDAR days, exactly as the
    funnel counts it. ``spot_source`` says where the price axis came from
    (``chain_underlying`` or ``row_close``) - the chart is only as good as its
    centre, and the answer names it instead of leaving it to be guessed.
    """
    long_strike = _strike(long_quote)
    short_strike = _strike(short_quote)
    if long_strike is None or short_strike is None:
        raise SpreadDetailError("leg_not_listed", "Ein Leg hat keinen Strike.")
    if spot is None or spot <= 0:
        raise SpreadDetailError("chain_unavailable", "Kein Kurs für die Preisachse.")
    if expiration is None or day is None:
        raise SpreadDetailError("invalid_spread", "Tag oder Verfall fehlt.")

    zahlen = spread_numbers(long_quote, short_quote, option_type)
    net_debit = zahlen["net_debit"]
    days = (expiration - day).days
    years = max(days, 0) / DAYS_PER_YEAR
    iv_long = _number(long_quote, "iv")
    iv_short = _number(short_quote, "iv")

    legs = [leg_row(long_quote, "long", long_strike),
            leg_row(short_quote, "short", short_strike)]

    serie = []
    for price in series_prices(spot):
        serie.append({
            "price": price,
            "pl_expiry": _round2(expiry_pl(option_type, price, long_strike,
                                           short_strike, net_debit)),
            "pl_today": _round2(today_pl(option_type, price, long_strike, short_strike,
                                         net_debit, iv_long, iv_short, years)),
        })

    szenarien = []
    for change in SCENARIO_CHANGES:
        underlying = spot * (1.0 + change / 100.0)
        werte = _model_position_greeks(option_type, underlying, long_strike, short_strike,
                                       years, iv_long, iv_short)
        szenarien.append({
            "change_pct": change,
            "underlying": _round2(underlying),
            "pl_expiry": _round2(expiry_pl(option_type, underlying, long_strike,
                                           short_strike, net_debit)),
            "delta": werte["delta"],
            "gamma": werte["gamma"],
            "vega": werte["vega"],
            "theta": werte["theta"],
        })

    notes = [
        "G&V je 1 Spread (100 Aktien), ohne Gebühren.",
        "Heute-Kurve: Black-Scholes mit der IV des jeweiligen Legs "
        f"(r = {RATE * 100:.0f} %, q = 0 %); Verfall exakt.",
        f"Preisachse {SERIES_FROM * 100:.0f} % bis {SERIES_TO * 100:.0f} % des Spot "
        f"({spot_source}).",
    ]
    if iv_long is None or iv_short is None:
        notes.append("Ohne IV eines Legs bleibt die Heute-Kurve leer.")

    return {
        "schema_version": "1.0",
        "symbol": symbol,
        "name": name,
        "day": day.isoformat(),
        "strategy": strategy,
        "strategy_label": strategy_label,
        "expiration": expiration.isoformat(),
        "dte": days,
        "spot": _round2(spot),
        "spot_source": spot_source,
        "strikes": {"long": _round2(long_strike), "short": _round2(short_strike)},
        "legs": legs,
        "net_debit": net_debit,
        "net_debit_basis": "ask_long_minus_bid_short",
        "max_loss": zahlen["max_loss"],
        "max_profit": zahlen["max_profit"],
        "breakeven": zahlen["breakeven"],
        "rr": zahlen["rr"],
        "position_greeks": position_greeks(long_quote, short_quote),
        "series": serie,
        "scenarios": szenarien,
        "notes": notes,
    }


def leg_row(quote, side, strike=None):
    """One leg of the answer: the quote as it is, missing values as ``None``."""
    strike = _strike(quote) if strike is None else strike
    bid = _number(quote, "bid")
    ask = _number(quote, "ask")
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else _number(quote, "price")
    return {
        "side": side,
        "strike": _round2(strike),
        "bid": _round2(bid),
        "ask": _round2(ask),
        "mid": _round2(mid),
        "iv": _round2(_number(quote, "iv")),
        "delta": _round2(_number(quote, "delta")),
        "gamma": _round2(_number(quote, "gamma")),
        "vega": _round2(_number(quote, "vega")),
        "theta": _round2(_number(quote, "theta")),
        "open_interest": _int_or_none(_number(quote, "open_interest")),
        "volume": _int_or_none(_number(quote, "volume")),
    }
