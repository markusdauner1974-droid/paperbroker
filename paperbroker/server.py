# ---------------------------------------------------------------------- #
# Web display for the options screener (Phase 5).                         #
#                                                                          #
# Design (debate panel, 2026-10-07):                                       #
# - SSR with Jinja2 + exactly one JSON endpoint, NO htmx, no frontend      #
#   build chain (single-user LAN on a QNAP; maintainability first).        #
# - GET /                SSR page: ticker/expiration form + results table  #
# - GET /api/screen      JSON (stable interface for Phase 6 Telegram bot)  #
# - GET /healthz         liveness ONLY (no CBOE/xang calls - a coupled     #
#                        healthcheck turns unhealthy when an external      #
#                        service is down)                                  #
# - ticker + expiration are validated BEFORE they touch the CBOE URL     #
#   (a raw ticker would allow path injection against cdn.cboe.com).      #
# ---------------------------------------------------------------------- #

import math
import re

import arrow
from flask import Flask, abort, jsonify, render_template, request

from .adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter
from .assets import calendar_days_between
from .PaperBroker import PaperBroker
from .screener import OptionScreener, ScreenerCriteria, screen

_TICKER_RE = re.compile(r"^[A-Z]{1,6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# fetch-age thresholds for the freshness lamp (design decision, not
# measurement): the CBOE delayed feed is ~15 min behind by nature, so the
# lamp measures data age; the feed delay itself is communicated as a
# static banner, not as a red state.
_FRESH_GREEN_MIN = 30
_FRESH_YELLOW_MIN = 120

# UX-1 (Ausbau-Slice 1): the screener hands back one free-text reason per
# dropped contract ("stale 257 min", "OI 7 < 500"). Counting the raw texts
# would give roughly one line per contract (measured: 48 distinct texts for
# 97 drops with the age guard off), so numbers are folded to a placeholder.
# It is a NUMBER TOKEN, not a single digit: folding digit by digit would
# split "7.41" and "10.2" into different groups although both are "spread
# too wide". A group whose raw texts are all identical keeps the raw text,
# so the measured value stays on the page ("stale 257 min", not "stale # min").
_REASON_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------- #
# Ausbau-Slice 2 (A): operator-settable filter criteria.                  #
#                                                                          #
# A request value overrides the injected base only when it is present and  #
# not empty, so a request without parameters keeps whatever the app was    #
# built with (the picker tests inject their own criteria).                  #
# The switch ignore_quote_age=1 is the ONLY way to disable the age guard:   #
# 0 is a valid, sharp bound ("everything older than 0 s"), not "off".       #
# ---------------------------------------------------------------------- #
_CRITERIA_INT_FIELDS = ("dte_min", "dte_max", "min_oi", "min_volume", "max_quote_age_min")
_CRITERIA_FLOAT_FIELDS = ("max_spread_pct",)
_CRITERIA_FIELDS = _CRITERIA_INT_FIELDS + _CRITERIA_FLOAT_FIELDS
_CRITERIA_ECHO_FIELDS = tuple(ScreenerCriteria.__dataclass_fields__)
_IGNORED_PARAMS = ("ticker", "expiration", "ignore_quote_age")


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
    """All 17 effective criteria as a JSON-able dict (None -> null).

    One serialisation for every field - a whitelist would drift.
    """
    return {name: getattr(criteria, name) for name in _CRITERIA_ECHO_FIELDS}


def create_app(quote_adapter=None, screener=None, now_fn=None):
    """now_fn: injectable clock for tests (default: real arrow.get).

    The screener's 30-min-stale guard compares quote_timestamp against
    now() - a hardcoded fixture timestamp goes stale in real time, so
    tests MUST pin the clock instead of freezing the fixtures.
    """
    app = Flask(__name__, template_folder="templates")
    if now_fn is None:
        now_fn = arrow.get

    if quote_adapter is None:
        quote_adapter = CBOEQuoteAdapter()
    if screener is None:
        screener = OptionScreener(ScreenerCriteria())
    broker = PaperBroker(quote_adapter=quote_adapter)

    # JSON error contract for the API (CodeRabbit finding): abort() would
    # send Flask's HTML error page, which a JSON client cannot parse
    @app.errorhandler(400)
    def _bad_request(err):
        if request.path.startswith("/api/"):
            return jsonify(error=getattr(err, "description", "bad request")), 400
        return err

    @app.get("/healthz")
    def healthz():
        # liveness only: no CBOE, no xang - must stay green in LAN tests
        # even when the internet is down
        return jsonify(status="ok")

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
        out = [{"reason": next(iter(e["raw"])) if len(e["raw"]) == 1 else pattern,
                "count": e["count"]}
               for pattern, e in groups.items()]
        out.sort(key=lambda g: -g["count"])   # stable: ties keep pipeline order
        return out

    def _selectable_dates(dates, criteria):
        # B-11: the picker must only OFFER dates that can still hit. A
        # chain runs years out, but the screener window is ~6 weeks, so
        # most listed dates were dead entries in the dropdown. Validation
        # keeps using the FULL list, so a bookmarked date that fell out of
        # the window still answers 200 with zero hits instead of 400.
        as_of = now_fn()
        return [d for d in dates
                if _in_dte_window(calendar_days_between(d, as_of), criteria)]

    def _parse_args():
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
            selectable = _selectable_dates(dates, criteria)
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

    @app.get("/api/screen")
    def api_screen():
        ticker, expiration, dates, selectable, criteria, unknown = _parse_args()
        if not expiration:
            # picker stage: offer only the dates that can still hit. No
            # screening happened, so the drop list is empty - but the key
            # is always present, so a consumer never needs a key check.
            return jsonify(ticker=ticker, expiration=None,
                           listed_dates=selectable, results=[], freshness=None,
                           dropped_reasons=[])
        quotes = broker.get_options(ticker, expiration)
        results, dropped = screen(quotes, criteria, now_fn=now_fn)
        return jsonify(
            ticker=ticker,
            expiration=expiration,
            chain_size=len(quotes),
            candidates=len(results),
            dropped=len(dropped),
            dropped_reasons=_drop_reason_groups(dropped),
            criteria=_criteria_echo(criteria),
            freshness=_freshness_lamp(quotes, now_fn=now_fn),
            results=[
                {
                    "symbol": r.quote.asset.symbol,
                    "bid": r.quote.bid,
                    "ask": r.quote.ask,
                    "iv": r.quote.iv,
                    "open_interest": r.quote.open_interest,
                    "volume": r.quote.volume,
                    "delta": r.quote.delta,
                    "score": round(r.score, 4),
                    "spread_pct": round(r.spread_pct, 2),
                    "reasons": r.reasons,
                }
                for r in results
            ],
        )

    @app.get("/")
    def index():
        ticker = request.args.get("ticker", "").upper().strip()
        expiration = request.args.get("expiration", "").strip()
        error = None
        hint = None
        results = dropped = dates = selectable = quotes = None
        criteria, criteria_error, unknown = _criteria_from_request(
            screener.criteria, request.args)
        if unknown:
            hint = ("Unbekannte Parameter ignoriert: " + ", ".join(unknown)
                    + " (bedienbar sind: " + ", ".join(_CRITERIA_FIELDS)
                    + ", ignore_quote_age)")
        if criteria_error:
            error = criteria_error
        try:
            if ticker and not criteria_error:
                if not _TICKER_RE.match(ticker):
                    error = "Ungueltiger Ticker - 1-6 Buchstaben A-Z."
                else:
                    dates = broker.get_expiration_dates(ticker)
                    selectable = _selectable_dates(dates, criteria)
                    if expiration:
                        if not _DATE_RE.match(expiration):
                            error = "Ungueltiges Verfallsdatum - JJJJ-MM-TT."
                        elif not dates or expiration not in dates:
                            error = "Verfallsdatum ist fuer diesen Ticker nicht gelistet."
                        else:
                            quotes = broker.get_options(ticker, expiration)
                            results, dropped = screen(
                                quotes, criteria, trend=_load_trend(ticker), now_fn=now_fn)
        except Exception:
            # no exception text in the page (CodeRabbit finding CWE-209:
            # CboeRequestError carries raw requests messages) - log it,
            # show a generic box; the server log holds the details
            app.logger.exception("screen failed for %s %s", ticker, expiration)
            error = "Marktdaten-Fehler - Details im Server-Log."
        lamp_source = quotes if quotes is not None else results
        # B-14: the <select> and the validation must read the same list. The
        # offer is the DTE-filtered list, but a bookmarked date that is real
        # yet filtered out must stay in it - otherwise the form renders one
        # date while screening another. Only dates that are really in the
        # chain qualify, so a made-up date still cannot get in.
        offered = list(selectable) if selectable else []
        if expiration and dates and expiration in dates and expiration not in offered:
            offered = [expiration] + offered
        age_min = _newest_quote_age_min(lamp_source, now_fn=now_fn)
        # D (Ausbau-Slice 3): the row order is a property of the RESULT, not
        # of the template. Sorting here keeps the order checkable in the test
        # client (there is no JS runner) and it is the order every consumer
        # sees. Tie order stays EXACTLY what the template's `|sort|reverse`
        # produced: stable ascending sort, then reversed. `sorted(reverse=True)`
        # would flip contracts with EQUAL scores - a silent behaviour change.
        if results is not None:
            results = list(reversed(sorted(results, key=lambda r: r.score)))
        # the header shows the DTE the DTE filter used (screener.filter_dte
        # reads quote.days_to_expiration) - the same field, not a second
        # computation. No rows -> no DTE claim.
        dte = results[0].quote.days_to_expiration if results else None
        return render_template(
            "screen.html", ticker=ticker, expiration=expiration,
            dates=offered, dates_loaded=dates is not None,
            results=results, dropped_count=len(dropped) if dropped else 0,
            dropped_reasons=_drop_reason_groups(dropped),
            criteria=criteria, age_off=criteria.max_quote_age_min is None,
            hint=hint,
            error=error, freshness=_lamp_state(age_min),
            freshness_age=None if age_min is None else int(round(age_min)),
            # the limit shown in the header is the EFFECTIVE one (A): it is the
            # value the operator picked, not the app default
            freshness_limit=criteria.max_quote_age_min,
            dte=dte,
            trend=_trend_display(ticker) if ticker else None,
            feed_note="CBOE delayed feed - Daten ca. 15 min hinter Echtzeit")

    return app


class _TrendInfo:
    """UI-facing trend snapshot (direction + availability, no crash)."""

    def __init__(self, direction, as_of=None, source_ok=True):
        self.direction = direction
        self.as_of = as_of
        self.source_ok = source_ok


def _load_trend(ticker):
    """Trend via provider if configured, else None (screener ignores it).

    Failure of the trend source must NOT look like a real 'flat' market:
    the UI receives source_ok=False and shows 'Trend nicht verfuegbar'.
    """
    provider = _get_trend_provider()
    if provider is None:
        return None
    try:
        return provider.get_trend(ticker)
    except Exception:
        return _TrendInfo("flat", source_ok=False)


def _get_trend_provider():
    # Phase 5: none wired yet (xang1234 gets connected in a follow-up);
    # the protocol + Fake provider land first so the wiring is a one-liner
    return None


def _trend_display(ticker):
    sig = _load_trend(ticker)
    if sig is None:
        return None
    if not getattr(sig, "source_ok", True):
        return {"direction": "unavailable", "label": "Trend nicht verfuegbar"}
    return {"direction": sig.direction, "label": _trend_label(sig.direction)}


def _trend_label(direction):
    return {"up": "steigend", "down": "fallend", "flat": "neutral"}.get(
        direction, direction)


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
    """
    if not quotes:
        return None
    now = now_fn() if now_fn is not None else arrow.get()
    newest = None
    for item in quotes:
        q = getattr(item, "quote", item)  # ScreenResult or OptionQuote
        ts = getattr(q, "quote_timestamp", None)
        if ts is None:
            continue
        try:
            t = arrow.get(str(ts).replace(" ", "T"))
        except Exception:
            continue
        if newest is None or t > newest:
            newest = t
    if newest is None:
        return None
    return (now - newest).total_seconds() / 60.0


def _lamp_state(age_min):
    """green/yellow/red from a quote age; None age -> None (no lamp)."""
    if age_min is None:
        return None
    if age_min < _FRESH_GREEN_MIN:
        return "green"
    if age_min < _FRESH_YELLOW_MIN:
        return "yellow"
    return "red"


def _freshness_lamp(quotes, now_fn=None):
    """Traffic light for the fetched chain (same value as the age above)."""
    return _lamp_state(_newest_quote_age_min(quotes, now_fn=now_fn))
