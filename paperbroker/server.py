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


def create_app(quote_adapter=None, screener=None):
    app = Flask(__name__, template_folder="templates")

    if quote_adapter is None:
        quote_adapter = CBOEQuoteAdapter()
    if screener is None:
        screener = OptionScreener(ScreenerCriteria())
    broker = PaperBroker(quote_adapter=quote_adapter)

    @app.get("/healthz")
    def healthz():
        # liveness only: no CBOE, no xang - must stay green in LAN tests
        # even when the internet is down
        return jsonify(status="ok")

    def _parse_args():
        ticker = request.args.get("ticker", "").upper().strip()
        if not _TICKER_RE.match(ticker):
            abort(400, description="invalid ticker (1-6 letters A-Z expected)")
        expiration = request.args.get("expiration", "").strip()
        if not _DATE_RE.match(expiration):
            abort(400, description="invalid expiration (YYYY-MM-DD expected)")
        # expiration must be one of the chain's real dates - prevents
        # probing arbitrary CBOE URLs with made-up dates
        dates = broker.get_expiration_dates(ticker)
        if not dates or expiration not in dates:
            abort(400, description="expiration is not a listed date for this ticker")
        return ticker, expiration, dates

    @app.get("/api/screen")
    def api_screen():
        ticker, expiration, dates = _parse_args()
        quotes = broker.get_options(ticker, expiration)
        results, dropped = screener.screen(quotes, now_fn=arrow.get)
        return jsonify(
            ticker=ticker,
            expiration=expiration,
            chain_size=len(quotes),
            candidates=len(results),
            dropped=len(dropped),
            freshness=_freshness_lamp(results),
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
        results = dropped = dates = None
        if ticker or expiration:
            if not (_TICKER_RE.match(ticker) and _DATE_RE.match(expiration)):
                error = "Ungueltige Eingabe - Ticker 1-6 Buchstaben, Verfall JJJJ-MM-TT."
            else:
                try:
                    dates = broker.get_expiration_dates(ticker)
                    if not dates or expiration not in dates:
                        error = "Verfallsdatum ist fuer diesen Ticker nicht gelistet."
                    else:
                        quotes = broker.get_options(ticker, expiration)
                        results, dropped = screener.screen(
                            quotes, trend=_load_trend(ticker), now_fn=arrow.get)
                except Exception as err:
                    error = f"Marktdaten-Fehler: {err}"
        return render_template(
            "screen.html", ticker=ticker, expiration=expiration,
            dates=dates, results=results, dropped_count=len(dropped) if dropped else 0,
            error=error, freshness=_freshness_lamp(results),
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


def _freshness_lamp(results):
    """Green/yellow/red from the youngest quote in the result set.

    None (no rows) -> None: the template shows 'keine Daten' instead of
    inventing a traffic light without input.
    """
    if not results:
        return None
    now = arrow.get()
    newest = None
    for r in results:
        ts = getattr(r.quote, "quote_timestamp", None)
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
