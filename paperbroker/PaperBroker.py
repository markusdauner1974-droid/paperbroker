"""
    PaperBroker (slimmed)

    Historical note: this class was the facade of a full simulated
    brokerage (accounts, orders, fills, margin). The project now uses it
    as a read-only market-data facade: quotes, option chains and
    expiration dates via a pluggable QuoteAdapter (CBOE adapter in
    Phase 3). All order/account/market logic was removed in Phase 2 -
    see git history for the original implementation.

    Instantiate with an explicit adapter:
        from paperbroker import PaperBroker
        broker = PaperBroker(quote_adapter=MyQuoteAdapter())
"""
from .adapters.quotes import QuoteAdapter


class PaperBroker:

    def __init__(self, quote_adapter: QuoteAdapter = None):
        # No default live quote adapter: always pass one explicitly.
        self.quote_adapter = quote_adapter

    def get_price(self, asset):
        quote = self.get_quote(asset)
        return quote.price if quote is not None else None

    def get_quote(self, asset):
        return self.quote_adapter.get_quote(asset)

    def get_options(self, underlying_asset=None, expiration_date=None):
        return self.quote_adapter.get_options(underlying_asset, expiration_date)

    def get_option_quotes(self, underlying_asset=None, expiration_date=None):
        return self.get_options(underlying_asset=underlying_asset, expiration_date=expiration_date)

    def get_expiration_dates(self, underlying_asset=None):
        return self.quote_adapter.get_expiration_dates(underlying_asset)
