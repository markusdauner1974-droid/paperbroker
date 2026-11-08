"""Reading CBOE's collective symbol list: which tickers have listed options.

Why this module exists
----------------------
The calendar may only show titles whose options the screener could actually
trade. Asking the option-chain endpoint per title was measured at 0.3-1.5 s and
300-1500 KB per answer, and 25 parallel requests were throttled with HTTP 429 -
so a per-title check cannot run on a page view. CBOE publishes one CSV with
every optionable symbol (measured: 5415 rows, 388 KB, 2.3-3.9 s). One download
a day answers the filter; no chain is ever fetched here.

What the file really looks like (measured, not assumed)
-------------------------------------------------------
* UTF-8 with a BOM,
* column names with LEADING spaces (``' Stock Symbol'``),
* quoted company names that contain commas,
* a title may appear more than once, empty lines occur.

The symbol column is therefore found by NAME, never by position: a quoted comma
in the company column must not shift the symbol. The set is normalised to
uppercase because the calendar speaks uppercase; a lowercase row is a broken
row and is not "repaired" anywhere else.

Fail closed
-----------
A header without the symbol column yields the EMPTY set. The adapter treats an
empty set as a failed answer - filtering by an empty set would hide every
title, which is the one outcome worse than showing too many.
"""

import csv
import io

_SYMBOL_COLUMN = "stock symbol"


def parse_cboe_symbols(csv_text):
    """The uppercase symbol set from CBOE's CSV, or ``set()`` when unusable.

    ``set()`` is the honest answer for anything that is not a CSV with the
    ``Stock Symbol`` column: an empty set can be recognised as "no list" by
    the caller, while a half-parsed list would silently hide titles.
    """
    if not isinstance(csv_text, str):
        return set()
    # The BOM sits on the FIRST header cell; lstrip keeps offsets irrelevant.
    text = csv_text.lstrip("\ufeff")
    try:
        zeilen = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return set()
    if not zeilen:
        return set()

    kopf = [zelle.strip().lower() for zelle in zeilen[0]]
    try:
        spalte = kopf.index(_SYMBOL_COLUMN)
    except ValueError:
        return set()

    symbole = set()
    for zeile in zeilen[1:]:
        if spalte >= len(zeile):
            continue
        wert = zeile[spalte].strip().upper()
        if wert:
            symbole.add(wert)
    return symbole


def filter_optionable(rows, symbols):
    """``(kept, removed)`` for the calendar rows, in the input order.

    ``symbols is None`` means "the list is not available": nothing is filtered
    and nothing is counted as removed - the page says "ungefiltert" instead of
    inventing a selection. A symbol is matched EXACTLY as the row delivers it;
    lowercase is not rewritten into a match (the calendar answers uppercase, so
    a lowercase value is broken data, not a filter question).
    """
    zeilen = [z for z in (rows or []) if isinstance(z, dict)]
    if symbols is None:
        return zeilen, []
    kept = []
    removed = []
    for zeile in zeilen:
        if zeile.get("symbol") in symbols:
            kept.append(zeile)
        else:
            removed.append(zeile)
    return kept, removed
