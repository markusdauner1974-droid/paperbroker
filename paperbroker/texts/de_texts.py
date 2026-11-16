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
    # --- js.* -----------------------------------------------------------
    # static/gap_funnel.js: the table, the detail block and the error states
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
