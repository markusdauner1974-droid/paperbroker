"""Request and display helpers shared by the routes, moved 1:1 out of ``server.py``.

Everything the route modules need to turn a request into criteria and a result
set into display values lives here, so a route module can import it without
touching ``server`` (which would be a circular import once the routes move).

Two helpers took their factory state from the closure of ``create_app``; they
now receive it as parameters instead:

- ``_selectable_dates(dates, criteria, now_fn)``
- ``_parse_args(broker, screener, now_fn)``
"""

from __future__ import annotations

import math
import re

from flask import abort, request

from . import exclusions, quote_age
from .assets import calendar_days_between
from .exclusions import LEVEL_CONTRACT, LEVEL_EXPIRY, LEVEL_PAIR
from .quote_age import AGE_MODES, MODE_FRESHNESS
from .screener import (
    FUTURE_TOLERANCE_S,
    LEGACY_OPTION_TYPES,
    PAIR_SEARCH_MODES,
    STRATEGIES,
    STRATEGY_IDS,
    ScreenerCriteria,
    pair_search_report,
    resolve_strategy,
    strategy_option_type,
)

_TICKER_RE = re.compile(r"^[A-Z]{1,6}$")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CRITERIA_INT_FIELDS = ("dte_min", "dte_max", "min_oi", "min_volume", "max_quote_age_min",
                        "min_bid_size", "min_ask_size", "multiplier", "quantity")

_CRITERIA_FLOAT_FIELDS = ("max_spread_pct", "max_spread_abs", "pair_max_width", "iv_min", "iv_max",
                          "weight_spread", "weight_oi", "weight_iv", "fees", "budget")

_CRITERIA_BOOL_FIELDS = ("require_oi", "require_volume")

_CRITERIA_CHOICE_FIELDS = {"iv_pref": ("high", "low"),
                           # how the strike pairs are searched: adjacent strikes
                           # or every combination inside pair_max_width
                           "pair_mode": PAIR_SEARCH_MODES,
                           # the age MODE is a choice, not a switch: "fresh" is
                           # the default and keeps every protection, the second
                           # value admits older data for a provisional run
                           "data_mode": AGE_MODES}

_CRITERIA_ECHO_FIELDS = tuple(ScreenerCriteria.__dataclass_fields__)

_CRITERIA_FIELDS = (_CRITERIA_INT_FIELDS + _CRITERIA_FLOAT_FIELDS
                    + _CRITERIA_BOOL_FIELDS + tuple(_CRITERIA_CHOICE_FIELDS))

_IGNORED_PARAMS = ("ticker", "expiration", "ignore_quote_age", "sort", "option_type",
                   "strategy", "no_calendar")

_REASON_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

_SPREAD_TYPES = ("call", "put")

_FRESH_GREEN_MIN = 30

_FRESH_YELLOW_MIN = 120

def _parse_criteria_value(field, raw):
    """Parse one request value into the criteria type.

    Raises ValueError with a readable hint. A comma is rejected on purpose:
    the value has to round-trip through the URL and the reasons printed by the
    screener use a dot ("spread 28.6% > 10.0%").
    """
    text = str(raw).strip()
    if "," in text:
        raise ValueError(f"{field}: bitte den Punkt als Dezimaltrenner verwenden "
                         f"(z. B. 7.5 statt '{text}').")
    if field in _CRITERIA_INT_FIELDS:
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError(f"{field}: '{text}' ist keine ganze Zahl.") from exc
    if field in _CRITERIA_BOOL_FIELDS:
        # The empty field (handled above) means "keep the default"; a switch
        # therefore only has to name the two explicit states.
        if text not in ("0", "1"):
            raise ValueError(f"{field}: '0' oder '1' erwartet (ist: '{text}').")
        return text == "1"
    if field in _CRITERIA_CHOICE_FIELDS:
        erlaubt = _CRITERIA_CHOICE_FIELDS[field]
        if text not in erlaubt:
            raise ValueError(f"{field}: einer von {', '.join(erlaubt)} erwartet "
                             f"(ist: '{text}').")
        return text
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{field}: '{text}' ist keine Zahl.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{field}: '{text}' ist keine endliche Zahl.")
    return value

def _criteria_from_request(base_criteria, args):
    """Merge the settable criteria from the request over the base criteria.

    Returns (criteria, error, unknown):
      error   - readable text when a value is unusable or the merged set is
                contradictory (validate()), else None
      unknown - request parameters that were not used (they are named on the
                page instead of being ignored silently)
    """
    values = {name: getattr(base_criteria, name) for name in _CRITERIA_ECHO_FIELDS}
    for field in _CRITERIA_FIELDS:
        raw = args.get(field, "").strip()
        if not raw:
            continue
        try:
            values[field] = _parse_criteria_value(field, raw)
        except ValueError as exc:
            return base_criteria, str(exc), []
    if args.get("ignore_quote_age", "").strip() == "1":
        values["max_quote_age_min"] = None
    criteria = ScreenerCriteria(**values)
    try:
        criteria.validate()
    except ValueError as exc:
        return base_criteria, str(exc), []
    unknown = [k for k in args.keys()
               if k not in _IGNORED_PARAMS and k not in _CRITERIA_FIELDS]
    return criteria, None, sorted(unknown)

def _criteria_echo(criteria):
    """All effective criteria as a JSON-able dict (None -> null).

    One serialisation for every field - a whitelist would drift.
    """
    return {name: getattr(criteria, name) for name in _CRITERIA_ECHO_FIELDS}

def _newest_quote_age_min(quotes, now_fn=None):
    """Age in minutes of the YOUNGEST quote in the FETCHED chain.

    (CodeRabbit finding: use the raw chain, not the screened results -
    the stale-quote guard can remove every candidate and hide the very
    staleness the lamp is supposed to report.)

    Accepts OptionQuote objects or ScreenResults (duck-typed). None/no
    rows / no readable timestamp -> None: the template shows 'keine
    Daten' instead of inventing a number without input.

    UX-1: this is the single place where the age is computed - the lamp
    colour and the minutes shown in the header come from the same value.

    The computation itself is ``quote_age`` - the same rule the filter uses,
    so the number on the page cannot disagree with the number that dropped
    the contract. A broken clock returns None here (a label must not raise);
    the filter names the clock error itself.
    """
    if not quotes:
        return None
    try:
        clock = quote_age.clock_utc(now_fn)
    except Exception:                       # noqa: BLE001 - a label must not raise
        return None
    neueste = quote_age.newest(quote_age.assess_chain(quotes, clock, None))
    return None if neueste is None or neueste.age_minutes is None else neueste.age_minutes

def _age_within_limit(quotes, criteria, now_fn=None):
    """E3: is the newest quote inside the operator OWN age limit?

    The traffic light beside it answers a different question (general feed freshness,
    30/120 minutes by design - the CBOE feed is ~15 minutes behind by nature). With the
    limit switched off there is nothing to be inside of, hence None.
    """
    if criteria is None or criteria.max_quote_age_min is None:
        return None
    age = _newest_quote_age_min(quotes, now_fn=now_fn)
    if age is None:
        return None
    return age <= criteria.max_quote_age_min

def _data_state(quotes, criteria, now_fn=None):
    """The age-mode / data-state block shared by the page and the API.

    ONE object with the mode, the provisional flag, the raw source stamp, the
    (only-if-proven) UTC value, the age with its ``age_proven`` flag, the fetch
    time, the counts per state and the market state. The page renders it and
    ``/api/screen`` returns it unchanged - that is what keeps the two surfaces
    on the same status rules instead of one sentence in HTML and another in JSON.
    """
    try:
        clock = quote_age.clock_utc(now_fn)
    except Exception:                       # noqa: BLE001 - the page must render
        clock = quote_age.clock_utc(None)
    mode = getattr(criteria, "data_mode", MODE_FRESHNESS) if criteria is not None else MODE_FRESHNESS
    max_age = None if criteria is None else criteria.max_quote_age_min
    ages = quote_age.assess_chain(quotes or (), clock, max_age)
    return quote_age.summarise(ages, mode=mode, max_age_min=max_age, clock=clock)

def _feed_stamp(quotes):
    """E3: the exact feed timestamp of the newest quote (what the data says).

    Compared as text: the adapter stores ISO-ish stamps, which sort correctly.
    """
    stamps = [str(q.quote_timestamp) for q in quotes or []
              if getattr(q, "quote_timestamp", None) is not None]
    return max(stamps) if stamps else None

def _freshness_lamp(quotes, now_fn=None):
    """Traffic light for the fetched chain (same value as the age above)."""
    return _lamp_state(_newest_quote_age_min(quotes, now_fn=now_fn))

def _lamp_state(age_min):
    """green/yellow/red from a quote age; None age -> None (no lamp)."""
    if age_min is None:
        return None
    if age_min < -FUTURE_TOLERANCE_S / 60:
        # Slice J: the youngest timestamp of the RAW chain is in the future.
        # Measured before the fix: the page said "frisch (-1440 min)" and the
        # JSON said green. Same tolerance as the stale guard - one rule.
        return "red"
    if age_min < _FRESH_GREEN_MIN:
        return "green"
    if age_min < _FRESH_YELLOW_MIN:
        return "yellow"
    return "red"

def _pair_search_block(criteria, coverage=None):
    """Search mode + what it really enumerated (never a silent cap)."""
    block = pair_search_report(criteria)
    if coverage:
        block["coverage"] = dict(coverage)
    return block

def _exclusion_blocks(dropped_contracts, dropped_pairs, criteria):
    """The exclusion overview of ONE screen, split by the unit it counts.

    Contracts and spread combinations are counted separately and are NEVER
    added up: one dropped combination is two contracts, and an expired expiry
    can hide the whole chain behind it. A section gate (``(None, reason)``) is
    an EXPIRY verdict, not a pair verdict, so it gets its own block.
    """
    paare, verfaelle = [], []
    for item, reason in dropped_pairs or ():
        (verfaelle if item is None else paare).append((item, reason))
    bloecke = [
        exclusions.summarise(dropped_contracts, LEVEL_CONTRACT, criteria),
        exclusions.summarise(paare, LEVEL_PAIR, criteria),
    ]
    if verfaelle:
        bloecke.append(exclusions.summarise(verfaelle, LEVEL_EXPIRY, criteria))
    return [block for block in exclusions.combine(bloecke) if block["counted"]]

def _strategy_selection(args):
    """Slice L: which strategy a request asks for.

    `strategy` is the primary parameter; `option_type` stays the legacy alias and
    means exactly what it always meant (`call` = bull call debit, `put` = bear put
    debit). Returns (strategy, spread_type, error_code, error_text). A request that
    names both with contradicting values is an ERROR on both routes - see
    `screener.resolve_strategy` for the documented rule; nothing is guessed.
    """
    strategy, code, text = resolve_strategy(args.get("strategy"), args.get("option_type"))
    if code:
        return None, None, code, text
    return strategy, strategy_option_type(strategy), None, None

def _strategy_error_body(code):
    """The JSON body for a rejected strategy request.

    The two legacy refusals keep their old shape word for word so an existing
    consumer does not have to change; the new codes name themselves.
    """
    if code == "unknown_option_type":
        return {"error": "unknown spread type", "allowed": list(_SPREAD_TYPES)}
    if code == "conflict":
        return {"error": "conflicting strategy and option_type",
                "allowed": list(STRATEGY_IDS),
                "option_type_to_strategy": dict(LEGACY_OPTION_TYPES)}
    return {"error": "unknown strategy", "allowed": list(STRATEGY_IDS)}

def _strategy_label(strategy):
    """The human label of a resolved strategy id; None when no strategy is known."""
    return None if strategy is None else STRATEGIES[strategy]["label"]


def _in_dte_window(dte, criteria):
    # Same semantics as the screener's per-quote DTE filter
    # (screener.py:242-247): a None bound disables that side.
    if criteria.dte_min is not None and dte < criteria.dte_min:
        return False
    if criteria.dte_max is not None and dte > criteria.dte_max:
        return False
    return True

def _drop_reason_groups(dropped):
    """Aggregate the screener's per-quote drop reasons for page + JSON.

    Output: [{"reason": str, "count": int}, ...], count descending.
    Ties keep the order in which the filters produced them (= pipeline
    order), because that is the order a reader expects: the filter that
    acts first is named first. HTML and JSON consume the same list -
    parity by construction.
    """
    groups = {}
    for _quote, reason in dropped or []:
        raw = str(reason)
        pattern = _REASON_NUM_RE.sub("#", raw)
        entry = groups.setdefault(pattern, {"count": 0, "raw": set()})
        entry["count"] += 1
        entry["raw"].add(raw)
    # E7: instead of blanking the numbers out (the pattern "Spread #% > #%" was
    # what the page showed) the summary names one real example WITH its bounds;
    # the pattern stays only the grouping key. sorted() keeps the example stable.
    out = [{"reason": sorted(e["raw"])[0], "count": e["count"]}
           for _pattern, e in groups.items()]
    out.sort(key=lambda g: -g["count"])   # stable: ties keep pipeline order
    return out

def _selectable_dates(dates, criteria, now_fn):
    # B-11: the picker must only OFFER dates that can still hit. A
    # chain runs years out, but the screener window is ~6 weeks, so
    # most listed dates were dead entries in the dropdown. Validation
    # keeps using the FULL list, so a bookmarked date that fell out of
    # the window still answers 200 with zero hits instead of 400.
    as_of = now_fn()
    return [d for d in dates
            if _in_dte_window(calendar_days_between(d, as_of), criteria)]

def _parse_args(broker, screener, now_fn):
    # one criteria object per request: base = injected criteria, the URL
    # overrides only what it really sets (Ausbau-Slice 2, A)
    criteria, criteria_error, unknown = _criteria_from_request(
        screener.criteria, request.args)
    ticker = request.args.get("ticker", "").upper().strip()
    # form-first flow (CodeRabbit finding): load the expiration list
    # as soon as the ticker is valid so the selector appears without
    # requiring a pre-known date; empty expiration = picker only
    if ticker and not _TICKER_RE.match(ticker):
        abort(400, description="invalid ticker (1-6 letters A-Z expected)")
    expiration = request.args.get("expiration", "").strip()
    dates = None
    selectable = None
    if ticker:
        dates = broker.get_expiration_dates(ticker)
        selectable = _selectable_dates(dates, criteria, now_fn)
    if expiration:
        if not _DATE_RE.match(expiration):
            abort(400, description="invalid expiration (YYYY-MM-DD expected)")
        # expiration must be one of the chain's real dates - prevents
        # probing arbitrary CBOE URLs with made-up dates
        if not dates or expiration not in dates:
            abort(400, description="expiration is not a listed date for this ticker")
    elif not ticker:
        abort(400, description="ticker and expiration required")
    if criteria_error:
        # a contradictory or unparsable criteria set is a client error -
        # no silent correction (panel decision)
        abort(400, description=criteria_error)
    return ticker, expiration, dates, selectable, criteria, unknown
