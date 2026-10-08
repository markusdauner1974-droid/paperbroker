"""Contract tests for asset_factory() and the shared OCC parser.

The module had no coverage for the factory itself, so these tests pin:

- the class chosen for OCC symbols (Call/Put), including strikes >= 10000
  where the old substring test ('P0'/'C0') failed and returned the Option
  base class,
- the defined error contract for symbols that are not valid OCC symbols,
- the three early-exit guards (None, Asset passthrough, len <= 8 ticker),
- that parse_occ keeps the leap-day-safe month/day table semantics.

Symbol table is frozen: every entry was verified against the adapter's
behaviour before the OCC logic moved. Six of them are also recorded in
docs/debate/b2_parse_occ_frozen_before_c1.json (1727 symbols); the other
six were checked directly against the pre-move regex and day table.
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
    # the strike is *1000 and zero-filled to 8 digits, so the leading zeros
    # disappear at 10000: 'C0' is present in 'SPX261218C00250000' but not in
    # 'SPX261218C10000000', which is why the old substring (or 'P0') test
    # fell through to the Option base class from here up
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
# D. QA follow-up (Phase 7): root length and digit-root coverage
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("symbol", ["ABCDEFG260116C00250000", "SPXW261016C00250000"])
def test_long_and_suffixed_root_accepted(symbol):
    # pins that the root carries no length limit AND no digit restriction:
    # the pattern is [A-Z]+, so 7 letters (ABCDEFG) and the SPXW weekly
    # suffix parse. The 1-6 character rule belongs to the web ticker field,
    # not here; the Judge proposal to tighten the root to [A-Z]{1,6} would
    # break both cases.
    assert type(asset_factory(symbol)) is Call


@pytest.mark.parametrize("symbol", [
    "AAPL1261120C00330000",   # digit inside the root
    "BRK1261016C00250000",
    "AAPL2261016C00250000",
])
def test_digit_root_is_not_an_occ_symbol(symbol):
    # digit roots are not OCC option symbols. The old substring factory
    # happily built a Call for them (AAPL1261120C00330000 -> Call K=330.0,
    # underlying 'AAPL1'), i.e. it invented an underlying from a number.
    with pytest.raises(ValueError, match=INVALID):
        asset_factory(symbol)


def test_strike_zero_kept():
    # strike 0 passes the OCC pattern and both the parser and the factory
    # accept it (unchanged from before); pinned so a future tightening is
    # a deliberate decision, not an accident
    a = asset_factory("AAPL260116C00000000")
    assert type(a) is Call
    assert a.strike == 0.0


def test_short_symbol_nine_chars_raises():
    assert asset_factory("GERTHAN8") is not None  # 8 chars -> ticker
    with pytest.raises(ValueError, match=INVALID):
        asset_factory("AAPL2601C")


def test_feb28_accepted():
    a = asset_factory("AAPL260228P00150000")
    assert type(a) is Put
    assert a.strike == 150.0


def test_lowercase_occ_full_symbol():
    # lowercase is normalised before the parse, so a lowercase OCC string
    # becomes a real option - not a ticker
    a = asset_factory("aapl260120c00100000")
    assert type(a) is Call
    assert a.underlying is not None
    assert a.underlying.symbol == "AAPL"
    assert a.strike == 100.0


def test_spx_put_strike_4500():
    # SPX strike 4500 -> the root "SPX" must not be eaten by the strike.
    # The Judge case "SPX260219P45000000" is NOT 4500.0: 8 digits are read
    # as int(k)/1000.0, so that string is 45000.0. Measured, not assumed.
    a = asset_factory("SPX260219P04500000")
    assert type(a) is Put
    assert a.underlying is not None
    assert a.underlying.symbol == "SPX"
    assert a.strike == 4500.0


def test_spx_strike_45000_from_eight_digits():
    # the companion pin: same string without the leading zero is 45000.0.
    # Both are one contract apart in strike, not a factor of ten apart in
    # intent - the eight digits are the raw strike, not a shifted one.
    a = asset_factory("SPX260219P45000000")
    assert type(a) is Put
    assert a.strike == 45000.0


def test_screener_top_result_symbol():
    # the live screener delivered AAPL261120C00330000 with a top-ranked
    # score; pinned so a delivered symbol stays a Call. (The Phase 6 note
    # claiming this exact symbol ranked first at score 0.8567 was wrong -
    # measured top is AAPL261120C00345000 at 0.8217, and the ranking is
    # data- and time-dependent. The pin below is about the class, which is
    # what the factory decides.)
    a = asset_factory("AAPL261120C00330000")
    assert type(a) is Call
    assert a.underlying is not None
    assert a.underlying.symbol == "AAPL"
    assert a.strike == 330.0
    assert str(a.expiration_date) == "2026-11-20"


# --------------------------------------------------------------------- #
# E. long roots (Phase 10): parse_occ accepts them, Option.__init__ must
#    not hand the root back to the strict asset_factory check
# --------------------------------------------------------------------- #

def test_long_root_call_accepted():
    # a 9-character root is a valid OCC symbol. Before this fix it raised
    # "invalid OCC symbol: 'AAAAAAAAA'" from inside Option.__init__.
    a = asset_factory("AAAAAAAAA261218C00250000")
    assert type(a) is Call
    assert a.underlying is not None
    assert a.underlying.symbol == "AAAAAAAAA"
    assert a.strike == 250.0
    assert str(a.expiration_date) == "2026-12-18"


def test_long_root_put_accepted():
    a = asset_factory("ABCDEFGHI261218P00250000")
    assert type(a) is Put
    assert a.underlying is not None
    assert a.underlying.symbol == "ABCDEFGHI"
    assert a.strike == 250.0


def test_long_root_strike_zero_kept():
    # the strike-0 contract must survive the long-root path too
    a = asset_factory("AAAAAAAAA260116C00000000")
    assert type(a) is Call
    assert a.strike == 0.0


def test_long_root_direct_construction():
    # the class itself, not only the factory, must accept a long root
    a = Call("ABCDEFGHI261218C00250000")
    assert a.strike == 250.0
    assert a.underlying is not None
    assert a.underlying.symbol == "ABCDEFGHI"


def test_long_root_parse_occ_tuple():
    # documents the boundary: parse_occ always accepted these; only the
    # construction path disagreed with it
    assert parse_occ("ABCDEFGHI261218C00250000") == (
        "ABCDEFGHI", "2026-12-18", "call", 250.0)


@pytest.mark.parametrize("symbol", [
    "AAAAAAAAA261218C00250000",   # 9
    "AAAAAAAAAA261218C00250000",  # 10
    "AAAAAAAAAAAA261218C00250000",  # 12
])
def test_long_root_length_sweep(symbol):
    a = asset_factory(symbol)
    assert type(a) is Call
    assert a.underlying is not None
    assert a.underlying.symbol == symbol[:len(symbol) - 15]


def test_eight_char_root_still_accepted():
    # the inclusive boundary below the divergence: 8 characters already
    # worked through the ticker branch and must keep working
    a = asset_factory("AAAAAAAA261218C00250000")
    assert type(a) is Call
    assert a.underlying is not None
    assert a.underlying.symbol == "AAAAAAAA"


def test_long_root_lowercase_normalised():
    # the construction path must normalise the root the same way the
    # ticker branch does, otherwise the underlying comes out lowercase
    a = asset_factory("abcdefghi261218c00250000")
    assert type(a) is Call
    assert a.underlying is not None
    assert a.underlying.symbol == "ABCDEFGHI"


# --------------------------------------------------------------------- #
# F. move guard - the single OCC implementation stays in sync
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
