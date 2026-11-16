"""The German book - the source of every display text.

Keys are neutral English IDs in dotted notation. The first component names the
section (the file whose texts it holds), the rest names the place in that file.
Values are either a plain string or a branch dict ``{"one": ..., "other": ...}``;
the branch is selected by the language's rule in ``registry.py`` and never by a
comparison at the call site.

This file is the reference for the structure check: every other book must carry
exactly these keys. Texts are added section by section, one pull request per
section, so the file grows with the extraction and nothing is guessed ahead of
time.
"""

from __future__ import annotations

#: This book is the reference: its sections are derived from the keys above,
#: so it declares none of them.
TEXTS: dict[str, object] = {
    # --- page.* ---------------------------------------------------------
    # templates/screen.html: the page itself. The route computes a lot more
    # context than this template reads - measured: the only Jinja expressions in
    # screen.html are two url_for() calls - so the route's own sentences are not
    # in here. They are dead for this page and are named in the PR, not
    # translated.
    "page.head.title": "Gap-Spread-Trichter",
    # The lede is split into three text nodes by the two live spans the script
    # fills (R/R hurdle, movement cap). The whitespace belongs to the text, not
    # to the template: a line break inside a sentence is a formatting decision a
    # translator has to see.
    "page.head.lede_before": (
        "Earnings-Gap, Optionen, Verfall nach dem nächsten Bericht.\n"
        "    Zugelassen: R/R ≥ "
    ),
    "page.head.lede_between": " und eine nötige Bewegung bis\n    ",
    "page.head.lede_after": " %.",
    "page.form.heading": "Reaktionstag prüfen",
    "page.form.day_label": "Reaktionstag",
    "page.form.run": "Scannen",
    "page.form.expand_all": "alle aufklappen",
    "page.state.no_run": "Noch kein Scan gelaufen",
    "page.table.col.number": "Nummer",
    "page.table.col.symbol": "Symbol",
    "page.table.col.gap": "Gap Prozent",
    "page.table.col.strategy": "Strategie",
    "page.table.col.close": "Kurs",
    "page.table.col.strikes": "Strikes (Long / Short)",
    "page.table.col.max_loss": "maximaler Verlust",
    "page.table.col.max_profit": "maximaler Gewinn",
    "page.table.col.rr": "R/R",
    "page.table.col.chance": "Chance",
    "page.table.col.required_move": "nötig %",
    "page.table.col.volatility": "Schwankung",
    "page.table.col.hit_rate": "Treffer",
    "page.table.col.expiration": "Verfall",
    "page.table.col.dte": "DTE",
    "page.table.col.next_report": "nächster Bericht",
    "page.table.col.options": "Optionen",
    "page.detail.summary": "Detail zum Spread",
    "page.excluded.summary": "Ausschlüsse",
    "page.failures.summary": "Fehlschläge",
    "page.foot.feed_note": "CBOE-Kursdaten zeitversetzt – ca. 15 Minuten hinter Echtzeit.",
    #
    # --- funnel.* -------------------------------------------------------
    # gap_funnel.py: what the funnel measured, what it rejected, and the pair
    # counters - the first values that inflect, so the branch lives in the
    # value and the rule stays with the language.
    # ---------------------------------------------------------------------
    "funnel.variant.near_money": "nah am Geld",
    "funnel.variant.at_resistance": "am Widerstand",
    "funnel.variant.tightest_spread": "engster Spread",
    "funnel.variant.row_pick": "Hebel (Zeile)",
    "funnel.move.up": "steigen",
    "funnel.move.down": "fallen",
    "funnel.move.too_far": (
        "Der Kurs müsste sich um {move} % {direction}; zugelassen sind {limit} %."
    ),
    "funnel.pairs.valid": {"one": "{count} gültiges Paar", "other": "{count} gültige Paare"},
    "funnel.pairs.enumerated": {
        "one": "{count} aufgezählten Paar",
        "other": "{count} aufgezählten Paaren",
    },
    "funnel.pairs.possible": {"one": "{count} mögliches Paar", "other": "{count} möglich"},
    "funnel.pairs.possible_unknown": "? möglich",
    # Dativ: "Bei 1 Titel war", "Bei 3 Titeln war" - die Form haengt am Zaehlwort.
    "funnel.pairs.capped_title": {"one": "{count} Titel", "other": "{count} Titeln"},
    "funnel.pairs.capped": (
        "{text} ({valid} von {enumerated}, Suche am Deckel von {limit} - {possible})"
    ),
    "funnel.pairs.capped_note": (
        'Bei {title} war die Paarsuche am Deckel{ceiling} - dort ist das gezeigte R/R '
        'das beste GEFUNDENE Paar, und "kein Paar" heißt "keins im geprüften Teil"; '
        "ein besseres Paar weiter hinten in der Aufzählung wurde nicht geprüft."
    ),
    "funnel.chain.unreadable": "Die Optionskette konnte nicht gelesen werden{detail}.",
    "funnel.chain.not_listed": "Die Quelle listet keine handelbare Optionskette für diesen Titel.",
    "funnel.chain.empty": "Die Kette dieses Titels enthält keinen Kontrakt.",
    "funnel.expiry.empty": "keine Kontrakte in diesem Verfall",
    "funnel.expiry.none_after_earnings": (
        "Kein gelisteter Verfall liegt nach dem Berichtstermin {date}."
    ),
    "funnel.expiry.line": "Verfall {date}: {note}",
    "funnel.strike.single": "nur ein Strike gelistet - kein Strike-Paar möglich",
    "funnel.strike.no_pair": "kein gültiges Strike-Paar im Screening",
    "funnel.strike.sample": " (z. B. {sample})",
    "funnel.oi.missing": "Open Interest des {leg}-Legs fehlt",
    "funnel.oi.below": "Open Interest des {leg}-Legs {value} < {minimum}",
    "funnel.rr.none": "kein Paar mit R/R >= {limit}",
    "funnel.rr.delta": "R/R erfüllt ({ratios} von {checked}), aber |Delta| des Long-Legs < {limit}",
    "funnel.rr.debit": (
        "R/R und Delta erfüllt ({ratios} von {checked}), aber Netto-Debit < {limit} USD"
    ),
    "funnel.gap.no_direction": "Der Gap hat keine Richtung - keine Debit-Strategie.",
    "funnel.earnings.no_symbol": "Ein Kandidat ohne Symbol hat keinen Berichtstermin.",
    "funnel.earnings.none_in_window": (
        "Die Quelle nennt für diesen Titel keinen Berichtstermin im Fenster."
    ),
    "funnel.earnings.unreadable": "Der Berichtstermin konnte nicht gelesen werden ({error}).",
    "funnel.budget.timeout": "Die Prüfung endete nicht innerhalb des Zeitbudgets.",
    "funnel.budget.failed": "Die Prüfung ist gescheitert ({error}).",
    # --- js.* -----------------------------------------------------------
    # static/gap_funnel.js: the table, the detail block and the error states.
    # The script gets these through one JSON block in the page; a value may be a
    # {one, other} branch, and the script selects the branch with the rule the
    # block names (the same rule the registry uses).
    "js.strategy.bull_call_debit": "Bull Call",
    "js.strategy.bear_put_debit": "Bear Put",
    "js.reason.no_next_earnings": "kein nächster Bericht bekannt",
    "js.reason.no_expiry_after_earnings": "kein Verfall nach dem nächsten Bericht",
    "js.reason.not_optionable": "keine gelisteten Optionen",
    "js.reason.chain_unavailable": "Optionskette nicht lesbar",
    "js.rr_hurdle.fallback": "1 : 2",
    "js.stale": (
        "Kalenderstand veraltet — die Zeilen stammen aus einem alten "
        "Kalenderstand und können sich noch ändern."
    ),
    "js.state.running": "Lauf für {day} … {seconds} s",
    "js.state.failed": "Lauf fehlgeschlagen",
    "js.state.not_started": "Kein Lauf gestartet",
    "js.state.ready": "Stand {clock} · {rows}",
    "js.error.invalid_day": (
        "Kein gültiges Datum — bitte einen Reaktionstag im Format JJJJ-MM-TT angeben."
    ),
    "js.error.not_json": "Die Antwort war kein JSON.",
    "js.error.run_failed": "Lauf für {day} fehlgeschlagen: {message}",
    "js.direction.up": "aufwärts",
    "js.direction.down": "abwärts",
    "js.direction.unknown": "unbekannt",
    "js.liquidity.full": "voll liquide",
    "js.liquidity.thin": "dünn",
    "js.history.too_short": "zu wenig Historie",
    "js.history.source_failed": "Quelle nicht lesbar",
    # Counts line: one key per hurdle, the number inside the text so the plural
    # form can inflect the noun (German inflects the adjective too).
    "js.counts.gaps": {"one": "{n} Gap gemessen", "other": "{n} Gaps gemessen"},
    "js.counts.turnover": "{n} über Umsatzhürde",
    "js.counts.candidates": {"one": "{n} Kandidat", "other": "{n} Kandidaten"},
    "js.counts.next_earnings": "{n} mit nächstem Termin",
    "js.counts.expiry": "{n} mit passendem Verfall",
    "js.counts.rr": "{n} mit R/R mindestens {rr}",
    "js.counts.move": "{n} mit nötiger Bewegung bis {cap} %",
    "js.counts.shown": "{n} angezeigt",
    "js.plural.excluded": {"one": "{n} Ausschluss", "other": "{n} Ausschlüsse"},
    "js.plural.failure": {"one": "{n} Fehlschlag", "other": "{n} Fehlschläge"},
    "js.plural.rows": {"one": "{n} Zeile", "other": "{n} Zeilen"},
    # Cells and their tooltips: the tooltip answers the question the number in
    # the cell raises, so it is text like any other.
    "js.cell.gap.title": "Gap: Schluss des Reaktionstags gegen den Schluss davor",
    "js.cell.close.title": "Schlusskurs des Reaktionstags",
    "js.cell.strikes.sub": "Long / Short",
    "js.cell.strikes.title": "Long {long} · Short {short}",
    "js.cell.max_loss.title": "maximaler Verlust je Einheit",
    "js.cell.max_profit.title": "maximaler Gewinn je Einheit",
    "js.cell.rr.title": "Gewinn zu Verlust bei Verfall",
    "js.cell.chance.title": (
        "Chance = Trefferquote × R/R; Obergrenze des Erwartungswerts, keine "
        "Zusage - sortiert wird nach Trefferquote"
    ),
    "js.cell.move.title": (
        "Bewegung bis zum Break-even des Spreads, in Prozent des Schlusses am "
        "Reaktionstag"
    ),
    "js.move.rise": "steigen",
    "js.move.fall": "fallen",
    "js.cell.volatility.day": "Tag {value} %",
    "js.cell.volatility.title": (
        "Schwankung: annualisierte Streuung der Tagesrenditen; darunter der "
        "mittlere Tagesausschlag"
    ),
    "js.cell.windows": "n={count}",
    "js.cell.windows_sessions": "{count} HT",
    "js.cell.hit.title": (
        "Anteil der vergangenen Fenster von {sessions} Handelstagen, in denen "
        "der Kurs die nötige Bewegung mindestens erreicht hat (Fensterlänge in "
        "Handelstagen; dte nebenan in Kalendertagen)"
    ),
    "js.cell.expiration.title": "Verfall nach dem nächsten Bericht",
    "js.cell.dte.title": "Kalendertage bis Verfall",
    "js.cell.report.title_earnings_day": "Bericht des Reaktionstags: {date}",
    "js.cell.report.title_next": "nächster bekannter Bericht",
    "js.cell.options.title_per_leg": (
        "Open Interest beider Legs (Long / Short) · Bid-Ask-Aufschlag in "
        "Prozent des Mittelkurses, je Leg"
    ),
    "js.cell.options.title_one_leg": (
        "Open Interest beider Legs (Long / Short) · Bid-Ask-Aufschlag der "
        "schlechteren Leg in Prozent ihres Mittelkurses"
    ),
    "js.cell.options.oi": "OI {long} / {short}",
    "js.cell.options.bid_ask_pair": "Bid-Ask {long} % / {short} %",
    "js.cell.options.bid_ask_max": "Bid-Ask max {spread} %",
    "js.cell.rank.one": "die eine Alternative dieser Zeile aufklappen",
    "js.cell.rank.many": "{count} Alternativen dieser Zeile aufklappen",
    "js.expand.open": "alle aufklappen",
    "js.expand.close": "alle zuklappen",
    "js.reason.no_spread_ge_rr": "kein Spread mit R/R mindestens {rr}",
    "js.reason.unknown": "Grund unbekannt",
    "js.list.excluded": "Ausschlüsse",
    "js.list.failures": "Fehlschläge",
    "js.list.summary": "{label} ({count})",
    "js.detail.summary": "Detail zum Spread",
    "js.detail.loading": "Detail zu {symbol} — lädt …",
    "js.detail.title": "Detail zu {symbol}",
    "js.detail.failed": "Detail für {symbol} fehlgeschlagen: {message}",
    "js.detail.unknown_symbol": "Zu diesem Titel gibt es an diesem Tag keine Zeile.",
    "js.detail.leg_not_listed": "Ein Leg dieses Spreads liegt nicht in der Kette.",
    "js.detail.chain_unavailable": "Die Optionskette dieses Titels ist nicht lesbar.",
    "js.detail.no_next_earnings": "Für diesen Titel ist kein nächster Berichtstermin bekannt.",
    "js.detail.no_expiry": "Kein gelisteter Verfall liegt nach dem nächsten Bericht.",
    "js.detail.no_candidate": "Dieser Titel ist an diesem Tag kein Kandidat des Trichters.",
    # The detail block: leg table, chart, its accessible description, legend,
    # the eight figures and the method note.
    "js.legs.col.side": "Seite",
    "js.legs.col.strike": "Strike",
    "js.legs.col.bid": "Bid",
    "js.legs.col.ask": "Ask",
    "js.legs.col.mid": "Mid",
    "js.legs.col.iv": "IV",
    "js.legs.col.oi": "OI",
    "js.legs.col.delta": "Delta",
    "js.legs.side.long": "Long",
    "js.legs.side.short": "Short",
    "js.scenarios.col.change": "Prozent",
    "js.scenarios.col.underlying": "Basiswert",
    "js.scenarios.col.pl": "G&V",
    "js.scenarios.col.delta": "Delta",
    "js.scenarios.col.gamma": "Gamma",
    "js.scenarios.col.vega": "Vega",
    "js.scenarios.col.theta": "Theta",
    "js.chart.aria": "G&V von {symbol}: durchgezogen heute, gestrichelt bei Verfall",
    "js.chart.title": "G&V je Spread über den Kurs — heute und bei Verfall",
    "js.chart.desc": (
        "Durchgezogene Linie: G&V heute nach dem Modell. Gestrichelte Linie: "
        "G&V bei Verfall. Senkrechte Linie: Kurs {spot}. Waagerechte Achse: "
        "Kurs. Senkrechte Achse: G&V in US-Dollar je Spread."
    ),
    "js.chart.axis.price": "Kurs",
    "js.chart.axis.pl": "G&V (USD je Spread)",
    "js.chart.spot": "Spot {value}",
    "js.chart.legend.today": "heute (Modell)",
    "js.chart.legend.expiry": "bei Verfall",
    "js.figures.debit": "Debit",
    "js.figures.breakeven": "Break-even",
    "js.figures.max_loss": "max. Verlust",
    "js.figures.max_profit": "max. Gewinn",
    "js.figures.rr": "R/R",
    "js.figures.required_move": "nötig",
    "js.figures.volatility": "Schwankung",
    "js.figures.hit_rate": "Treffer",
    "js.detail.legs_heading": "Beide Legs",
    "js.detail.chart_heading": "G&V über den Kurs — heute und bei Verfall",
    "js.detail.scenarios_heading": "Szenarien",
    "js.detail.method": "Methodik: {sentence}",
    "js.detail.subtitle": "{strategy} · Verfall {expiration}",
    "js.detail.expiry_label": "Verfall {expiration}",
    "js.empty.no_candidate": "Kein Kandidat — {parts}.",
    "js.foot.hit_sentence": (
        "Treffer = Anteil der vergangenen Fenster von {sessions} Handelstagen "
        "(≈ {calendar} Kalendertage bis Verfall), in denen der Kurs die nötige "
        "Bewegung mindestens erreicht hat; gemessen an {windows} Fenstern aus "
        "höchstens 520 Tagesbars."
    ),
    "js.foot.frequency": "Eine Häufigkeit, keine Wahrscheinlichkeit.",
    "js.foot.chance": (
        "Chance = Trefferquote × R/R; weil die Trefferquote das Erreichen des"
        " Break-even misst, ist das eine Obergrenze des Erwartungswerts, keine"
        " Zusage. Sortiert wird nach Trefferquote."
    ),
    "js.foot.chance_method": (
        "Chance = Trefferquote × R/R; weil die Trefferquote das Erreichen des"
        " Break-even misst, ist das eine Obergrenze des Erwartungswerts, keine"
        " Zusage. Sortiert wird weiter nach Trefferquote."
    ),
    #
    # --- funnel.* -------------------------------------------------------
    # gap_funnel.py: variant labels and the reasons the funnel rejects a title
    #
    # --- exclusion.* ----------------------------------------------------
    # exclusions.py: the exclusion overview per unit
    #
    # --- spreads.* ------------------------------------------------------
    # routes/gap_spreads.py: the answers of the two page-facing endpoints
    #
    # --- support.* ------------------------------------------------------
    # gap_support.py, view_support.py: notes, sort labels, parse errors
    #
    # --- server.* -------------------------------------------------------
    # server.py: the trend note
    #
    # --- age.* ----------------------------------------------------------
    # quote_age.py: the freshness labels and the market-closed note
    #
    # --- calendar.* -----------------------------------------------------
    # earnings_calendar.py: the reasons a window blocks a title
    #
    # --- detail.* -------------------------------------------------------
    # spread_detail.py: the methodology notes of the detail block
}
