"""SQLite schema, connection helpers and the single point-in-time implementation.

`resolve_as_of()` / `pit_clause()` live here on purpose: every read endpoint must
build its date filter through this one pair of functions, so the meaning of
"visible as of date D" has exactly one implementation and can be pinned by tests.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cot_raw (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type         TEXT    NOT NULL,
    market_code         TEXT    NOT NULL,
    report_date         TEXT    NOT NULL,
    published_at        TEXT    NOT NULL,
    published_at_source TEXT    NOT NULL DEFAULT 'derived',
    source_kind         TEXT    NOT NULL DEFAULT 'seed_csv',
    source_file         TEXT,
    source_file_sha256  TEXT,
    source_row_number   INTEGER,
    sources_json        TEXT,
    raw_json            TEXT    NOT NULL,
    raw_hash            TEXT    NOT NULL,
    cftc_id             TEXT,
    ingest_batch_id     TEXT    NOT NULL,
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT    NOT NULL,
    UNIQUE (report_type, market_code, report_date)
);
CREATE INDEX IF NOT EXISTS ix_cot_raw_pub  ON cot_raw (report_type, market_code, published_at);
CREATE INDEX IF NOT EXISTS ix_cot_raw_date ON cot_raw (report_date);
CREATE INDEX IF NOT EXISTS ix_cot_raw_id   ON cot_raw (cftc_id);

CREATE TABLE IF NOT EXISTS cot_weekly (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type         TEXT    NOT NULL,
    market_code         TEXT    NOT NULL,
    market_name         TEXT,
    contract_name       TEXT,
    exchange_code       TEXT,
    region_code         TEXT,
    commodity_code      TEXT,
    contract_units      TEXT,
    report_date         TEXT    NOT NULL,
    published_at        TEXT    NOT NULL,
    published_at_source TEXT    NOT NULL DEFAULT 'derived',
    report_week_label   TEXT,

    open_interest       INTEGER,
    prod_merc_long      INTEGER,
    prod_merc_short     INTEGER,
    swap_long           INTEGER,
    swap_short          INTEGER,
    swap_spread         INTEGER,
    mm_long             INTEGER,
    mm_short            INTEGER,
    mm_spread           INTEGER,
    other_rept_long     INTEGER,
    other_rept_short    INTEGER,
    other_rept_spread   INTEGER,
    tot_rept_long       INTEGER,
    tot_rept_short      INTEGER,
    nonrept_long        INTEGER,
    nonrept_short       INTEGER,

    noncomm_long        INTEGER,
    noncomm_short       INTEGER,
    noncomm_spread      INTEGER,
    comm_long           INTEGER,
    comm_short          INTEGER,

    conc_gross_le4_long  INTEGER,
    conc_gross_le4_short INTEGER,
    conc_gross_le8_long  INTEGER,
    conc_gross_le8_short INTEGER,
    conc_net_le4_long    INTEGER,
    conc_net_le4_short   INTEGER,
    conc_net_le8_long    INTEGER,
    conc_net_le8_short   INTEGER,

    traders_tot             INTEGER,
    traders_prod_long       INTEGER,
    traders_prod_short      INTEGER,
    traders_swap_long       INTEGER,
    traders_swap_short      INTEGER,
    traders_swap_spread     INTEGER,
    traders_mm_long         INTEGER,
    traders_mm_short        INTEGER,
    traders_mm_spread       INTEGER,
    traders_other_long      INTEGER,
    traders_other_short     INTEGER,
    traders_other_spread    INTEGER,

    null_fields_json    TEXT,
    row_hash            TEXT    NOT NULL,
    source_raw_id       INTEGER NOT NULL REFERENCES cot_raw(id),
    ingest_batch_id     TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (report_type, market_code, report_date)
);
CREATE INDEX IF NOT EXISTS ix_cot_weekly_pub  ON cot_weekly (report_type, market_code, published_at);
CREATE INDEX IF NOT EXISTS ix_cot_weekly_date ON cot_weekly (report_type, market_code, report_date);

CREATE TABLE IF NOT EXISTS cot_indicators (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type         TEXT    NOT NULL,
    market_code         TEXT    NOT NULL,
    report_date         TEXT    NOT NULL,
    published_at        TEXT    NOT NULL,
    indicator_code      TEXT    NOT NULL,
    window_key          TEXT    NOT NULL DEFAULT '',
    value_num           REAL,
    value_text          TEXT,
    is_valid            INTEGER NOT NULL DEFAULT 1,
    invalid_reason      TEXT,
    sample_size         INTEGER,
    params_json         TEXT    NOT NULL,
    params_hash         TEXT    NOT NULL,
    calculation_version TEXT    NOT NULL,
    computed_at         TEXT    NOT NULL,
    UNIQUE (report_type, market_code, report_date, indicator_code, window_key, calculation_version)
);
CREATE INDEX IF NOT EXISTS ix_ind_series ON cot_indicators
    (report_type, market_code, indicator_code, window_key, published_at);
CREATE INDEX IF NOT EXISTS ix_ind_ver ON cot_indicators (calculation_version);

CREATE TABLE IF NOT EXISTS price_daily (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL,
    adj_close       REAL,
    volume          INTEGER,
    source          TEXT NOT NULL,
    source_symbol   TEXT,
    ingest_batch_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS ix_price_date ON price_daily (trade_date);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id           TEXT NOT NULL,
    run_type           TEXT NOT NULL,
    status             TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    duration_ms        INTEGER,
    source_kind        TEXT,
    source_file        TEXT,
    source_file_sha256 TEXT,
    report_types       TEXT,
    markets_json       TEXT,
    rows_read          INTEGER DEFAULT 0,
    rows_valid         INTEGER DEFAULT 0,
    rows_inserted      INTEGER DEFAULT 0,
    rows_updated       INTEGER DEFAULT 0,
    rows_unchanged     INTEGER DEFAULT 0,
    rows_rejected      INTEGER DEFAULT 0,
    rows_revised       INTEGER DEFAULT 0,
    validation_json    TEXT,
    issues_json        TEXT,
    params_json        TEXT,
    error_json         TEXT,
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_started ON ingestion_log (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_log_status  ON ingestion_log (status);
CREATE INDEX IF NOT EXISTS ix_log_hash    ON ingestion_log (source_file_sha256);
"""

TABLES = ("cot_raw", "cot_weekly", "cot_indicators", "price_daily", "ingestion_log")


def open_con(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a connection with the pragmas this project relies on.

    ``check_same_thread=False`` is required by the FastAPI dependency below:
    Starlette runs each *sync* dependency and each *sync* endpoint in the
    threadpool, and the thread that closes the generator is not guaranteed to be
    the one that opened it. A request still touches its connection from one
    thread at a time (open -> query -> close is strictly sequential), so this
    only permits a hand-off, never concurrent use.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_db(db_path: Path | str = DB_PATH) -> None:
    con = open_con(db_path)
    try:
        con.executescript(SCHEMA_SQL)
        con.commit()
    finally:
        con.close()


def get_con(db_path: Path | str = DB_PATH):
    """FastAPI dependency yielding a per-request connection."""
    con = open_con(db_path)
    try:
        yield con
    finally:
        con.close()


# --------------------------------------------------------------------------
# Point-in-time
# --------------------------------------------------------------------------

PIT_MODES = ("published_at", "conservative", "report_date")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Canonical ISO-8601 UTC rendering used everywhere in the DB."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_as_of(as_of: str | None) -> datetime:
    """Normalise an `as_of` parameter to an aware UTC datetime.

    Accepts an ISO date, an ISO datetime, or nothing (= now).
    """
    if as_of is None or as_of == "":
        return utc_now()
    raw = as_of.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid as_of value: {as_of!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pit_clause(pit_mode: str, column_prefix: str = "") -> tuple[str, dict[str, Any]]:
    """Build the WHERE fragment that enforces point-in-time visibility.

    Returns (sql_fragment, params). The fragment always compares against the
    bound parameter `:as_of` so callers cannot interpolate dates by hand.
    """
    if pit_mode not in PIT_MODES:
        raise ValueError(f"invalid pit_mode: {pit_mode!r}; expected one of {PIT_MODES}")
    col = f"{column_prefix}." if column_prefix else ""
    if pit_mode == "published_at":
        return f"{col}published_at <= :as_of", {}
    if pit_mode == "conservative":
        # report_date + 7 days must already be in the past: the safest lower
        # bound on when the report could possibly have been visible.
        return f"date({col}report_date, '+7 days') <= date(:as_of)", {}
    return f"{col}report_date <= date(:as_of)", {}


def conservative_publish_floor(report_date: str) -> str:
    """report_date + 7 days, used by the `conservative` mode."""
    d = date.fromisoformat(report_date) + timedelta(days=7)
    return d.isoformat()


if __name__ == "__main__":  # pragma: no cover - manual helper
    import argparse

    parser = argparse.ArgumentParser(description="Gold COT Lab database helper")
    parser.add_argument("--init", action="store_true", help="create tables and indexes")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()
    if args.init:
        init_db(args.db)
        con = open_con(args.db)
        try:
            found = {
                r["name"]
                for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            idx = {
                r["name"]
                for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            print(f"db: {args.db}")
            print(f"tables: {sorted(found)}")
            print(f"indexes: {sorted(i for i in idx if i.startswith('ix_'))}")
        finally:
            con.close()
    else:
        parser.print_help()