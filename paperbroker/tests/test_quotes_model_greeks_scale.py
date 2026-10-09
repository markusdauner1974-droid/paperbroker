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


# --- delta: the last factor of the scale series (added in slice 4) ----------
#
# ``delta`` is stored x100 in both paths: the adapter scales the CBOE raw value
# with ``scale=100.0`` and the model path multiplies the module value by 100.
# The web UI formats it with ``'%.0f'`` and the /api/screen payload carries it,
# so the 0-100 (percent point) convention is the one the surface expects.
#
# The module returns delta as a fraction, so the factor 100 is correct here -
# unlike vega and rho, which the module already returns per 1.00 unit.
#
# The tests below pin the RELATION to the module value, not a bare literal: a
# literal copied from the running code cannot tell right from wrong.

CALL_DELTA_RAW = 0.5987589102724356
PUT_DELTA_RAW = -0.4647431650488514


def test_the_module_still_returns_delta_as_a_fraction():
    """Pin the source of truth every x100 assertion below is derived from.

    If this fails, the module's reference scale moved and the tolerances in
    this section are meaningless.
    """
    raw = get_option_greeks("call", CALL_STRIKE, SPOT, CALL_DTE, CALL_PRICE,
                            dividend=0.0)
    assert raw["delta"] == pytest.approx(CALL_DELTA_RAW, abs=1e-9)
    raw_put = get_option_greeks("put", PUT_STRIKE, SPOT, PUT_DTE, PUT_PRICE,
                                dividend=0.0)
    assert raw_put["delta"] == pytest.approx(PUT_DELTA_RAW, abs=1e-9)


def test_model_path_stores_call_delta_on_the_x100_scale():
    """Dropping the factor 100 would store 0.5987589102724356 instead.

    The magnitude bound alone already catches that: it fails for every value
    <= 1, independently of the exact raw delta.
    """
    q = _model_quote(CALL_SYMBOL, CALL_PRICE)
    assert q.delta == pytest.approx(CALL_DELTA_RAW * 100, abs=1e-9)
    assert 1 < abs(q.delta) <= 100


def test_model_path_stores_put_delta_on_the_x100_scale_and_keeps_the_sign():
    """A put carries a negative delta; the x100 factor must not drop the sign.

    Note the live chain does contain puts with delta exactly 0.000, so the
    assertion is ``< 0`` only for this fixture, never ``<= 0`` as an identity.
    """
    q = _model_quote(PUT_SYMBOL, PUT_PRICE)
    assert q.delta == pytest.approx(PUT_DELTA_RAW * 100, abs=1e-9)
    assert q.delta < 0.0


def test_delta_carries_the_factor_vega_and_rho_do_not():
    """The asymmetry is the point of this file: delta x100, vega/rho x1.

    A future "unify the scale" change that adds the factor to vega/rho, or
    removes it from delta, fails here in one place.
    """
    q = _model_quote(CALL_SYMBOL, CALL_PRICE)
    raw = get_option_greeks("call", CALL_STRIKE, SPOT, CALL_DTE, CALL_PRICE,
                            dividend=0.0)
    assert q.delta == pytest.approx(raw["delta"] * 100, abs=1e-9)
    assert q.vega == pytest.approx(raw["vega"], abs=1e-9)
    assert q.rho == pytest.approx(raw["rho"], abs=1e-9)


@pytest.mark.parametrize("model_delta", [None, float("nan")])
def test_delta_falls_back_to_the_supplied_parameter(monkeypatch, model_delta):
    """The other half of the line: a missing model delta keeps the caller's.

    ``monkeypatch`` replaces the imported symbol, so the guard
    ``is not None and not math.isnan(...)`` is exercised directly.
    """
    import paperbroker.quotes as quotes_module

    monkeypatch.setattr(quotes_module, "get_option_greeks", lambda *a, **k: {
        "delta": model_delta, "iv": 0.2, "gamma": 0.01,
        "vega": 1.0, "theta": -0.1, "rho": 0.5,
    })
    q = _model_quote(CALL_SYMBOL, CALL_PRICE, delta=42.0)
    assert q.delta == 42.0


# --- gamma and iv: the last two fields of the model-path scale series ---------
#
# The model path stores delta, gamma, iv and theta x100, and vega and rho x1.
# After slice 3 and slice 4 the file pinned delta, vega, rho and theta. gamma and
# iv stayed unpinned: two mutants (gamma x100x100, iv without the factor) ran
# through all 158 tests with rc=0. The assertions below close that gap.
#
# gamma has no consumer in the codebase - the word appears only in a comment in
# screener.py. Its pin is regression protection, not a user-facing fix. iv is
# read in four places (screener iv=0 gate, iv_min/iv_max band, scoring, reasons).
#
# Values measured 2026-10-09 against master 7c2b24d.

CALL_GAMMA_GOLD = 1.2385527516986996
CALL_IV_GOLD = 26.709501566323933
PUT_GAMMA_GOLD = 1.3429366744652824
PUT_IV_GOLD = 25.316917091381995


def test_model_path_stores_call_gamma_on_the_x100_scale():
    """Dropping or doubling the factor 100 stores 0.012385527516986995 or 123.85527516986996."""
    q = _model_quote(CALL_SYMBOL, CALL_PRICE)
    assert q.gamma == pytest.approx(CALL_GAMMA_GOLD, rel=1e-9)
    assert 1 < q.gamma <= 100


def test_model_path_stores_call_iv_on_the_x100_scale():
    """The factor 100 is what turns the module's 0.26709501566323934 into percent points."""
    q = _model_quote(CALL_SYMBOL, CALL_PRICE)
    assert q.iv == pytest.approx(CALL_IV_GOLD, rel=1e-9)
    assert 1 < q.iv < 100


def test_model_path_stores_put_gamma_and_iv_on_the_x100_scale():
    """The put carries its own sigma, so both values are pinned from its own price.

    gamma is put/call symmetric for a shared sigma; this put has a different one.
    """
    q = _model_quote(PUT_SYMBOL, PUT_PRICE)
    assert q.gamma == pytest.approx(PUT_GAMMA_GOLD, rel=1e-9)
    assert q.iv == pytest.approx(PUT_IV_GOLD, rel=1e-9)
    assert q.gamma > 0.0
    assert q.iv > 0.0


def test_gamma_and_iv_fall_back_to_the_supplied_parameters():
    """The other half of both lines: the fallback branch must not scale at all.

    ``underlying_price=None`` makes the model branch unreachable, so the caller's
    values pass through untouched. The values are deliberately ragged: a mutant
    that rounds or truncates in the else branch (round(g, 2), int(g)) still passes
    the round values 7.0 / 33.0, but not these.
    """
    q = _model_quote(CALL_SYMBOL, CALL_PRICE, underlying_price=None,
                     gamma=7.123456789012345, iv=33.987654321)
    assert q.gamma == 7.123456789012345
    assert q.iv == 33.987654321
