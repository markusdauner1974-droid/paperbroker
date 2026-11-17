"""The page route, moved 1:1 out of ``server.create_app``.

The trend block stays in ``server.py`` on purpose: two tests patch
``server._get_trend_provider`` and ``server._MCPTrendProvider``, so the page
reaches it through the context (``trend_for_screener``, ``trend_display``)
instead of importing it. Its own display helpers moved with it.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from flask import current_app, jsonify, render_template, request, url_for

from .. import texts
from ..earnings_calendar import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_ESTIMATED,
    SESSION_AFTER,
    SESSION_BEFORE,
    combine_earnings_and_expirations,
    is_us_trading_day,
    last_trading_day_before,
    next_trading_day_after,
)
from ..earnings_day_calendar import DAY_NEXT, DAY_PREV, DAY_TODAY, build_calendar
from ..screener import (
    DEFAULT_STRATEGY,
    STRATEGIES,
    STRATEGY_IDS,
    screen,
    strategy_option_type,
    vertical_spreads,
)
from ..texts import t
from ..view_support import (
    _CRITERIA_FIELDS,
    _DATE_RE,
    _TICKER_RE,
    SORT_LABELS,
    SORT_MODES,
    _age_within_limit,
    _criteria_from_request,
    _data_state,
    _drop_reason_groups,
    _exclusion_blocks,
    _feed_stamp,
    _lamp_state,
    _newest_quote_age_min,
    _option_symbols,
    _pair_search_block,
    _selectable_dates,
    _spread_be_gap_pct,
    _spread_return_pct,
    _strategy_selection,
    _us_today,
    _vertical_sort_key,
)
from .context import context


def _switch_links() -> list[dict[str, object]]:
    """Der Fuss-Wechsler - aus der Registry, nicht aus einer Liste im Template.

    Jede registrierte Sprache bekommt einen Eintrag; die gerade gelesene ist kein
    Link, sondern eine Markierung. Alle Abfrage-Parameter bleiben stehen, nur
    ``lang`` wird getauscht - sonst verliert ein Wechsel die Suche.
    """
    werte = {key: value for key, value in request.args.items() if key != "lang"}
    jetzt = texts.active_language()
    eintraege = []
    for code in texts.languages():
        eintraege.append({
            "code": code,
            "name": texts.language_name(code),
            "current": code == jetzt,
            "href": url_for("index", **{**werte, "lang": code}),
        })
    return eintraege


def index():
    ctx = context()
    ticker = request.args.get("ticker", "").upper().strip()
    expiration = request.args.get("expiration", "").strip()
    # The page shows no calendar and no per-ticker earnings panel, so it
    # asks no source for either: a plain load spends no adapter call (and
    # no network round trip). `?no_calendar` stays a known parameter only
    # so an old bookmark is not reported as an unknown one.
    error = None
    hint = None
    results = dropped = dates = selectable = quotes = None
    vertical_kept = vertical_dropped = None
    pair_coverage = {}
    criteria, criteria_error, unknown = _criteria_from_request(
        ctx.screener.criteria, request.args)
    strategy, spread_type, strategy_error, strategy_message = _strategy_selection(request.args)
    gewuenschte = (request.args.get("strategy") or "").strip().lower()
    if strategy_error == "unknown_option_type" and not gewuenschte:
        # Legacy behaviour, unchanged (D14) for the BARE old address: the page
        # names what is allowed and keeps computing the default. Only here -
        # the caller named nothing but a side.
        error = strategy_message
        strategy = DEFAULT_STRATEGY
        spread_type = strategy_option_type(strategy)
        strategy_error = None
    elif strategy_error:
        # A named strategy is NEVER replaced by a default: the request cannot be
        # honoured (unknown value, or a malformed legacy alias next to it), so the
        # page says so and computes nothing - the same answer /api/screen gives
        # with its 400. The radio keeps the id the caller asked for, so the form
        # does not silently show a different strategy than the request carried.
        error = strategy_message
        strategy = gewuenschte if gewuenschte in STRATEGIES else None
        spread_type = None if strategy is None else strategy_option_type(strategy)
    if unknown:
        hint = t("page_route.hint.unknown_params",
                 names=", ".join(unknown),
                 fields=", ".join([*_CRITERIA_FIELDS, "ignore_quote_age", "sort"]))
    if criteria_error:
        error = criteria_error
    try:
        if ticker and not criteria_error and not strategy_error:
            if not _TICKER_RE.match(ticker):
                error = t("page_route.error.invalid_ticker")
            else:
                dates = ctx.broker.get_expiration_dates(ticker)
                selectable = _selectable_dates(dates, criteria, ctx.now_fn)
                if expiration:
                    if not _DATE_RE.match(expiration):
                        error = t("page_route.error.invalid_expiration")
                    elif not dates or expiration not in dates:
                        error = t("page_route.error.expiration_not_listed")
                    else:
                        quotes = ctx.broker.get_options(ticker, expiration)
                        results, dropped = screen(
                            quotes, criteria, trend=ctx.trend_for_screener(ticker),
                            now_fn=ctx.now_fn)
                        # the RAW chain, not the screened candidates (B)
                        vertical_kept, vertical_dropped = vertical_spreads(
                            quotes, criteria, strategy=strategy, now_fn=ctx.now_fn,
                            coverage=pair_coverage)
    except Exception:
        # no exception text in the page (CodeRabbit finding CWE-209:
        # CboeRequestError carries raw requests messages) - log it,
        # show a generic box; the server log holds the details
        current_app.logger.exception("screen failed for %s %s", ticker, expiration)
        error = t("page_route.error.market_data")
    lamp_source = quotes if quotes is not None else results
    # B-14: the <select> and the validation must read the same list. The
    # offer is the DTE-filtered list, but a bookmarked date that is real
    # yet filtered out must stay in it - otherwise the form renders one
    # date while screening another. Only dates that are really in the
    # chain qualify, so a made-up date still cannot get in.
    offered = list(selectable) if selectable else []
    if expiration and dates and expiration in dates and expiration not in offered:
        offered = [expiration] + offered
    # Darstellung (Slice K): die Reihenfolge der ANZEIGE, nicht die der
    # Schnittstelle. /api/screen liefert unveraendert die Kettenreihenfolge -
    # dort haengen Zusicherungen dran; sortiert wird nur, was die Seite zeigt.
    # Unbekannte Werte behandelt _vertical_sort_key wie die Voreinstellung;
    # eine zweite Pruefung hier waere wirkungslos (als Mutant M5 gemessen).
    sort_mode = request.args.get("sort", "rendite_desc")
    vertical_rows = None if vertical_kept is None else [
        (v, _spread_return_pct(v), _spread_be_gap_pct(v))
        for v in sorted(vertical_kept, key=_vertical_sort_key(sort_mode, criteria))
    ]
    age_min = _newest_quote_age_min(lamp_source, now_fn=ctx.now_fn)
    # Datenmodus + Datenstand + Vorlaeufig-Marker. Dasselbe Objekt liefert
    # /api/screen - die Seite rechnet nichts nach.
    zustand = _data_state(lamp_source, criteria, ctx.now_fn)
    zustand["fetched_text"] = _german_stamp(zustand["fetched_at_utc"])
    # D (Ausbau-Slice 3): the row order is a property of the RESULT, not
    # of the template. Sorting here keeps the order checkable in the test
    # client (there is no JS runner) and it is the order every consumer
    # sees. Tie order stays EXACTLY what the template's `|sort|reverse`
    # produced: stable ascending sort, then reversed. `sorted(reverse=True)`
    # would flip contracts with EQUAL scores - a silent behaviour change.
    if results is not None:
        results = list(reversed(sorted(results, key=lambda r: r.score)))
    # the header shows the DTE the DTE filter used (ctx.screener.filter_dte
    # reads quote.days_to_expiration) - the same field, not a second
    # computation. No rows -> no DTE claim.
    # D8: two numbers, both named. The feed stamp is what the data says, the DTE
    # is what this scan sees - they used to disagree silently (the picker offered a
    # date the filter then dropped).
    top = results[0].quote if results else None
    dte = top.asset.get_days_to_expiration(ctx.now_fn()) if top is not None else None
    feed_time = getattr(top, "quote_timestamp", None) if top is not None else None
    # L: label and the two legs are printed from the same table the calculation
    # used - one source for the wording, no second copy in the template.
    spez = STRATEGIES.get(strategy or "")
    return render_template(
        "screen.html", ticker=ticker, expiration=expiration,
        feed_time=feed_time,
        dates=offered, dates_loaded=dates is not None,
        results=results, dropped_count=len(dropped) if dropped else 0,
        dropped_reasons=_drop_reason_groups(dropped),
        spread_type=spread_type,
        strategy=strategy,
        strategy_label=None if spez is None else spez["label"],
        strategy_kind=None if spez is None else spez["kind"],
        strategy_buy=None if spez is None else spez["buy"],
        strategy_sell=None if spez is None else spez["sell"],
        strategy_direction=None if spez is None else spez["direction"],
        verticals=vertical_rows,
        sort_links=_sort_links(request.args),
        sort_mode=sort_mode,
        sort_modes=SORT_MODES,
        sort_labels=SORT_LABELS,
        vertical_drops=_drop_reason_groups(vertical_dropped),
        # Suchart, Suchumfang und die Ausschlussuebersicht (je Einheit)
        pair_search=_pair_search_block(criteria, pair_coverage),
        exclusions=_exclusion_blocks(dropped, vertical_dropped, criteria),
        criteria=criteria, age_off=criteria.max_quote_age_min is None,
        hint=hint,
        error=error, freshness=_lamp_state(age_min),
        # E3: die Seite braucht dieselben zwei Antworten wie das JSON - die
        # allgemeine Lampe oben und die Nutzergrenze, getrennt benannt.
        age_within_limit=_age_within_limit(lamp_source, criteria, now_fn=ctx.now_fn),
        feed_stamp=_feed_stamp(lamp_source),
        freshness_age=None if age_min is None else int(round(age_min)),
        # the limit shown in the header is the EFFECTIVE one (A): it is the
        # value the operator picked, not the app default
        freshness_limit=criteria.max_quote_age_min,
        dte=dte,
        trend=ctx.trend_display(ticker) if ticker else None,
        # The day scan is the page's second action: what it may send and
        # what its table is called come from one server-side table.
        scan=scan_view_data(),
        feed_note=t("page_route.feed_note.cboe"),
        # The script's texts travel as one JSON block: one source, no second
        # catalogue file in JavaScript. The block also names the plural rule.
        js_i18n=texts.block("js"),
        lang=texts.active_language(),
        switch_links=_switch_links(),
        data_state=zustand)



def _german_stamp(iso_text):
    """``2026-11-08T06:00:00Z`` -> ``08.11.2026 06:00 UTC`` (source stamp, no guess)."""
    try:
        wert = datetime.strptime(str(iso_text), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return wert.strftime("%d.%m.%Y %H:%M UTC")


def _sort_links(args):
    """Eine Sortier-Adresse je Modus, MIT den uebrigen Parametern.

    Ohne sie wuerde ein Klick auf einen Spaltenkopf Ticker, Verfall und alle
    Filter verlieren - die Seite fiele auf die Auswahl zurueck.
    """
    links = {}
    for mode in SORT_MODES:
        paar = [(k, wert) for k, wert in args.items(multi=True) if k != "sort"]
        paar.append(("sort", mode))
        links[mode] = urlencode(paar)
    return links


def scan_view_data():
    """What the earnings-overview panel needs to run the day scan from the page.

    One source for both sides of the same contract:

    * ``strategies`` - the ids and the short column headers. The full labels
      name both legs ("Bull Call Spread (Debit)") and do not fit above a
      two-digit count, so the header prints the part before " Spread".
    * ``fields`` - the form fields the browser may copy into the scan request,
      built from the criteria table the route parses. A new filter therefore
      reaches the scan automatically; the alternative (a second list in the
      JavaScript) would silently drop it and the scan would then answer hits
      for filters the page did not ask for. ``strategy`` and the age switch are
      not criteria fields but ARE part of the request, so they are named here.
    * ``api`` - the route, spelled once.

    ``ticker``, ``expiration`` and ``sort`` are deliberately absent: they belong
    to the single-ticker view and the route refuses them as unknown parameters.
    """
    return {
        "api": "/api/v1/earnings-scan",
        "strategies": [
            {
                "id": sid,
                "label": STRATEGIES[sid]["label"],
                "short": STRATEGIES[sid]["label"].split(" Spread")[0],
            }
            for sid in STRATEGY_IDS
        ],
        "fields": list(_CRITERIA_FIELDS) + ["ignore_quote_age", "strategy"],
    }


def register(app) -> None:
    app.add_url_rule("/", endpoint="index", view_func=index, methods=["GET"])
    # JSON error contract for the API (CodeRabbit finding): abort() would send
    # Flask's HTML error page, which a JSON client cannot parse. Registered
    # here because _bad_request moved with the page it belongs to.
    app.register_error_handler(400, _bad_request)


def _option_note(applied, hidden, stale):
    """One German sentence for the filter: a number, or honestly no filter.

    ``hidden`` is the block-wide count of titles the list removed. An
    unavailable list must not name a number: a "0" would read like a result.
    """
    if not applied:
        return t("page_route.option_note.unavailable")
    satz = (t("page_route.option_note.hidden", count=hidden) if hidden
            else t("page_route.option_note.hidden_none"))
    if stale:
        satz += t("page_route.option_note.stale_suffix")
    return satz


def _bad_request(err):
    if request.path.startswith("/api/"):
        return jsonify(error=getattr(err, "description", "bad request")), 400
    return err



def _earnings_view(ticker, expirations, offered, ctx):
    """What the page shows - derived from the same calls the API makes.

    ``offered`` is the DTE-filtered list the expiration picker shows. Only
    dates from that list are printed: the page has a standing promise not to
    name a date outside the screener window (the picker test pins it), while
    the API still returns the full list. What is left out is named as a
    count, not silently dropped.
    """
    try:
        koerper = ctx.earnings_adapter.get_earnings(ticker)
        fenster = combine_earnings_and_expirations(
            koerper["earnings"], expirations, today=_us_today(ctx.now_fn)
        )
    except Exception:                                    # noqa: BLE001 - page must render
        current_app.logger.exception("earnings view failed for %s", ticker)
        return {"failed": True}
    eintrag = koerper["earnings"]
    cache = koerper.get("cache") or {}
    sichtbar = [d for d in fenster["expirations"]["after_earnings"] if d in set(offered or [])]
    return {
        "failed": False,
        "date": eintrag.get("next_earnings_date"),
        "session": _SESSION_TEXT.get(eintrag.get("release_session"), "UNBEKANNT"),
        "confirmation": _CONFIRMATION_TEXT.get(eintrag.get("date_confirmation"), "unbekannt"),
        "status": eintrag.get("status"),
        "source": eintrag.get("source"),
        "checked": eintrag.get("source_checked_at_utc"),
        "flags": eintrag.get("quality_flags") or [],
        "exit": fenster["exit"]["planned_last_exit_date"],
        "after": sichtbar,
        "after_hidden": len(fenster["expirations"]["after_earnings"]) - len(sichtbar),
        "selected": fenster["expirations"]["selected"],
        "eligible": fenster["decision"]["eligible"],
        "reasons": [grund["message"] for grund in fenster["decision"]["reasons"]],
        "notes": fenster["decision"]["notes"],
        "cache_state": cache.get("state"),
        "age_seconds": cache.get("age_seconds"),
        "checked_live": cache.get("state") == "live",
    }



def _calendar_view(ctx):
    """Yesterday/today/tomorrow for the start page - degrades, never raises.

    The three days are US calendar dates, because the calendar is a US one
    (a German date would shift the whole block by the night offset). Every
    failure mode ends in a dict the template can render: a missing source
    is a plain sentence, not an exception and not an empty day pretending
    there is nothing to report.

    Each group therefore carries the answer state the adapter reported
    (``unavailable``: no content, ``stale``: last known rows) so the page
    can tell "nobody could ask" apart from "the source answered: nothing
    on that day". The header note names one failure per sentence, which is
    the only way a single failed day stays visible while its two siblings
    are live.

    One group is flagged ``is_default`` and is the tab the page opens on:
    ``today`` when it has rows, else the first day in ``DAY_ORDER``
    (prev, today, next) that has rows, else ``today`` again when no day
    has rows. A blind ``today`` default opens an empty tab on a weekend
    while the next day lists the only titles of the page - the content
    would be reachable but hidden behind a tab nobody clicks. Rows, not
    state, decide: an ``unavailable`` day has no rows and therefore never
    becomes the default while another day answered with titles.
    """
    try:
        heute = _us_today(ctx.now_fn)
        heute_iso = heute.isoformat() if heute is not None else ""
        # The three tabs are TRADING days, never calendar neighbours: on a
        # Friday the next tab used to be a Saturday with 0 rows the UI could
        # not leave. Each day is asked of the one US calendar; a day it
        # cannot name (``None``) falls out of the answer instead of being
        # replaced by ``heute ± 1``. The keys stay DAY_PREV/DAY_TODAY/
        # DAY_NEXT - only the dates they point at change.
        tag_prev = last_trading_day_before(heute)
        tag_today = heute if is_us_trading_day(heute) else next_trading_day_after(heute)
        tag_next = next_trading_day_after(tag_today) if tag_today is not None else None
        tage = {
            key: tag.isoformat()
            for key, tag in ((DAY_PREV, tag_prev), (DAY_TODAY, tag_today), (DAY_NEXT, tag_next))
            if tag is not None
        }
        antwort = ctx.earnings_adapter.get_calendar_days(dict(tage)) or {}
        roh = antwort.get("days") if isinstance(antwort, dict) else None
        roh = roh if isinstance(roh, dict) else {}
        tagesdaten = {}
        for key, tag in tage.items():
            eintrag = roh.get(tag)
            if not isinstance(eintrag, dict):
                # No entry for this day: the group stays empty and says so.
                eintrag = {"date": tag, "rows": [], "payload_ok": False, "skipped": 0}
            tagesdaten[key] = eintrag

        # Per-day answer state, taken from the adapter's own labelling and
        # never inferred from "the rows are empty": a live day may honestly
        # list nothing (a weekend answers `rows: null` with its own date
        # stamp), while an unavailable one has no content at all. A day the
        # answer does not mention is unavailable too - an absent answer is
        # no answer, so the page must not read it as an empty day.
        tageszustand = {}
        for key, tag in tage.items():
            meldung = roh.get(tag)
            gemeldet = meldung.get("state") if isinstance(meldung, dict) else None
            tageszustand[key] = gemeldet if gemeldet in ("live", "stale") else "unavailable"

        symbole, option_state, option_fetched = _option_symbols(ctx.earnings_adapter)
        kalender = build_calendar(tagesdaten, optionable=symbole)
        gruppen = []
        for gruppe in kalender["groups"]:
            zeilen = [dict(zeile, session_text=_SESSION_TEXT.get(zeile.get("session"), _SESSION_UNKNOWN_TEXT))
                      for zeile in gruppe["rows"]]
            zustand = tageszustand.get(gruppe["key"], "unavailable")
            meldung = roh.get(tage.get(gruppe["key"]))
            grund = None
            if zustand == "unavailable" and isinstance(meldung, dict):
                grund = _failure_reason(meldung.get("error"))
            gruppen.append(dict(
                gruppe,
                rows=zeilen,
                label=_calendar_day_label(gruppe["date"], heute_iso) or "",
                date_text=_german_date(gruppe["date"]),
                short_day=_short_day(gruppe["date"]),
                counter=f"{len(zeilen)} von {gruppe['total']}",
                # Tab counter: how many titles the day lists (after the
                # option filter). A failed day has no number - "0" would
                # read like "nothing reported".
                tab_counter=None if zustand == "unavailable" else gruppe["total"],
                unavailable=zustand == "unavailable",
                stale=zustand == "stale",
                # WHY the day is missing, when the source said so (a block
                # and a timeout are different events and must not read
                # alike). Empty when the answer carries no reason.
                failure_reason=grund,
            ))

        # The page opens on exactly ONE day, so that day must be the one
        # with something to show: a hard-wired "today" opens an empty tab on
        # a Sunday while tomorrow lists 191 titles, which leaves the only
        # content of the block behind a tab nobody sees. Rule, in
        # DAY_ORDER: "today" wins when it has rows; otherwise the first day
        # (prev, then next) that has rows. Has no day rows at all, "today"
        # stays the default - an empty calendar must still open on the tab
        # a reader expects, not on an arbitrary one. A day whose state is
        # "unavailable" has no rows by definition, so it can never become
        # the default while any other day answered with titles.
        standard_key = DAY_TODAY
        if not any(gruppe["key"] == DAY_TODAY and gruppe["rows"] for gruppe in gruppen):
            for gruppe in gruppen:
                if gruppe["rows"]:
                    standard_key = gruppe["key"]
                    break
        for gruppe in gruppen:
            gruppe["is_default"] = gruppe["key"] == standard_key

        angewendet = symbole is not None
        ohne_optionen = sum(gruppe["without_options"] for gruppe in gruppen)
        option_stale = bool(angewendet and option_state == "stale")

        state = str(antwort.get("state") or "unavailable")
        zustaende = list(tageszustand.values())
        vorhanden = any(z in ("live", "stale") for z in zustaende)
        ausgefallen = sum(1 for z in zustaende if z == "unavailable")
        letzter_stand = "stale" in zustaende or state == "stale"
        if state == "not_configured":
            note = t("page_route.earnings.note.no_source")
        else:
            # One sentence per failure mode, because a partial outage and a
            # stale answer are different promises: the first says a day is
            # missing, the second says what is shown is old. "Nothing was
            # answered at all" stays the block-wide sentence - it is the
            # honest wording for three missing days, and it keeps the
            # weekend's two empty-but-answered days free of any warning.
            saetze = []
            if not vorhanden:
                saetze.append(t("page_route.earnings.note.source_silent"))
            elif ausgefallen == 1:
                saetze.append(t("page_route.earnings.note.one_day_failed"))
            elif ausgefallen:
                saetze.append(t("page_route.earnings.note.days_failed",
                                count=ausgefallen))
            if letzter_stand:
                saetze.append(t("page_route.earnings.note.last_state"))
            # One reason per distinct failure, named next to the sentence:
            # "does not answer" alone hides that the source DID answer 403.
            gruende = []
            for key, tag in tage.items():
                if tageszustand.get(key) != "unavailable":
                    continue
                meldung = roh.get(tag)
                grund = _failure_reason(
                    meldung.get("error") if isinstance(meldung, dict) else None
                )
                if grund and grund not in gruende:
                    gruende.append(grund)
            if gruende:
                saetze.append("Grund: " + " · ".join(gruende))
            note = " · ".join(saetze) or None

        return {
            "available": bool(vorhanden),
            # Groups are rendered unless no source is configured at all: a
            # failed day must stay a visible, labelled day instead of a
            # column the page silently drops - and a not-configured source
            # is a setup fact, not three outages.
            "show_groups": bool(gruppen) and state != "not_configured",
            "state": state,
            "note": note,
            "stand": _german_stamp(antwort.get("fetched_at_utc")),
            "groups": gruppen,
            # The option filter is a block-wide statement: which list was
            # used (or not), how old it is, and one sentence naming what it
            # did. The per-group "without_options" says the same for a day.
            "option_filter": {"applied": angewendet, "stale": option_stale,
                              "unavailable": not angewendet},
            "option_stamp": _german_stamp(option_fetched) if angewendet else None,
            "option_note": _option_note(angewendet, ohne_optionen, option_stale),
        }
    except Exception:                                    # noqa: BLE001 - page must render
        current_app.logger.exception("earnings calendar view failed")
        return {
            "available": False,
            "state": "unavailable",
            "note": t("page_route.error.source_unreadable"),
            "stand": None,
            "groups": [],
            "option_filter": {"applied": False, "stale": False, "unavailable": True},
            "option_stamp": None,
            "option_note": t("page_route.option_note.unavailable"),
        }


#: Die drei Badge-Mappen bleiben Modulkonstanten: /api/screen ist ein
#: Alt-Vertrag und bleibt deutsch, auch nach der Umschaltung. Der Abschnitt
#: "page_route" wird in en_texts.py deshalb nicht deklariert.
_CONFIRMATION_TEXT = {CONFIRMATION_CONFIRMED: t("page_route.confirmation.confirmed"),
                      CONFIRMATION_ESTIMATED: t("page_route.confirmation.estimated")}


_SESSION_TEXT = {SESSION_BEFORE: t("page_route.session.before"),
                 SESSION_AFTER: t("page_route.session.after")}


_SESSION_UNKNOWN_TEXT = t("page_route.status.unknown_time")


def _calendar_day_label(iso_text, today_iso):
    """The tab text for a day: ``gestern · Do 12.11.`` / ``heute · Fr 13.11.``.

    Date-based on purpose. With trading-day tabs the group after a Friday is a
    Monday, so the word "morgen" would promise a day two nights away and
    "gestern" would name a Friday as yesterday on a Monday. The word is used
    only where it is literally true (the calendar neighbour); every other day
    names itself with weekday and date. ``None`` when the date is unusable -
    the caller then renders no date rather than a guessed one.
    """
    kurz = _short_day(iso_text)
    if kurz is None:
        return None
    try:
        heute = datetime.strptime(str(today_iso), "%Y-%m-%d").date()
        tag = datetime.strptime(str(iso_text), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return kurz
    versatz = (tag - heute).days
    wort = {-1: "gestern", 0: "heute", 1: "morgen"}.get(versatz)
    return f"{wort} · {kurz}" if wort else kurz


def _failure_reason(error):
    """One German clause naming WHY a source query failed, or ``None``.

    A block is not a silent source: the source answered 403, and a note that
    says only "does not answer" would describe the wrong event. The code stays
    visible next to the wording so the log and the page agree.
    """
    if not isinstance(error, dict):
        return None
    code = str(error.get("code") or "").strip()
    if not code:
        return None
    text = _FAILURE_REASONS.get(code, t("page_route.failure.source_reports", code=code))
    status = error.get("http_status")
    if status:
        text = f"{text} (HTTP {status})"
    return text


def _german_date(iso_text):
    """``2026-11-08`` -> ``08.11.2026``; an unusable value stays ``None``."""
    try:
        return datetime.strptime(str(iso_text), "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return None


def _short_day(iso_text):
    """``2026-11-08`` -> ``So 08.11.``; an unusable value stays ``None``."""
    try:
        wert = datetime.strptime(str(iso_text), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return f"{_WEEKDAY_SHORT_DE[wert.weekday()]} {wert.strftime('%d.%m.')}"


#: Code -> Katalogschluessel; der Schluessel wiederholt den Code, damit Code und
#: Satz nicht auseinanderlaufen koennen.
_FAILURE_REASON_KEYS = {
    "source_blocked": "page_route.failure.source_blocked",
    "source_throttled": "page_route.failure.source_throttled",
    "source_unreachable": "page_route.failure.source_unreachable",
    "invalid_payload": "page_route.failure.invalid_payload",
    "invalid_json": "page_route.failure.invalid_json",
    "recent_failure": "page_route.failure.recent_failure",
}

_FAILURE_REASONS = {code: t(key) for code, key in _FAILURE_REASON_KEYS.items()}


_WEEKDAY_SHORT_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
