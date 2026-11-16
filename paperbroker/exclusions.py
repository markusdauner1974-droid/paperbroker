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

from .texts import t

# ---------------------------------------------------------------------- #
# levels (what one line of the summary counts)
# ---------------------------------------------------------------------- #

LEVEL_CONTRACT = "contract"
LEVEL_PAIR = "pair"
LEVEL_EXPIRY = "expiry"
LEVEL_SYMBOL = "symbol"
LEVELS = (LEVEL_CONTRACT, LEVEL_PAIR, LEVEL_EXPIRY, LEVEL_SYMBOL)
#: Level -> (plural key, singular key). The words live in the catalogue; this
#: table only knows WHERE they are, so a language switch touches no code here.
_UNIT_KEYS = {
    LEVEL_CONTRACT: ("exclusion.unit.contracts", "exclusion.unit.contract"),
    LEVEL_PAIR: ("exclusion.unit.pairs", "exclusion.unit.pair"),
    LEVEL_EXPIRY: ("exclusion.unit.expiries", "exclusion.unit.expiry"),
    LEVEL_SYMBOL: ("exclusion.unit.symbols", "exclusion.unit.symbol"),
}


def unit_label(level):
    """``LEVEL_CONTRACT`` -> ``"Kontrakte"`` in the active language."""
    return t(_UNIT_KEYS[level][0])


def unit_singular(level):
    """``LEVEL_CONTRACT`` -> ``"Kontrakt"`` - the unit ONE reason is counted in."""
    return t(_UNIT_KEYS[level][1])

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

#: Category -> catalogue key. NOTE: ``categorise`` below matches the German
#: reason TEXT, so the reasons have to stay German for the rules to keep
#: working - see the note above ``_RULES``.
_CATEGORY_KEYS = {
    CAT_SCAN_LIMIT: "exclusion.category.scan_limit",
    CAT_LIQUIDITY: "exclusion.category.liquidity",
    CAT_AGE: "exclusion.category.age",
    CAT_DTE: "exclusion.category.dte",
    CAT_VOLATILITY: "exclusion.category.volatility",
    CAT_PRICE: "exclusion.category.price",
    CAT_BUDGET: "exclusion.category.budget",
    CAT_COSTS: "exclusion.category.costs",
    CAT_DATA: "exclusion.category.data",
    CAT_OTHER: "exclusion.category.other",
}


def category_label(category):
    """``CAT_LIQUIDITY`` -> ``"Liquidität"`` in the active language."""
    return t(_CATEGORY_KEYS[category])

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


class Reason(str):
    """A drop reason that still IS its text, and knows which filter wrote it.

    The summary groups reasons by category. Today it has to READ the German
    sentence to do that (``_RULES`` below), which means the reasons can never be
    translated: an English "open interest missing" would fall through every
    prefix test and land in ``sonstiges``. A code is the stable half - the
    sentence is presentation, the code is what happened.

    It is a ``str`` subclass on purpose, so nothing downstream changes: the
    payload still carries a string, ``json`` still writes it as one, the tuple
    shapes ``(item, reason)`` stay the same, and every existing comparison
    against a plain string keeps working. Only ``categorise`` treats it
    differently, and only to prefer the code over the text.
    """

    #: Der Code des Filters, der diesen Grund geschrieben hat.
    code: str

    # Kein __slots__: auf einer str-Unterklasse ist das je nach Interpreter
    # nicht erlaubt (CPython lehnt nichtleere __slots__ fuer Unterklassen
    # variabler Builtins ab), und der Gewinn waere ein einziges Attribut.
    def __new__(cls, text, code):
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


#: Code -> category. The codes that exist today: the scan states and the expiry
#: reasons of this module, and the funnel's own rejection reasons. A producer
#: that starts tagging hands its code in here, so the mapping stays one table
#: and not one table per producer.
_CODE_CATEGORIES = {
    **SCAN_STATE_CATEGORIES,
    **EXPIRY_REASON_CATEGORIES,
    # Die Schluessel sind die Werte der REASON_*-Konstanten aus gap_funnel.py,
    # nicht ihre Namen: getaggt wird der Wert, und ein Tippfehler hier faellt
    # nicht auf, sondern wirft den Grund still in "sonstiges". Der Test
    # "every_funnel_reason_has_a_category" haelt beide Seiten zusammen.
    "not_optionable": CAT_DATA,            # REASON_NOT_OPTIONABLE
    "chain_fetch_failed": CAT_DATA,        # _CODE_CHAIN_FAILED
    "no_next_earnings": CAT_DATA,          # REASON_NO_NEXT_EARNINGS
    "no_expiry_after_earnings": CAT_DTE,   # REASON_NO_EXPIRY
    "no_spread_ge_rr": CAT_LIQUIDITY,      # REASON_NO_SPREAD
    "move_too_far": CAT_OTHER,             # REASON_MOVE_TOO_FAR
    "deadline_exceeded": CAT_SCAN_LIMIT,   # CODE_DEADLINE
    # --- die Filter des Screener (screener.py) --------------------------- #
    # Der Code benennt den FILTER, nicht die Formulierung. Die Kategorie ist
    # heute genau die, die die Textregel darunter liefert - der Test
    # "code_and_text_agree_today" haelt beide Seiten wortgleich zusammen.
    "iv_zero_illiquid": CAT_LIQUIDITY,
    "oi_zero_illiquid": CAT_LIQUIDITY,
    "quote_missing": CAT_LIQUIDITY,
    "spread_abs_above": CAT_LIQUIDITY,
    "spread_pct_above": CAT_LIQUIDITY,
    "oi_missing": CAT_DATA,
    "oi_below_min": CAT_LIQUIDITY,
    "volume_missing": CAT_DATA,
    "volume_below_min": CAT_LIQUIDITY,
    "iv_missing": CAT_DATA,
    "iv_below_min": CAT_VOLATILITY,
    "iv_above_max": CAT_VOLATILITY,
    "dte_missing": CAT_DATA,
    "dte_below_min": CAT_DTE,
    "dte_above_max": CAT_DTE,
    "size_below_min": CAT_LIQUIDITY,
    "credit_unknown": CAT_PRICE,
    "credit_out_of_range": CAT_PRICE,
    "debit_out_of_range": CAT_PRICE,
    "costs_exceed_profit": CAT_COSTS,
    "budget_exceeded": CAT_BUDGET,
    # --- das Datenalter (quote_age.py) ----------------------------------- #
    "age_missing": CAT_AGE,
    "age_unreadable": CAT_AGE,
    "age_clock": CAT_AGE,
    "age_future": CAT_AGE,
    "age_stale": CAT_AGE,
}


def code_category(code):
    """Category of a reason CODE, or ``None`` when the code is unknown."""
    return _CODE_CATEGORIES.get(str(code))

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
    # Ein getaggter Grund nennt seine Kategorie selbst - der Text ist dann nur
    # noch Anzeige und darf uebersetzt werden, ohne die Zuordnung zu brechen.
    tagged = code_category(getattr(reason, "code", None))
    if tagged is not None:
        return tagged
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
        aus.append(category_label(CAT_AGE))
    if getattr(criteria, "min_oi", None) is None and not getattr(criteria, "require_oi", False):
        aus.append(t("exclusion.not_checked.oi_off", label=category_label(CAT_LIQUIDITY)))
    if getattr(criteria, "max_spread_pct", None) is None and getattr(criteria, "max_spread_abs", None) is None:
        aus.append(t("exclusion.not_checked.spread_off", label=category_label(CAT_LIQUIDITY)))
    if getattr(criteria, "iv_min", None) is None and getattr(criteria, "iv_max", None) is None:
        aus.append(category_label(CAT_VOLATILITY))
    if getattr(criteria, "budget", None) is None:
        aus.append(category_label(CAT_BUDGET))
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
        # NICHT str(reason): ein Reason ist schon ein str, und str() wuerde
        # seine .code wegwerfen - die Kategorie kaeme dann wieder aus dem
        # deutschen Satz. Nur echte Nicht-Strings werden umgewandelt.
        if reason is None:
            gruende.append("")
        elif isinstance(reason, str):
            gruende.append(reason)
        else:
            gruende.append(str(reason))
    zaehler = {}
    unbekannt = []
    for reason in gruende:
        key = categorise(reason)
        zaehler[key] = zaehler.get(key, 0) + 1
        if key == CAT_OTHER and reason.strip() and reason not in unbekannt:
            unbekannt.append(reason)
    kategorien = [
        {"key": key, "label": category_label(key), "count": zaehler[key]}
        for key in sorted(zaehler, key=lambda k: (-zaehler[k], CATEGORY_ORDER.index(k)))
    ]
    return {
        "level": level,
        "unit": unit_label(level),
        "unit_singular": unit_singular(level),
        "counted": len(gruende),
        # The one sentence a reader must not have to infer: this is a count of
        # FIRST reasons, not a cause analysis.
        "first_reason_only": True,
        "first_reason_note": t("exclusion.note.first_reason", unit=unit_singular(level)),
        "categories": kategorien,
        "unmapped": unbekannt,
        "not_checked": _not_checked(criteria),
    }


def headline(summary):
    """One German line for the page - or None when nothing was excluded."""
    if not summary or not summary["counted"]:
        return None
    teile = [f"{k['label']} {k['count']}" for k in summary["categories"]]
    return t(
        "exclusion.headline",
        unit=summary["unit"],
        counted=summary["counted"],
        parts=" · ".join(teile),
        unit_one=summary["unit_singular"],
    )


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
        {"key": key, "label": category_label(key), "count": zaehler[key]}
        for key in sorted(zaehler, key=lambda k: (-zaehler[k], CATEGORY_ORDER.index(k)))
        if zaehler[key]
    ]
    return {
        "level": level,
        "unit": unit_label(level),
        "unit_singular": unit_singular(level),
        "counted": sum(k["count"] for k in kategorien),
        "first_reason_only": True,
        "first_reason_note": t("exclusion.note.worst_state", unit=unit_singular(level)),
        "categories": kategorien,
        "unmapped": [],
        "not_checked": _not_checked(criteria),
    }
