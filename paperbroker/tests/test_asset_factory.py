"""Contract tests for asset_factory() and the shared OCC parser.

The module had no coverage for the factory itself, so these tests pin:

- the class chosen for OCC symbols (Call/Put), including strikes >= 10000
  where the old substring test ('P0'/'C0') failed and returned the Option
  base class,
- the defined error contract for symbols that are not valid OCC symbols,
- the three early-exit guards (None, Asset passthrough, len <= 8 ticker),
- that parse_occ keeps the leap-day-safe month/day table semantics.

Symbol table is frozen: it was captured from the adapter's behaviour
before the OCC logic moved, so it doubles as a move-regression guard.
"""
import pytest

from paperbroker.adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter
from paperbroker.assets import Asset, Call, Put, asset_factory, parse_occ

INVALID = "invalid OCC symbol"

# frozen expectations: (symbol, expected parse_occ result)
FROZEN_OCC_SYMBOLS = [
    ("AAPL261016C00250000", ("AAPL", "2026-10-16", "call", 250.0)),
    ("AAPL261016P00250000", ("AAPL", "2026-10-16", "put", 250.0)),
    ("SPX261218C12000000", ("SPX", "2026-12-18", "call", 12000.0)),
    ("SPX261218P12000000", ("SPX", "2026-12-18", "put", 12000.0)),
    ("SPX261218C10000000", ("SPX", "2026-12-18", "call", 10000.0)),
    ("SPX261218C09999999", ("SPX", "2026-12-18", "call", 9999.999)),
    ("AAPL261218C00125500", ("AAPL", "2026-12-18", "call", 125.5)),
    ("AAPL240229C00245000", ("AAPL", "2024-02-29", "call", 245.0)),
    ("AAPL261007X00125000", None),
    ("AAPL000000C00245000", None),
    ("FAKE260230C00100000", None),
    ("WEIRD", None),
]


# --------------------------------------------------------------------- #
# A. defect tests - these must FAIL against the old substring factory
# --------------------------------------------------------------------- #

def test_call_strike_12000():
    a = asset_factory("SPX261218C12000000")
    assert type(a) is Call
    assert a.strike == 12000.0


def test_put_strike_12000():
    a = asset_factory("SPX261218P12000000")
    assert type(a) is Put
    assert a.strike == 12000.0


def test_strike_boundary_at_10000():
    # strike*1000 zfills to 8 digits; the old 'Z0' substring matched only
    # at index 0, i.e. for strikes below 10000
    a = asset_factory("SPX261218C10000000")
    assert type(a) is Call
    assert a.strike == 10000.0


def test_invalid_cp_char_raises():
    # old code produced Option with option_type guessed as 'put'
    with pytest.raises(ValueError, match=INVALID):
        asset_factory("AAPL261007X00125000")


@pytest.mark.parametrize("symbol", ["NONOCC123456", "LONGERTHAN8", "ABCDEFGHI", "AAPL2602C"])
def test_non_occ_symbol_raises(symbol):
    # these already raised ValueError before, but from float() - the
    # match= makes the test meaningful
    with pytest.raises(ValueError, match=INVALID):
        asset_factory(symbol)


@pytest.mark.parametrize("symbol", ["AAPL000000C00245000", "FAKE260230C00100000"])
def test_impossible_date_raises(symbol):
    # before: ValueError from arrow inside Option.__init__
    with pytest.raises(ValueError, match=INVALID):
        asset_factory(symbol)


# --------------------------------------------------------------------- #
# B. guards and boundaries - pins, green before and after
# --------------------------------------------------------------------- #

def test_none_passthrough():
    assert asset_factory(None) is None


def test_asset_identity_passthrough():
    original = Asset("AAL")
    assert asset_factory(original) is original


@pytest.mark.parametrize("symbol", ["AAL", "AAPL", "SPX"])
def test_ticker_stays_asset(symbol):
    assert type(asset_factory(symbol)) is Asset


def test_lowercase_normalised():
    a = asset_factory("aapl")
    assert type(a) is Asset
    assert a.symbol == "AAPL"


@pytest.mark.parametrize("symbol", ["ABCDEFGH", "AAPL2602"])
def test_len_boundary_8(symbol):
    assert type(asset_factory(symbol)) is Asset


def test_parse_occ_accepts_feb29_leap_safe():
    # pins the leap-day-safe upper bound (29 for February): the parser
    # must not be narrowed by a %4 rule
    assert parse_occ("AAPL260229C00245000") == ("AAPL", "2026-02-29", "call", 245.0)


def test_feb29_non_leap_raises_in_arrow():
    # documented divergence: parse_occ accepts the string date, but
    # Option.__init__ parses it with arrow and 2026 is not a leap year.
    # No match= - the arrow message is not an API.
    with pytest.raises(ValueError):
        asset_factory("AAPL260229C00245000")


# --------------------------------------------------------------------- #
# C. regression and contract pins
# --------------------------------------------------------------------- #

def test_strike_below_boundary():
    a = asset_factory("SPX261218C09999999")
    assert type(a) is Call
    assert a.strike == pytest.approx(9999.999)


def test_valid_occ_regression():
    assert type(asset_factory("AAPL261016C00250000")) is Call
    assert asset_factory("AAPL261016C00250000").strike == 250.0
    assert type(asset_factory("AAPL261016P00250000")) is Put
    assert asset_factory("AAPL261016P00250000").strike == 250.0


def test_leap_year_accepted():
    a = asset_factory("AAPL240229C00245000")
    assert type(a) is Call
    assert a.strike == 245.0


def test_decimal_strike():
    a = asset_factory("AAPL261218C00125500")
    assert type(a) is Call
    assert a.strike == 125.5


def test_adapter_rejects_invalid_symbol():
    assert CBOEQuoteAdapter._parse_occ("AAPL261007X00125000") is None


# --------------------------------------------------------------------- #
# D. move guard - the single OCC implementation stays in sync
# --------------------------------------------------------------------- #

def asset_factory_equivalent(symbol):
    """None for symbols the factory rejects, else the parsed tuple."""
    try:
        asset_factory(symbol)
    except ValueError:
        return None
    return parse_occ(symbol)


@pytest.mark.parametrize("symbol,expected", FROZEN_OCC_SYMBOLS)
def test_parse_occ_matches_frozen_table(symbol, expected):
    assert parse_occ(symbol) == expected
    assert asset_factory_equivalent(symbol) == expected
