# paperbroker (slimmed fork)

Options **market-data core** for a private screener. Forked from
[philipodonnell/paperbroker](https://github.com/philipodonnell/paperbroker)
and re-purposed: the original brokerage simulation (orders, accounts,
positions, margin, fills) was removed in Phase 2; this fork now feeds a
private options screener (QNAP, not published).

## What this fork provides

- `paperbroker/assets.py` – OCC option symbol parsing
  (`AAPL261007C00245000` → underlying / expiration / call-put / strike)
- `paperbroker/quotes.py` – `Quote` / `OptionQuote` data classes
- `paperbroker/logic/bs_model.py` – pure-Python Black-Scholes greeks
- `paperbroker/logic/ivolat3_option_greeks.py` – greeks entry point
- `paperbroker/adapters/quotes/CBOEQuoteAdapter.py` – adapter for the
  keyless CBOE delayed-quotes feed
- `PaperBroker` facade – thin, read-only market-data API:
  `get_price`, `get_quote`, `get_options`, `get_option_quotes`,
  `get_expiration_dates` (always with an explicit quote adapter)

## Adapter conventions

- IV and greeks are stored as percentages (×100). CBOE `iv` comes decimal
  (0.25 → 25), CBOE greeks come raw (delta 0.45 → 45) - verified against a
  live AAPL chain (3568 contracts, 2026-10-07).
- `0` means zero (legitimately illiquid contract), `None` means missing.
  Both are preserved; consumers must treat them differently.
- The CBOE feed timestamp is exposed as `quote_timestamp` on adapter quotes.
- Error contract: `CboeNotFoundError` (unknown symbol / HTTP 403 / contract
  not in chain), `CboeRequestError` (network failure / malformed response),
  `ValueError` (unparseable expiration date).
- Chains are cached per underlying (LRU 64 underlyings, 900 s TTL, refreshed
  on feed timestamp change); one HTTP fetch serves expirations, full chain
  and single-symbol lookups.

## Usage

```python
from paperbroker import PaperBroker
from paperbroker.adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter

broker = PaperBroker(quote_adapter=CBOEQuoteAdapter())

prices = broker.get_expiration_dates('AAPL')     # ['2026-10-07', ...]
quotes = broker.get_options('AAPL', prices[0])   # OptionQuote list for that date
liquid = [q for q in quotes
          if (q.open_interest or 0) >= 500 and q.ask and q.bid
          and (q.ask - q.bid) / q.ask < 0.05]
stock = broker.get_quote('AAPL')                 # underlying price quote
```

The original 2017 test data (`TestDataQuoteAdapter`) is still available and
used by the unit tests.

## Testing

    python -m pytest paperbroker/tests

CI runs pytest on Python 3.13 for every PR (`Test` workflow).

## Development workflow (this fork)

- Work on feature branches; open a PR per change set.
- CI must pass; **all** CodeRabbit findings must be addressed (docs changes
  count too); larger designs get a debate-panel review before implementation.
- **PR titles, bodies and comments are written in English.**
- Merge only after CI green + all bot findings resolved + owner approval.
- Dependencies kept minimal (`arrow`, `requests`); no simulation code
  reintroduced - history carries everything that was removed.