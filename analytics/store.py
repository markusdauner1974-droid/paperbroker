"""Persistent store for the stock analysis.

The collector spends credits (the filing list) and downloads original reports.
Both must happen once per company, not once per page view, so everything the
collector learns is written here and read back from here.

Four tables, one file, no server:

    company -- symbol, ISIN, exchange, currency, when it was last seen
    filing  -- which original report was downloaded for which fiscal year
    figure  -- one row per (company, section, date, item); the value plus its
               currency and its source. An item that is not there is absent,
               never zero: the page shows it as open.
    price   -- daily closes, so the same day always shows the same price

The API key is NOT stored here. It stays in the environment.

A database written by the German version of this module is migrated in place on
open: the tables firma/bericht/wert/kurs are renamed to company/filing/figure/price
and their columns along with them, so existing data keeps its rows.
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SECTIONS = ("income", "quarterly", "balance", "cash")

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
    symbol    TEXT PRIMARY KEY,
    isin      TEXT,
    name      TEXT,
    exchange  TEXT,
    currency  TEXT,
    seen_at   TEXT
);
CREATE TABLE IF NOT EXISTS filing (
    symbol      TEXT,
    fiscal_year INTEGER,
    kind        TEXT,
    address     TEXT,
    released    TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (symbol, fiscal_year, kind)
);
CREATE TABLE IF NOT EXISTS figure (
    symbol     TEXT,
    section    TEXT,
    day        TEXT,
    item       TEXT,
    amount     REAL,
    currency   TEXT,
    source     TEXT,
    updated_at TEXT,
    PRIMARY KEY (symbol, section, day, item)
);
CREATE TABLE IF NOT EXISTS export_budget (
    day  TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS export (
    symbol        TEXT PRIMARY KEY,
    provider_id   INTEGER,
    checksum      TEXT,
    downloaded_at TEXT,
    gaps          TEXT
);
CREATE TABLE IF NOT EXISTS price (
    symbol   TEXT,
    day      TEXT,
    close    REAL,
    currency TEXT,
    source   TEXT,
    PRIMARY KEY (symbol, day)
);
"""

# Old German name -> new English name. Both tables and columns.
TABLE_RENAMES = (("firma", "company"), ("bericht", "filing"), ("wert", "figure"), ("kurs", "price"))
COLUMN_RENAMES = (
    ("company", (("boersenplatz", "exchange"), ("waehrung", "currency"), ("stand", "seen_at"))),
    ("filing", (("geschaeftsjahr", "fiscal_year"), ("typ", "kind"), ("adresse", "address"),
                ("veroeffentlicht", "released"), ("abgerufen_am", "fetched_at"))),
    ("figure", (("abschnitt", "section"), ("tag", "day"), ("position", "item"), ("wert", "amount"),
                ("waehrung", "currency"), ("quelle", "source"), ("stand", "updated_at"))),
    ("price", (("tag", "day"), ("schluss", "close"), ("waehrung", "currency"), ("quelle", "source"))),
)


def db_path() -> Path:
    """Where the store lives: one file, given by AKTIENANALYSE_DB."""
    return Path(os.environ.get("AKTIENANALYSE_DB", "") or Path.home() / ".cache" / "aktienanalyse.db")


def connect(path=None) -> sqlite3.Connection:
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Rename before the schema is applied: otherwise the old tables stay next to new, empty ones
    # and the page would read an empty store while the data sits beside it.
    _rename_german_schema(conn)
    conn.executescript(SCHEMA)
    _add_column(conn, "filing", "released", "TEXT")
    _add_column(conn, "company", "provider_id", "INTEGER")
    conn.commit()
    return conn


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _rename_german_schema(conn):
    """Carry a database of the German version over to the English names."""
    tables = _tables(conn)
    for old, new in TABLE_RENAMES:
        if old in tables and new not in tables:
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
            tables = _tables(conn)
    for table, columns in COLUMN_RENAMES:
        if table not in tables:
            continue
        present = _columns(conn, table)
        for old, new in columns:
            if old in present and new not in present:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                present = _columns(conn, table)
    conn.commit()


def _add_column(conn, table: str, column: str, kind: str):
    """Add a column to a database that was created by an older version."""
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.commit()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def age_hours(stamp: str) -> float:
    """Age of a stored row in hours; a missing or unreadable stamp counts as infinite."""
    try:
        then = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds() / 3600


def remember_company(conn, symbol, isin=None, name=None, exchange=None, currency=None):
    """Remember what identifies this symbol; unknown fields are left as they are."""
    conn.execute(
        """INSERT INTO company (symbol, isin, name, exchange, currency, seen_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             isin=COALESCE(excluded.isin, company.isin),
             name=COALESCE(excluded.name, company.name),
             exchange=COALESCE(excluded.exchange, company.exchange),
             currency=COALESCE(excluded.currency, company.currency),
             seen_at=excluded.seen_at""",
        (symbol, isin, name, exchange, currency, _now()))
    conn.commit()


def company(conn, symbol):
    row = conn.execute("SELECT * FROM company WHERE symbol = ?", (symbol,)).fetchone()
    return dict(row) if row else {}


def remember_filings(conn, symbol, filings):
    """Store the filing list: (fiscal year, kind, address[, released]) per report."""
    now = _now()
    for entry in filings:
        year, kind, address = entry[:3]
        released = entry[3] if len(entry) > 3 else ""
        conn.execute(
            """INSERT INTO filing (symbol, fiscal_year, kind, address, released, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, fiscal_year, kind) DO UPDATE SET
                 address=excluded.address, fetched_at=excluded.fetched_at,
                 released=COALESCE(NULLIF(excluded.released, ''), filing.released)""",
            (symbol, year, kind, address, released, now))
    conn.commit()


def filings(conn, symbol, max_age_h=30 * 24):
    """The stored filing list, but only while it is young enough to trust."""
    rows = conn.execute(
        "SELECT fiscal_year, kind, address, released, fetched_at FROM filing WHERE symbol = ?",
        (symbol,)).fetchall()
    if not rows or any(age_hours(row["fetched_at"]) > max_age_h for row in rows):
        return []
    return [(row["fiscal_year"], row["kind"], row["address"], row["released"] or "")
            for row in rows]


def write_figures(conn, symbol, sections, source=None, currency=None):
    """Write a statements dict shaped like {section: {day: {item: amount}}}."""
    now = _now()
    count = 0
    for section in SECTIONS:
        for day, line in (sections.get(section) or {}).items():
            for item, amount in line.items():
                if amount is None:
                    continue
                conn.execute(
                    """INSERT INTO figure (symbol, section, day, item, amount, currency, source, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(symbol, section, day, item) DO UPDATE SET
                         amount=excluded.amount,
                         currency=COALESCE(excluded.currency, figure.currency),
                         source=COALESCE(excluded.source, figure.source),
                         updated_at=excluded.updated_at""",
                    (symbol, section, day, item, float(amount), currency, source, now))
                count += 1
    conn.commit()
    return count


def remember_provider(symbol, provider_id, path=None):
    """The provider's own number for an issuer, so it never has to be looked up twice."""
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO company (symbol, provider_id, seen_at) VALUES (?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET provider_id=excluded.provider_id""",
            (symbol, int(provider_id), _now()))
        conn.commit()


def stored_provider(symbol, path=None):
    """The remembered provider number, or None."""
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT provider_id FROM company WHERE symbol = ?", (symbol,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def remember_export(symbol, provider_id, checksum, gaps=(), path=None):
    """Record what the export contained and when it was read."""
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO export (symbol, provider_id, checksum, downloaded_at, gaps)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 provider_id=excluded.provider_id, checksum=excluded.checksum,
                 downloaded_at=excluded.downloaded_at, gaps=excluded.gaps""",
            (symbol, int(provider_id), checksum, _now(),
             json.dumps([list(gap) for gap in gaps], ensure_ascii=False)))
        conn.commit()


def export_is_fresh(symbol, max_age_days, path=None):
    """Whether the export was read successfully within the given number of days.

    A failed attempt also leaves a row behind - without a checksum it is not a reading, and the
    file must not be treated as present.
    """
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT downloaded_at, checksum FROM export WHERE symbol = ?",
                           (symbol,)).fetchone()
    if not row or not row["downloaded_at"] or not (row["checksum"] or "").strip():
        return False
    return age_hours(row["downloaded_at"]) <= max_age_days * 24


def budget_day(path=None):
    """The provider counts per calendar day; UTC is the safe side of every time zone."""
    return datetime.now(UTC).date().isoformat()


def budget_take(limit, path=None):
    """Take one slot of today's allowance, or answer that there was none."""
    day = budget_day(path)
    with closing(connect(path)) as conn:
        conn.execute("INSERT OR IGNORE INTO export_budget (day, used) VALUES (?, 0)", (day,))
        conn.execute("UPDATE export_budget SET used = used + 1 WHERE day = ?", (day,))
        used = conn.execute("SELECT used FROM export_budget WHERE day = ?", (day,)).fetchone()[0]
        if used > limit:
            conn.execute("UPDATE export_budget SET used = used - 1 WHERE day = ?", (day,))
            conn.commit()
            return False
        conn.commit()
    return True


def budget_release(path=None):
    """Give a slot back - an attempt that brought nothing must not cost one of the ten."""
    with closing(connect(path)) as conn:
        conn.execute("UPDATE export_budget SET used = MAX(used - 1, 0) WHERE day = ?", (budget_day(path),))
        conn.commit()


def budget_used(path=None):
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT used FROM export_budget WHERE day = ?", (budget_day(path),)).fetchone()
    return int(row[0]) if row else 0


def export_attempted_within(symbol, minutes, path=None):
    """Whether the export was tried recently - a failing provider must not be asked every view."""
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT downloaded_at FROM export WHERE symbol = ?", (symbol,)).fetchone()
    if not row or not row[0]:
        return False
    return age_hours(row[0]) <= minutes / 60


def stored_export_gaps(symbol, path=None):
    """The lines the export itself could not close, as recorded when it was read."""
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT gaps FROM export WHERE symbol = ?", (symbol,)).fetchone()
    try:
        return [tuple(gap) for gap in json.loads(row[0])] if row and row[0] else []
    except (ValueError, TypeError):
        return []


def read_figures(conn, symbol, sections=SECTIONS, max_age_h=24):
    """Read the stored statements back; empty when nothing is stored or it is stale."""
    result = {}
    for section in sections:
        rows = conn.execute(
            "SELECT day, item, amount, updated_at FROM figure WHERE symbol = ? AND section = ?",
            (symbol, section)).fetchall()
        if not rows or any(age_hours(row["updated_at"]) > max_age_h for row in rows):
            continue
        block = {}
        for row in rows:
            block.setdefault(row["day"], {})[row["item"]] = row["amount"]
        result[section] = block
    return result


def figure_sources(conn, symbol):
    """Which sources the stored values come from, for the source line on the page."""
    rows = conn.execute(
        "SELECT DISTINCT source FROM figure WHERE symbol = ? AND source IS NOT NULL",
        (symbol,)).fetchall()
    return [row["source"] for row in rows]


def write_prices(conn, symbol, series, currency=None, source=None):
    """Write a price series [(day, close)]; the same day is never stored twice."""
    if not series:
        return 0
    conn.executemany(
        """INSERT INTO price (symbol, day, close, currency, source) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(symbol, day) DO UPDATE SET close=excluded.close,
             currency=COALESCE(excluded.currency, price.currency),
             source=COALESCE(excluded.source, price.source)""",
        [(symbol, day, float(close), currency, source) for day, close in series
         if isinstance(close, (int, float)) and close > 0])
    conn.commit()
    return len(series)


def read_prices(conn, symbol, max_age_h=24):
    """The stored series, but only while the newest close is young enough."""
    rows = conn.execute(
        "SELECT day, close FROM price WHERE symbol = ? ORDER BY day", (symbol,)).fetchall()
    if not rows or age_hours(rows[-1]["day"] + "T23:59:59+00:00") > max_age_h:
        return []
    return [(row["day"], row["close"]) for row in rows]


def freshness(conn, symbol):
    """Human-readable freshness of the three data kinds, for the page."""
    def newest(sql):
        row = conn.execute(sql, (symbol,)).fetchone()
        return row[0] if row and row[0] else None
    return {
        "figures": newest("SELECT MAX(updated_at) FROM figure WHERE symbol = ?"),
        "filings": newest("SELECT MAX(fetched_at) FROM filing WHERE symbol = ?"),
        "prices": newest("SELECT MAX(day) FROM price WHERE symbol = ?"),
    }


def summary(path=None):
    """A short overview, useful for the command line and for tests."""
    with closing(connect(path)) as conn:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("company", "filing", "figure", "price")}
    return json.dumps(counts, ensure_ascii=False)
