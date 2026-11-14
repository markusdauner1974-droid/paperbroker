"""From a gap candidate to ONE tradable debit spread - the funnel after the gap.

What this module adds
---------------------
``gap_scan`` answers "which titles moved, and how far". It knows no option, no
chain and no report date. This module takes those candidates and runs the four
questions an option trade needs, in this order, so that every title either
becomes a row or is named with the step that stopped it:

1. WHEN does the title report next (Finnhub, see ``next_earnings.py``)?
2. WHICH listed expiry is the first one STRICTLY after that date?
3. does the chain of that expiry hold a debit spread of the gap's direction with
   a reward/risk of at least ``DEFAULT_RR_MIN`` (2.0), a long leg whose |delta| is
   at least 20 and a net debit of at least 0.50 USD?
4. how liquid are its two legs (``thin`` / ``full``)?

Every step is a pure function over data a caller supplies, and the run around
them (``run_funnel``) fetches in parallel with a whole-run deadline. Nothing
here places an order, and nothing here invents a number: a missing value stays
``None`` and the title is excluded with a reason.

Why the filters are these filters
---------------------------------
* **Expiry strictly after the report.** The trade is the reaction to the
  numbers; an expiry ON the report day would be priced before them and expire
  into the move. "First listed series after" is a statement about the CHAIN, not
  about a calendar: a monthly cycle that the source does not list cannot be
  chosen, and a title with no series after its report has no candidate.
* **Reward/risk from ``net_return_pct``.** The screener already computes the
  ratio the costs leave behind (``net_return_pct / 100``, fees included), so the
  funnel reads it instead of deriving a second, differently rounded ratio.
* **|delta| of the long leg >= 20.** A spread whose bought leg is deep
  out-of-the-money is a lottery ticket, not the gap trade; 20 % is the floor
  under "moves with the underlying". ``delta`` is in the percentage convention
  of this repo (-96.56 means -0.9656), hence ``abs()``.
* **Net debit >= 0.50 USD.** A ten-cent debit on a wide spread produces a
  spectacular ratio out of a single tick of the quote; half a dollar keeps the
  ratio a statement about a trade someone could actually place.
* **Open interest >= 100 per leg, ``full`` from 500 and a 10 % spread.** A
  candidate needs a counterparty; ``full`` is the stricter, human-readable
  label on top of the same two legs, not a second filter (a thin pair is still a
  candidate, it is only named as thin).

The data's age is NOT a filter here
-----------------------------------
The spread is priced on the last available market state
(``MODE_LAST_AVAILABLE``) and no quote-age limit is applied
(``max_quote_age_min=None``): the funnel answers "what would this cost on the
last print", and the answer says which print that was by construction of the
chain. Every other filter of the screener pipeline (data completeness, spread
readability, open interest) stays active - they are validity, not freshness.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime

from .gap_scan import GAP_DOWN, GAP_UP, STRATEGY_BEAR_PUT_DEBIT, STRATEGY_BULL_CALL_DEBIT
from .next_earnings import CODE_NO_API_KEY, NextEarningsError
from .quote_age import MODE_LAST_AVAILABLE
from .screener import (
    PAIR_SEARCH_WITHIN_WIDTH,
    ScreenerCriteria,
    strategy_option_type,
    vertical_spreads,
)

#: Reward/risk floor: the best case must be worth at least twice the risk.
DEFAULT_RR_MIN = 2.0
#: Open interest a CANDIDATE needs on each leg.
DEFAULT_MIN_OI = 100
#: Open interest that turns a candidate into a ``full`` one.
FULL_MIN_OI = 500
#: Bid-ask (in percent of the mid) a ``full`` leg may carry.
FULL_MAX_SPREAD_PCT = 10.0
#: |delta| of the bought leg, in the repo's percentage convention.
DEFAULT_MIN_LONG_DELTA = 20.0
#: Smallest net debit a ratio may be built on, in USD per share.
DEFAULT_MIN_NET_DEBIT = 0.50
#: Six chains in parallel: the CBOE feed throttles earlier than that, and one
#: request per symbol is all this funnel ever asks for.
DEFAULT_MAX_WORKERS = 6
#: The whole run's budget. A chain that has not answered by then is named
#: ``deadline_exceeded`` in ``failures`` - the request never waits on the
#: slowest symbol.
DEFAULT_DEADLINE_SECONDS = 150.0
#: Fees are part of the ratio (``net_return_pct``), so the assumption is spelled
#: here and repeated in the answer's notes. 0.0 is the honest default: this repo
#: has no fee schedule, and a made-up fee would move every ratio silently.
DEFAULT_FEES = 0.0
DEFAULT_MULTIPLIER = 100
DEFAULT_QUANTITY = 1

LIQUIDITY_THIN = "thin"
LIQUIDITY_FULL = "full"
LIQUIDITIES = (LIQUIDITY_THIN, LIQUIDITY_FULL)

#: The four exclusion reasons of the agreed contract.
REASON_NO_NEXT_EARNINGS = "no_next_earnings"
REASON_NO_EXPIRY = "no_expiry_after_earnings"
REASON_NO_SPREAD = "no_spread_ge_rr"
REASON_NOT_OPTIONABLE = "not_optionable"
EXCLUSION_REASONS = (REASON_NO_NEXT_EARNINGS, REASON_NO_EXPIRY, REASON_NO_SPREAD,
                     REASON_NOT_OPTIONABLE)

#: The chain-failure classes that mean "this title has no tradeable option chain"
#: (values of ``CboeChainError.reason``, spelled here to keep this module free of
#: the adapter). Every other class is an outage and goes to ``failures``.
_CHAIN_NO_OPTION = ("not_listed", "unusable")
_CODE_CHAIN_FAILED = "chain_fetch_failed"
CODE_DEADLINE = "deadline_exceeded"

#: The direction -> strategy mapping of the gap scan, restated as the funnel's
#: own table so a caller cannot hand in a strategy that contradicts the gap.
STRATEGY_BY_DIRECTION = {
    GAP_UP: STRATEGY_BULL_CALL_DEBIT,
    GAP_DOWN: STRATEGY_BEAR_PUT_DEBIT,
}


def _as_date(value):
    """``date`` / ``datetime`` / ISO string -> ``date``, else ``None``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _round2(value):
    """Two decimals, or ``None`` - never a rounded stand-in for a missing value."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 2)


def _quote_parts(quote):
    """``(strike, delta, open_interest, bid, ask)`` of one leg, tolerantly read."""
    asset = getattr(quote, "asset", None)
    return (
        getattr(asset, "strike", None),
        getattr(quote, "delta", None),
        getattr(quote, "open_interest", None),
        getattr(quote, "bid", None),
        getattr(quote, "ask", None),
    )


def bid_ask_pct(quote):
    """Bid-ask as a percentage of the mid, or ``None`` when it cannot be measured.

    ``None`` for a missing side, a crossed or zero market: those are unreadable
    quotes, not wide ones. The mid is the reference because that is what a
    half-spread costs.
    """
    bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return None
    if bid <= 0 or ask <= 0 or bid > ask:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100


def expirations_in(chain):
    """The listed expiries of one chain, ascending and unique."""
    days = set()
    for quote in chain or ():
        day = _as_date(getattr(getattr(quote, "asset", None), "expiration_date", None))
        if day is not None:
            days.add(day)
    return sorted(days)


def pick_expiration(expirations, next_earnings_date):
    """The first listed expiry STRICTLY after the report, or ``None``.

    ``expirations`` is anything iterable of dates/ISO strings (``expirations_in``
    answers the chain's own list). Strictly after: an expiry on the report day
    would be priced before the numbers and expire into the move, which is the
    opposite of the trade.
    """
    report = _as_date(next_earnings_date)
    if report is None:
        return None
    later = sorted(day for day in (_as_date(value) for value in expirations or ())
                   if day is not None and day > report)
    return later[0] if later else None


def slice_chain(chain, expiration):
    """The contracts of ONE expiry - one request, sliced locally."""
    wanted = _as_date(expiration)
    return [quote for quote in chain or ()
            if _as_date(getattr(getattr(quote, "asset", None), "expiration_date", None)) == wanted]


def criteria_for(section):
    """The screener criteria of this funnel: no DTE window, OI floor 100, no age limit.

    ``pair_mode`` is ``within_width`` with the section's whole strike span as the
    width - that is what "every combination of the expiry" means here, and the
    width is taken from the chain instead of a made-up large number so the
    enumeration cannot silently stop at an arbitrary bound. The screener's own
    50 000-pair ceiling (``MAX_PAIRS_PER_SECTION``) still applies - it is a
    memory bound, not a width rule - and this funnel now carries its report
    (``possible`` / ``enumerated`` / ``capped`` / ``limit``) through
    ``spread_candidates`` into every row and into the run's notes, so a capped
    search can no longer look like a complete one.
    """
    strikes = [strike for strike in (_quote_parts(quote)[0] for quote in section or ())
               if isinstance(strike, (int, float))]
    width = (max(strikes) - min(strikes)) if len(strikes) >= 2 else None
    return ScreenerCriteria(
        # The gap trade has no 7-45-day window: the expiry is chosen by the
        # report date, so a window here would silently empty the section.
        dte_min=None,
        dte_max=None,
        min_oi=DEFAULT_MIN_OI,
        require_oi=True,
        min_volume=None,                 # volume is no liquidity proof; OI is
        require_volume=False,
        # No spread filter: a wide pair is still a candidate, it is labelled
        # ``thin`` (see liquidity_of). Unreadable quotes are dropped by the
        # pipeline itself, which is a validity rule, not a width rule.
        max_spread_pct=None,
        max_spread_abs=None,
        # Age is explicitly not a filter (see the module docstring).
        max_quote_age_min=None,
        data_mode=MODE_LAST_AVAILABLE,
        pair_mode=PAIR_SEARCH_WITHIN_WIDTH,
        pair_max_width=width,
        multiplier=DEFAULT_MULTIPLIER,
        quantity=DEFAULT_QUANTITY,
        fees=DEFAULT_FEES,
    )


def _tausender(number):
    """``45000`` -> ``"45 000"``: the notes are German prose, the grouping a space."""
    return f"{int(number):,}".replace(",", " ")


def possible_pairs(section, option_type=None):
    """The EXACT number of strike pairs of one section: ``n * (n - 1) // 2``.

    ``section`` is already one expiry of one underlying, so this is not an
    estimate of a search - it is the arithmetic of the chain, and it does not
    move when the enumeration is cut short. ``option_type`` narrows the count to
    the side the strategy trades: a call and a put of the same strike are two
    contracts and never one combination. Distinct strikes only - two quotes of
    the same strike are not a pair.

    ``None`` for fewer than two strikes: no pair exists there, and a 0 would
    read as "searched, nothing found" instead of "not searchable".
    """
    strikes = set()
    for quote in section or ():
        asset = getattr(quote, "asset", None)
        if option_type is not None and getattr(asset, "option_type", None) != option_type:
            continue
        strike = getattr(asset, "strike", None)
        if isinstance(strike, (int, float)):
            strikes.add(float(strike))
    count = len(strikes)
    if count < 2:
        return None
    return count * (count - 1) // 2


def _valid_pairs(number):
    """``1`` -> ``"1 gültiges Paar"``, ``5`` -> ``"5 gültige Paare"``.

    German inflects the adjective AND the noun, so "1 gültige Paare" is wrong
    twice over; the count itself is untouched.
    """
    if number == 1:
        return "1 gültiges Paar"
    return f"{_tausender(number)} gültige Paare"


def _enumerated_pairs(number):
    """``"von 1 aufgezählten Paar"`` / ``"von 5 aufgezählten Paaren"``."""
    if number == 1:
        return "1 aufgezählten Paar"
    return f"{_tausender(number)} aufgezählten Paaren"


def _possible_pairs_word(number):
    """``"1 mögliches Paar"`` / ``"45 möglich"`` / ``"?"`` - one number, one number word."""
    if number is None:
        return "? möglich"
    if number == 1:
        return "1 mögliches Paar"
    return f"{_tausender(number)} möglich"


def _capped_counts(text, checked, coverage, section, strategy):
    """``text`` with BOTH pair counts, naming the ceiling when the search hit it.

    ``checked`` is what survived the screener's validity rules, ``enumerated``
    what the search really enumerated - two different numbers, and "N Paare
    geprüft" let one stand for the other: a sentence reading "5 Paare geprüft"
    beside ``pairs_enumerated: 45`` says the search only SAW five pairs.

    A capped enumeration stops COUNTING as well as collecting, so the section's
    exact combination count must stand beside the ceiling: "Suche am Deckel von
    L - M möglich". Without a run there is no enumerated count to name, so the
    sentence stays at "N gültige Paare" instead of inventing one; without a
    section there is no possible count and it stays "?".

    Every count is read in the number it is: one pair is "1 gültiges Paar von
    1 aufgezählten Paar", not the plural of a count that happens to be one.
    """
    report = coverage or {}
    enumerated = report.get("enumerated")
    if not isinstance(enumerated, int) or isinstance(enumerated, bool):
        return f"{text} ({_valid_pairs(checked)})"
    if not report.get("capped"):
        return f"{text} ({_valid_pairs(checked)} von {_enumerated_pairs(enumerated)})"
    possible = possible_pairs(section, strategy_option_type(strategy))
    possible_word = _possible_pairs_word(possible)
    limit = report.get("limit")
    deckel = "?" if not isinstance(limit, int) else _tausender(limit)
    return (f"{text} ({_valid_pairs(checked)} von {_enumerated_pairs(enumerated)}, "
            f"Suche am Deckel von {deckel} - {possible_word})")


def liquidity_of(pair, min_oi=DEFAULT_MIN_OI):
    """``"full"`` when both legs are deep and tight, else ``"thin"``.

    Both legs must reach ``FULL_MIN_OI`` open interest AND carry a bid-ask of at
    most ``FULL_MAX_SPREAD_PCT`` percent of their mid (with a mid above zero,
    which is what makes the percentage meaningful). Anything less - including a
    leg whose spread cannot be measured - is ``thin``: the label may not claim
    more than the data shows.
    """
    for leg in (pair.long, pair.short):
        interest = getattr(leg, "open_interest", None)
        spread = bid_ask_pct(leg)
        if not isinstance(interest, (int, float)) or interest < FULL_MIN_OI:
            return LIQUIDITY_THIN
        # Rounded to six decimals before the comparison: (1.05 - 0.95) / 1.00
        # is 10.000000000000009 in binary floating point, and a bound that a
        # float artifact can miss is not a bound.
        if spread is None or round(spread, 6) > FULL_MAX_SPREAD_PCT:
            return LIQUIDITY_THIN
    return LIQUIDITY_FULL


def legs_liquid(pair, min_oi=DEFAULT_MIN_OI):
    """``(ok, message)`` - the CANDIDATE hurdle: open interest per leg."""
    for name, leg in (("long", pair.long), ("short", pair.short)):
        interest = getattr(leg, "open_interest", None)
        if interest is None:
            return False, f"Open Interest des {name}-Legs fehlt"
        if interest < min_oi:
            return False, f"Open Interest des {name}-Legs {int(interest)} < {int(min_oi)}"
    return True, None


def spread_candidates(section, strategy, *, now_fn=None):
    """``(kept, criteria, sample_drop, coverage)`` for one expiry and one strategy.

    ``vertical_spreads`` owns the pair rules (both legs quoted, debit inside
    (0, width), the legs' own filters), so this function adds nothing but the
    strategy and the ordering. ``sample_drop`` is ONE of the screener's own drop
    sentences when it kept nothing - "OI 40 < 100" explains an empty section
    far better than "no pair", and it is the pipeline's wording, not a second
    opinion about the same chain.

    ``coverage`` is the screener's report of the search that really happened
    (``mode``, ``possible``, ``enumerated``, ``capped``, ``limit``), taken over
    unchanged: the ceiling is the only thing that can cut this search short, and
    a run that hit it must be able to say so. An empty dict means "no search
    happened" (no section, or a single strike) - the caller reports ``None`` for
    those numbers instead of a zero that would look like a measurement.
    """
    criteria = criteria_for(section)
    if not section or criteria.pair_max_width is None:
        return [], criteria, None, {}
    coverage = {}
    kept, dropped = vertical_spreads(section, criteria, strategy=strategy, now_fn=now_fn,
                                     coverage=coverage)
    kept = [pair for pair in kept if pair.net_return_pct(criteria) is not None]
    kept.sort(key=lambda pair: (-pair.net_return_pct(criteria), pair.long.asset.strike,
                                pair.short.asset.strike))
    return kept, criteria, (None if kept or not dropped else str(dropped[0][1])), coverage


def best_spread(section, strategy, *, now_fn=None, rr_min=DEFAULT_RR_MIN,
                min_long_delta=DEFAULT_MIN_LONG_DELTA,
                min_net_debit=DEFAULT_MIN_NET_DEBIT):
    """``(pair, criteria, note, coverage)`` - the best qualifying pair, or ``(None, ...)``.

    "Best" is the highest ``net_return_pct`` (i.e. the highest reward/risk) among
    the pairs that clear ALL three hurdles: the ratio itself, the |delta| of the
    bought leg and the minimum debit. The three are checked in that order so the
    note names the hurdle that mattered when nothing qualifies.

    ``checked`` in that note is ALWAYS the number of valid pairs (``len(kept)``)
    - the pairs the screener's own rules kept - never the size of a stage. How
    many pairs cleared the ratio (or the ratio and the delta) is a different
    number and stands INSIDE the sentence ("R/R erfüllt (3 von 10), aber ..."),
    so the label "N gültige Paare" keeps meaning what it says on every path.

    ``coverage`` is the search's own report, passed through from
    ``spread_candidates`` on every path - including the ones where nothing
    qualified: a capped search is capped whether or not it produced a winner.
    """
    kept, criteria, sample_drop, coverage = spread_candidates(section, strategy, now_fn=now_fn)
    if not kept:
        if not section:
            return None, criteria, "keine Kontrakte in diesem Verfall", coverage
        if criteria.pair_max_width is None:
            return None, criteria, "nur ein Strike gelistet - kein Strike-Paar möglich", coverage
        return None, criteria, ("kein gültiges Strike-Paar im Screening"
                                + (f" (z. B. {sample_drop})" if sample_drop else "")), coverage

    ratios = 0
    deltas = 0
    debits = 0
    for pair in kept:
        ratio = pair.net_return_pct(criteria)
        if ratio is None or ratio < rr_min * 100:
            continue
        ratios += 1
        delta = getattr(pair.long, "delta", None)
        if delta is None or abs(float(delta)) < min_long_delta:
            continue
        deltas += 1
        if pair.net_debit is None or pair.net_debit < min_net_debit:
            continue
        debits += 1
        return pair, criteria, None, coverage
    if not ratios:
        return None, criteria, _capped_counts(
            f"kein Paar mit R/R >= {rr_min:.1f}", len(kept), coverage, section,
            strategy), coverage
    if not deltas:
        return None, criteria, _capped_counts(
            f"R/R erfüllt ({ratios} von {len(kept)}), aber |Delta| des Long-Legs "
            f"< {min_long_delta:.0f}", len(kept), coverage, section, strategy), coverage
    return None, criteria, _capped_counts(
        f"R/R und Delta erfüllt ({deltas} von {len(kept)}), aber Netto-Debit "
        f"< {min_net_debit:.2f} USD", len(kept), coverage, section, strategy), coverage


def spread_row(candidate, pair, criteria, expiration, day, coverage=None, section=None):
    """One result row of the agreed contract - numbers rounded, nothing invented.

    ``coverage`` is the pair search's report (``spread_candidates``) and
    ``section`` the contracts that were searched. ``pairs_possible`` is the
    section's EXACT combination count (``possible_pairs``: ``n*(n-1)//2``) - it
    is the chain's arithmetic, not the size of the prefix the ceiling let
    through. The screener's own count stands beside it as
    ``pairs_possible_measured`` with ``pairs_possible_exact``: that number is a
    lower bound the moment the search was capped, and the row says which it is.
    Without a section (no chain, one strike) all of them stay ``None`` - a zero
    would read as "measured, nothing found", the opposite of "not measured".
    """
    ratio = pair.net_return_pct(criteria)
    expiry = _as_date(expiration)
    scanned = _as_date(day)
    long_delta = getattr(pair.long, "delta", None)
    short_delta = getattr(pair.short, "delta", None)
    possible = possible_pairs(section, strategy_option_type(pair.strategy_id))
    # No section or a single strike is no search at all: nothing is measured,
    # so every pair number is ``None`` rather than a zero that would look one.
    report = {} if possible is None else dict(coverage or {})
    return {
        "symbol": candidate.get("symbol"),
        "name": candidate.get("name"),
        "gap_pct": _round2(candidate.get("gap_pct")),
        "direction": candidate.get("direction"),
        "strategy": pair.strategy_id,
        "strategy_label": pair.strategy_label,
        "close": _round2(candidate.get("close")),
        "turnover_usd": _round2(candidate.get("turnover_usd")),
        "earnings_date": candidate.get("earnings_date"),
        "next_earnings_date": None,        # filled by the caller, which knows it
        "expiration": expiry.isoformat() if expiry else None,
        # Calendar days from the scanned session, not from "now": the answer
        # describes that day's trade, and a second request for the same day has
        # to answer the same way.
        "dte": (expiry - scanned).days if (expiry and scanned) else None,
        "long_strike": _round2(getattr(pair.long.asset, "strike", None)),
        "short_strike": _round2(getattr(pair.short.asset, "strike", None)),
        "long_delta": _round2(long_delta),
        "short_delta": _round2(short_delta),
        "net_debit": _round2(pair.net_debit),
        "max_loss": _round2(pair.max_loss),
        "max_profit": _round2(pair.max_profit),
        # R/R as the decimal the contract asks for, from the SAME percentage the
        # screener computed - one ratio, one rounding.
        "rr": None if ratio is None else _round2(ratio / 100.0),
        "net_return_pct": _round2(ratio),
        "liquidity": liquidity_of(pair),
        "open_interest": [getattr(pair.long, "open_interest", None),
                          getattr(pair.short, "open_interest", None)],
        "bid_ask_pct": [_round2(bid_ask_pct(pair.long)),
                        _round2(bid_ask_pct(pair.short))],
        "position_debit": _round2(pair.position_debit(criteria)),
        # The pair search's own report: the ceiling is real (50 000 pairs per
        # section) and a search that hit it did NOT see every combination. The
        # row carries the numbers so the answer cannot present a cut search as a
        # complete one - "capped" is the only reading of a cut that is honest.
        # ``pairs_possible`` is the EXACT count of the section (n*(n-1)//2, see
        # possible_pairs) and stays true whether or not the search was cut; the
        # screener's own count is kept beside it, NAMED as what it is - a
        # measurement that stops counting at the ceiling (``possible_exact``).
        "pairs_possible": possible,
        "pairs_possible_measured": report.get("possible"),
        "pairs_possible_exact": report.get("possible_exact"),
        "pairs_enumerated": report.get("enumerated"),
        "pairs_limit": report.get("limit"),
        "pairs_capped": report.get("capped"),
    }


@dataclass(frozen=True)
class Exclusion:
    """One title that did not become a row, with the step that stopped it.

    ``pairs_*`` describe the pair search behind a ``no_spread_ge_rr``: a title
    whose search hit the ceiling did not see every combination, so "no pair with
    R/R >= 2" is a statement about the prefix that was built, not about the
    expiry. The three fields are additive - a reason that never searched a
    section keeps ``pairs_capped=False`` and both numbers ``None``.
    """

    symbol: str
    reason: str
    message: str
    pairs_capped: bool = False
    pairs_enumerated: int | None = None
    pairs_possible: int | None = None

    def as_dict(self):
        return {"symbol": self.symbol, "reason": self.reason, "message": self.message,
                "pairs_capped": self.pairs_capped,
                "pairs_enumerated": self.pairs_enumerated,
                "pairs_possible": self.pairs_possible}


@dataclass(frozen=True)
class Failure:
    """One title whose question could not be answered at all (a source failed)."""

    symbol: str
    code: str
    message: str

    def as_dict(self):
        return {"symbol": self.symbol, "code": self.code, "message": self.message}


@dataclass
class FunnelRun:
    """The whole funnel: rows, exclusions, failures and the stage counters.

    ``results``, ``excluded`` and ``failures`` are disjoint and cover every
    candidate that went in - a title is either traded, explained or reported as
    unanswerable, never silently missing.

    ``notes`` carries what the counters cannot: a sentence about the run as a
    whole (a capped pair search, for instance). It stays separate from the rows
    because it is a statement about the answer, not about one title.
    """

    results: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def _classify_chain_error(exc):
    """``(reason, code, message)`` for a failed chain call.

    An unlisted symbol or an unusable chain is a fact about the TITLE
    (``not_optionable``); everything else - a block, a throttle, a timeout, a
    malformed payload - is an outage and goes to ``failures`` with its class.
    """
    reason = getattr(exc, "reason", None)
    if reason in _CHAIN_NO_OPTION:
        return (REASON_NOT_OPTIONABLE, None,
                "Die Quelle listet keine handelbare Optionskette für diesen Titel.")
    detail = f" ({reason})" if isinstance(reason, str) and reason else ""
    return None, _CODE_CHAIN_FAILED, f"Die Optionskette konnte nicht gelesen werden{detail}."


def _assess(candidate, *, fetch_next_earnings, fetch_chain, day, now_fn,
            rr_min, min_oi, min_long_delta, min_net_debit):
    """One candidate through the funnel -> ``(row, exclusion, failure, stages)``.

    Runs inside a worker, so it never raises: every failure that leaves it is a
    classified object, because one broken title must not cost the other 29.
    """
    symbol = str(candidate.get("symbol") or "").strip().upper()
    stages = {"next_earnings": False, "chain": False, "expiry": False, "spread": False}
    if not symbol:
        return None, Exclusion("", REASON_NO_NEXT_EARNINGS,
                               "Ein Kandidat ohne Symbol hat keinen Berichtstermin."), None, stages

    strategy = STRATEGY_BY_DIRECTION.get(candidate.get("direction"))
    if strategy is None:
        return None, Exclusion(symbol, REASON_NO_SPREAD,
                               "Der Gap hat keine Richtung - keine Debit-Strategie."), None, stages

    # 1) the next report date
    try:
        next_earnings = fetch_next_earnings(symbol)
    except NextEarningsError as exc:
        if exc.code == CODE_NO_API_KEY:
            # A missing key is a configuration fact, and the contract names it as
            # "no next earnings" - not as a failure of this title.
            return None, Exclusion(symbol, REASON_NO_NEXT_EARNINGS, exc.message), None, stages
        return None, None, Failure(symbol, exc.code, exc.message), stages
    except Exception as exc:                    # noqa: BLE001 - boundary by design
        return None, None, Failure(symbol, "next_earnings_failed",
                                   f"Der Berichtstermin konnte nicht gelesen werden "
                                   f"({type(exc).__name__})."), stages
    if next_earnings is None:
        return None, Exclusion(symbol, REASON_NO_NEXT_EARNINGS,
                               "Die Quelle nennt für diesen Titel keinen "
                               "Berichtstermin im Fenster."), None, stages
    stages["next_earnings"] = True

    # 2) the chain - one request per symbol, all expiries in it
    try:
        chain = fetch_chain(symbol) or []
    except Exception as exc:                    # noqa: BLE001 - boundary by design
        reason, code, message = _classify_chain_error(exc)
        if reason is None:
            return None, None, Failure(symbol, code, message), stages
        return None, Exclusion(symbol, reason, message), None, stages
    if not chain:
        return None, Exclusion(symbol, REASON_NOT_OPTIONABLE,
                               "Die Kette dieses Titels enthält keinen Kontrakt."), None, stages
    stages["chain"] = True

    # 3) the first listed expiry strictly after the report
    expiration = pick_expiration(expirations_in(chain), next_earnings)
    if expiration is None:
        return None, Exclusion(
            symbol, REASON_NO_EXPIRY,
            f"Kein gelisteter Verfall liegt nach dem Berichtstermin "
            f"{next_earnings.isoformat()}."), None, stages
    stages["expiry"] = True

    # 4) the best debit spread of that expiry
    section = slice_chain(chain, expiration)
    pair, criteria, note, coverage = best_spread(
        section, strategy, now_fn=now_fn, rr_min=rr_min,
        min_long_delta=min_long_delta, min_net_debit=min_net_debit)
    if pair is not None:
        liquid, liquid_note = legs_liquid(pair, min_oi)
        if not liquid:
            pair, note = None, liquid_note
    if pair is None:
        # The search behind this exclusion may have been cut: the reason keeps
        # that fact (and the exact size of the section) instead of letting the
        # message read like a full search that found nothing.
        report = coverage or {}
        return None, Exclusion(
            symbol, REASON_NO_SPREAD, f"Verfall {expiration.isoformat()}: {note}",
            pairs_capped=bool(report.get("capped")),
            pairs_enumerated=report.get("enumerated"),
            pairs_possible=possible_pairs(section, strategy_option_type(strategy)),
        ), None, stages
    stages["spread"] = True
    row = spread_row(candidate, pair, criteria, expiration, day, coverage=coverage,
                     section=section)
    row["next_earnings_date"] = next_earnings.isoformat()
    return row, None, None, stages


def cap_note(rows, excluded=None):
    """The sentence a capped pair search has to be named by, or ``None``.

    The ceiling (``MAX_PAIRS_PER_SECTION``) cuts the enumeration at a fixed
    point, so on such a title the shown R/R is the best pair that was FOUND, not
    the best that exists - the pairs further down the enumeration were never
    built. Saying that once, for the whole answer, is the difference between a
    limitation and a silent one.

    Both sides count, because both are the ceiling's doing: a capped ROW shows
    the best pair found, and a capped EXCLUSION means "kein Paar mit R/R >= 2"
    was decided on a prefix too. The number in brackets is the ceiling the rows
    actually ran against, not a constant this module happens to know - and when
    only exclusions were capped there is no row to read it from, so the
    sentence names the ceiling without inventing a number.
    """
    capped = [row for row in rows or () if row.get("pairs_capped")]
    capped_excluded = [entry for entry in excluded or ()
                       if getattr(entry, "pairs_capped", False)]
    count = len(capped) + len(capped_excluded)
    if not count:
        return None
    limits = sorted({row.get("pairs_limit") for row in capped
                     if isinstance(row.get("pairs_limit"), int)})
    deckel = f" ({_tausender(limits[0])})" if limits else ""
    titel = f"{count} Titel" if count == 1 else f"{count} Titeln"
    return (f"Bei {titel} war die Paarsuche am Deckel{deckel} - dort ist das "
            f"gezeigte R/R das beste GEFUNDENE Paar, und \"kein Paar\" heißt "
            f"\"keins im geprüften Teil\"; ein besseres Paar weiter hinten in "
            f"der Aufzählung wurde nicht geprüft.")


def run_funnel(candidates, *, fetch_next_earnings, fetch_chain, day, now_fn=None,
               max_workers=DEFAULT_MAX_WORKERS, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
               rr_min=DEFAULT_RR_MIN, min_oi=DEFAULT_MIN_OI,
               min_long_delta=DEFAULT_MIN_LONG_DELTA,
               min_net_debit=DEFAULT_MIN_NET_DEBIT):
    """Run every candidate through the funnel - in parallel, bounded, explained.

    ``fetch_next_earnings(symbol)`` answers a ``date`` or ``None`` and may raise
    ``NextEarningsError``; ``fetch_chain(symbol)`` answers the whole chain of one
    underlying and may raise (the class is read from ``exc.reason``).

    ``deadline_seconds`` bounds the WHOLE run. Every candidate is still asked -
    only the waiting ends - and a title that has not finished by then is named
    ``deadline_exceeded`` in ``failures``. Work that had not STARTED by then is
    cancelled, so the run does not go on fetching for titles it no longer
    reports; a request already in flight finishes in the background. Nothing is
    raised for a single title: a symbol is either a row, an exclusion or a
    failure. ``notes`` adds what the counters cannot say - a capped pair search
    is named there, because its rows show the best pair FOUND, not the best.
    """
    rows = list(candidates or ())
    counts = {"with_next_earnings": 0, "with_chain": 0, "with_expiry_after_earnings": 0,
              "with_spread_ge_rr": 0, "shown": 0}
    run = FunnelRun(counts=counts)
    if not rows:
        return run

    budget = None if deadline_seconds is None else max(0.0, float(deadline_seconds))
    pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
    pairs = []
    finished = set()
    try:
        pairs = [(row, pool.submit(
            _assess, row,
            fetch_next_earnings=fetch_next_earnings, fetch_chain=fetch_chain,
            day=day, now_fn=now_fn, rr_min=rr_min, min_oi=min_oi,
            min_long_delta=min_long_delta, min_net_debit=min_net_debit))
            for row in rows]
        finished, open_futures = wait([future for _row, future in pairs], timeout=budget)
        # Cancelling BEFORE the shutdown is the deadline: a worker that never
        # started would otherwise be handed the next queued title after the
        # answer went out, and the run would keep asking Finnhub and the chain
        # for titles it already reported as ``deadline_exceeded``. Only a future
        # that has not started can be cancelled - one already RUNNING keeps its
        # single in-flight request (cancelling a running call is not a thing),
        # which is exactly what the deadline accepts.
        for future in open_futures:
            future.cancel()
    finally:
        # Not waiting on an expired deadline is the deadline: the unanswered
        # titles are already accounted for below, and a chain that was already
        # in flight finishes in the background rather than holding the request.
        pool.shutdown(wait=budget is None)

    for candidate, future in pairs:
        symbol = str(candidate.get("symbol") or "").strip().upper()
        if future not in finished:
            run.failures.append(Failure(symbol, CODE_DEADLINE,
                                        "Die Prüfung endete nicht innerhalb des "
                                        "Zeitbudgets."))
            continue
        try:
            row, exclusion, failure, stages = future.result()
        except Exception as exc:                # noqa: BLE001 - a bug is not a 500
            run.failures.append(Failure(symbol, "funnel_error",
                                        f"Die Prüfung ist gescheitert "
                                        f"({type(exc).__name__})."))
            continue
        if stages.get("next_earnings"):
            counts["with_next_earnings"] += 1
        if stages.get("chain"):
            counts["with_chain"] += 1
        if stages.get("expiry"):
            counts["with_expiry_after_earnings"] += 1
        if stages.get("spread"):
            counts["with_spread_ge_rr"] += 1
        if row is not None:
            run.results.append(row)
        elif exclusion is not None:
            run.excluded.append(exclusion)
        elif failure is not None:
            run.failures.append(failure)

    # Sorted by the SIZE of the move, biggest first - the order the gap scan
    # already ranks by, kept here so two runs of the same day cannot disagree.
    run.results.sort(key=lambda entry: (-abs(entry.get("gap_pct") or 0.0),
                                        str(entry.get("symbol") or "")))
    counts["shown"] = len(run.results)
    # One sentence for the whole answer when a search hit the ceiling: every
    # R/R on such a row is the best pair FOUND, not the best that exists, and a
    # "no pair" decided on such a cut is equally provisional.
    gekappt = cap_note(run.results, run.excluded)
    if gekappt:
        run.notes.append(gekappt)
    return run
