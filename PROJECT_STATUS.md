# Project status — frozen baseline, 2026-10-12

This is the durable summary of where the project stands. It is written in English because it lives
in the repository; the detailed German documentation (phase documents and raw measurement evidence)
stays local in `docs/` and is **not** part of the repository.

## Baseline (frozen)

**This file is a snapshot, not a live view.** It is refreshed when the state is deliberately frozen.
The always-current sources are `git log --merges` (history) and the CI page of the repository.

| Item | Value |
|---|---|
| `master` | `31f19af675f9390525a046a410623d3410013137` (merge of PR #29, last merge of the documentation freeze) |
| Test suite | **295 passed** (`pytest paperbroker/tests`) |
| Lint | `ruff` clean, run in the CI form over the tracked files |
| CI on `master` | **4/4 green** — Lint ruff · Test suite pytest 3.13 · Docker image build · publish |
| Commits | 158 |
| Runtime dependencies | `arrow`, `requests`, `flask` (`requirements.txt` and `setup.py` agree — pinned by a test) |
| Open audit findings | **none** |

Everything in this table was measured on the commit above, not estimated.

## Merged history (newest first)

| PR | Merge commit | What it added |
|---|---|---|
| #29 | `31f19af` | Pass the trend variables through the local Arcane stack as well |
| #28 | `f58d24c` | Freeze the state: `PROJECT_STATUS.md`, rewritten README, trend variables in the compose file |
| #27 | `9bef691` | Declare Flask and the template asset; `setuptools` + explicit `[build-system]` (B-13) |
| #26 | `6d7c79e` | Pin the sign of the extrinsic value above the intrinsic value (B-15) |
| #25 | `5d6c4bb` | One unusable contract no longer takes the whole chain down; skip count reported (B-16 + B-9) |
| #24 | `5fedb0f` | Read the trend stage from `technicals` (measured payload shape) |
| #23 | `76a01ea` | Wire the trend provider to the xang1234 MCP source |
| #22 | `82f6288` | Vertical call spreads built from the raw chain |
| #21 | `c4f24ca` | Explain each row, label the delta unit, move the row order into the server |
| #20 | `07b5516` | Make the filter criteria settable (option A) |
| #19 | `736bd10` | Name the drop reasons and the quote age on the page (UX-1) |
| #18 | `baf32a5` | `get_extrinsic_value` on a missing spot returns `None` instead of raising (B-3a) |
| #17 | `b12cf89` | `quote_factory` keeps `underlying_price` (B-3) |
| #16 | `19d2c03` | Filter the expiration picker to the DTE window (B-11) |
| #15 | `7dcc090` | Pin `gamma` and `iv` numerically (B-12) |
| #14 | `7c2b24d` | Pin `delta` for the model greeks path (Slice 4) |
| #13 | `2027f7d` | Fix the greek scale in the model path (B-7) |
| #11 | `df4aa91` | Classify OCC symbols by the indicator instead of substrings (B-2) |
| #10 | `556c559` | Annualise the risk-free rate (B-1) |
| #7 | `7407541` | Auto-load the expiration list |
| #6 | `2f309ce` | Web display (Phase 5) |
| #5 | `fa125f5` | Screener core (Phase 4) |

## What the application can do today (measured 2026-10-12)

**Page (`GET /`)** — form plus results table, freshness lamp, readable errors, and the drop reasons
(`stale 252 min · 67`, `oi=0 illiquid · 24`, …) so an empty window explains itself.

**API (`GET /api/screen`)** — JSON keys `ticker, expiration, listed_dates, results, freshness,
dropped_reasons, verticals, vertical_drops`, plus `chain_skipped` when a chain was fetched. Each
result carries `symbol, bid, ask, iv, open_interest, volume, delta, score, spread_pct, reasons`.

**Settable filters — 6 of the 17 `ScreenerCriteria` fields, plus the age switch.** Reachable from the
form and from the query string: `max_spread_pct`, `min_oi`, `min_volume`, `dte_min`, `dte_max`,
`max_quote_age_min`, and `ignore_quote_age=1` as the only way to disable the age guard (`0` is a
sharp bound, not "off"). The other 11 fields exist in the code with defaults and are **not** reachable
from the page: `max_spread_abs`, `min_bid_size`, `min_ask_size`, `iv_min`, `iv_max`, `iv_pref`,
`require_oi`, `require_volume`, `weight_spread`, `weight_oi`, `weight_iv`.

**Trend** — the provider is created only when `PAPERBROKER_TREND_MCP_URL` **and**
`PAPERBROKER_TREND_TOKEN` are set; otherwise the app runs without trend input, byte-for-byte as before.
A failing trend service degrades to "trend unavailable" and never breaks the screen.

**Chain robustness** — a contract that cannot be parsed is dropped and counted (`ChainResult.skipped`,
`chain_skipped` in the JSON); a chain that is unusable as a whole still raises, so an empty result can
never look like "no candidates".

**Packaging** — `pip install .` yields a runnable installation (Flask present, `templates/screen.html`
shipped). The Docker image was never affected because it installs `requirements.txt` and copies the
whole package directory.

## Open topics

1. **Future timestamps pass the age guard** (measured 2026-10-12). The guard compares
   `age_s > max_age_s`; a timestamp *after* the clock yields a negative age and is kept. Measured with
   `9999-12-31 23:59:59`: 2 of 2 contracts kept, no reason reported. A feed with a clock running ahead
   would slip through. Candidate fix: treat a future timestamp as suspect and name it.
2. **Eleven filter criteria are not reachable from the page** (see above). The engine supports them;
   the form does not expose them, so the operator cannot use them. This is the largest usability gap.
3. **Result table columns** — the API carries bid/ask/iv/open interest/volume/delta/score/spread; the
   table does not yet offer DTE, mid, moneyness, sorting or a CSV export.
4. **sdist behaviour is unverified.** Wheel and install path are measured; a source distribution was
   never built or inspected in this project.

## How work is done here (unchanged)

- One change set per pull request; **all** checks must be green before a merge — the code-review bots
  included. PR titles, bodies and comments are written in English.
- **A merge gate must read two systems, not one.** GitHub Actions report as *check runs*; CodeRabbit
  reports as a *commit status*. A gate that only looks at `check-runs` sees "all green" while a review
  is still pending, and `mergeable_state: unstable` then looks like a race instead of what it is — the
  open status. Measured on PR #29: four green check runs, `combined status: pending`,
  `CodeRabbit: Review in progress`. Both sources are polled, and the merge waits.
- A bot that ends in `failure` **without a finding** (HTTP 503, "temporarily overloaded", "rules could
  not be evaluated") is an outage, not a review result: do not merge, do not treat it as a finding, and
  retry the review a bounded number of times. Measured on PR #28: the retry brought the review back to
  green and the merge then went through.
- **Every number in a PR body is measured, never estimated**, and the before/after evidence is quoted
  verbatim. A finding is re-measured before it is worked on: several entries in the local knowledge
  base had already been fixed when they were picked up, and the status lines were corrected instead of
  the code being touched again.
- Defects are reproduced first, then fixed, then pinned by a test that closes the **class** of the
  defect, not the single symptom.
- Mutants that no test can catch (e.g. a precautionary `[build-system]` block) are reported as
  not-testable rather than covered by a decorative assertion.
- `docs/` stays local (git-excluded): it holds the phase documents and the raw evidence. The durable
  knowledge lives in the shared SilverBullet base under `Projects/paperbroker`.
