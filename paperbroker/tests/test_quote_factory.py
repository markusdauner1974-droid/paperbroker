"""Pass-through contract of ``quote_factory``.

``quote_factory`` accepts an ``underlying_price`` and hands it to ``OptionQuote``.
The parameter exists since upstream commit ``95085659`` (2017-07-17), whose message
says the spot is to be stored in the option quote - but the factory body passed a
hard ``None``. The model branch was therefore unreachable through the factory: the
spot, all six greeks and both value getters stayed ``None``, and
``get_extrinsic_value()`` raised ``TypeError``.

Fixture: the real CBOE contract ``AAPL261120C00335000`` (delayed feed,
2026-10-08 20:26 UTC, AAPL spot 340.5301, dte 43), prices are the quoted mid points.
``bid_size``/``ask_size`` are deliberately non-default and crooked (13/27): with the
default 0 a dropped keyword argument inside the reformatted call would stay
invisible.

No exact greek values are pinned - they move with any model change. The contract is
"the spot arrives" plus "the model branch is reached", plus field equivalence with
the direct constructor.
"""
import pytest

from paperbroker.quotes import OptionQuote, Quote, quote_factory

QUOTE_DATE = "2026-10-08 20:26:30"
OPTION = "AAPL261120C00335000"
SPOT = 340.5301
PRICE = 15.75
BID = 15.60
ASK = 15.90
BID_SIZE = 13
ASK_SIZE = 27
STRIKE = 335.0
INTRINSIC = 5.5301
EXTRINSIC = 10.2199

ARGS = dict(quote_date=QUOTE_DATE, asset=OPTION, price=PRICE, bid=BID, ask=ASK,
            bid_size=BID_SIZE, ask_size=ASK_SIZE)

# every field the factory is supposed to forward or compute
FIELDS = ("quote_type", "bid", "ask", "bid_size", "ask_size", "price",
          "underlying_price", "days_to_expiration", "greeks_source",
          "delta", "iv", "gamma", "vega", "theta", "rho")

GREEKS = ("delta", "iv", "gamma", "vega", "theta", "rho")


@pytest.mark.xfail(strict=True, reason='B-3: quote_factory drops underlying_price (master)')
def test_factory_passes_the_underlying_price_through():
    quote = quote_factory(underlying_price=SPOT, **ARGS)
    assert quote.underlying_price == SPOT
    # 0.0 is falsy: a gate like ``underlying_price or None`` looks identical at SPOT
    # but not here - the boundary of the model/fallback branch sits exactly at this value
    zero = quote_factory(underlying_price=0.0, **ARGS)
    assert zero.underlying_price == 0.0


@pytest.mark.xfail(strict=True, reason='B-3: quote_factory drops underlying_price (master)')
def test_factory_is_field_equivalent_to_the_direct_constructor():
    from_factory = quote_factory(underlying_price=SPOT, **ARGS)
    from_constructor = OptionQuote(underlying_price=SPOT, **ARGS)
    for field in FIELDS:
        assert getattr(from_factory, field) == getattr(from_constructor, field), field


@pytest.mark.xfail(strict=True, reason='B-3: quote_factory drops underlying_price (master)')
def test_intrinsic_value_uses_the_forwarded_spot():
    quote = quote_factory(underlying_price=SPOT, **ARGS)
    assert quote.get_intrinsic_value() == pytest.approx(SPOT - STRIKE, abs=1e-6)
    assert quote.get_intrinsic_value() == pytest.approx(INTRINSIC, abs=1e-6)


@pytest.mark.xfail(strict=True, reason='B-3: quote_factory drops underlying_price (master)')
def test_extrinsic_value_no_longer_raises():
    quote = quote_factory(underlying_price=SPOT, **ARGS)
    assert quote.get_extrinsic_value() == pytest.approx(EXTRINSIC, abs=1e-6)


@pytest.mark.xfail(strict=True, reason='B-3: quote_factory drops underlying_price (master)')
def test_model_branch_fills_all_six_greeks():
    quote = quote_factory(underlying_price=SPOT, **ARGS)
    assert quote.greeks_source == "model"
    for greek in GREEKS:
        assert getattr(quote, greek) is not None, greek


def test_without_a_spot_the_fallback_is_unchanged():
    """Regression pin: no spot in, no spot out - before and after the fix."""
    quote = quote_factory(**ARGS)
    assert quote.underlying_price is None
    assert quote.delta is None
    assert quote.iv is None


def test_factory_returns_a_plain_quote_for_an_equity():
    quote = quote_factory(quote_date=QUOTE_DATE, asset="AAPL", price=250.0)
    assert type(quote) is Quote


def test_adapter_path_is_untouched_by_the_factory():
    """Regression pin: the delivered CBOE path keeps its own greeks."""
    quote = OptionQuote(quote_date=QUOTE_DATE, asset=OPTION, price=PRICE, bid=BID,
                        ask=ASK, bid_size=BID_SIZE, ask_size=ASK_SIZE,
                        underlying_price=SPOT, delta=53.79, iv=25.56, gamma=1.36,
                        vega=45.21, theta=-13.68, rho=19.78,
                        greeks_source="adapter")
    assert quote.greeks_source == "adapter"
    assert quote.delta == 53.79
    assert quote.iv == 25.56
