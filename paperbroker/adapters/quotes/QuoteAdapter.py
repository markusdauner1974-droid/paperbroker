
class QuoteAdapter:

    def get_quote(self, asset):
        raise NotImplementedError("QuoteAdapter.get_quote: You should subclass this and create an adapter.")

    def get_options(self, underlying_asset=None, expiration_date=None):
        raise NotImplementedError("QuoteAdapter.get_options: You should subclass this and create an adapter.")

    def get_option_chain_snapshot(self, underlying_symbol):
        raise NotImplementedError("QuoteAdapter.get_option_chain_snapshot: You should subclass this and create an adapter.")
    def get_expiration_dates(self, underlying_asset=None):
        raise NotImplementedError("QuoteAdapter.get_expiration_dates: You should subclass this and create an adapter.")

