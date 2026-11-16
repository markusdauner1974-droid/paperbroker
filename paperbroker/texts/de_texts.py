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
    # routes/page.py and templates/screen.html: the page itself
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
