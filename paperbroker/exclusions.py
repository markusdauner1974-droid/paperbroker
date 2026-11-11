"""Why things were excluded - counted per unit, never summed across units.

The screener has always named ONE reason per dropped item: the first filter that
rejected it (``screen()`` documents that order). That is the only thing this
project can honestly claim, so the summary here says it in words instead of
implying a full cause analysis:

* every item is counted exactly ONCE, with its first reason;
* the four units (contracts, spread combinations, expirations, symbols) are
  reported separately - adding them up would produce a number that means
  nothing, because one expired expiry can hide 200 contracts;
* categories that were switched OFF (no age guard, no budget, no IV band) are
  named as "not checked" instead of silently missing from the list, and no
  category ever appears with a count it did not earn.

Nothing here recomputes a filter - the reasons come from the pipeline that ran.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------- #
# levels (what one line of the summary counts)
# ---------------------------------------------------------------------- #

LEVEL_CONTRACT = "contract"
LEVEL_PAIR = "pair"
LEVEL_EXPIRY = "expiry"
LEVEL_SYMBOL = "symbol"
LEVELS = (LEVEL_CONTRACT, LEVEL_PAIR, LEVEL_EXPIRY, LEVEL_SYMBOL)
UNIT_LABELS = {
    LEVEL_CONTRACT: "Kontrakte",
    LEVEL_PAIR: "Spread-Kombinationen",
    LEVEL_EXPIRY: "Verfälle",
    LEVEL_SYMBOL: "Aktien",
}
UNIT_SINGULAR = {
    LEVEL_CONTRACT: "Kontrakt",
    LEVEL_PAIR: "Spread-Kombination",
    LEVEL_EXPIRY: "Verfall",
    LEVEL_SYMBOL: "Aktie",
}

# ---------------------------------------------------------------------- #
# categories
# ---------------------------------------------------------------------- #

CAT_LIQUIDITY = "liquidity"
CAT_AGE = "age"
CAT_DTE = "dte"
CAT_VOLATILITY = "volatility"
CAT_PRICE = "price"
CAT_BUDGET = "budget"
CAT_COSTS = "costs"
CAT_DATA = "data"
CAT_SCAN_LIMIT = "scan_limit"
CAT_OTHER = "other"

CATEGORY_LABELS = {
    CAT_SCAN_LIMIT: "Scan-Grenze (Budget/Zeit)",
    CAT_LIQUIDITY: "Liquidität",
    CAT_AGE: "Datenalter",
    CAT_DTE: "Laufzeit",
    CAT_VOLATILITY: "Volatilität",
    CAT_PRICE: "ungültiger Debit/Credit",
    CAT_BUDGET: "Budget",
    CAT_COSTS: "Kosten",
    CAT_DATA: "fehlende Daten",
    CAT_OTHER: "sonstiges",
}

#: The order the summary lists categories in when two counts are equal.
CATEGORY_ORDER = (CAT_LIQUIDITY, CAT_AGE, CAT_DTE, CAT_BUDGET, CAT_PRICE,
                  CAT_COSTS, CAT_VOLATILITY, CAT_DATA, CAT_SCAN_LIMIT, CAT_OTHER)

#: The scan's own states, each with the category it really belongs to. A
#: separate table because these are not screener reasons: "fetch_failed" is a
#: data gap of one title, "window_blocked" is a maturity verdict, and
#: "not_checked" is the scan's own budget - three different things that must not
#: be folded into one number.
SCAN_STATE_CATEGORIES = {
    "fetch_failed": CAT_DATA,
    "incomplete_data": CAT_DATA,
    "stale_data": CAT_AGE,
    "window_blocked": CAT_DTE,
    "window_unknown": CAT_DTE,
    "not_checked": CAT_SCAN_LIMIT,
}
EXPIRY_REASON_CATEGORIES = {
    "expiration_not_checkable": CAT_DATA,
    "section_gate": CAT_DTE,
}

# A pair reason may quote a leg reason ("strike 240.0 call: OI 1500 < 2000").
_LEG_PREFIX = re.compile(r"^strike\s+\S+\s+(?:call|put):\s*")

#: Ordered rules: (category, prefix/substring test). The FIRST match wins, which
#: mirrors the pipeline: a reason is produced by exactly one filter, so it must
#: be attributable to exactly one category.
_RULES = (
    # the age rule speaks its own words
    (CAT_AGE, lambda r: r.startswith("veraltet")),
    (CAT_AGE, lambda r: r.startswith("Aktualität unbekannt")),
    (CAT_AGE, lambda r: r.startswith("Zeitstempel in der Zukunft")),
    # pair-level arithmetic: an invalid debit/credit is not a liquidity problem
    (CAT_PRICE, lambda r: r.startswith("Debit ")),
    (CAT_PRICE, lambda r: r.startswith("Credit ")),
    (CAT_PRICE, lambda r: r.startswith("Credit nicht bestimmbar")),
    (CAT_COSTS, lambda r: r.startswith("Nach Kosten nicht gewinnfähig")),
    (CAT_BUDGET, lambda r: "> Budget" in r),
    # the maturity window, on a contract and as the whole-section gate
    (CAT_DTE, lambda r: r.startswith("DTE ")),
    (CAT_DTE, lambda r: r.startswith("Verfall:")),
    (CAT_DTE, lambda r: r.startswith("Verfall ") and "Laufzeit" in r),
    # an IV band is a volatility opinion, not a liquidity verdict
    (CAT_VOLATILITY, lambda r: r.startswith("IV ") and r != "IV fehlt"),
    # a missing value is a data gap - named as one, never as "illiquid"
    (CAT_DATA, lambda r: r.endswith("fehlt")),
    (CAT_DATA, lambda r: r.startswith("Daten unvollständig")),
    # everything that measures the quote itself
    # "Spread 12.0% > 10.0%", its penny-zone sibling "absoluter Spread ..."
    # and the missing-quote case "Spread: kein Geld-/Briefkurs"
    (CAT_LIQUIDITY, lambda r: "Spread" in r),
    (CAT_LIQUIDITY, lambda r: r.startswith("OI ")),
    (CAT_LIQUIDITY, lambda r: r.startswith("Volumen ")),
    (CAT_LIQUIDITY, lambda r: r.startswith("Größe unter Minimum")),
    (CAT_LIQUIDITY, lambda r: "illiquide" in r),
)


def categorise(reason):
    """Category key of ONE drop reason. Unknown reasons land in ``CAT_OTHER``."""
    text = _LEG_PREFIX.sub("", str(reason or "").strip())
    for key, test in _RULES:
        if test(text):
            return key
    return CAT_OTHER


def _not_checked(criteria):
    """Filters that were switched OFF for this request - named, not guessed.

    Only a filter that really did not run belongs here. A category that ran and
    found nothing is simply absent from the counts, which is a statement of its
    own; claiming "0 because of X" for a filter that never ran would be a lie.
    """
    if criteria is None:
        return []
    aus = []
    if getattr(criteria, "max_quote_age_min", None) is None:
        aus.append(CATEGORY_LABELS[CAT_AGE])
    if getattr(criteria, "min_oi", None) is None and not getattr(criteria, "require_oi", False):
        aus.append(f"{CATEGORY_LABELS[CAT_LIQUIDITY]} (OI-Grenze aus)")
    if getattr(criteria, "max_spread_pct", None) is None and getattr(criteria, "max_spread_abs", None) is None:
        aus.append(f"{CATEGORY_LABELS[CAT_LIQUIDITY]} (Spread-Grenze aus)")
    if getattr(criteria, "iv_min", None) is None and getattr(criteria, "iv_max", None) is None:
        aus.append(CATEGORY_LABELS[CAT_VOLATILITY])
    if getattr(criteria, "budget", None) is None:
        aus.append(CATEGORY_LABELS[CAT_BUDGET])
    return aus


def summarise(dropped, level, criteria=None):
    """Compact exclusion overview for ONE unit.

    ``dropped`` is what the pipeline returned: ``[(item, reason), ...]`` at
    contract and pair level, ``[(None, reason), ...]`` for a section gate, or a
    list of reason strings for the scan's expiry/symbol states.

    The result carries ONE counted number for ONE unit. There is deliberately no
    grand total: contracts and spread combinations are different things, and a
    sum over them would be a fiction with a number attached.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level: {level!r}")
    gruende = []
    for eintrag in dropped or ():
        if isinstance(eintrag, str):
            gruende.append(eintrag)
            continue
        try:
            _item, reason = eintrag
        except (TypeError, ValueError):
            gruende.append(str(eintrag))
            continue
        gruende.append("" if reason is None else str(reason))
    zaehler = {}
    unbekannt = []
    for reason in gruende:
        key = categorise(reason)
        zaehler[key] = zaehler.get(key, 0) + 1
        if key == CAT_OTHER and reason.strip() and reason not in unbekannt:
            unbekannt.append(reason)
    kategorien = [
        {"key": key, "label": CATEGORY_LABELS[key], "count": zaehler[key]}
        for key in sorted(zaehler, key=lambda k: (-zaehler[k], CATEGORY_ORDER.index(k)))
    ]
    return {
        "level": level,
        "unit": UNIT_LABELS[level],
        "unit_singular": UNIT_SINGULAR[level],
        "counted": len(gruende),
        # The one sentence a reader must not have to infer: this is a count of
        # FIRST reasons, not a cause analysis.
        "first_reason_only": True,
        "first_reason_note": (
            f"Je {UNIT_SINGULAR[level]} zählt genau ein Grund: der erste Filter, "
            f"der ihn abgelehnt hat. Weitere zutreffende Gründe werden nicht "
            f"gezählt und deshalb auch nicht behauptet."
        ),
        "categories": kategorien,
        "unmapped": unbekannt,
        "not_checked": _not_checked(criteria),
    }


def headline(summary):
    """One German line for the page - or None when nothing was excluded."""
    if not summary or not summary["counted"]:
        return None
    teile = [f"{k['label']} {k['count']}" for k in summary["categories"]]
    return (f"{summary['unit']}: {summary['counted']} ausgeschlossen - "
            + " · ".join(teile)
            + " (je " + summary["unit_singular"] + " zählt der erste Grund)")


def combine(summaries):
    """List of summaries with a stable order (contracts, pairs, expiries, stocks).

    No aggregation across levels: the caller renders one block per unit, and the
    order is the order a reader needs (the finest unit first).
    """
    nach_ebene = {s["level"]: s for s in summaries or () if s}
    return [nach_ebene[level] for level in LEVELS if level in nach_ebene]


def summarise_scan(counts, entries=None, criteria=None):
    """Exclusion overview of a whole earnings scan - TWO units, never one sum.

    ``counts`` is the scan's per-status tally (one row per title), ``entries``
    the scan's rows (used for the expiries a title had to skip). The result is a
    list of summaries in the order a reader needs them: stocks, then expiries.

    A title the scan did not reach is reported as a scan-limit fact, not as a
    data problem - the two used to be indistinguishable in the coverage line.
    """
    zaehler = {}
    for status, anzahl in (counts or {}).items():
        key = SCAN_STATE_CATEGORIES.get(status)
        if key is None or not anzahl:
            continue
        zaehler[key] = zaehler.get(key, 0) + int(anzahl)
    aktien = _summary_from_counts(LEVEL_SYMBOL, zaehler, criteria)

    verfall_gruende = {}
    for eintrag in entries or ():
        for skip in (eintrag.get("skipped_expirations") or ()):
            grund = str(skip.get("reason") or "")
            key = EXPIRY_REASON_CATEGORIES.get(grund, CAT_OTHER)
            verfall_gruende[key] = verfall_gruende.get(key, 0) + 1
    verfaelle = _summary_from_counts(LEVEL_EXPIRY, verfall_gruende, criteria)

    # Deliberately NOT through combine(): a scan answers stocks first (the
    # reader asked for stocks, an expiry is a detail below one), while the
    # screen's contract/pair/expiry order stays as it is.
    return [block for block in (aktien, verfaelle) if block["counted"]]


def _summary_from_counts(level, zaehler, criteria):
    """One summary from an already-counted dict of category totals."""
    kategorien = [
        {"key": key, "label": CATEGORY_LABELS[key], "count": zaehler[key]}
        for key in sorted(zaehler, key=lambda k: (-zaehler[k], CATEGORY_ORDER.index(k)))
        if zaehler[key]
    ]
    return {
        "level": level,
        "unit": UNIT_LABELS[level],
        "unit_singular": UNIT_SINGULAR[level],
        "counted": sum(k["count"] for k in kategorien),
        "first_reason_only": True,
        "first_reason_note": (
            f"Je {UNIT_SINGULAR[level]} zählt genau ein Zustand - der schwerste, "
            f"der auf ihn zutrifft. Weitere Gründe werden nicht gezählt."
        ),
        "categories": kategorien,
        "unmapped": [],
        "not_checked": _not_checked(criteria),
    }
