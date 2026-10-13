# Project status — frozen baseline, 2026-10-13

This is the durable summary of where the project stands. It is written in English because it lives
in the repository; the detailed German documentation (phase documents and raw measurement evidence)
stays local in `docs/` and is **not** part of the repository.

## Baseline (frozen)

**This file is a snapshot, not a live view.** It is refreshed when the state is deliberately frozen.
The always-current sources are `git log --merges` (history) and the CI page of the repository.

| Item | Value |
|---|---|
| `master` | `6de055e11292efb7e6c9a1be55d13b4eec675240` (merge of PR #37) |
| Test suite | **345 passed** (`pytest paperbroker/tests`) |
| Lint | `ruff` clean, run in the CI form over the tracked files |
| CI on `master` | **4/4 green** — Lint ruff · Test suite pytest 3.13 · Docker image build · publish |
| Commits | 180 |
| Runtime dependencies | `arrow`, `requests`, `flask` (`requirements.txt` and `setup.py` agree — pinned by a test) |
| Open audit findings | **none** |

Everything in this table was measured on the commit above, not estimated.

## Merged history (newest first)

| PR | Merge commit | What it added |
|---|---|---|
| #37 | `6de055e` | Four filter groups in a grid on the fieldset, `main` 1100 px wide, seven labels with units, the sharp zero of the age field named (D1) |
| #36 | `4b1f976` | German interface in one spelling — the project's first umlauts (D0) |
| #35 | `bc356df` | Spread list ready to decide: return, break-even distance, plain-text trade, sorting (Slice K) |
| #34 | `f996862` | Record the end state after slice J |
| #33 | `3b6309b` | A timestamp after the clock fails closed; the lamp warns instead of reassuring |
| #32 | `f8f44c6` | Record that every criterion is settable |
| #31 | `102a940` | Make all eleven remaining criteria settable from the form; the last usability gap |
| #30 | `7cffacd` | Refresh the frozen baseline and record the two-system merge gate |
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

**Filters — all 17 `ScreenerCriteria` fields are settable**, from the form and from the query string.
The form shows them in **four groups** (PR #37): *Liquidität* (spread, size, OI, volume, the two
"required" switches) · *Laufzeit und Frische* (DTE window, quote age, the age switch) ·
*Volatilität* (IV window, IV preference) · *Bewertung* (the three weights). The groups sit in a CSS
grid **on `fieldset.crit`**, never on the `<form>`: a grid on the form makes every `<fieldset>` a
single grid cell, and without `flex:1 1 100%` the fieldsets shrink to their content (measured:
206–345 px instead of full width). The page is 1100 px wide because the widest table needs 837 px —
at 760 px the spread table overflowed its container by 77 px and the page scrolled horizontally below
a 930 px window. Grouping is not cosmetic: before it, the heading `Volumen-Pflicht` was torn in two
(the text ended one line, its radio buttons started the next, 40 px below the neighbouring group).
Labels that carry a unit name it (`DTE von (Tage)`, `Alter max (min)`, `Spread max (%)`); units that
nothing in the code proves (`Kontrakte` for OI/volume, a range for the weights — the validator checks
`>= 0` only) were deliberately left out. The ones that change the *selection*: `max_spread_pct`,
`max_spread_abs` ($), `min_oi`, `min_volume`, `min_bid_size`, `min_ask_size`, `iv_min` (%),
`iv_max` (%), `require_oi`, `require_volume`, `dte_min`, `dte_max`, `max_quote_age_min`. The ones
that change the *ranking*: `iv_pref` (high/low) and the three weights `weight_spread`, `weight_oi`,
`weight_iv`. `ignore_quote_age=1` remains the only way to disable the age guard (`0` is a sharp
bound, not "off") — and the field says so on the page since PR #37.

Three rules the form follows, each one measured: an **empty field keeps the default** (`min_bid_size`
empty means "off", while `0` means "size at least 0" and drops every quote of size 0); a **switch with a
`True` default is a pair of radio buttons**, not a checkbox, because an unticked checkbox means "field
absent" and would keep the default forever; a **zero weight sum scores 0.0** instead of raising, because
that is the documented answer and not a client error. IV is percent, without conversion (measured at the
adapter: stored as percent, `scale=100.0`).

**Trend** — the provider is created only when `PAPERBROKER_TREND_MCP_URL` **and**
`PAPERBROKER_TREND_TOKEN` are set; otherwise the app runs without trend input, byte-for-byte as before.
A failing trend service degrades to "trend unavailable" and never breaks the screen.

**Freshness** — a quote older than the guard is dropped and named (`stale 252 min`); so is one whose
age cannot be established (`no quote_timestamp`, `unparseable timestamp`, `clock error`), and — since
PR #33 — one that claims to be more than 60 s in the **future** (`future timestamp (+1440 min)`). The
lamp reads the raw chain and turns **red** for that case instead of green; the badge reads `Zukunft`
rather than `alt`. The 60 s tolerance is clock skew, not a licence. With the guard switched off
(`ignore_quote_age=1`) nothing is filtered and the lamp is the only warning left.

**Chain robustness** — a contract that cannot be parsed is dropped and counted (`ChainResult.skipped`,
`chain_skipped` in the JSON); a chain that is unusable as a whole still raises, so an empty result can
never look like "no candidates".

**Packaging** — `pip install .` yields a runnable installation (Flask present, `templates/screen.html`
shipped). The Docker image was never affected because it installs `requirements.txt` and copies the
whole package directory.

## Open topics

1. **Result table columns** — the API carries bid/ask/iv/open interest/volume/delta/score/spread; the
   table does not yet offer DTE, mid, moneyness, sorting or a CSV export.
2. **sdist behaviour is unverified.** Wheel and install path are measured; a source distribution was
   never built or inspected in this project.
3. **The spread sort's tie-break key is unpinned.** Slice K left one surviving mutant there: two
   spreads with exactly equal return keep their chain order, but no test fixes which key decides that.
4. **The old label margins survived the grid.** `fieldset.crit label` still carries
   `margin:4px 10px 4px 0` from the inline-flow layout. Inside the grid it costs 8 px per row (the
   form is 558 px instead of 518 px) and makes the smallest column gap 26 px instead of 16 px.
   Removing them is a candidate for the next frontend slice (D2), not a defect.
5. **The legend hint `(leer = Standard)` is gone** (PR #37) because it was measured to be misleading:
   10 of the 14 number fields are pre-filled. A *correct* default marking is still open (slice D3).

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
- **A red proof is measured, not assumed, and the expectation is corrected afterwards.** Two of the
  ten new assertions in PR #37 already hold at the base commit (they pin the field set and the table
  shape, i.e. exactly what the rebuild can destroy). The red run therefore shows 8 failures, not 10;
  their effect is proven by **mutants** instead (delete a field, give a `<tr>` an attribute).
- **The merge watch must read the body file of its own slice.** It resets the PR body before merging;
  in PR #37 the path was still hard-wired to the previous slice's file, so the body was restored to
  the wrong text and had to be patched back afterwards. The path is an environment variable now.
- **A mutant that only changes appearance survives by design.** Deleting the CSS rule of the warning
  text changes no markup, so no test can catch it — such survivors are named in the PR body as a limit
  of the method, not hidden.
- `docs/` stays local (git-excluded): it holds the phase documents and the raw evidence. The durable
  knowledge lives in the shared SilverBullet base under `Projects/paperbroker`.
