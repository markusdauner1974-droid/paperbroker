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

## Spread strategies (`/` and `GET /api/screen`)

The spread section builds **four** verticals: two debit spreads and two credit spreads. Each one has
a stable id that decides which leg is bought, so the page, the JSON and the arithmetic all read the
same name:

| `strategy` | Kind | What is traded | Premium | Break-even | Max profit | Max loss |
|---|---|---|---|---|---|---|
| `bull_call_debit` (default) | debit | buy the call with the **lower** strike, sell the call with the higher one | ask(bought) − bid(sold) | lower strike + debit | width − debit | debit |
| `bear_put_debit` | debit | buy the put with the **higher** strike, sell the put with the lower one | ask(bought) − bid(sold) | higher strike − debit | width − debit | debit |
| `bull_put_credit` | credit | buy the put with the **lower** strike, sell the put with the higher one | bid(sold) − ask(bought) | sold (higher) strike − credit | credit | width − credit |
| `bear_call_credit` | credit | buy the call with the **higher** strike, sell the call with the lower one | bid(sold) − ask(bought) | sold (lower) strike + credit | credit | width − credit |

`width` is the distance between the two strikes. All of these are per unit and gross; the position
size (multiplier × quantity, plus fees) is a separate figure and only appears in the position
columns and the `net_*` JSON keys.

A credit is the **conservative, immediately executable** amount: the sold leg is valued at its bid,
the bought leg at its ask — never a mid price. A pair is only a result while `0 < credit < width`;
a credit at or below zero, at or above the width, or a leg without a usable price is dropped with a
named reason. Both legs always run through the same filters as the single-option list above.

### Risk budget and the budget limit

A credit spread is opened for a credit, so it costs nothing to enter — the collected premium is
**not** capital and is never reported as a negative debit. For a credit spread
`position_debit` is `null`; the size of the position is its **risk budget**:

```
risk_budget = (width − credit) × multiplier × quantity + fees      # credit spread
risk_budget = debit × multiplier × quantity + fees                  # debit spread (= position_debit)
```

`budget` is compared against this risk budget for **both** kinds, so a credit spread is bounded by
its maximum loss including fees, not by its credit. `net_return_pct` uses the same denominator
(`net_max_profit / risk_budget`). The figure is this app's own arithmetic from the quoted legs, the
strike width and the form's costs: it is **not** a broker margin requirement, and no exchange,
broker or risk model is consulted.

### Parameters and the legacy alias

```
GET /api/screen?ticker=SPY&expiration=2026-11-20&strategy=bear_call_credit
```

* `strategy=bull_call_debit|bear_put_debit|bull_put_credit|bear_call_credit` is the primary parameter.
* `option_type=call|put` is the **legacy alias** from before the strategies had names. On its own it
  still means the debit spread: `call` → `bull_call_debit`, `put` → `bear_put_debit`. Old links and
  API calls keep their meaning exactly.
* Sent **together with** a `strategy`, `option_type` only names the chain side and must match it
  (`bull_put_credit&option_type=put` and `bear_call_credit&option_type=call` are accepted).
* **Conflict rule:** a request whose two parameters disagree is an error, not a preference order.
  `strategy=bear_put_debit&option_type=call` and `strategy=bull_put_credit&option_type=call` are
  rejected: the page shows the contradiction and renders no spread table (no silent fallback to
  either side), and `/api/screen` answers `400` with
  `{"error": "conflicting strategy and option_type", "option_type_to_strategy": {...}}`. An unknown
  `strategy` is `{"error": "unknown strategy", "allowed": [...]}`; an unknown `option_type` keeps its
  old `{"error": "unknown spread type", "allowed": ["call", "put"]}` — that is the *bare* legacy
  request (`?option_type=bogus` with no `strategy`). Sent **together with** a `strategy`, an
  unreadable `option_type` makes the agreement unprovable: the request is rejected like a
  contradiction (page: no table, API: `400`), and the page keeps the strategy the caller asked for
  selected instead of showing a default.
* The JSON carries `strategy`, `strategy_label` and - unchanged - `spread_type` (`call`/`put`), the
  derived chain side. Inside every row `long_symbol` is the leg that is **bought**, `short_symbol`
  the leg that is **sold**; the page spells that out per row.
* `data_mode=fresh|last_available` picks what the age limit does (see
  [Quote age](#quote-age-two-modes-one-rule-data_mode)); the default `fresh` is the previous
  behaviour. All other filters in the same request are untouched by it.

### JSON fields of a vertical row

Unchanged for existing consumers: `symbol`, `expiration`, `long_symbol`, `short_symbol`,
`long_strike`, `short_strike`, `strike_width`, `multiplier`, `quantity`, `fees`, `budget`,
`currency`, `net_max_loss`, `net_max_profit`, `net_breakeven`, `net_return_pct`,
`profitable_after_costs`, `max_loss`, `max_profit`, `breakeven`, `snapshot_id`, `eva_eligible`; and
`strategy` / `strategy_label` from the strategy slice. For a debit row every value is computed
exactly as before.

Added for the credit spreads (additive, no existing key changed meaning):

| Field | Debit row | Credit row |
|---|---|---|
| `strategy_kind` | `"debit"` | `"credit"` |
| `net_debit` | the debit per unit | `null` |
| `position_debit` | debit × multiplier × quantity + fees | `null` |
| `debit_width_ratio`, `debit_ge_width` | as before | `null` |
| `net_credit` | `null` | the credit per unit |
| `credit_width_ratio`, `credit_ge_width` | `null` | credit / width, and `credit >= width` |
| `risk_budget` | = `position_debit` | `(width − credit) × multiplier × quantity + fees` |
| `risk_budget_note` | own arithmetic, no broker margin | same text |
| `budget_fits` | `risk_budget <= budget`, `null` without a budget | same |

`max_loss`, `max_profit` and `breakeven` are strategy-dependent: a credit row carries
`width − credit`, the credit itself, and the break-even off the **sold** strike.


## Quote age: two modes, one rule (`data_mode`)

The CBOE payload names **no timezone**. Its envelope stamp (`"2026-11-11 16:11:19"`) is a naive
string, and `arrow` 0.10 reads a naive string as UTC — so `.to("UTC")` converted nothing while
looking like a conversion. Measured 2026-11-11: that stamp answered `16:12:25 UTC` requests with
`16:11:19` and, 75 s later, `16:14:19`, i.e. it tracks UTC wall clock (a New York stamp would have
been four to five hours behind). An age is therefore computable — but it is an **assumption**, and
`paperbroker/quote_age.py` is the single place that says so:

| Field | Meaning |
|---|---|
| `zone` | `"stated"` when the value carries `Z` or an explicit offset, `"assumed_utc"` when it does not, `"none"` when there is nothing to read |
| `age_proven` | `true` **only** for a stated zone — an assumption is never presented as a measurement |
| `source_timestamp_raw` | the value as the source delivered it |
| `source_timestamp_utc` | a UTC value **only** when the zone was stated, else `null` |
| `fetched_at_utc` | when *we* fetched — deliberately a different fact from the source time |

The same rule answers all four surfaces (page, `/api/screen`, `/api/v1/option-chain`, the earnings
scan); `option_chain_api` keeps reporting `SOURCE_TIMEZONE_UNKNOWN` for the same reason.

`data_mode` selects what the age limit **does** — it never changes any other filter:

- `fresh` (default, unchanged): a quote older than `max_quote_age_min` is dropped with its own
  reason (`veraltet: 275 Min.`). A quote whose age cannot be established is dropped too — fail-closed.
- `last_available`: the limit becomes a statement instead of a rejection. An old quote may be used
  for a provisional preparation, every other filter stays active, and the answer is marked
  provisional. **Not** relaxed: a missing, unreadable or future stamp is dropped in this mode as
  well — the mode admits older data, not undated data.

```bash
# default: the age limit decides
curl 'http://127.0.0.1:8090/api/screen?ticker=AAPL&expiration=2026-11-18'

# preparation outside trading hours: old data may be used, clearly labelled
curl 'http://127.0.0.1:8090/api/screen?ticker=AAPL&expiration=2026-11-18&data_mode=last_available'
```

Both answers carry one `data_state` block (identical shape on `/api/screen` and on
`/api/v1/earnings-scan`):

```json
{"mode": "last_available", "mode_label": "Letzter verfügbarer Marktstand",
 "provisional": true, "provisional_note": "Vorläufig: … nicht ein aktueller Kurs. Keine Aussage über aktuelle Handelbarkeit.",
 "fetched_at_utc": "2026-11-11T16:30:13Z",
 "source_timestamp_raw": "2026-11-11 16:29:20", "source_timestamp_utc": null,
 "source_timestamp_zone": "assumed_utc", "source_age_minutes": 0.9, "age_proven": false,
 "max_age_min": 30, "counts": {"total": 96, "fresh": 0, "stale": 96, "future": 0, "undated": 0},
 "market": {"state": "open", "reason": null, "exchange_day": "2026-11-11", "exchange_time": "11:29"}}
```

`market` comes from the calendar this project already had (`earnings_calendar.is_us_trading_day`
plus the 09:30–16:00 New York session), so a weekend or an NYSE holiday is named as the reason the
data is old instead of being guessed. `provisional` is `true` only when the mode is `last_available`
**and** at least one quote is old or undated — fresh data is never dressed up as provisional.

Measured end to end against the live feed (2026-11-11, market open, `max_quote_age_min=0`):
default mode `candidates: 0` with 77 × `veraltet: 1 Min.`; the same request with
`data_mode=last_available` gives `candidates: 1`, `provisional: true` and the **same** source stamp.
With the real default (30 min) nothing changed: `candidates: 1`, lamp green, age 0.89 min.



## Strike search: two modes (`pair_mode`)

Two searches are offered explicitly - they differ in which strike pairs are
*formed*, never in which filters are applied afterwards:

| `pair_mode` | Pairs formed | Width |
|---|---|---|
| `adjacent` (default) | strikes that are neighbours **in the chain** | `pair_max_width` must be empty (a leftover value is ignored and named in `pair_search.width_ignored`) |
| `within_width` | every pair whose strike distance is inside `pair_max_width` | a **positive** width is required, otherwise the request is a `400` |

`pair_mode` unset keeps the historical derivation: no width -> adjacent, a width ->
`within_width`. That is why every existing link and API call behaves exactly as
before.

Both modes pair from the **raw** chain: a strike is never removed because it is
illiquid, so a bad strike between two good legs cannot hide the pair
`240 / 250`. Adjacency therefore means "next listed strike" - the feed lists no
empty strike, and nothing else can be proven from this data.

```bash
# neighbours only (default)
GET /api/screen?ticker=AAPL&expiration=2026-11-18

# every combination up to 25 $ wide
GET /api/screen?ticker=AAPL&expiration=2026-11-18&pair_mode=within_width&pair_max_width=25
```

The search is bounded in **time and memory**, and never silently:
`MAX_PAIRS_PER_SECTION` (50 000 pairs of one expiry) is enforced *inside* the
pairing loop, so a user-chosen width of 100 000 $ on a long chain cannot allocate
n²/2 tuples first. The answer reports the search that really happened:

```json
"pair_search": {"mode": "within_width", "label": "Alle Kombinationen bis maximale Breite",
                "max_width": 25.0, "width_ignored": false,
                "coverage": {"possible": 380, "enumerated": 380, "possible_exact": true,
                             "capped": false, "limit": 50000}}
```

A search that hits the ceiling is **cut, not filtered**: pairs are collected in
enumeration order (lower strike, then upper) until the limit, the rest is not
examined, and the count stops too — so `capped: true` comes with
`possible_exact: false`, and the page says "gekappt bei N; die Suche ist damit
NICHT vollständig" instead of printing a reassuring "N von N".

Measured on local data (4 expiries, 400 strikes each, 25 $ width): 38 700 pairs in
87 ms; adjacent mode 2.9 ms; at the ceiling ~100 ms regardless of the width asked
for.

`pair_max_width=0` is refused with `400` (a width of zero is no search); a
negative width is refused by its own rule.

## Why something was excluded (per unit, never summed)

`/api/screen` and the page carry an `exclusions` list with **one block per unit**:
contracts, spread combinations, expirations, (in the earnings scan) stocks.

```json
[{"level": "contract", "unit": "Kontrakte", "counted": 95, "first_reason_only": true,
  "first_reason_note": "Je Kontrakt zaehlt genau ein Grund: der erste Filter, der ihn abgelehnt hat. ...",
  "categories": [{"key": "liquidity", "label": "Liquiditaet", "count": 92},
                 {"key": "data", "label": "fehlende Daten", "count": 3}],
  "unmapped": [], "not_checked": ["Volatilitaet", "Budget"]}]
```

Three statements the block keeps apart on purpose:

* **One reason per item.** The pipeline reports the *first* filter that rejected
  a contract or a pair; `first_reason_only` and its sentence say so in words.
  No additional cause is claimed - a claim that was not measured is not made.
* **No sum across units.** A dropped spread combination is two contracts and an
  expired expiry can hide a whole chain; there is no grand total anywhere.
* **Filters that did not run are named.** `not_checked` lists the switches that
  were off for this request (no age guard, no IV band, no budget). A category
  with a count really occurred; a category that never ran never appears with a
  number.

Categories: `liquidity`, `age`, `dte`, `budget`, `price` (invalid debit/credit),
`costs`, `volatility`, `data` (a value is missing), `scan_limit` (the earnings
scan's own budget), `other` (anything unmapped - and it is shown, not filed
away).

## Sorting (`sort`)

Six orders, all deterministic:

| `sort` | Order |
|---|---|
| `rendite_desc` (default) | gross max profit / max loss, descending |
| `be_asc` | distance to the break-even, nearest first |
| `net_ratio_desc` | net profit / max loss after costs, descending |
| `net_profit_desc` | net profit potential after costs, descending |
| `max_loss_asc` | max loss after costs, smallest first |
| `none` | chain order |

Rules: a row **without a number** sorts last in every direction (a missing
denominator must not look like the best or the worst trade), and ties fall back
to symbol + long strike, so the same chain always produces the same list.

None of these columns is a probability or an expected return: they rank the
payoff *at expiry* (best case / worst case) and the page says so above the
table.

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

## Unfiltered option chains for Hermes (`GET /api/v1/option-chain`)

The screener endpoints (`/` and `/api/screen`) stay filtered and unchanged.
This endpoint is a **data request**, not a quality filter: it returns every
contract the source delivered, including rows a screener would drop.

```
GET /api/v1/option-chain?ticker=AAPL
GET /api/v1/option-chain?ticker=AAPL&expiration=2027-01-15
```

**How Hermes should use it**

1. Fetch the whole chain first and read `listed_expirations` - do not assume a
   45-day window; the source lists expirations years out.
2. Check `snapshot.completeness`, `snapshot.warnings` and the per-contract
   `quality_flags` before computing anything.
3. Check `contract_metadata_status`. It is `unknown` for every contract today,
   because the source names no multiplier, deliverable, exercise style,
   settlement type or currency. **Do not treat such a contract as a standard
   100-share contract without checking** - adjusted series can differ.
4. Check the timestamp block. The feed carries no timezone, so
   `source_timestamp_utc` and `source_age_seconds` stay `null` and
   `SOURCE_TIMEZONE_UNKNOWN` is reported. Only `fetched_at_utc` and
   `cache_age_seconds` are real clocks.
5. Only then compute selection, liquidity and chance/risk. This endpoint
   decides nothing: it contains no `buyable`, `recommended` or freshness
   verdict.

**Units** (measured against the live feed on 2026-10-17)

- `iv_decimal` is decimal: `0.25` means 25 %. The screener's `OptionQuote`
  multiplies by 100; this endpoint never does.
- `delta` is the source value (`0.40` = 0.40).
- `gamma`, `theta`, `vega`, `rho` are **source-native with unverified
  units** - the feed gives no time base or volatility/rate step. Treat them as
  unusable for calculations until that is settled; they are carried for
  transparency, not for pricing.
- `bid_size`/`ask_size` are contract counts as delivered by the source.

A missing value is `null` and never `0`; a real `0` stays `0`. A last trade is
never replaced by the bid/ask midpoint.

## Earnings date and the expiry deadline (`GET /api/v1/earnings`)

The spread strategy needs one thing the chain cannot answer: **when does the
company report?** The options expiry must lie after the numbers, the position is
meant to be closed before them. Two read-only routes answer that, and they
decide nothing else.

```
GET /api/v1/earnings?ticker=AAPL         # date, session, confirmation, freshness
GET /api/v1/earnings-window?ticker=AAPL  # + listed expirations + planned exit day
```

The full contract is `earnings_v1.schema.json` (draft 2020-12).

**Source.** Nasdaq's key-free public JSON endpoints (`api.nasdaq.com`), no
account, no API key, no cookie:

- `/api/analyst/<SYMBOL>/earnings-date?assetclass=stocks` - the date, the session
  in plain text, and the estimate marker,
- `/api/calendar/earnings?date=YYYY-MM-DD` - a structured row per reporting
  company, used **only** as a cross-check for the date the first endpoint named.

Yahoo was measured and rejected for this deployment: `querySummary` answered
HTTP 429 on the crumb handshake, i.e. it needs a session dance.

**What the source really delivers** (measured 2026-10-24, AAPL/MSFT/NVDA/TSLA)

- `date_confirmation` is `estimated` for every future date Nasdaq names
  ("is expected* to report", or "derived from an algorithm"). `confirmed` stays
  reserved for a date the source states without an estimate marker; a calendar
  cross-check never upgrades it.
- `release_session` comes from the text ("before market open" / "after market
  close"); NVDA's algorithm-derived date carries no session, so it stays
  `unknown`.
- TSLA returned no date at all - the answer is `next_earnings_date: null`,
  `status: unknown`. Nothing is rolled forward by three months.
- No timezone and no clock time is read from anywhere.

**Two limits, two jobs**

- `ttl_seconds` (6 h): when the source is asked again. Earnings dates move
  rarely, but vendors revise them.
- `freshness_limit_seconds` (48 h): when a stored proof may still authorise
  anything. 48 h survives a market-closed weekend; three missed days turn the
  record `stale`, and a stale record **blocks**.

A failed live query never refreshes `source_checked_at_utc` and always sets
`live_query_failed`; the cache block carries the error code. A cache entry is
never dressed up as a live check.

**The rule** (`paperbroker/earnings_calendar.py`, pure, clock-free)

1. The earnings date must be present, valid, fresh and not in the past.
2. `date_confirmation` must be `confirmed` or `estimated` - `unknown` blocks.
3. The planned exit is the **last US trading day strictly before** the earnings
   date, whatever the session. Conservative on purpose: exiting one day early
   never holds the position into the numbers.
4. The expiry must be **in the chain's own list** and strictly after the
   earnings date. The earliest such date is selected; if none exists the answer
   is `blocked` with `no_listed_expiration_after_earnings` - never the next best
   date.
5. A listed expiry after earnings is **necessary, not sufficient**. The decision
   says nothing about liquidity, IV or whether the trade is worth taking.

The US calendar is computed, not tabulated: weekends, the ten NYSE holidays
(including Good Friday and the Saturday/Sunday observance rule, e.g. 31 Dec 2021
for New Year 2022) and the documented special closures (9/11, Hurricane Sandy,
days of mourning).

**Configuration.** The code default is OFF: without a source the Null adapter
answers `unknown` with `earnings_source_not_configured`. The compose stack sets
`PAPERBROKER_EARNINGS_SOURCE=nasdaq`. Deploying the code therefore never starts
a new external call by itself.

**For Hermes EVA.** Use `decision.eligible` plus `expirations.selected`; do not
re-derive the exit day from the session, and do not treat `eligible` as a trade
recommendation. `reasons[].code` is the stable machine field, `message` is the
German page text.

## Whole-day earnings scan (`GET /api/v1/earnings-scan`)

The single-ticker view costs one request and one form per title: 25 titles a day
mean 25 page loads, and the titles behind the start page's Top-25 cut cannot be
reached at all. This route answers for the whole day at once:

```
GET /api/v1/earnings-scan?day=2026-11-16
GET /api/v1/earnings-scan?day=2026-11-16&strategy=bull_call_debit&strategy=bear_put_debit
GET /api/v1/earnings-scan?day=2026-11-16&max_symbols=20&max_expirations=4&concurrency=3
```

**It runs on an explicit request only.** `GET /` never calls it - one chain
lookup per title is far too expensive for a page view. Nothing is scheduled and
nothing is cached beyond the adapters' own caches (6 h for earnings dates, 900 s
for a chain), so a repeated scan inside those windows costs no source call.

**The universe is the day, not the page.** The starting set is every row Nasdaq
lists for `day`, before any ranking. The CBOE symbol list (`Stock Symbol` column,
one download a day) is the prefilter, and only then does the budget decide how
many titles are worked through. `coverage` reports all three numbers - and if
the budget cut the day short, `complete: false` plus `truncation_reasons` plus
the names in `hidden_symbols`.

**Every listed expiry is checked, not just the first one.** For each title the
scan reads the chain once, takes the expiries the chain really lists, and checks
every one of them that lies after the earnings date and inside the maturity
window. Expiries it leaves out are listed under `skipped_expirations` with the
reason (`expiration_on_or_before_earnings`, `outside_dte_window`,
`max_expirations`).

**It does not re-implement the calculation.** Hits come from
`screener.vertical_spreads` - the function `/api/screen` calls - with the same
`ScreenerCriteria` and the same clock, so a scan hit and a single-view hit are
the same spread by construction. Measured 2026-11-08 over live data (day
2026-11-16, 6 titles): 1/21/7/2/5/2 hits per ticker and strategy, every one of
them identical to the single-ticker route over the same expiries.

**Eight states per ticker and strategy**, because "no hits" and "we could not
look" are different answers:

- `hits` - at least one spread survived every filter; `best` is the first row of
  the documented `rendite_desc` sort (max profit / max loss, then break-even
  distance, then symbol),
- `no_match` - the data was good, nothing survived,
- `stale_data` - the quotes are older than `max_quote_age_min`; the decision is
  `screener.filter_stale`, the same rule the single view uses, applied to the
  expiries that were actually checked (`checked_freshness`). A stale chain
  section on an expiry that was skipped for another reason does not turn a
  fresh, empty result into "too old",
- `incomplete_data` - part of the chain was unusable (skipped contracts, a
  truncated expiry list),
- `fetch_failed` - the earnings record or the chain could not be read; one
  failing title never cancels the others,
- `not_checked` - the scan budget did not reach the title, or no listed expiry
  falls inside the window,
- `window_blocked` / `window_unknown` - the earnings window itself refuses (or
  cannot decide); the reasons are the ones `/api/v1/earnings-window` returns.

**The budget** (all parameters optional, upper bounds are enforced server-side):

- `max_symbols` (default 40, max 200) - chain lookups,
- `max_expirations` (default 8, max 40) - per title,
- `max_seconds` (default 120, max 300) - wall clock; checked before each wave,
- `concurrency` (default 4, max 8) - parallel chain lookups.

Everything else is a screener criterion (`max_spread_pct`, `min_oi`, `dte_min`,
`ignore_quote_age`, ...), exactly as on `/api/screen`.

`coverage.source_calls` counts the adapter lookups that were really made - not
HTTP requests: inside their TTLs both adapters answer from cache.
