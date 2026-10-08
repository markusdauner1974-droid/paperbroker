"""Gold-value tests for the model greeks entry point.

The module had no test coverage at all, so these tests pin the numeric
result of get_option_greeks() for one well-known contract across three
expirations. The values were produced with an independent textbook
Black-Scholes implementation (r annualized at 0.02) and cross-checked by
feeding the resulting IV back into the reference price.

Reference contract: S=100, K=100, call, price=3.00, q=0, r=0.02.

Units: get_option_greeks() returns RAW values (iv 0.255321, not 25.5321;
theta per day). OptionQuote multiplies by 100 later - that scaling is not
this module's job.
"""
import math

import pytest

from paperbroker.logic.ivolat3_option_greeks import get_option_greeks

SPOT = 100.0
STRIKE = 100.0
PRICE = 3.00
ABS_TOL = 1e-5

EXPECTED_KEYS = frozenset({
    "iv", "delta", "vega", "theta", "rho", "gamma", "vanna", "charm",
    "speed", "zomma", "color", "veta", "vomma", "ultima", "dual_delta",
})


def _greeks(dte, option_type="call"):
    """Call the module with keywords only - argument order has bitten before."""
    return get_option_greeks(
        option_type=option_type,
        strike=STRIKE,
        underlying_price=SPOT,
        days_to_expiration=dte,
        price=PRICE,
        dividend=0.0,
    )


# --- bug exclusion: gold values across three expirations ------------------- #

def test_iv_call_dte7_gold():
    assert _greeks(7)["iv"] == pytest.approx(0.539762, abs=ABS_TOL)


def test_iv_call_dte30_gold():
    assert _greeks(30)["iv"] == pytest.approx(0.255321, abs=ABS_TOL)


def test_iv_call_dte90_gold():
    assert _greeks(90)["iv"] == pytest.approx(0.139038, abs=ABS_TOL)


def test_delta_call_dte30_gold():
    assert _greeks(30)["delta"] == pytest.approx(0.52354642, abs=ABS_TOL)


def test_gamma_dte30_gold():
    assert _greeks(30)["gamma"] == pytest.approx(0.05440661, abs=ABS_TOL)


def test_vega_per_1vol_dte30_gold():
    assert _greeks(30)["vega"] == pytest.approx(11.41739232, abs=ABS_TOL)


def test_theta_call_per_day_dte30_gold():
    assert _greeks(30)["theta"] == pytest.approx(-0.05128936, abs=ABS_TOL)


# --- supporting checks ----------------------------------------------------- #

def test_result_shape():
    result = _greeks(30)
    assert set(result) == EXPECTED_KEYS
    assert all(isinstance(v, float) for v in result.values())
    assert not any(v is None for v in result.values())


@pytest.mark.parametrize("override", [
    {"option_type": "foo"},
    {"strike": 0.0},
    {"strike": -5.0},
    {"strike": None},
    {"underlying_price": 0.0},
    {"underlying_price": -1.0},
    {"price": 0.0},
    {"price": -1.0},
    {"days_to_expiration": 0},
    {"days_to_expiration": -5},
])
def test_invalid_input_all_none(override):
    """Invalid input yields the all-None contract - it must not raise."""
    args = dict(option_type="call", strike=STRIKE, underlying_price=SPOT,
                days_to_expiration=30, price=PRICE, dividend=0.0)
    args.update(override)
    result = get_option_greeks(**args)
    assert set(result) == EXPECTED_KEYS
    assert all(v is None for v in result.values())


def test_boundary_dte1_no_nan():
    result = _greeks(1)
    assert all(math.isfinite(v) for v in result.values())


def test_raw_scale_not_percent():
    """Raw values, not the x100 convention OptionQuote applies."""
    result = _greeks(30)
    assert result["iv"] == pytest.approx(0.255321, abs=ABS_TOL)
    assert result["iv"] < 1.0
    assert result["theta"] == pytest.approx(-0.05128936, abs=ABS_TOL)
    assert -1.0 < result["theta"] < 0.0
