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
5. can the underlying even GET to the break-even - and how often did it manage
   that distance before (see "Reachability instead of a ratio ceiling")?

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

Reachability instead of a ratio ceiling
---------------------------------------
A reward/risk ratio says what a move would PAY, not whether the title ever
moves that far: measured on 2026-11-11, CRWV carried R/R 1:34 for a move of
61 %, LAND 1:7.3 for a move of 10 %. A ratio ceiling would delete the better
trade. So the funnel keeps the ratio floor (no ceiling) and adds an upper bound
on the DISTANCE to break-even (``max_required_move_pct``, default 30 %), plus a
measured frequency per row: how often this title covered that distance within
the spread's remaining life before - ``dte`` calendar days, converted to trading
sessions in ``reachability.measure`` (``reachability.py``). A row that has to
move further is excluded with ``move_too_far``.

The alternatives are characters, not a ladder
---------------------------------------------
Beside the pick a row carries up to three OTHER structures of the same expiry,
each named for what it is FOR: ``nah am Geld`` (the bought strike closest to the
close), ``am Widerstand`` (the bought strike closest to the next level above the
price - 52-week high, MA20/50/200 - and nothing at all when the price is at its
high) and ``engster Spread`` (the smallest WORSE leg spread). The pick itself is
the ``Hebel`` and is never among them: a list that opens with the trade one
already has compares nothing.

A character is a filter, not a decoration. Every alternative clears the same
three hurdles as the row AND the same ``max_required_move_pct``: a structure the
underlying cannot reach is the same trap with a friendlier name. Two characters
that land on one strike pair yield ONE variant - the first keeps it, the second
is dropped rather than re-pointed at something else. And each variant's
``hit_rate_pct`` is measured with ``reachability.measure`` on the bars of the
row, with the variant's own distance - which is why the variant context travels
from ``_assess`` into the history phase instead of a second history request.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime

from . import hand_calendar as hand_calendar_source
from . import reachability, texts
from .gap_scan import GAP_DOWN, GAP_UP, STRATEGY_BEAR_PUT_DEBIT, STRATEGY_BULL_CALL_DEBIT
from .next_earnings import CODE_NO_API_KEY, NextEarningsError
from .quote_age import MODE_LAST_AVAILABLE
from .screener import (
    PAIR_SEARCH_WITHIN_WIDTH,
    ScreenerCriteria,
    strategy_option_type,
    vertical_spreads,
)
from .texts import t

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
#: How far the underlying may have to travel to the spread's break-even, in
#: percent of the reaction day's close. An UPPER bound on the distance - the
#: reward/risk ratio has none (see the module docstring).
DEFAULT_MAX_REQUIRED_MOVE_PCT = 30.0
#: The whole history phase's budget, in seconds. Only rows that already have a
#: spread cost a request, and a title that has not answered by then keeps its
#: row with ``history_state=source_failed`` - it never holds the answer.
DEFAULT_HISTORY_DEADLINE_SECONDS = 60.0
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

#: The characters an alternative structure can have, in the order the page
#: shows them. The row itself is the HEBEL and is NOT one of them: a list that
#: offers the pick as its own first alternative compares nothing.
#: Die drei Charaktere einer Zeile tragen eine ID und einen Satz. Die ID ist die
#: Identitaet: sie bleibt in jeder Sprache gleich, waehrend der Satz aus dem Katalog
#: kommt. Vorher WAR der deutsche Satz die Identitaet - mit einem englischen Buch
#: waere damit jede Zuordnung auseinandergefallen.
VARIANT_NEAR_MONEY = "near_money"
VARIANT_AT_RESISTANCE = "at_resistance"
VARIANT_TIGHTEST_SPREAD = "tightest_spread"
VARIANT_IDS = (VARIANT_NEAR_MONEY, VARIANT_AT_RESISTANCE, VARIANT_TIGHTEST_SPREAD)

#: Der Charakter, der die ZEILE selbst ist - kein eigener Satz im Umlauf.
ROW_PICK_ID = "row_pick"


def variant_label(variant_id: str) -> str:
    """Der Anzeigetext eines Charakters - aus dem Katalog, zur Anfragezeit.

    Die ID wiederholt den Katalogschluessel, damit ID und Satz nicht auseinander
    laufen koennen.
    """
    return t(f"funnel.variant.{variant_id}")
#: How the detail names the row's own structure - the character of the pick.

#: How many alternatives a row may carry: three characters, not a menu.
DEFAULT_VARIANT_COUNT = len(VARIANT_IDS)
#: The 52 weeks the resistance level is called after, in SESSIONS: a year of
#: trading is ~252 sessions, and counting calendar days would let a holiday
#: week move the high.
RESISTANCE_HIGH_SESSIONS = 252
#: The moving averages that count as resistance, in sessions.
RESISTANCE_MA_WINDOWS = (20, 50, 200)

LIQUIDITY_THIN = "thin"
LIQUIDITY_FULL = "full"
LIQUIDITIES = (LIQUIDITY_THIN, LIQUIDITY_FULL)

#: The exclusion reasons of the agreed contract.
REASON_NO_NEXT_EARNINGS = "no_next_earnings"
REASON_NO_EXPIRY = "no_expiry_after_earnings"
REASON_NO_SPREAD = "no_spread_ge_rr"
REASON_NOT_OPTIONABLE = "not_optionable"
#: The spread exists, but the underlying would have to travel further than
#: ``max_required_move_pct`` to reach its break-even.
REASON_MOVE_TOO_FAR = "move_too_far"
EXCLUSION_REASONS = (REASON_NO_NEXT_EARNINGS, REASON_NO_EXPIRY, REASON_NO_SPREAD,
                     REASON_NOT_OPTIONABLE, REASON_MOVE_TOO_FAR)

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


def chance_of(hit_rate_pct, rr):
    """``chance`` = Trefferquote (as a fraction) times R/R, two decimals.

    A reading aid, not a second measurement: it multiplies the row's own hit
    rate by the row's own reward/risk and rounds the product once. Because the
    hit rate counts the windows that REACHED break-even - the point where the
    trade merely breaks even - the product is an upper bound of the expected
    value per unit of risk, never a probability of profit.

    Either input missing (``None``: no history, a failed source, an unusable
    ratio) makes the chance ``None``. A 0 would be a measurement ("this never
    happens") and is not what a missing number means, so it is never used as a
    stand-in.
    """
    rate = hit_rate_pct
    ratio = rr
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return None
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return None
    return _round2((float(rate) / 100.0) * float(ratio))


def _zahl(value):
    """``20.0`` -> ``"20"``, ``61.25`` -> ``"61,25"`` (German) - no dead zeros.

    The sentence a title is excluded by names real numbers, so a cap of 20 must
    read "20 %" and not "20,0 %", while 12.5 keeps its decimal.

    The SEPARATOR belongs to the sentence, not to the number: a German sentence
    says "12,5 %", an English one "12.5 %". Only the rendering of the number in
    prose follows the language - the payload keeps its own floats, and the CSV
    format is untouched.
    """
    text = f"{abs(float(value)):.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if texts.active_language() == "de" else text


def move_too_far_message(required_move_pct, max_required_move_pct):
    """The sentence of a ``move_too_far`` exclusion, with both numbers."""
    return t(
        "funnel.move.too_far",
        move=_zahl(required_move_pct),
        direction=t("funnel.move.up" if required_move_pct > 0 else "funnel.move.down"),
        limit=_zahl(max_required_move_pct),
    )


def required_move_of(pair, close):
    """The distance from ``close`` to the pair's break-even, in percent.

    The break-even is the spread's own (``pair.breakeven``: bought strike plus
    the debit for a call, minus it for a put), so the number follows the trade
    that is priced instead of being re-derived here. ``None`` when either side
    is unusable - the row then carries no distance and the cap cannot judge it.
    """
    return reachability.required_move_pct(getattr(pair, "breakeven", None), close)


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
    """``45000`` -> ``"45 000"`` in German prose, ``"45,000"`` in English.

    Same rule as ``_zahl``: the grouping belongs to the sentence. A translator
    reading "45 000 valid pairs" in an English note would call it a bug - and
    would be right.
    """
    gruppiert = f"{int(number):,}"
    return gruppiert.replace(",", " ") if texts.active_language() == "de" else gruppiert


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
    return t("funnel.pairs.valid", n=number, count=_tausender(number))


def _enumerated_pairs(number):
    """``"von 1 aufgezählten Paar"`` / ``"von 5 aufgezählten Paaren"``."""
    return t("funnel.pairs.enumerated", n=number, count=_tausender(number))


def _possible_pairs_word(number):
    """``"1 mögliches Paar"`` / ``"45 möglich"`` / ``"?"`` - one number, one number word."""
    if number is None:
        return t("funnel.pairs.possible_unknown")
    return t("funnel.pairs.possible", n=number, count=_tausender(number))


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
        return "{} ({})".format(
        text,
        t("funnel.pairs.of", valid=_valid_pairs(checked),
          enumerated=_enumerated_pairs(enumerated)),
    )
    possible = possible_pairs(section, strategy_option_type(strategy))
    possible_word = _possible_pairs_word(possible)
    limit = report.get("limit")
    deckel = "?" if not isinstance(limit, int) else _tausender(limit)
    return t(
        "funnel.pairs.capped",
        text=text,
        valid=_valid_pairs(checked),
        enumerated=_enumerated_pairs(enumerated),
        limit=deckel,
        possible=possible_word,
    )


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
            return False, t("funnel.oi.missing", leg=name)
        if interest < min_oi:
            return False, t("funnel.oi.below", leg=name,
                            value=int(interest), minimum=int(min_oi))
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


def _enumerate(section, strategy, *, now_fn=None):
    """``(kept, criteria, sample_drop, coverage)`` - ONE enumeration of the section.

    The first half of ``best_spread``, standing on its own because a row needs
    its pairs TWICE: once to pick the spread, once to name the alternatives
    (``variant_characters``). Handing the SAME list to the second step
    (``_select``/``qualifying_pairs``) is what keeps a row at one enumeration -
    ``spread_candidates`` builds every strike pair of the section, up to
    ``MAX_PAIRS_PER_SECTION``, and running it twice per row doubles that work
    inside the deadline-bounded workers for no new information.
    """
    return spread_candidates(section, strategy, now_fn=now_fn)


def _select(kept, criteria, *, rr_min=DEFAULT_RR_MIN,
            min_long_delta=DEFAULT_MIN_LONG_DELTA,
            min_net_debit=DEFAULT_MIN_NET_DEBIT, section=None, sample_drop=None,
            coverage=None, strategy=None):
    """``(pair, note, coverage)`` - the best pair of an ALREADY enumerated list.

    The second half of ``best_spread``: it checks the three hurdles against the
    order ``_enumerate`` produced and never searches the section again.
    ``section``/``sample_drop``/``strategy`` are only read to WORD the note when
    nothing qualifies (and to name the ceiling of a capped search) - they do not
    change which pair wins.
    """
    if not kept:
        if not section:
            return None, t("funnel.expiry.empty"), coverage
        if criteria.pair_max_width is None:
            return None, t("funnel.strike.single"), coverage
        return None, (t("funnel.strike.no_pair")
                      + (t("funnel.strike.sample", sample=sample_drop) if sample_drop else "")), coverage

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
        return pair, None, coverage
    if not ratios:
        return None, _capped_counts(
            t("funnel.rr.none", limit=f"{rr_min:.1f}"), len(kept), coverage, section,
            strategy), coverage
    if not deltas:
        return None, _capped_counts(
            t("funnel.rr.delta", ratios=ratios, checked=len(kept),
              limit=f"{min_long_delta:.0f}"),
            len(kept), coverage, section, strategy), coverage
    return None, _capped_counts(
        t("funnel.rr.debit", ratios=deltas, checked=len(kept),
          limit=f"{min_net_debit:.2f}"),
        len(kept), coverage, section, strategy), coverage


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

    The section is enumerated exactly ONCE (``_enumerate`` + ``_select``): a
    caller that also needs the pairs themselves uses those two halves instead of
    this wrapper, so the second need does not cost a second search.
    """
    kept, criteria, sample_drop, coverage = _enumerate(section, strategy, now_fn=now_fn)
    pair, note, coverage = _select(
        kept, criteria, rr_min=rr_min, min_long_delta=min_long_delta,
        min_net_debit=min_net_debit, section=section, sample_drop=sample_drop,
        coverage=coverage, strategy=strategy)
    return pair, criteria, note, coverage


def _strike_pair(pair):
    """The pair's identity: its two strikes, so it cannot be offered twice."""

    def one(side):
        asset = getattr(getattr(pair, side, None), "asset", None)
        strike = getattr(asset, "strike", None)
        return None if not isinstance(strike, (int, float)) else round(float(strike), 6)

    return (one("long"), one("short"))


def _long_strike(pair):
    """The bought leg's strike as a float, or ``None``."""
    asset = getattr(getattr(pair, "long", None), "asset", None)
    strike = getattr(asset, "strike", None)
    return None if not isinstance(strike, (int, float)) else float(strike)


def worse_leg_spread(pair):
    """The WIDER of the two legs' bid-ask percentages, or ``None``.

    A spread is only as tradable as its worse leg - the tight one cannot be
    sold without the wide one. ``bid_ask_pct`` already answers ``None`` for a
    leg without a readable two-sided market (and therefore without a positive
    mid), so such a pair carries no number here either: it drops out of the
    tightest-spread character instead of winning it with half a measurement.
    """
    long_pct = bid_ask_pct(pair.long)
    short_pct = bid_ask_pct(pair.short)
    if long_pct is None or short_pct is None:
        return None
    return max(long_pct, short_pct)


def resistance_levels(bars, close):
    """The levels ABOVE ``close`` the history holds, the nearest first.

    Four numbers, all of them read from the daily closes of the title (the
    same bars the reachability of the row is measured on): the 52-week high
    (the highest close of the last ``RESISTANCE_HIGH_SESSIONS`` sessions) and
    the 20/50/200-session moving averages. Only levels STRICTLY above the close
    count - a level the price already stands on is no resistance - and a
    window the history is too short for contributes nothing rather than a
    shorter average that would pretend to be the same line.

    An empty list is the honest answer for a title at its all-time high: there
    is no level above it, and the character is then omitted instead of being
    filled with the second-best idea.
    """
    price = _round2(close)
    if price is None or price <= 0:
        return []
    closes = reachability.closes_oldest_first(bars)
    if not closes:
        return []
    levels = []
    head = closes[-RESISTANCE_HIGH_SESSIONS:]
    if head:
        levels.append(max(head))
    for window in RESISTANCE_MA_WINDOWS:
        if len(closes) >= window:
            levels.append(sum(closes[-window:]) / window)
    strictly_above = [level for level in levels
                      if math.isfinite(level) and level > float(close)]
    return sorted(set(round(level, 6) for level in strictly_above))


def qualifying_pairs(section, strategy, *, close=None, now_fn=None,
                     rr_min=DEFAULT_RR_MIN, min_long_delta=DEFAULT_MIN_LONG_DELTA,
                     min_net_debit=DEFAULT_MIN_NET_DEBIT, kept=None, criteria=None):
    """Every pair of the expiry that clears the row's three hurdles, with its move.

    "Qualifying" means exactly what it means for the funnel's own pick - the
    reward/risk floor, the |delta| of the bought leg and the minimum debit, in
    that order. An alternative that would have been rejected as a first choice
    must not be offered as a second one, so the same three hurdles apply
    unchanged. Each entry carries ``pair``, ``ratio`` (the screener's
    percentage) and ``move`` (``required_move_of`` against ``close``, ``None``
    when the close or the break-even is unusable).

    ``kept``/``criteria`` are the row's OWN enumeration (``_enumerate``): when
    they are handed in, the section is NOT searched again - the hurdles are
    checked against the same list, in the same order, the pick was taken from.
    A row that needs both its spread and its alternatives therefore enumerates
    its strike pairs once, not twice.
    """
    if kept is None:
        kept, criteria, _sample_drop, _coverage = spread_candidates(
            section, strategy, now_fn=now_fn)
    elif criteria is None:
        criteria = criteria_for(section)
    entries = []
    for pair in kept:
        ratio = pair.net_return_pct(criteria)
        if ratio is None or ratio < rr_min * 100:
            continue
        delta = getattr(pair.long, "delta", None)
        if delta is None or abs(float(delta)) < min_long_delta:
            continue
        if pair.net_debit is None or pair.net_debit < min_net_debit:
            continue
        entries.append({"pair": pair, "ratio": ratio,
                        "move": required_move_of(pair, close)})
    return entries


def _variant_history(bars, move, dte):
    """``hit_rate_pct``/``windows_measured`` of one variant, from the row's bars.

    ``reachability.measure`` is the one recipe for this number, so it is called
    with the VARIANT's distance and the ROW's ``dte`` - the same window the row
    is measured over. Without bars there is nothing to measure and both fields
    stay ``None``: an unmeasured frequency is not 0 %.
    """
    if bars is None:
        return {"hit_rate_pct": None, "windows_measured": None}
    fields = reachability.measure(bars, move, dte)
    return {"hit_rate_pct": fields["hit_rate_pct"],
            "windows_measured": fields["windows_measured"]}


def variant_row(entry, variant_id, *, bars, dte):
    """One alternative in the numbers of the row - rounded, nothing invented."""
    pair = entry["pair"]
    ratio = entry["ratio"]
    move = entry.get("move")
    history = _variant_history(bars, move, dte)
    rr = None if ratio is None else _round2(ratio / 100.0)
    return {
        "id": variant_id,
        "label": variant_label(variant_id),
        "long_strike": _round2(getattr(pair.long.asset, "strike", None)),
        "short_strike": _round2(getattr(pair.short.asset, "strike", None)),
        "net_debit": _round2(pair.net_debit),
        "max_loss": _round2(pair.max_loss),
        "max_profit": _round2(pair.max_profit),
        "rr": rr,
        # The same reading aid the row carries, from the variant's OWN numbers:
        # its hit rate at its own distance times its own R/R.
        "chance": chance_of(history["hit_rate_pct"], rr),
        "required_move_pct": _round2(move),
        # The wider leg, not the pair's average: the spread one PAYS is the one
        # that has to be crossed.
        "bid_ask_pct": _round2(worse_leg_spread(pair)),
        **history,
        "liquidity": liquidity_of(pair),
        "open_interest": [getattr(pair.long, "open_interest", None),
                          getattr(pair.short, "open_interest", None)],
    }


def variant_characters(section, strategy, *, primary, close, bars=None, dte=None,
                       now_fn=None, rr_min=DEFAULT_RR_MIN,
                       min_long_delta=DEFAULT_MIN_LONG_DELTA,
                       min_net_debit=DEFAULT_MIN_NET_DEBIT,
                       max_required_move_pct=DEFAULT_MAX_REQUIRED_MOVE_PCT,
                       count=DEFAULT_VARIANT_COUNT, kept=None, criteria=None):
    """Up to ``count`` other structures of the SAME expiry, as named characters.

    The alternatives are not a ladder of ratios - they are three readings of the
    same chain, each answering a different question a reader can have:

    * ``NAH AM GELD`` - the bought strike closest to the close: the structure
      that moves first when the underlying does.
    * ``AM WIDERSTAND`` - the pair whose bought strike sits closest to the
      NEAREST level above the price (52-week high, MA20/50/200). That is the
      structure that pays when the reaction runs into the next wall. Without a
      level above the close - a title at its all-time high - the character is
      omitted; it is never filled with the second-best idea.
    * ``ENGSTER SPREAD`` - the smallest WORSE leg spread, i.e. the pair that
      costs the least to cross. A leg without a readable two-sided market
      cannot win this, because half a spread is not a tight one.

    Every candidate clears the row's three hurdles (``qualifying_pairs``) AND
    the row's reachability cap: a structure the underlying cannot reach is not
    an alternative, it is the same trap with another label. The row's own pair
    is removed first, and a pair claimed by an earlier character is not offered
    again - so two characters that point at the same strikes yield ONE variant,
    not a duplicate.

    Each variant carries the frequency of its own distance, measured on the
    SAME bars as the row (``reachability.measure``, the row's ``dte``). An empty
    list is a fact ("no other structure clears the same hurdles") - this
    function never answers ``None``: whether a ladder could be BUILT at all is
    the row's question and is answered where the row is filled
    (``measure_reachability``, ``variants`` ``null`` vs ``[]``).

    ``kept``/``criteria`` are passed straight to ``qualifying_pairs``: the run
    hands the row's OWN enumeration in, so naming the alternatives does not
    search the section a second time.
    """
    if primary is None or count <= 0 or close is None:
        return []
    entries = qualifying_pairs(
        section, strategy, close=close, now_fn=now_fn, rr_min=rr_min,
        min_long_delta=min_long_delta, min_net_debit=min_net_debit,
        kept=kept, criteria=criteria)

    taken = {_strike_pair(primary)}
    free = []
    for entry in entries:
        key = _strike_pair(entry["pair"])
        if key in taken:
            continue
        taken.add(key)
        move = entry.get("move")
        # A distance that cannot be computed cannot be checked against the cap,
        # and an unchecked cap is not passed.
        if move is None:
            continue
        if max_required_move_pct is not None and abs(move) > max_required_move_pct:
            continue
        free.append(entry)
    if not free:
        return []

    levels = resistance_levels(bars, close)

    def nearest_to(level):
        return min(free, key=lambda entry: (abs(_long_strike(entry["pair"]) - level),
                                            abs(entry["move"]), -entry["ratio"]))

    def tightest():
        priced = [entry for entry in free if worse_leg_spread(entry["pair"]) is not None]
        if not priced:
            return None
        return min(priced, key=lambda entry: (worse_leg_spread(entry["pair"]),
                                              -entry["ratio"]))

    candidates = [
        (VARIANT_NEAR_MONEY, nearest_to(close)),
        # No level above the close is no character - not a substitute.
        (VARIANT_AT_RESISTANCE, nearest_to(levels[0]) if levels else None),
        (VARIANT_TIGHTEST_SPREAD, tightest()),
    ]

    chosen = []
    claimed = set()
    for variant_id, entry in candidates:
        if entry is None or len(chosen) >= count:
            continue
        key = _strike_pair(entry["pair"])
        if key in claimed:
            # Dedupe: the FIRST character keeps the pair, the later one is
            # dropped - never re-pointed at a different pair.
            continue
        claimed.add(key)
        chosen.append((variant_id, entry))

    return [variant_row(entry, variant_id, bars=bars, dte=dte)
            for variant_id, entry in chosen]


def spread_row(candidate, pair, criteria, expiration, day, coverage=None, section=None,
               variants=None):
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
    move = required_move_of(pair, candidate.get("close"))
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
        # Chance = hit rate x R/R, the reading aid of the row (see
        # ``chance_of``). It is filled by the run once the row's OWN hit rate is
        # measured (``measure_reachability``) - a row built alone has no rate,
        # so it has no chance either, and never a 0 standing in for one.
        "chance": None,
        "net_return_pct": _round2(ratio),
        # How far the underlying has to travel to reach this spread's
        # break-even, in percent of the reaction day's close. The SIGN is the
        # direction (+ = has to rise); the cap reads ``abs()``. ``None`` when
        # the close or the break-even is unusable - never 0 as a stand-in.
        "required_move_pct": _round2(move),
        "required_move_direction": reachability.required_direction(_round2(move)),
        # The history numbers are filled by the run's own phase
        # (``measure_reachability``) - a row built alone carries ``None`` and
        # says so, because a 0 % hit rate is a measurement and this is not.
        **reachability.unmeasured(None),
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
        # The alternatives of the SAME expiry as named characters (see
        # ``variant_characters``). ``None`` and ``[]`` are two different facts:
        # ``None`` means NO ladder could be built (no close, no bars - nothing
        # was named), ``[]`` means built and empty ("no other structure clears
        # the same hurdles"). The UI shows no counter in either case; only the
        # contract tells them apart, and a row may not claim a check that never
        # happened.
        "variants": None if variants is None else list(variants),
    }


@dataclass(frozen=True)
class Exclusion:
    """One title that did not become a row, with the step that stopped it.

    ``pairs_*`` describe the pair search behind a ``no_spread_ge_rr``: a title
    whose search hit the ceiling did not see every combination, so "no pair with
    R/R >= 2" is a statement about the prefix that was built, not about the
    expiry. The three fields are additive - a reason that never searched a
    section keeps ``pairs_capped=False`` and both numbers ``None``.

    ``required_move_pct``/``max_required_move_pct`` belong to ``move_too_far``:
    the distance the title needed and the bound it broke. Both stay ``None`` on
    every other reason - those titles were never measured.
    """

    symbol: str
    reason: str
    message: str
    pairs_capped: bool = False
    pairs_enumerated: int | None = None
    pairs_possible: int | None = None
    required_move_pct: float | None = None
    max_required_move_pct: float | None = None
    #: The hand-calendar part of a ``no_next_earnings`` exclusion: the feed
    #: signal a human may read, or the state of an expired curated date. ``None``
    #: on every other reason AND on a title that fails the pre-selection (no
    #: options list, not optionable) - there is nothing to type in then.
    hand_calendar: dict | None = None

    def as_dict(self):
        return {"symbol": self.symbol, "reason": self.reason, "message": self.message,
                "pairs_capped": self.pairs_capped,
                "pairs_enumerated": self.pairs_enumerated,
                "pairs_possible": self.pairs_possible,
                "required_move_pct": self.required_move_pct,
                "max_required_move_pct": self.max_required_move_pct,
                "hand_calendar": self.hand_calendar}


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
        return REASON_NOT_OPTIONABLE, None, t("funnel.chain.not_listed")
    detail = f" ({reason})" if isinstance(reason, str) and reason else ""
    return None, _CODE_CHAIN_FAILED, t("funnel.chain.unreadable", detail=detail)


def _hand_calendar_block(candidate, curated, *, feed, optionable, day):
    """The hand-calendar part of one ``no_next_earnings`` exclusion, or ``None``.

    The pre-selection is the first word: without a loaded options list nothing
    is shown (an unloaded list must not look like "no options"), and a title
    that is not optionable gets the old text unchanged - a report date typed in
    by hand would only lead to the next stage failing.

    ``status`` is one of ``signal`` (the feed names an announcement), ``no_signal``
    (it does not) or ``expired`` (a curated date is behind us). The feed is read
    ONLY here, beside the curated lookup: a title the layer rejects costs no
    request.
    """
    if optionable is None:
        return None
    symbol = str(candidate.get("symbol") or "").strip().upper()
    if symbol not in optionable:
        return None
    signal = feed.signal(candidate.get("name")) if feed is not None else None
    block = {
        "status": ("expired" if curated is not None else
                   ("signal" if signal else "no_signal")),
        "title": (signal or {}).get("title"),
        "link": (signal or {}).get("link"),
        "curated_date": (curated or {}).get("curated_date"),
        "source": (curated or {}).get("source"),
        "notiert_am": (curated or {}).get("notiert_am"),
    }
    # An expired date without a signal is still worth showing: the badge says
    # what happened, the (missing) field says the feed cannot help this time.
    return block


def _assess(candidate, *, fetch_next_earnings, fetch_chain, day, now_fn,
            rr_min, min_oi, min_long_delta, min_net_debit, max_required_move_pct,
            hand_calendar=None, feed=None, optionable=None):
    """One candidate through the funnel -> ``(row, exclusion, failure, stages, context)``.

    Runs inside a worker, so it never raises: every failure that leaves it is a
    classified object, because one broken title must not cost the other 29.

    ``context`` is what the history phase needs to build the row's variants
    (``variant_characters``): the section, the chosen pair and the hurdles the
    alternatives have to clear too. It is returned instead of stashed on the row
    so the answer's shape stays exactly the documented row - and it is ``None``
    for a title that did not become a row, because a title without a spread has
    no alternatives either.
    """
    symbol = str(candidate.get("symbol") or "").strip().upper()
    stages = {"next_earnings": False, "chain": False, "expiry": False, "spread": False,
              "move": False}
    if not symbol:
        return None, Exclusion("", REASON_NO_NEXT_EARNINGS,
                               t("funnel.earnings.no_symbol")), None, stages, None

    strategy = STRATEGY_BY_DIRECTION.get(candidate.get("direction"))
    if strategy is None:
        return None, Exclusion(symbol, REASON_NO_SPREAD,
                               t("funnel.gap.no_direction")), None, stages, None

    # 1) the next report date
    earnings_source = "finnhub"
    try:
        next_earnings = fetch_next_earnings(symbol)
    except NextEarningsError as exc:
        if exc.code == CODE_NO_API_KEY:
            # A missing key is a configuration fact, and the contract names it as
            # "no next earnings" - not as a failure of this title.
            return None, Exclusion(symbol, REASON_NO_NEXT_EARNINGS, exc.message), None, stages, None
        return None, None, Failure(symbol, exc.code, exc.message), stages, None
    except Exception as exc:                    # noqa: BLE001 - boundary by design
        return None, None, Failure(symbol, "next_earnings_failed",
                                   t("funnel.earnings.unreadable",
                                     error=type(exc).__name__)), stages, None
    curated = None
    if next_earnings is None:
        # The gap the funnel cannot fill from the calendar: a human may have
        # filled it. Finnhub keeps the first word - a curated date is read ONLY
        # here, where the source answered "no date in the window".
        curated = (hand_calendar.entry_for(symbol, day)
                   if hand_calendar is not None else None)
        kuratiert = (hand_calendar_source.parse_date(curated["curated_date"])
                     if curated is not None
                     and curated["status"] == hand_calendar_source.STATUS_CURATED
                     else None)
        if kuratiert is None:
            return None, Exclusion(
                symbol, REASON_NO_NEXT_EARNINGS,
                t("funnel.earnings.none_in_window"),
                hand_calendar=_hand_calendar_block(
                    candidate, curated, feed=feed, optionable=optionable, day=day),
            ), None, stages, None
        next_earnings = kuratiert
        earnings_source = "manual_curated"
    stages["next_earnings"] = True

    # 2) the chain - one request per symbol, all expiries in it
    try:
        chain = fetch_chain(symbol) or []
    except Exception as exc:                    # noqa: BLE001 - boundary by design
        reason, code, message = _classify_chain_error(exc)
        if reason is None:
            return None, None, Failure(symbol, code, message), stages, None
        return None, Exclusion(symbol, reason, message), None, stages, None
    if not chain:
        return None, Exclusion(symbol, REASON_NOT_OPTIONABLE,
                               t("funnel.chain.empty")), None, stages, None
    stages["chain"] = True

    # 3) the first listed expiry strictly after the report
    expiration = pick_expiration(expirations_in(chain), next_earnings)
    if expiration is None:
        return None, Exclusion(
            symbol, REASON_NO_EXPIRY,
            t("funnel.expiry.none_after_earnings", date=next_earnings.isoformat()),
        ), None, stages, None
    stages["expiry"] = True

    # 4) the best debit spread of that expiry - ONE enumeration of the section,
    # whose pair list is kept in the context: the history phase names the row's
    # alternatives from the SAME list instead of searching the section again.
    section = slice_chain(chain, expiration)
    kept, criteria, sample_drop, coverage = _enumerate(section, strategy, now_fn=now_fn)
    pair, note, coverage = _select(
        kept, criteria, rr_min=rr_min, min_long_delta=min_long_delta,
        min_net_debit=min_net_debit, section=section, sample_drop=sample_drop,
        coverage=coverage, strategy=strategy)
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
            symbol, REASON_NO_SPREAD,
            t("funnel.expiry.line", date=expiration.isoformat(), note=note),
            pairs_capped=bool(report.get("capped")),
            pairs_enumerated=report.get("enumerated"),
            pairs_possible=possible_pairs(section, strategy_option_type(strategy)),
        ), None, stages, None
    stages["spread"] = True
    row = spread_row(candidate, pair, criteria, expiration, day, coverage=coverage,
                     section=section)
    row["next_earnings_date"] = next_earnings.isoformat()
    # Where the date came from, said out loud: a curated date is never dressed
    # as a source date. ``finnhub`` is the default and every other row keeps it.
    row["next_earnings_source"] = earnings_source
    if earnings_source == "manual_curated" and curated is not None:
        row["hand_calendar"] = {
            "status": hand_calendar_source.STATUS_CURATED,
            "curated_date": next_earnings.isoformat(),
            "source": curated.get("source"),
            "notiert_am": curated.get("notiert_am"),
        }

    # 5) can the underlying even get there? The spread is a fact now, so the
    # distance to its break-even can be measured - and a row that needs more
    # than the agreed bound is named, not silently dropped.
    move = row.get("required_move_pct")
    if move is not None and abs(move) > max_required_move_pct:
        return None, Exclusion(
            symbol, REASON_MOVE_TOO_FAR,
            move_too_far_message(move, max_required_move_pct),
            required_move_pct=move,
            max_required_move_pct=max_required_move_pct,
        ), None, stages, None
    stages["move"] = True
    context = {
        "section": section, "strategy": strategy, "primary": pair,
        "close": candidate.get("close"), "dte": row.get("dte"),
        "now_fn": now_fn, "rr_min": rr_min,
        "min_long_delta": min_long_delta, "min_net_debit": min_net_debit,
        "max_required_move_pct": max_required_move_pct,
        # The row's OWN enumeration: the alternatives are checked against the
        # list this pick came from, not against a second search of the section.
        "kept": kept, "criteria": criteria,
    }
    return row, None, None, stages, context


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
    return t("funnel.pairs.capped_note",
             title=t("funnel.pairs.capped_title", n=count, count=count),
             ceiling=deckel)


def _row_variants(context, bars):
    """The row's alternatives, or ``None`` when no ladder could be built.

    ``None`` is NOT "nothing qualifies" - it says the ladder was never built:
    there is no context (no pick, so no section to name alternatives from), no
    close (no distance, so neither a move nor a cap) or no bars. The row must
    not look checked in that case. ``[]`` stays reserved for a ladder that was
    built and holds nothing.
    """
    if not context or context.get("close") is None or not bars:
        return None
    return variant_characters(bars=bars, **context)


def fill_chance(row):
    """Set ``chance`` on a row and on each of its variants, from their own numbers.

    Called once the row's hit rate is known - a row's ``chance`` cannot exist
    before its measurement, and it is deliberately NOT part of the sort: the
    order of the list stays the hit rate's (``result_sort_key``). A row whose
    hit rate is ``None`` gets ``None``, and so does a variant without one; the
    field is never left out and never filled with a 0.
    """
    row["chance"] = chance_of(row.get("hit_rate_pct"), row.get("rr"))
    for variant in row.get("variants") or ():
        variant["chance"] = chance_of(variant.get("hit_rate_pct"), variant.get("rr"))
    return row


def measure_reachability(rows, *, fetch_daily, day, max_workers=DEFAULT_MAX_WORKERS,
                         deadline_seconds=DEFAULT_HISTORY_DEADLINE_SECONDS,
                         history_bars=reachability.HISTORY_BARS,
                         contexts=None):
    """Fill every row's history numbers - in parallel, bounded, never guessed.

    Only rows that already have a spread are measured (they are the ones whose
    distance to break-even is known), and one request per row is all this
    costs: ``fetch_daily(symbol, fromdate, history_bars)`` answers the daily
    bars newest first, exactly as ``gap_scan`` asks for them.

    ``contexts`` maps ``id(row)`` to the variant context of that row
    (``_assess``): the bars of the SAME request are then handed to
    ``variant_characters``, so the alternatives are named from the history the
    row itself was measured on - one request, one set of bars, no second
    opinion about the same title. The context carries the row's own pair list
    (``kept``), so naming the alternatives does not enumerate the section a
    second time.

    ``variants`` distinguishes two facts that both show no counter: ``None``
    when no structure could be built (no context, no close, no bars - the row
    was never named) and ``[]`` when the ladder was built and holds nothing.
    A row without a source therefore reads ``variants = None``: it cannot say
    "checked, nothing else fits" about a check that never ran.

    ``deadline_seconds`` bounds the whole phase. A title that has not answered
    by then keeps its row with ``history_state=source_failed`` and no numbers -
    the same honesty as ``too_short``: an unmeasured frequency is not 0 %.
    ``fetch_daily=None`` means this run has no history source at all, and every
    row says so instead of showing an invented number.

    The function never raises: a broken source is a state on the row.
    """
    rows = list(rows or ())
    if not rows:
        return rows
    contexts = contexts or {}
    fromdate = reachability.fromdate_for(day)
    if fetch_daily is None or fromdate is None:
        for row in rows:
            row.update(reachability.unmeasured(reachability.HISTORY_SOURCE_FAILED))
            # No bars, no ladder: nothing was named, so nothing may look named.
            row["variants"] = None
            fill_chance(row)
        return rows

    def _one(row):
        bars = fetch_daily(row.get("symbol"), fromdate, history_bars)
        # ``dte`` is CALENDAR days (the column the page shows); ``measure``
        # converts it into the sessions the window is counted in and returns
        # both numbers (``window_sessions``, ``window_calendar_days``).
        fields = reachability.measure(bars, row.get("required_move_pct"), row.get("dte"))
        return fields, _row_variants(contexts.get(id(row)), bars)

    budget = None if deadline_seconds is None else max(0.0, float(deadline_seconds))
    pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
    pairs = []
    finished = set()
    try:
        # Die Sprache der Anfrage MUSS mit in den Arbeiter: ein ThreadPoolExecutor
        # erbt keine ContextVar, der Arbeiter saehe sonst die Quellsprache und
        # schriebe deutsche Ausschluss- und Fehlertexte in eine englische Antwort.
        sprache = texts.active_language()
        pairs = [(row, pool.submit(texts.in_language(sprache, _one), row)) for row in rows]
        finished, open_futures = wait([future for _row, future in pairs], timeout=budget)
        # Same deadline rule as the chain stage: what has not started is
        # cancelled, what is in flight finishes in the background.
        for future in open_futures:
            future.cancel()
    finally:
        pool.shutdown(wait=budget is None)

    for row, future in pairs:
        if future not in finished:
            row.update(reachability.unmeasured(reachability.HISTORY_SOURCE_FAILED))
            row["variants"] = None
            fill_chance(row)
            continue
        try:
            fields, variants = future.result()
        except Exception:                   # noqa: BLE001 - a source failure is a state
            fields = reachability.unmeasured(reachability.HISTORY_SOURCE_FAILED)
            variants = None
        row.update(fields)
        row["variants"] = variants
        fill_chance(row)
    return rows


def result_sort_key(entry):
    """``hit_rate_pct`` descending, then the smallest move, then the biggest gap.

    A row WITHOUT a hit rate (``null``: too short a history, a failing source)
    stands behind every measured one - an unmeasured frequency is not a bad one
    - and the unmeasured rows are ordered among themselves by the same rule.
    ``|required_move_pct|`` ascending is the tie-break because a title that has
    to travel less is the easier trade at the same frequency; the gap keeps the
    old scan's order as the last word, and the symbol makes it total.
    """
    rate = entry.get("hit_rate_pct")
    measured = isinstance(rate, (int, float)) and not isinstance(rate, bool)
    move = entry.get("required_move_pct")
    move_abs = abs(move) if isinstance(move, (int, float)) and not isinstance(move, bool) else 0.0
    gap = entry.get("gap_pct")
    gap_abs = abs(gap) if isinstance(gap, (int, float)) and not isinstance(gap, bool) else 0.0
    return (0 if measured else 1,
            -(float(rate) if measured else 0.0),
            move_abs,
            -gap_abs,
            str(entry.get("symbol") or ""))


def run_funnel(candidates, *, fetch_next_earnings, fetch_chain, day, now_fn=None,
               max_workers=DEFAULT_MAX_WORKERS, deadline_seconds=DEFAULT_DEADLINE_SECONDS,
               rr_min=DEFAULT_RR_MIN, min_oi=DEFAULT_MIN_OI,
               min_long_delta=DEFAULT_MIN_LONG_DELTA,
               min_net_debit=DEFAULT_MIN_NET_DEBIT,
               max_required_move_pct=DEFAULT_MAX_REQUIRED_MOVE_PCT,
               fetch_daily=None,
               history_deadline_seconds=DEFAULT_HISTORY_DEADLINE_SECONDS,
               hand_calendar=None, feed=None, optionable=None):
    """Run every candidate through the funnel - in parallel, bounded, explained.

    ``fetch_next_earnings(symbol)`` answers a ``date`` or ``None`` and may raise
    ``NextEarningsError``; ``fetch_chain(symbol)`` answers the whole chain of one
    underlying and may raise (the class is read from ``exc.reason``);
    ``fetch_daily(symbol, fromdate, limit)`` answers the daily bars of the
    history source for the reachability numbers and may raise - ``None`` means
    the run has no history source and every row says ``source_failed``.

    ``deadline_seconds`` bounds the WHOLE run. Every candidate is still asked -
    only the waiting ends - and a title that has not finished by then is named
    ``deadline_exceeded`` in ``failures``. Work that had not STARTED by then is
    cancelled, so the run does not go on fetching for titles it no longer
    reports; a request already in flight finishes in the background. Nothing is
    raised for a single title: a symbol is either a row, an exclusion or a
    failure. ``notes`` adds what the counters cannot say - a capped pair search
    is named there, because its rows show the best pair FOUND, not the best.

    ``max_required_move_pct`` is the upper bound on the DISTANCE to break-even
    (``abs(required_move_pct)``, so it holds for calls and puts alike). It is
    not a ratio ceiling: the R/R floor stays the only ratio rule. The history
    phase (``measure_reachability``) runs after the funnel, on the rows that
    have a spread - and before the sort, because the hit rate IS the order.
    """
    rows = list(candidates or ())
    counts = {"with_next_earnings": 0, "with_chain": 0, "with_expiry_after_earnings": 0,
              "with_spread_ge_rr": 0, "with_reachable_move": 0, "shown": 0}
    run = FunnelRun(counts=counts)
    # id(row) -> the variant context of that row, filled as the rows come in and
    # handed to the history phase (which owns the bars).
    contexts = {}
    if not rows:
        return run

    budget = None if deadline_seconds is None else max(0.0, float(deadline_seconds))
    pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
    pairs = []
    finished = set()
    try:
        sprache = texts.active_language()
        pairs = [(row, pool.submit(
            texts.in_language(sprache, _assess), row,
            fetch_next_earnings=fetch_next_earnings, fetch_chain=fetch_chain,
            day=day, now_fn=now_fn, rr_min=rr_min, min_oi=min_oi,
            min_long_delta=min_long_delta, min_net_debit=min_net_debit,
            max_required_move_pct=max_required_move_pct,
            hand_calendar=hand_calendar, feed=feed, optionable=optionable))
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
                                        t("funnel.budget.timeout")))
            continue
        try:
            row, exclusion, failure, stages, context = future.result()
        except Exception as exc:                # noqa: BLE001 - a bug is not a 500
            run.failures.append(Failure(symbol, "funnel_error",
                                        t("funnel.budget.failed",
                                          error=type(exc).__name__)))
            continue
        if stages.get("next_earnings"):
            counts["with_next_earnings"] += 1
        if stages.get("chain"):
            counts["with_chain"] += 1
        if stages.get("expiry"):
            counts["with_expiry_after_earnings"] += 1
        if stages.get("spread"):
            counts["with_spread_ge_rr"] += 1
        if stages.get("move"):
            counts["with_reachable_move"] += 1
        if row is not None:
            run.results.append(row)
            if context is not None:
                # The variant context of THIS row, keyed by identity: the
                # history phase rebuilds the characters from the same bars.
                contexts[id(row)] = context
        elif exclusion is not None:
            run.excluded.append(exclusion)
        elif failure is not None:
            run.failures.append(failure)

    # The history phase: one request per SHOWN row (a title without a spread has
    # no distance to measure), bounded like the chain stage. It runs before the
    # sort because the measured frequency is what the answer is ordered by - and
    # it names the alternatives of each row from the same bars (``contexts``).
    measure_reachability(
        run.results, fetch_daily=fetch_daily, day=day, max_workers=max_workers,
        deadline_seconds=history_deadline_seconds, contexts=contexts)
    # Ordered by reachability: highest measured hit rate first, the rows without
    # a measurement behind them, ties broken by the shorter distance and then by
    # the bigger gap (``result_sort_key`` holds the whole rule).
    run.results.sort(key=result_sort_key)
    counts["shown"] = len(run.results)
    # One sentence for the whole answer when a search hit the ceiling: every
    # R/R on such a row is the best pair FOUND, not the best that exists, and a
    # "no pair" decided on such a cut is equally provisional.
    gekappt = cap_note(run.results, run.excluded)
    if gekappt:
        run.notes.append(gekappt)
    return run
