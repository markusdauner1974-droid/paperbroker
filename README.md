# paperbroker (slimmed fork)

Options **screener** for a private deployment on a QNAP NAS. Forked from
[philipodonnell/paperbroker](https://github.com/philipodonnell/paperbroker) and re-purposed: the
original brokerage simulation (orders, accounts, positions, margin, fills) was removed in Phase 2 —
this fork feeds a private options screener.

**Current state of the project: see [PROJECT_STATUS.md](PROJECT_STATUS.md).** That file carries the
frozen baseline (merged commits, test count, CI state) and the list of open topics.

## What this fork provides

- `paperbroker/assets.py` — OCC option symbol parsing (`AAPL261007C00245000` →
  underlying / expiration / call-put / strike), intrinsic and extrinsic value.
- `paperbroker/quotes.py` — `Quote` / `OptionQuote` data classes.
- `paperbroker/logic/bs_model.py` — pure-Python Black-Scholes greeks.
- `paperbroker/logic/ivolat3_option_greeks.py` — greeks entry point.
- `paperbroker/adapters/quotes/CBOEQuoteAdapter.py` — adapter for the keyless CBOE delayed-quotes feed.
  One unusable contract in a chain does not take the chain down: the adapter drops it and counts it
  (`ChainResult.skipped`), so a single malformed row costs one contract instead of the whole request.
- `paperbroker/screener.py` — the filter pipeline and the scoring:
  - `ScreenerCriteria` — every field `None` disables that filter (`max_quote_age_min` defaults to 30).
  - `filter_data_complete` · `filter_stale` · `filter_spread` · `filter_oi` · `filter_volume` ·
    `filter_iv` · `filter_dte` and the size filters, applied in that order; a contract is reported
    with the **first** filter that rejects it.
  - `screen()` — orchestrator, scores candidates 0–1 (weights `spread 0.5 / oi 0.3 / iv 0.2`).
  - `vertical_spreads()` — vertical spreads off the same chain (net credit, breakeven, max loss,
    strike distance).
  - `TrendProvider` protocol (`TrendSignal` picks the call/put side only — never the IV direction).
- `paperbroker/server.py` + `paperbroker/templates/screen.html` — SSR (Jinja2) with one JSON endpoint
  and no frontend build chain. Endpoints:
  - `GET /` — form + results table, freshness lamp, readable errors, and the **drop reasons**
    (`stale 252 min · 67`, `oi=0 illiquid · 24`, …) so an empty window explains itself.
  - `GET /api/screen?ticker=&expiration=` — JSON with the keys `ticker, expiration, listed_dates,
    results, freshness, dropped_reasons, verticals, vertical_drops` and, when a chain was fetched,
    `chain_skipped` (contracts dropped inside the adapter). Each result carries `symbol, bid, ask,
    iv, open_interest, volume, delta, score, spread_pct, reasons`.
  - `GET /healthz` — liveness only, never calls CBOE or the trend service.
  Ticker and expiration are validated against a whitelist/regex and the real expiration list
  **before** the CBOE URL is touched (path-injection guard). Freshness lamp thresholds are a design
  decision (green < 30 min, yellow < 2 h, red ≥ 2 h); the CBOE feed delay is communicated as a static
  banner, not as a red state.
- `Dockerfile` + `docker-compose.yml` — QNAP deployment: `python:3.13-slim`, healthcheck against
  `/healthz`, named volume for the CBOE chain cache. **Publish only on the LAN IP** (host port 8090 —
  8089 is taken by xang1234); remote access goes through the QNAP reverse proxy.
- `PaperBroker` facade — thin, read-only market-data API: `get_price`, `get_quote`, `get_options`,
  `get_option_quotes`, `get_expiration_dates` (always with an explicit quote adapter).

## Installing and running

Two supported paths. They are **not** equivalent — the Docker path never had the packaging defect
described below, which is exactly why both are listed.

### Docker (QNAP / production)

```bash
docker build -t paperbroker-screener .
docker run -d --name paperbroker-screener -p 127.0.0.1:8090:8089 \
  -v cboe_cache:/app/.cache paperbroker-screener
# then open http://127.0.0.1:8090/?ticker=SPY&expiration=<date>
```

Or as a Portainer/Arcane stack: import `docker-compose.yml`, which reads `LAN_IP`, `HOST_PORT`,
`IMAGE`, `APP_PORT`, `FLASK_DEBUG` (keep `0`) and the optional trend variables below from the
environment and defaults sensibly. The image installs `requirements.txt` and copies the whole
package directory.

### `pip install .` (library or bare-metal use)

```bash
python -m venv .venv && . .venv/bin/activate
pip install .
python -m flask --app 'paperbroker.server:create_app()' run --host 127.0.0.1 --port 8090
```

`setup.py` declares Flask and ships `paperbroker/templates/*.html` as package data; without those two
declarations the installed package had no Flask and no template, and `GET /` answered
`500 TemplateNotFound` (fixed 2026-10-12 — see `PROJECT_STATUS.md`). `pyproject.toml` carries an
explicit `[build-system]` block because `distutils` was removed from the standard library in 3.12
(PEP 632).

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FLASK_DEBUG` | `0` | keep `0` — `1` exposes the Werkzeug debugger |
| `PAPERBROKER_TREND_MCP_URL` | *(empty)* | MCP endpoint of the trend service (xang1234) |
| `PAPERBROKER_TREND_TOKEN` | *(empty)* | bearer token for that endpoint |
| `PAPERBROKER_TREND_TIMEOUT_S` | built-in default | request timeout in seconds; invalid or ≤ 0 falls back |

The trend provider is created **only** when the URL **and** the token are set; otherwise the app runs
without trend input (its documented, byte-for-byte behaviour — no badge, no exception). A failing
trend service never breaks the page: the trend is reported as unavailable and the screen still works.

## Adapter conventions

- IV and greeks are stored as percentages (×100). CBOE `iv` comes decimal (0.25 → 25), CBOE greeks
  come raw (delta 0.45 → 45) — verified against a live AAPL chain (3568 contracts, 2026-10-07).
- `0` means zero (a legitimately illiquid contract), `None` means missing. Both are preserved;
  consumers must treat them differently.
- The CBOE feed timestamp is exposed as `quote_timestamp` on adapter quotes.
- Error contract: `CboeNotFoundError` (unknown symbol / HTTP 403 / contract not in chain),
  `CboeRequestError` (network failure / malformed response), `ValueError` (unparseable expiration
  date).
- Chains are cached per underlying (LRU 64 underlyings, 900 s TTL, refreshed on feed timestamp
  change); one HTTP fetch serves expirations, the full chain and single-symbol lookups.

## Usage

```python
from paperbroker import PaperBroker
from paperbroker.adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter

broker = PaperBroker(quote_adapter=CBOEQuoteAdapter())

dates = broker.get_expiration_dates('AAPL')     # ['2026-10-07', ...]
quotes = broker.get_options('AAPL', dates[0])   # OptionQuote list for that date
liquid = [q for q in quotes
          if (q.open_interest or 0) >= 500 and q.ask and q.bid
          and (q.ask - q.bid) / q.ask < 0.05]
stock = broker.get_quote('AAPL')                # underlying price quote
```

The original 2017 test data (`TestDataQuoteAdapter`) is still available and used by the unit tests.

## Testing and lint

```bash
# test suite
.venv/bin/python -m pytest paperbroker/tests -q

# lint — always in the CI form, over the tracked files only
git ls-files -z '*.py' | xargs -0 .venv/bin/python -m ruff check
```

CI (`.github/workflows/tests.yml`) runs four checks on every pull request: **Lint ruff** · **Test
suite pytest 3.13** · **Docker image build** (+ a `/healthz` smoke test), and on `master` also
**publish** to `ghcr.io`. All of them must be green before a merge — the code-review bot as well.

## Development workflow (this fork)

- Work on a feature branch; one change set per pull request.
- **PR titles, bodies and comments are written in English.**
- Merge only when **every** check is green — including the code-review bot.
- Every number in a PR body must be measured, not estimated; before/after evidence goes into the PR
  text verbatim.
- Dependencies stay minimal (`arrow`, `requests`, `flask`); no simulation code is reintroduced —
  history carries everything that was removed.
