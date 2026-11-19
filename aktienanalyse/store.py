"""Persistent store for the stock analysis.

The collector spends credits (the filing list) and downloads original reports.
Both must happen once per company, not once per page view, so everything the
collector learns is written here and read back from here.

Four tables, one file, no server:

    firma   -- symbol, ISIN, exchange, currency, when it was last seen
    bericht -- which original report was downloaded for which fiscal year
    wert    -- one row per (company, section, date, position); the value plus its
               currency and its source. A position that is not there is absent,
               never zero: the page shows it as open.
    kurs    -- daily closes, so the same day always shows the same price

The API key is NOT stored here. It stays in the environment.
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timezone
from pathlib import Path

SECTIONS = ("income", "quarterly", "balance", "cash")

SCHEMA = """
CREATE TABLE IF NOT EXISTS firma (
    symbol       TEXT PRIMARY KEY,
    isin         TEXT,
    name         TEXT,
    boersenplatz TEXT,
    waehrung     TEXT,
    stand        TEXT
);
CREATE TABLE IF NOT EXISTS bericht (
    symbol       TEXT,
    geschaeftsjahr INTEGER,
    typ          TEXT,
    adresse      TEXT,
    veroeffentlicht TEXT,
    abgerufen_am TEXT,
    PRIMARY KEY (symbol, geschaeftsjahr, typ)
);
CREATE TABLE IF NOT EXISTS wert (
    symbol     TEXT,
    abschnitt  TEXT,
    tag        TEXT,
    position   TEXT,
    wert       REAL,
    waehrung   TEXT,
    quelle     TEXT,
    stand      TEXT,
    PRIMARY KEY (symbol, abschnitt, tag, position)
);
CREATE TABLE IF NOT EXISTS kurs (
    symbol   TEXT,
    tag      TEXT,
    schluss  REAL,
    waehrung TEXT,
    quelle   TEXT,
    PRIMARY KEY (symbol, tag)
);
"""


def db_path() -> Path:
    """Where the store lives: one file, given by AKTIENANALYSE_DB."""
    return Path(os.environ.get("AKTIENANALYSE_DB", "") or Path.home() / ".cache" / "aktienanalyse.db")


def verbindung(path=None) -> sqlite3.Connection:
    ziel = Path(path) if path else db_path()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ziel, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _spalte_ergaenzen(conn, "bericht", "veroeffentlicht", "TEXT")
    return conn


def _spalte_ergaenzen(conn, tabelle: str, spalte: str, typ: str):
    """Add a column to a database that was created by an older version."""
    vorhanden = {row[1] for row in conn.execute(f"PRAGMA table_info({tabelle})")}
    if spalte not in vorhanden:
        conn.execute(f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {typ}")
        conn.commit()


def _jetzt() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _alter(stand: str) -> float:
    """Age of a stored row in hours; a missing or unreadable stamp counts as infinite."""
    try:
        damals = datetime.fromisoformat(stand)
    except (TypeError, ValueError):
        return float("inf")
    if damals.tzinfo is None:
        damals = damals.replace(tzinfo=UTC)
    return (datetime.now(UTC) - damals).total_seconds() / 3600


def firma_merken(conn, symbol, isin=None, name=None, boersenplatz=None, waehrung=None):
    """Remember what identifies this symbol; unknown fields are left as they are."""
    conn.execute(
        """INSERT INTO firma (symbol, isin, name, boersenplatz, waehrung, stand)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             isin=COALESCE(excluded.isin, firma.isin),
             name=COALESCE(excluded.name, firma.name),
             boersenplatz=COALESCE(excluded.boersenplatz, firma.boersenplatz),
             waehrung=COALESCE(excluded.waehrung, firma.waehrung),
             stand=excluded.stand""",
        (symbol, isin, name, boersenplatz, waehrung, _jetzt()))
    conn.commit()


def firma(conn, symbol):
    row = conn.execute("SELECT * FROM firma WHERE symbol = ?", (symbol,)).fetchone()
    return dict(row) if row else {}


def berichte_merken(conn, symbol, berichte):
    """Store the filing list: (fiscal year, type, address[, released]) per report."""
    jetzt = _jetzt()
    for eintrag in berichte:
        jahr, typ, adresse = eintrag[:3]
        veroeffentlicht = eintrag[3] if len(eintrag) > 3 else ""
        conn.execute(
            """INSERT INTO bericht (symbol, geschaeftsjahr, typ, adresse, veroeffentlicht, abgerufen_am)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, geschaeftsjahr, typ) DO UPDATE SET
                 adresse=excluded.adresse, abgerufen_am=excluded.abgerufen_am,
                 veroeffentlicht=COALESCE(NULLIF(excluded.veroeffentlicht, ''), bericht.veroeffentlicht)""",
            (symbol, jahr, typ, adresse, veroeffentlicht, jetzt))
    conn.commit()


def berichte(conn, symbol, max_alter_h=30 * 24):
    """The stored filing list, but only while it is young enough to trust."""
    rows = conn.execute(
        "SELECT geschaeftsjahr, typ, adresse, veroeffentlicht, abgerufen_am FROM bericht WHERE symbol = ?",
        (symbol,)).fetchall()
    if not rows or any(_alter(row["abgerufen_am"]) > max_alter_h for row in rows):
        return []
    return [(row["geschaeftsjahr"], row["typ"], row["adresse"], row["veroeffentlicht"] or "")
            for row in rows]


def werte_schreiben(conn, symbol, abschnitte, quelle=None, waehrung=None):
    """Write a statements dict shaped like {section: {day: {position: value}}}."""
    jetzt = _jetzt()
    anzahl = 0
    for abschnitt in SECTIONS:
        for tag, zeile in (abschnitte.get(abschnitt) or {}).items():
            for position, wert in zeile.items():
                if wert is None:
                    continue
                conn.execute(
                    """INSERT INTO wert (symbol, abschnitt, tag, position, wert, waehrung, quelle, stand)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(symbol, abschnitt, tag, position) DO UPDATE SET
                         wert=excluded.wert, waehrung=COALESCE(excluded.waehrung, wert.waehrung),
                         quelle=COALESCE(excluded.quelle, wert.quelle), stand=excluded.stand""",
                    (symbol, abschnitt, tag, position, float(wert), waehrung, quelle, jetzt))
                anzahl += 1
    conn.commit()
    return anzahl


def werte_lesen(conn, symbol, abschnitte=SECTIONS, max_alter_h=24):
    """Read the stored statements back; empty when nothing is stored or it is stale."""
    result = {}
    for abschnitt in abschnitte:
        rows = conn.execute(
            "SELECT tag, position, wert, stand FROM wert WHERE symbol = ? AND abschnitt = ?",
            (symbol, abschnitt)).fetchall()
        if not rows or any(_alter(row["stand"]) > max_alter_h for row in rows):
            continue
        block = {}
        for row in rows:
            block.setdefault(row["tag"], {})[row["position"]] = row["wert"]
        result[abschnitt] = block
    return result


def werte_quelle(conn, symbol):
    """Which sources the stored values come from, for the source line on the page."""
    rows = conn.execute(
        "SELECT DISTINCT quelle FROM wert WHERE symbol = ? AND quelle IS NOT NULL", (symbol,)).fetchall()
    return [row["quelle"] for row in rows]


def kurse_schreiben(conn, symbol, reihe, waehrung=None, quelle=None):
    """Write a price series [(day, close)]; the same day is never stored twice."""
    if not reihe:
        return 0
    conn.executemany(
        """INSERT INTO kurs (symbol, tag, schluss, waehrung, quelle) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(symbol, tag) DO UPDATE SET schluss=excluded.schluss,
             waehrung=COALESCE(excluded.waehrung, kurs.waehrung),
             quelle=COALESCE(excluded.quelle, kurs.quelle)""",
        [(symbol, tag, float(schluss), waehrung, quelle) for tag, schluss in reihe
         if isinstance(schluss, (int, float)) and schluss > 0])
    conn.commit()
    return len(reihe)


def kurse_lesen(conn, symbol, max_alter_h=24):
    """The stored series, but only while the newest close is young enough."""
    rows = conn.execute(
        "SELECT tag, schluss FROM kurs WHERE symbol = ? ORDER BY tag", (symbol,)).fetchall()
    if not rows or _alter(rows[-1]["tag"] + "T23:59:59+00:00") > max_alter_h:
        return []
    return [(row["tag"], row["schluss"]) for row in rows]


def stand(conn, symbol):
    """Human-readable freshness of the three data kinds, for the page."""
    def newest(sql):
        row = conn.execute(sql, (symbol,)).fetchone()
        return row[0] if row and row[0] else None
    return {
        "werte": newest("SELECT MAX(stand) FROM wert WHERE symbol = ?"),
        "berichte": newest("SELECT MAX(abgerufen_am) FROM bericht WHERE symbol = ?"),
        "kurse": newest("SELECT MAX(tag) FROM kurs WHERE symbol = ?"),
    }


def summary(path=None):
    """A short overview, useful for the command line and for tests."""
    with closing(verbindung(path)) as conn:
        zahlen = {tabelle: conn.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]
                  for tabelle in ("firma", "bericht", "wert", "kurs")}
    return json.dumps(zahlen, ensure_ascii=False)
