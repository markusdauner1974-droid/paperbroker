"""

    Specialized classes for assets. Anything that can be represented by a symbol.
    Overrides the == to make it easier
    Use asset_factory() if you don't know if an object is a string or an asset
    Logic within the asset classes is kept to a minimum to make it easier
      to learn from the code. Most is in /paperbroker/logic/

"""
import re

import arrow

_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # leap-day-safe upper bound


def parse_occ(symbol):
    """OCC symbol -> (underlying, 'YYYY-MM-DD', 'call'/'put', strike) or None.

    Adjusted/mini contracts and other non-standard symbols return None;
    the caller decides whether that is an error or a skip. The month/day
    check uses a leap-day-safe upper bound table (29 for February), so
    29 February is accepted in every year - the same semantics as the
    previous CBOEQuoteAdapter._parse_occ.
    """
    m = _OCC.match(symbol)
    if not m:
        return None
    underlying, yymmdd, cp, strike8 = m.groups()
    # fast string decode of YYMMDD - real-calendar month/day table
    # (no arrow in the hot path: this runs per contract)
    mm, dd = int(yymmdd[2:4]), int(yymmdd[4:6])
    if not (1 <= mm <= 12) or not (1 <= dd <= _DAYS_IN_MONTH[mm - 1]):
        return None  # impossible dates like 21-18 or 30 February
    expiration = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return underlying, expiration, "call" if cp == "C" else "put", int(strike8) / 1000.0


def asset_factory(symbol=None):
    """
        Create the appropriate asset based on the symbol.
    :param symbol: Case-insensitive symbol for the asset being created
    :return: An object that's a subclass of Asset or None
    :raises ValueError: when the symbol is neither a short ticker nor a
        valid OCC option symbol (e.g. an adjusted contract with a
        non-C/P indicator). Previously such symbols were silently built
        as a Put via a substring test, or failed with an unrelated
        ValueError from float()/arrow.
    """

    if symbol is None:
        return None

    if isinstance(symbol, Asset):
        return symbol

    symbol = symbol.upper()

    if len(symbol) <= 8:
        return Asset(symbol)

    parsed = parse_occ(symbol)
    if parsed is None:
        raise ValueError(f"invalid OCC symbol: {symbol!r}")

    return Call(symbol) if parsed[2] == 'call' else Put(symbol)

"""
Asset: Assets are always identified by a symbol which uniquely identifies the asset and a type.
"""
class Asset:

    def __init__(self, symbol: str=None, asset_type: str=None):
        self.symbol = symbol.upper()
        self.asset_type = asset_type or 'asset'
        return

    def __eq__(self, other):
        """Override the default Equals behavior"""
        if isinstance(other, self.__class__):
            return self.symbol == other.symbol
        if isinstance(other, str):
            return self.symbol == other.upper()
        return False

    def __ne__(self, other):
        """Define a non-equality test"""
        return not self.__eq__(other)



"""
    Base class for any option derivative
"""
def calendar_days_between(expiration_date, as_of_date):
    # calendar-day difference (date-only): expiration day minus as-of day.
    # Time-of-day must not leak into this: with a timestamped as_of
    # ('2026-10-07 10:45' OR '2026-10-07T10:45:00') raw .days returned
    # -1 ON expiration day and 0 the day before. Parse first, compare
    # .date() so both space- and T-separated inputs behave identically.
    as_of = arrow.get(str(as_of_date)).date()
    return (arrow.get(expiration_date).date() - as_of).days


class Option(Asset):

    def __init__(self, symbol:str = None, underlying=None, option_type:str = None, strike:float = None, expiration_date = None):

        if symbol is not None:

            # if a symbol is provided, then we create the asset based on the symbol

            r = symbol[::-1]

            self.strike = float(r[0:8][::-1]) / 1000
            self.option_type = 'call' if r[8] == 'C' else 'put'
            self.expiration_date = arrow.get(r[9:15][::-1], 'YYMMDD').format('YYYY-MM-DD')
            # build the underlying directly instead of handing the root back
            # to asset_factory: that function rejects non-OCC strings longer
            # than 8 characters, so any valid OCC symbol with a root of 9+
            # characters (e.g. ABCDEFGHI261218C00250000) failed here. The
            # root is already parsed, and upper() matches the normalisation
            # the ticker branch applies.
            self.underlying = Asset(r[15:][::-1].upper())

        else:

            # if not then we piece it together with the data we have

            underlying = asset_factory(underlying)

            if underlying is None:
                raise Exception('Option(Asset): An underlying is required')

            if option_type is None or option_type not in ['call', 'put']:
                raise Exception('Option(Asset): option_type is required and must be `call` or `put`')

            if strike is None or strike <= 0.0:
                raise Exception('Option(Asset): strike is required and must be > 0.0')

            if expiration_date is None:
                raise Exception('Option(Asset): expiration_date is required')

            # parse the date real quick to check on it
            try:
                expiration_date = arrow.get(expiration_date).format('YYMMDD')
            except Exception as err:
                raise Exception('Option(Asset): expiration_date is invalid') from err

            # build the symbol
            symbol = (underlying.symbol + expiration_date + option_type[0] + str(int(round(strike, 2) * 1000)).zfill(8)).upper()

            self.underlying = underlying
            self.option_type = option_type
            self.strike = float(strike)
            self.expiration_date = arrow.get(expiration_date, 'YYMMDD').format('YYYY-MM-DD')

        super().__init__(symbol, self.option_type)

    def get_extrinsic_value(self, underlying_price=None, price=None):
        intrinsic = self.get_intrinsic_value(underlying_price=underlying_price)
        return (abs(price) - intrinsic) if price is not None and intrinsic is not None else None

    def get_intrinsic_value(self, underlying_price=None):

        if self.strike is None:
            return None

        if underlying_price is None:
            return None

        if self.option_type == 'call':
            return max(underlying_price - self.strike, 0)
        if self.option_type == 'put':
            return max(self.strike - underlying_price, 0)

        return None

    def get_days_to_expiration(self, as_of_date):
        return calendar_days_between(self.expiration_date, as_of_date)


class Put(Option):
    def __init__(self, symbol: str = None, underlying = None,
                 underlying_symbol: str = None, strike: float = None, expiration_date=None):
        super().__init__(symbol=symbol, option_type='put', underlying = underlying, strike=strike, expiration_date=expiration_date)

class Call(Option):
    def __init__(self, symbol: str = None, underlying = None,
                 underlying_symbol: str = None, strike: float = None, expiration_date=None):
        super().__init__(symbol=symbol, option_type='call', underlying = underlying, strike=strike, expiration_date=expiration_date)

