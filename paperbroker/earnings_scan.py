"""One scan across a whole earnings day - every optionable ticker, every listed
expiry inside the maturity window, one row per strategy.

Why this module exists
----------------------
Until now a reader had to type one ticker and one expiry at a time: 25 titles a
day meant 25 page loads, and the titles the page hides behind its Top-25 cut
could not be reached at all. This module answers one question for a whole day:
*which of these titles have a vertical spread that survives the very same
filters the single-ticker view uses?*

Three rules make the answer trustworthy, and each one is a decision, not an
implementation detail:

**The universe is the day as the source listed it, not the page.** The rows come
in before any ranking, so a title outside the Top 25 is scanned like every
other. Ranking only decides the ORDER the budget is spent in, and whatever the
budget leaves out is reported by name - never dropped in silence.

**The calculation is not re-implemented here.** Every hit comes from
``screener.vertical_spreads`` - the function ``GET /api/screen`` calls - with
the same ``ScreenerCriteria`` object and the same clock. This module forms no
pair, prices no spread and applies no filter of its own; it only decides WHICH
chain slices are worth asking and what the answer means.

**The scan is not a page view.** It costs one chain lookup per ticker (the adapter
caches the whole chain per underlying), so it runs on an explicit request
only, behind a hard budget (titles, expiries,
worker count, wall clock). A scan that could not finish says so: ``complete``
turns false, ``truncated`` names the limit that bit, and every title the scan
never reached is counted.

Data never has the benefit of the doubt
---------------------------------------
A ticker contributes one of eight named states (``STATUSES``). A ticker whose
chain could not be read, whose earnings record is unavailable, whose expiry
list is empty or whose window is blocked is *not* a ticker without hits - the
distinction is the whole point of the response. And one failing ticker never
cancels the others: every symbol runs inside its own boundary.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

from .assets import calendar_days_between
from .earnings_calendar import combine_earnings_and_expirations
from .earnings_day_calendar import rank_rows
from .screener import STRATEGIES, STRATEGY_IDS, filter_stale, vertical_spreads

SCHEMA_VERSION = "1.0"

# --- what one ticker/strategy row MEANS ----------------------------------- #
STATUS_HITS = "hits"                        # at least one spread passed every filter
STATUS_NO_MATCH = "no_match"                # data was good, nothing survived
STATUS_STALE_DATA = "stale_data"            # quotes too old to authorise a decision
STATUS_INCOMPLETE_DATA = "incomplete_data"  # part of the chain was unusable
STATUS_FETCH_FAILED = "fetch_failed"        # a source call failed for this ticker
STATUS_NOT_CHECKED = "not_checked"          # the scan never got to this ticker
STATUS_WINDOW_BLOCKED = "window_blocked"    # the earnings window says: no entry
STATUS_WINDOW_UNKNOWN = "window_unknown"    # the earnings window cannot be decided

STATUSES = (
    STATUS_HITS,
    STATUS_NO_MATCH,
    STATUS_STALE_DATA,
    STATUS_INCOMPLETE_DATA,
    STATUS_FETCH_FAILED,
    STATUS_NOT_CHECKED,
    STATUS_WINDOW_BLOCKED,
    STATUS_WINDOW_UNKNOWN,
)

#: Display order of the entries: what a reader came for first, "we do not know"
#: last. Ties fall back to the day's own ranking (market cap) and then the
#: symbol, so the same input always produces the same order.
_STATUS_RANK = {
    STATUS_HITS: 0,
    STATUS_INCOMPLETE_DATA: 1,
    STATUS_STALE_DATA: 2,
    STATUS_FETCH_FAILED: 3,
    STATUS_WINDOW_BLOCKED: 4,
    STATUS_WINDOW_UNKNOWN: 5,
    STATUS_NO_MATCH: 6,
    STATUS_NOT_CHECKED: 7,
}

#: How the "best" row of a strategy is chosen - identical to the page's
#: ``rendite_desc`` column sort, so the scan never points at a different spread
#: than the table a reader would sort by hand. ``earnings_scan`` keeps its own
#: copy only because ``server`` imports this module and not the other way
#: round; a test pins that both keys order the same pairs identically.
BEST_SORT = "rendite_desc"

# --- budget reasons (machine codes) --------------------------------------- #
LIMIT_MAX_SYMBOLS = "max_symbols"
LIMIT_DEADLINE = "max_seconds"
LIMIT_MAX_EXPIRATIONS = "max_expirations"
LIMIT_EXPIRATION_WINDOW = "outside_dte_window"
LIMIT_EXPIRATION_BEFORE_EARNINGS = "expiration_on_or_before_earnings"

#: Defaults of the hard budget. ``max_symbols`` is deliberately larger than the
#: page's Top 25: the scan must be able to reach titles the page hides, while
#: still bounding the number of chain requests one click can fire.
DEFAULT_MAX_SYMBOLS = 40
DEFAULT_MAX_EXPIRATIONS = 8
DEFAULT_MAX_SECONDS = 120.0
DEFAULT_CONCURRENCY = 4

MAX_SYMBOLS_CEILING = 200
MAX_EXPIRATIONS_CEILING = 40
MAX_SECONDS_CEILING = 300.0
CONCURRENCY_CEILING = 8

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ScanLimits:
    """The hard budget of one scan, echoed into the answer.

    Frozen on purpose: the worker threads read it, nobody rewrites it.
    """

    max_symbols: int = DEFAULT_MAX_SYMBOLS
    max_expirations_per_symbol: int = DEFAULT_MAX_EXPIRATIONS
    max_seconds: float = DEFAULT_MAX_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY

    def as_dict(self):
        return {
            "max_symbols": self.max_symbols,
            "max_expirations_per_symbol": self.max_expirations_per_symbol,
            "max_seconds": self.max_seconds,
            "concurrency": self.concurrency,
        }


def _exclusion(code, message):
    return {"code": code, "message": message}


def _expiration_of(quote):
    asset = getattr(quote, "asset", None)
    return None if asset is None else getattr(asset, "expiration_date", None)


def _expiry_key(quote):
    """The expiry as the ISO string everything else is keyed by, or ``None``.

    One normalisation for the LIST and the BUCKETS: ``_listed_expirations``
    parses ``date.fromisoformat``, so the expiry buckets must be keyed by that
    same parsed date. Keying one side by the raw ``asset.expiration_date``
    string split the two whenever the string carried whitespace - the bucket
    stayed empty and the expiry looked like one that was never read.
    """
    wert = _expiration_of(quote)
    if not isinstance(wert, str):
        return None
    try:
        return date.fromisoformat(wert.strip()).isoformat()
    except ValueError:
        return None


def _listed_expirations(quotes):
    """Real, unique expiry dates the passed quotes carry, ascending.

    Only dates ``date.fromisoformat`` accepts count. A symbol whose expiry does
    not parse is not a date we could check, and inventing one would be an
    unbacked claim about the chain.
    """
    gefunden = set()
    for quote in quotes:
        schluessel = _expiry_key(quote)
        if schluessel is None:
            continue
        gefunden.add(date.fromisoformat(schluessel))
    return sorted(gefunden)


def _in_window(dte, criteria):
    """The screener's own DTE window rule, same semantics (None disables a side)."""
    if dte is None:
        return False
    if criteria.dte_min is not None and dte < criteria.dte_min:
        return False
    if criteria.dte_max is not None and dte > criteria.dte_max:
        return False
    return True


def _return_pct(pair):
    """Same denominator as the page's "Rendite" column: max profit / max loss."""
    return pair.max_profit / pair.max_loss * 100.0 if pair.max_loss else None


def _be_gap_pct(pair):
    """Distance of the spot to the break-even in percent, never signed."""
    spot = getattr(pair.long, "underlying_price", None)
    if not spot:
        return None
    return abs(pair.breakeven - spot) / spot * 100.0


def best_sort_key(pair):
    """Sort key of :data:`BEST_SORT`; unknown values sort last, symbol breaks ties."""
    rendite = _return_pct(pair)
    abstand = _be_gap_pct(pair)
    return (-(rendite if rendite is not None else -1.0),
            abstand if abstand is not None else 9e9,
            pair.symbol)


def _reason_groups(dropped):
    """Aggregate leg/pair drop reasons: one entry per shape, count descending.

    Same idea as the page's summary: numbers are the grouping key, the first
    real text is the example, so a reader sees "OI 40 < 500" rather than a
    blanked-out template. Ties keep the order the filters produced.
    """
    groups = {}
    for _pair, reason in dropped or []:
        raw = str(reason)
        pattern = _NUMBER_RE.sub("#", raw)
        eintrag = groups.setdefault(pattern, {"count": 0, "raw": set()})
        eintrag["count"] += 1
        eintrag["raw"].add(raw)
    out = [{"reason": sorted(e["raw"])[0], "count": e["count"]} for e in groups.values()]
    out.sort(key=lambda g: -g["count"])
    return out


def _age_minutes(stamp, now_fn):
    """Quote age in minutes, or None when either side cannot be read.

    Reporting only - the DECISION about staleness is ``screener.filter_stale``,
    which this module calls rather than re-deriving. Both normalise to UTC
    before subtracting; a naive clock value is read as UTC exactly as the
    filter reads it.
    """
    if stamp is None:
        return None
    try:
        import arrow

        jetzt = arrow.get(now_fn()).to("UTC").naive
        gestempelt = arrow.get(str(stamp).replace(" ", "T")).to("UTC").naive
    except Exception:                               # noqa: BLE001 - a stamp we cannot date
        return None
    return round((jetzt - gestempelt).total_seconds() / 60.0, 1)


def _freshness(quotes, criteria, now_fn):
    """``fresh`` / ``partial`` / ``stale`` / ``unchecked`` / ``unknown`` for a quote set.

    The decision is ``screener.filter_stale`` - the single-ticker view's own rule.
    Called twice on purpose: once over the WHOLE chain (what the reader is told
    about the data) and once over the quotes a strategy really looked at (what
    decides its status). A stale contract on an expiry that was skipped for
    another reason must not turn a fresh, empty result into "too old".
    """
    if criteria.max_quote_age_min is None:
        return "unchecked"
    if not quotes:
        return "unknown"
    frisch, veraltet = filter_stale(quotes, criteria, now_fn)
    if not frisch:
        return "stale"
    return "partial" if veraltet else "fresh"


#: The per-expiry verdicts, folded back into the verdict of a set of expiries.
_FRESHNESS_RANK = {"unchecked": 0, "unknown": 1, "fresh": 2, "partial": 3, "stale": 4}


def _empty_freshness(criteria):
    """The verdict for a set with nothing in it - by the same rule as any other.

    ``_freshness`` answers this itself (no age guard -> ``unchecked``, nothing
    to read -> ``unknown``); the helper exists so both not_checked paths use
    that one rule instead of a chain-wide number here and a constant there.
    """
    return _freshness((), criteria, None)


def _combine_freshness(verdicts, criteria):
    """The freshness of several expiries, from their individual verdicts.

    One pass over the union and the fold over the parts must agree - that is
    what makes it safe to compute the split ONCE per expiry instead of once
    per strategy (four passes re-parsed the same timestamps for nothing).

    Rule: no age guard -> ``unchecked``; nothing checked -> ``unknown``; every
    part fully stale -> ``stale``; any part with stale quotes -> ``partial``;
    else ``fresh``. Exactly what ``_freshness`` answers for the concatenated
    quote set, because "a fresh quote exists" and "a stale quote exists" are
    both properties of the union.
    """
    if criteria.max_quote_age_min is None:
        return "unchecked"
    if not verdicts:
        return "unknown"
    if any(v == "unchecked" for v in verdicts):
        return "unchecked"
    # An expiry nothing could be read for contributes no evidence - exactly as
    # the empty set does in _freshness. Ignoring it (instead of counting it as
    # fresh-leaning) keeps the fold equal to one pass over the union.
    bekannt = [v for v in verdicts if v != "unknown"]
    if not bekannt:
        return "unknown"
    if all(v == "stale" for v in bekannt):
        return "stale"
    if any(v in ("stale", "partial") for v in bekannt):
        return "partial"
    return "fresh"


class _Counter:
    """Thread-safe tally of the external calls one scan really made."""

    def __init__(self):
        self._lock = threading.Lock()
        self.earnings = 0
        self.chains = 0

    def count_earnings(self):
        with self._lock:
            self.earnings += 1

    def count_chain(self):
        with self._lock:
            self.chains += 1

    def as_dict(self):
        with self._lock:
            return {"earnings": self.earnings, "chains": self.chains}


@dataclass
class _Context:
    """Everything a worker needs, read-only and shared."""

    fetch_earnings: object
    fetch_chain: object
    strategies: tuple
    criteria: object
    today: date
    now_fn: object
    limits: ScanLimits
    counter: _Counter
    on_ticker_error: object = None


def _report(ctx, symbol, exc):
    """Hand a per-ticker failure to the caller's logger - never to the body.

    The answer names the code (``earnings_fetch_failed``/``chain_fetch_failed``);
    the exception type and text are diagnostics for the server log. A logger
    that raises must not cost the scan, so the call is bounded.
    """
    if not callable(ctx.on_ticker_error):
        return
    try:
        ctx.on_ticker_error(symbol, exc)
    except Exception:                               # noqa: BLE001 - logging is best effort
        pass


def _scan_symbol(symbol, row, ctx):
    """One ticker, every selected strategy, inside its own failure boundary.

    Returns the entry dict. Nothing raises out of here: a broken ticker is a
    reported state, because one bad symbol must not cost the other 39.
    """
    eintrag = {
        "symbol": symbol,
        "name": row.get("name"),
        "session": row.get("session"),
        "market_cap": row.get("market_cap"),
        "status": None,
        "exclusion": None,
        "quality_flags": [],
        "data": None,
        "window": None,
        "checked_expirations": [],
        "skipped_expirations": [],
        "strategies": {},
    }

    # --- 1. the earnings record ------------------------------------------
    try:
        koerper = ctx.fetch_earnings(symbol)
        earnings = koerper.get("earnings") if isinstance(koerper, dict) else None
        if not isinstance(earnings, dict):
            raise ValueError("earnings answer carries no record")
        ctx.counter.count_earnings()
    except Exception as exc:                        # noqa: BLE001 - one ticker, one verdict
        # No exception text and no class name in the 200 body: the code below
        # names WHICH call failed, and that is the actionable part for the
        # caller. The type goes to the caller's log (``on_ticker_error``).
        _report(ctx, symbol, exc)
        eintrag["status"] = STATUS_FETCH_FAILED
        eintrag["exclusion"] = _exclusion(
            "earnings_fetch_failed",
            "earnings lookup failed",
        )
        return eintrag

    # --- 2. the chain (ONE request covers every expiry of this underlying)
    try:
        chain = ctx.fetch_chain(symbol)
        quotes = [q for q in (chain or []) if _expiration_of(q) is not None]
        uebersprungen = int(getattr(chain, "skipped", 0) or 0)
        ctx.counter.count_chain()
    except Exception as exc:                        # noqa: BLE001 - one ticker, one verdict
        _report(ctx, symbol, exc)
        eintrag["status"] = STATUS_FETCH_FAILED
        eintrag["exclusion"] = _exclusion(
            "chain_fetch_failed",
            "option chain could not be read",
        )
        return eintrag

    verfaelle = _listed_expirations(quotes)

    # --- 3. freshness, decided by the screener's own rule -----------------
    # filter_stale is the function the single-ticker view uses; calling it here
    # means "too old" means the same thing in both places.
    freshness = _freshness(quotes, ctx.criteria, ctx.now_fn)

    stempel = [str(q.quote_timestamp) for q in quotes
               if getattr(q, "quote_timestamp", None) is not None]
    neuester = max(stempel) if stempel else None

    flags = []
    completeness = "complete"
    if uebersprungen:
        completeness = "partial"
        flags.append("chain_contracts_skipped")
    if not quotes:
        completeness = "unknown"
        flags.append("chain_empty")

    eintrag["data"] = {
        "earnings_status": earnings.get("status"),
        "earnings_date": earnings.get("next_earnings_date"),
        "earnings_source_checked_at_utc": earnings.get("source_checked_at_utc"),
        "chain_contracts": len(quotes),
        "chain_skipped_contracts": uebersprungen,
        "chain_quote_timestamp_raw": neuester,
        "chain_quote_age_minutes": _age_minutes(neuester, ctx.now_fn),
        "freshness": freshness,
        "completeness": completeness,
        "listed_expirations": [d.isoformat() for d in verfaelle],
    }

    # --- 4. the earnings window (the SAME gate the single-ticker route uses)
    fenster = combine_earnings_and_expirations(
        earnings, [d.isoformat() for d in verfaelle], today=ctx.today
    )
    entscheidung = fenster["decision"]
    eintrag["window"] = {
        "status": entscheidung["status"],
        "eligible": entscheidung["eligible"],
        "planned_last_exit_date": fenster["exit"]["planned_last_exit_date"],
        "remaining_trading_days": fenster["exit"]["remaining_trading_days"],
        "selected_expiration": fenster["expirations"]["selected"],
        "reasons": [dict(g) for g in entscheidung["reasons"]],
    }
    if not entscheidung["eligible"]:
        # A blocked or undecidable window is not "no hits": it is a verdict
        # about the trade itself, and it is reported as such.
        eintrag["status"] = (STATUS_WINDOW_UNKNOWN
                             if entscheidung["status"] == "unknown"
                             else STATUS_WINDOW_BLOCKED)
        eintrag["exclusion"] = _exclusion(
            "earnings_window_" + ("unknown" if entscheidung["status"] == "unknown" else "blocked"),
            "; ".join(g["message"] for g in entscheidung["reasons"]),
        )
        eintrag["quality_flags"] = flags
        return eintrag

    # --- 5. which listed expiries are worth checking ----------------------
    nach_earnings = fenster["expirations"]["after_earnings"]
    for eintrag_excluded in fenster["expirations"]["excluded"]:
        eintrag["skipped_expirations"].append({
            "expiration": eintrag_excluded["date"],
            "reason": LIMIT_EXPIRATION_BEFORE_EARNINGS,
        })

    as_of = ctx.now_fn()
    zu_pruefen = []
    for iso in nach_earnings:
        dte = calendar_days_between(iso, as_of)
        if not _in_window(dte, ctx.criteria):
            eintrag["skipped_expirations"].append({
                "expiration": iso,
                "reason": LIMIT_EXPIRATION_WINDOW,
                "dte": dte,
            })
            continue
        if len(zu_pruefen) >= ctx.limits.max_expirations_per_symbol:
            eintrag["skipped_expirations"].append({
                "expiration": iso,
                "reason": LIMIT_MAX_EXPIRATIONS,
                "dte": dte,
            })
            completeness = "partial"
            if "expiration_limit_reached" not in flags:
                flags.append("expiration_limit_reached")
            continue
        zu_pruefen.append((iso, dte))

    eintrag["data"]["completeness"] = completeness
    eintrag["checked_expirations"] = [iso for iso, _dte in zu_pruefen]
    # A chain that is only PARTLY fresh is neither "fresh" nor "stale": the
    # excluded contracts must be visible next to a hit, and without a hit they
    # are the reason there is none.
    if freshness == "partial":
        flags.append("partially_stale_chain")
    eintrag["quality_flags"] = flags

    if not zu_pruefen:
        # The chain lists expiries, but none of them is inside the window: a
        # named exclusion, not an empty result.
        eintrag["status"] = STATUS_NOT_CHECKED
        eintrag["exclusion"] = _exclusion(
            "no_expiration_in_window",
            "no listed expiration inside the maturity window",
        )
        eintrag["strategies"] = _empty_strategies(
            ctx.strategies, STATUS_NOT_CHECKED, ctx.criteria
        )
        return eintrag

    # --- 6. the strategies, on the screener's own function ----------------
    per_expiry = {iso: [q for q in quotes if _expiry_key(q) == iso]
                  for iso, _dte in zu_pruefen}
    dte_by_expiry = dict(zu_pruefen)
    # One age pass per expiry, NOT per strategy: the split of an expiry cannot
    # change between strategies, and running filter_stale four times over the
    # same few hundred quotes only re-parsed the same timestamp strings.
    freshness_by_expiry = {iso: _freshness(per_expiry[iso], ctx.criteria, ctx.now_fn)
                           for iso, _dte in zu_pruefen}

    treffer_gesamt = 0
    for sid in ctx.strategies:
        treffer = 0
        bestes = None
        gruende = []
        geprueft = []
        for iso, _dte in zu_pruefen:
            kept, dropped = vertical_spreads(per_expiry[iso], ctx.criteria,
                                             strategy=sid, now_fn=ctx.now_fn)
            # vertical_spreads reports a whole-section gate as a (None, reason)
            # pair - that expiry was not really checked, so it is named instead
            # of counted as "checked and empty".
            if not kept and dropped and all(pair is None for pair, _r in dropped):
                eintrag["skipped_expirations"].append({
                    "expiration": iso,
                    "reason": "section_gate",
                    "detail": dropped[0][1],
                })
                continue
            geprueft.append(iso)
            treffer += len(kept)
            gruende.extend(dropped)
            for pair in kept:
                if bestes is None or best_sort_key(pair) < best_sort_key(bestes):
                    bestes = pair

        # "Too old" and "nothing fits" are different answers - and the age that
        # counts is the age of what this strategy actually LOOKED AT, not of the
        # whole chain. A stale contract on an expiry that was skipped (outside
        # the window, before the numbers, beyond max_expirations) must not turn
        # a fresh, empty result into "too old".
        geprueft_freshness = _combine_freshness(
            [freshness_by_expiry[iso] for iso in geprueft], ctx.criteria
        )
        # Without a hit, a checked set that is stale (or only partly fresh) IS
        # the reason there is none. A real hit is never hidden behind a data
        # note - it keeps its state and carries the flag beside it.
        if not treffer and geprueft_freshness in ("stale", "partial"):
            status = STATUS_STALE_DATA
        elif treffer:
            status = STATUS_HITS
        elif completeness == "partial":
            status = STATUS_INCOMPLETE_DATA
        else:
            status = STATUS_NO_MATCH

        eintrag["strategies"][sid] = {
            "status": status,
            "hits": treffer,
            "best": None if bestes is None else _best_payload(bestes, ctx.criteria,
                                                             dte_by_expiry),
            "checked_expirations": geprueft,
            # The age of exactly these expiries - the number the status above
            # was decided on, so a reader can check the verdict.
            "checked_freshness": geprueft_freshness,
            "drop_reasons": _reason_groups(gruende),
        }
        treffer_gesamt += treffer

    # The ticker row summarises its strategies: the strongest reason a reader
    # must not trust the row, with "a real hit" never hidden behind a data note.
    staende = [eintrag["strategies"][sid]["status"] for sid in ctx.strategies]
    if treffer_gesamt:
        eintrag["status"] = STATUS_HITS
    elif STATUS_STALE_DATA in staende:
        eintrag["status"] = STATUS_STALE_DATA
    elif STATUS_INCOMPLETE_DATA in staende:
        eintrag["status"] = STATUS_INCOMPLETE_DATA
    else:
        eintrag["status"] = STATUS_NO_MATCH
    return eintrag


def _best_payload(pair, criteria, dte_by_expiry):
    """The best spread of one strategy, in the same shape the API already uses."""
    daten = pair.as_dict(criteria)
    daten["dte"] = dte_by_expiry.get(pair.expiration)
    rendite = _return_pct(pair)
    daten["return_pct"] = None if rendite is None else round(rendite, 2)
    daten["underlying_price"] = getattr(pair.long, "underlying_price", None)
    return daten


def _empty_strategies(strategies, status, criteria):
    """Strategy stubs for a ticker that was not checked - nothing was read, so
    the freshness is derived (``unknown``, or ``unchecked`` without a guard)."""
    leer = _empty_freshness(criteria)
    return {sid: {"status": status, "hits": 0, "best": None,
                  "checked_expirations": [], "checked_freshness": leer,
                  "drop_reasons": []}
            for sid in strategies}


def scan_day(
    rows,
    *,
    fetch_earnings,
    fetch_chain,
    criteria,
    strategies=None,
    day,
    today,
    now_fn,
    limits=None,
    optionable=None,
    on_ticker_error=None,
):
    """Scan one earnings day. Returns the response body (a plain dict).

    ``rows`` is the day's list exactly as the source delivered it (before any
    ranking). ``fetch_earnings(symbol)`` and ``fetch_chain(symbol)`` are the
    only ways out of this module - both cached inside their adapters, which is
    why a repeated scan inside their TTLs costs no request at all.

    ``optionable`` is the CBOE symbol set; ``None`` means "not available" and
    the scan then runs unfiltered and says so.
    """
    limits = limits or ScanLimits()
    auswahl = tuple(strategies) if strategies else tuple(STRATEGY_IDS)
    unbekannt = [s for s in auswahl if s not in STRATEGIES]
    if unbekannt:
        raise ValueError(f"unknown strategy id(s): {', '.join(unbekannt)}")

    start = time.monotonic()
    counter = _Counter()

    # Ranking decides the ORDER the budget is spent in - never which titles
    # exist. The whole optionable universe is ordered first and cut afterwards,
    # so what the budget leaves out can be named instead of being mistaken for
    # "nothing else there".
    voll = rank_rows(rows, top_n=len(rows or []), optionable=optionable)
    alle = [dict(z) for z in voll["rows"]]
    zu_scannen = alle[:max(0, limits.max_symbols)]
    ausgelassen = alle[max(0, limits.max_symbols):]

    ctx = _Context(
        fetch_earnings=fetch_earnings,
        fetch_chain=fetch_chain,
        strategies=auswahl,
        criteria=criteria,
        today=today,
        now_fn=now_fn,
        limits=limits,
        counter=counter,
        on_ticker_error=on_ticker_error,
    )

    eintraege = []
    abbruch = None
    # Wave by wave, not one big submission: the wall clock is checked BEFORE
    # each wave, so a deadline really stops the work instead of merely stopping
    # the waiting.
    index = 0
    while index < len(zu_scannen):
        if abbruch is None and (time.monotonic() - start) >= limits.max_seconds:
            abbruch = LIMIT_DEADLINE
        if abbruch is not None:
            break
        welle = zu_scannen[index:index + max(1, limits.concurrency)]
        with ThreadPoolExecutor(max_workers=max(1, limits.concurrency)) as pool:
            ergebnisse = list(pool.map(lambda paar: _scan_symbol(paar[0], paar[1], ctx),
                                       [(z["symbol"], z) for z in welle]))
        eintraege.extend(ergebnisse)
        index += len(welle)

    # Everything the budget or the deadline kept out is a reported state, not
    # an absent row: "we did not look" is a fact the reader needs.
    nie_geprueft = [dict(z) for z in zu_scannen[index:]]
    for zeile in nie_geprueft:
        eintraege.append({
            "symbol": zeile.get("symbol"),
            "name": zeile.get("name"),
            "session": zeile.get("session"),
            "market_cap": zeile.get("market_cap"),
            "status": STATUS_NOT_CHECKED,
            "exclusion": _exclusion(abbruch or LIMIT_MAX_SYMBOLS,
                                    "the scan budget did not reach this ticker"),
            "quality_flags": [],
            "data": None,
            "window": None,
            "checked_expirations": [],
            "skipped_expirations": [],
            "strategies": _empty_strategies(auswahl, STATUS_NOT_CHECKED, criteria),
        })

    zaehler = {status: 0 for status in STATUSES}
    for eintrag in eintraege:
        zaehler[eintrag["status"]] = zaehler.get(eintrag["status"], 0) + 1

    verarbeitet = sum(1 for e in eintraege if e["data"] is not None)
    nicht_geprueft = len(nie_geprueft)
    abgeschnitten = bool(ausgelassen) or abbruch is not None

    gruende = []
    if ausgelassen:
        gruende.append(_exclusion(
            LIMIT_MAX_SYMBOLS,
            f"{len(ausgelassen)} further optionable title(s) of this day were not "
            f"selected (max_symbols={limits.max_symbols})",
        ))
    if abbruch is not None:
        gruende.append(_exclusion(
            LIMIT_DEADLINE,
            f"the scan stopped after {limits.max_seconds:g} s; {nicht_geprueft} "
            f"selected title(s) were not checked",
        ))

    eintraege.sort(key=lambda e: (
        _STATUS_RANK.get(e["status"], len(STATUSES)),
        -(e["market_cap"] or 0),
        e["symbol"] or "",
    ))

    return {
        "schema_version": SCHEMA_VERSION,
        "day": day,
        "strategies": list(auswahl),
        "best_sort": BEST_SORT,
        "criteria": {
            name: getattr(criteria, name) for name in criteria.__dataclass_fields__
        },
        "option_filter": {
            "applied": optionable is not None,
            "available_symbols": None if optionable is None else len(optionable),
        },
        "coverage": {
            # Three numbers, three different facts: the day as the source listed
            # it, the optionable prefilter, and the budget.
            "day_rows": len(rows or []),
            "optionable_rows": voll["total"],
            "without_options": voll["without_options"],
            "selected": len(zu_scannen),
            "processed": verarbeitet,
            "not_checked": nicht_geprueft,
            "hidden_symbols": [z.get("symbol") for z in ausgelassen],
            "complete": not abgeschnitten and nicht_geprueft == 0,
            "truncated": abgeschnitten,
            "truncation_reasons": gruende,
            "limits": limits.as_dict(),
            "elapsed_seconds": round(time.monotonic() - start, 3),
            # Adapter lookups, not HTTP requests: both adapters answer from
            # their own cache inside the TTL, and then nothing leaves the process.
            "source_calls": counter.as_dict(),
        },
        "counts": zaehler,
        "entries": eintraege,
    }
