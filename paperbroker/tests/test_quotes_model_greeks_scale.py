"""Scale contract for the model-path greeks in OptionQuote.

Two paths store greeks on an ``OptionQuote``:

- ``greeks_source="adapter"`` - CBOE feed values, used as-is
- ``greeks_source="model"``   - computed via ``get_option_greeks``, scaled here

``delta``, ``gamma``, ``iv`` and ``theta`` are stored x100. ``vega`` and ``rho``
are NOT: the model already returns them per 1.00 unit of volatility / rate, so
the extra x100 made them a factor 100 too large. Measured over 4210 liquid
contracts (AAPL/MSFT/SPY), the CBOE raw value divided by the textbook value is
1.0039 for delta, 1.0020 for gamma and 0.9692 for theta against the daily value -
but 0.0100 for vega and 0.0101 for rho.

The gold values come from ``get_option_greeks`` itself, because that function
derives its own sigma from the option price. A hand-picked sigma pins the wrong
numbers: with the CBOE raw iv 0.2608 the module returns vega 45.14871, not
45.19255.

Both fixtures are real CBOE contracts (delayed feed, 2026-10-08 20:26 UTC, AAPL
spot 340.5301); the prices are the quoted mid points.
"""
import pytest

from paperbroker.assets import asset_factory
from paperbroker.logic.ivolat3_option_greeks import get_option_greeks
from paperbroker.quotes import OptionQuote

QUOTE_DATE = "2026-10-08 20:26:30"
SPOT = 340.5301

# --- call fixture: AAPL261120C00335000, dte 43, bid 15.55 / ask 16.00 ----
CALL_SYMBOL = "AAPL261120C00335000"
CALL_STRIKE = 335.0
CALL_DTE = 43
CALL_PRICE = 15.75
CALL_VEGA_GOLD = 45.1925549868
CALL_RHO_GOLD = 22.1650782422
CALL_THETA_GOLD = -15.0666387346

# --- put fixture: AAPL261120P00340000, dte 43, bid 11.00 / ask 11.25 -----
PUT_SYMBOL = "AAPL261120P00340000"
PUT_STRIKE = 340.0
PUT_DTE = 43
PUT_PRICE = 11.125
PUT_VEGA_GOLD = 46.4464960936
PUT_RHO_GOLD = -19.9548316935


def _model_quote(symbol, price, **overrides):
    """Build a fixture option through the model path."""
    kwargs = dict(price=price, bid=price * 0.99, ask=price * 1.01,
                  underlying_price=SPOT, greeks_source="model")
    kwargs.update(overrides)
    return OptionQuote(QUOTE_DATE, asset_factory(symbol), **kwargs)


def test_the_module_still_returns_vega_and_rho_per_one_unit():
    """Pin the source of truth the storage scale below is derived from.

    If this fails, the reference scale moved and every tolerance in this file
    is meaningless - that is the one signal the exact-value pins cannot give.
    """
    raw = get_option_greeks("call", CALL_STRIKE, SPOT, CALL_DTE, CALL_PRICE,
                            dividend=0.0)
    assert raw["vega"] == pytest.approx(CALL_VEGA_GOLD, abs=1e-6)
    assert raw["rho"] == pytest.approx(CALL_RHO_GOLD, abs=1e-6)


def test_model_path_stores_call_vega_without_the_x100_factor():
    """The x100 defect stored 4519.2554986837; CALL_VEGA_GOLD is correct."""
    q = _model_quote(CALL_SYMBOL, CALL_PRICE)
    assert q.vega == pytest.approx(CALL_VEGA_GOLD, abs=1e-4)


def test_model_path_stores_call_rho_without_the_x100_factor():
    """The x100 defect stored 2216.5078242223; CALL_RHO_GOLD is correct."""
    q = _model_quote(CALL_SYMBOL, CALL_PRICE)
    assert q.rho == pytest.approx(CALL_RHO_GOLD, abs=1e-4)


def test_model_path_stores_put_rho_and_vega_without_the_x100_factor():
    """A put carries a negative rho; the x100 defect stored -1995.483169.

    vega is put/call symmetric only for a shared sigma and expiry; this put
    fixture has its own sigma, so both values are pinned from its own price.
    """
    q = _model_quote(PUT_SYMBOL, PUT_PRICE)
    assert q.rho == pytest.approx(PUT_RHO_GOLD, abs=1e-4)
    assert q.rho < 0.0
    assert q.vega == pytest.approx(PUT_VEGA_GOLD, abs=1e-4)
    assert q.vega > 0.0


def test_theta_keeps_its_x100_day_scaling():
    """theta is a neighbour field: it stays on the x100 daily scale.

    bs_model returns an annualised theta and the /365 sits in
    ivolat3_option_greeks, so the x100 here is correct and must not move.
    """
    q = _model_quote(CALL_SYMBOL, CALL_PRICE)
    assert q.theta == pytest.approx(CALL_THETA_GOLD, abs=1e-3)
