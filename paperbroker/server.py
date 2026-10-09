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

import re

import arrow
from flask import Flask, abort, jsonify, render_template, request

from .adapters.quotes.CBOEQuoteAdapter import CBOEQuoteAdapter
from .assets import calendar_days_between
from .PaperBroker import PaperBroker
from .screener import OptionScreener, ScreenerCriteria

_TICKER_RE = re.compile(r"^[A-Z]{1,6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# fetch-age thresholds for the freshness lamp (design decision, not
# measurement): the CBOE delayed feed is ~15 min behind by nature, so the
# lamp measures data age; the feed delay itself is communicated as a
# static banner, not as a red state.
_FRESH_GREEN_MIN = 30
_FRESH_YELLOW_MIN = 120


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

    def _selectable_dates(dates):
        # B-11: the picker must only OFFER dates that can still hit. A
        # chain runs years out, but the screener window is ~6 weeks, so
        # most listed dates were dead entries in the dropdown. Validation
        # keeps using the FULL list, so a bookmarked date that fell out of
        # the window still answers 200 with zero hits instead of 400.
        criteria = screener.criteria
        as_of = now_fn()
        return [d for d in dates
                if _in_dte_window(calendar_days_between(d, as_of), criteria)]

    def _parse_args():
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
            selectable = _selectable_dates(dates)
        if expiration:
            if not _DATE_RE.match(expiration):
                abort(400, description="invalid expiration (YYYY-MM-DD expected)")
            # expiration must be one of the chain's real dates - prevents
            # probing arbitrary CBOE URLs with made-up dates
            if not dates or expiration not in dates:
                abort(400, description="expiration is not a listed date for this ticker")
        elif not ticker:
            abort(400, description="ticker and expiration required")
        return ticker, expiration, dates, selectable

    @app.get("/api/screen")
    def api_screen():
        ticker, expiration, dates, selectable = _parse_args()
        if not expiration:
            # picker stage: offer only the dates that can still hit
            return jsonify(ticker=ticker, expiration=None,
                           listed_dates=selectable, results=[], freshness=None)
        quotes = broker.get_options(ticker, expiration)
        results, dropped = screener.screen(quotes, now_fn=now_fn)
        return jsonify(
            ticker=ticker,
            expiration=expiration,
            chain_size=len(quotes),
            candidates=len(results),
            dropped=len(dropped),
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
        results = dropped = dates = selectable = quotes = None
        try:
            if ticker:
                if not _TICKER_RE.match(ticker):
                    error = "Ungueltiger Ticker - 1-6 Buchstaben A-Z."
                else:
                    dates = broker.get_expiration_dates(ticker)
                    selectable = _selectable_dates(dates)
                    if expiration:
                        if not _DATE_RE.match(expiration):
                            error = "Ungueltiges Verfallsdatum - JJJJ-MM-TT."
                        elif not dates or expiration not in dates:
                            error = "Verfallsdatum ist fuer diesen Ticker nicht gelistet."
                        else:
                            quotes = broker.get_options(ticker, expiration)
                            results, dropped = screener.screen(
                                quotes, trend=_load_trend(ticker), now_fn=now_fn)
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
        return render_template(
            "screen.html", ticker=ticker, expiration=expiration,
            dates=offered, dates_loaded=dates is not None,
            results=results, dropped_count=len(dropped) if dropped else 0,
            error=error, freshness=_freshness_lamp(lamp_source, now_fn=now_fn),
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


def _freshness_lamp(quotes, now_fn=None):
    """Green/yellow/red from the youngest quote in the FETCHED chain.

    (CodeRabbit finding: pass the raw chain, not the screened results -
    the stale-quote guard can remove every candidate and hide the very
    staleness the lamp is supposed to report.)

    Accepts OptionQuote objects or ScreenResults (duck-typed). None/no
    rows -> None: the template shows 'keine Daten' instead of inventing
    a traffic light without input.
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
    age_min = (now - newest).total_seconds() / 60.0
    if age_min < _FRESH_GREEN_MIN:
        return "green"
    if age_min < _FRESH_YELLOW_MIN:
        return "yellow"
    return "red"
