"""Gold price adapter: network sources first, local fallback last.

Design constraint (step 11 of the plan): **this module must never break the COT
pipeline.** Every failure path — no network, HTTP 403, rate limit, unparseable
file — ends in an ``ingestion_log`` row with ``status='failed'`` and a populated
``error_json``, then returns a summary dict. Nothing here raises out to a caller
that was only asked to refresh prices.

Source order is deliberate degradation rather than preference:

1. ``sina`` for ``GC`` — Sina Finance's COMEX gold futures daily kline. Same
   instrument as the COT report, reachable from mainland networks, and the only
   source verified to answer from this machine.
2. ``yfinance`` for ``GC=F`` — the same instrument via Yahoo, which is rate
   limited and often blocked here.
3. ``stooq`` for ``xauusd`` — spot gold. A *different instrument* on purpose;
   it is recorded with ``source_symbol='xauusd'`` and flagged in ``issues`` so a
   later reader cannot mistake a spot series for the futures one.
4. ``manual_csv`` — a file the user dropped into ``data/raw/prices/``.

Consecutive-contract prices are not a tradable return series: a roll can produce
a jump that is an artefact of switching contract months, not a market move. This
module therefore reports jump candidates (in the summary and in the log's
``issues_json``) instead of silently smoothing them, and the schema
intentionally stores no adjusted series.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from ..config import DB_PATH, DEFAULT_PRICE_SYMBOL, RAW_PRICE_DIR
from ..db import iso, open_con, utc_now
from .normalize import sha256_file

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

PRICE_SOURCES = ("sina", "yfinance", "stooq", "manual_csv")

# stooq uses its own tickers; xauusd is spot gold, not the futures contract.
STOOQ_SYMBOLS = {"GC=F": "xauusd"}

STOOQ_URL = "https://stooq.com/q/d/l/"

# Sina Finance serves a JSONP wrapper around the daily kline. The referer is not
# optional: without it the endpoint answers with an error page instead of data.
SINA_SYMBOLS = {"GC=F": "GC"}
SINA_DAILY_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
    "var%20_gc/GlobalFuturesService.getGlobalFuturesDailyKLine"
)
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (compatible; gold-cot-lab research tool)",
}
SINA_JSONP = re.compile(r"var\s+\w+\s*\(\s*(\[.*\])\s*\)\s*;", re.S)

# A close-to-close move this large is more likely a contract roll or a data
# artefact than a real session move, so it is surfaced for inspection.
JUMP_THRESHOLD = 0.08

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "trade_date": ("trade_date", "date", "datetime", "time"),
    "open": ("open",),
    "high": ("high",),
    "low": ("low",),
    "close": ("close", "closeprice", "close_price", "price", "last"),
    "adj_close": ("adj_close", "adjclose", "adjusted_close"),
    "volume": ("volume", "vol"),
}

CANONICAL_COLUMNS = (
    "symbol", "trade_date", "open", "high", "low", "close", "adj_close",
    "volume", "source", "source_symbol",
)

PRICE_NUMERIC_KEYS = ("open", "high", "low", "close", "adj_close")

REVISION_NOTE = (
    "price rows are updated in place on a changed value: corrected prints and "
    "adjusted closes are normal for a market series, unlike COT revisions"
)


class PriceSourceError(Exception):
    """One source failed. Carries the reason so it can be logged verbatim."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# --------------------------------------------------------------------------
# Source adapters — each returns a raw frame, or raises PriceSourceError
# --------------------------------------------------------------------------


def fetch_yfinance(symbol: str, start: str | None, end: str | None) -> pd.DataFrame:
    """Daily OHLCV for the continuous front-month future via yfinance."""
    try:
        import yfinance as yf
    except Exception as exc:  # pragma: no cover - import guard
        raise PriceSourceError("import_failed", f"yfinance unavailable: {exc}") from exc

    try:
        frame = yf.Ticker(symbol).history(
            start=start, end=end, interval="1d", auto_adjust=False, actions=False
        )
    except Exception as exc:
        raise PriceSourceError("request_failed", f"{type(exc).__name__}: {exc}") from exc

    if frame is None or frame.empty:
        raise PriceSourceError("empty_response", "yfinance returned no rows")
    return frame


def fetch_stooq(symbol: str, start: str | None, end: str | None) -> pd.DataFrame:
    """Daily OHLCV from stooq's CSV endpoint (spot gold for ``GC=F``)."""
    import requests

    stooq_symbol = STOOQ_SYMBOLS.get(symbol, symbol.lower().replace("=", ""))
    params: dict[str, str] = {"s": stooq_symbol, "i": "d"}
    if start:
        params["d1"] = start.replace("-", "")
    if end:
        params["d2"] = end.replace("-", "")

    try:
        response = requests.get(
            STOOQ_URL,
            params=params,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (gold-cot-lab research tool)"},
        )
    except Exception as exc:
        raise PriceSourceError("request_failed", f"{type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise PriceSourceError("http_error", f"HTTP {response.status_code}")
    text = response.text.strip()
    if not text or not text.lower().startswith("date"):
        # stooq answers a JS challenge / "exceeded the daily hits limit" as HTML.
        raise PriceSourceError("unexpected_payload", text[:200] or "empty body")

    frame = pd.read_csv(StringIO(text))
    if frame.empty:
        raise PriceSourceError("empty_response", "stooq returned no rows")
    frame.attrs["source_symbol"] = stooq_symbol
    return frame


def fetch_sina(symbol: str, start: str | None, end: str | None) -> pd.DataFrame:
    """Daily OHLC for the COMEX gold continuation via Sina Finance.

    The response is JSONP, and the endpoint returns roughly ten years in one go;
    ``start`` / ``end`` are therefore applied client-side. The payload itself is
    ASCII numbers, but the wrapper page carries Chinese, so it is decoded as GBK
    with replacement rather than UTF-8.
    """
    import urllib.error
    import urllib.request

    sina_symbol = SINA_SYMBOLS.get(symbol, symbol.replace("=", ""))
    request = urllib.request.Request(
        f"{SINA_DAILY_URL}?symbol={sina_symbol}", headers=SINA_HEADERS
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise PriceSourceError("http_error", f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise PriceSourceError("request_failed", f"{type(exc).__name__}: {exc}") from exc

    text = body.decode("gbk", "replace")
    matched = SINA_JSONP.search(text)
    if not matched:
        raise PriceSourceError("unexpected_payload", text[:200] or "empty body")
    try:
        records = json.loads(matched.group(1))
    except ValueError as exc:
        raise PriceSourceError("parse_failed", f"{type(exc).__name__}: {exc}") from exc
    if not records:
        raise PriceSourceError("empty_response", "sina returned no rows")

    frame = pd.DataFrame(records)
    if "date" not in frame.columns:
        raise PriceSourceError(
            "missing_columns", f"no date column; got {[str(c) for c in frame.columns][:12]}"
        )
    # `volume` is present but always "0" for this feed, so it is dropped rather
    # than stored as a real zero.
    keep = [c for c in ("date", "open", "high", "low", "close") if c in frame.columns]
    frame = frame[keep].rename(columns={"date": "trade_date"})
    frame["trade_date"] = frame["trade_date"].astype(str).str.slice(0, 10)
    if start:
        frame = frame[frame["trade_date"] >= start]
    if end:
        frame = frame[frame["trade_date"] <= end]
    frame.attrs["source_symbol"] = sina_symbol
    return frame


def manual_csv_path(symbol: str) -> Path:
    safe = symbol.replace("=", "_").replace("/", "_")
    return RAW_PRICE_DIR / f"manual_{safe}.csv"


def load_manual_csv(symbol: str, path: Path | None = None) -> pd.DataFrame:
    """Read a user-supplied CSV dropped into ``data/raw/prices/``."""
    target = path or manual_csv_path(symbol)
    if not target.exists():
        raise PriceSourceError("file_missing", f"{target} does not exist")
    try:
        frame = pd.read_csv(target)
    except Exception as exc:
        raise PriceSourceError("parse_failed", f"{type(exc).__name__}: {exc}") from exc
    if frame.empty:
        raise PriceSourceError("empty_response", f"{target} has no rows")
    frame.attrs["source_file"] = str(target)
    return frame


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def _resolve_columns(frame: pd.DataFrame) -> dict[str, str]:
    """Map canonical names to actual column names, case/space insensitively."""
    lookup = {str(c).strip().lower().replace(" ", "_"): str(c) for c in frame.columns}
    resolved: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                resolved[canonical] = lookup[alias]
                break
    return resolved


def _numeric(series: pd.Series) -> pd.Series:
    try:
        return pd.to_numeric(series, errors="coerce")
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return pd.Series([None] * len(series), index=series.index)


def normalize_frame(
    raw: pd.DataFrame,
    source: str,
    symbol: str,
    source_symbol: str | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Coerce a source frame into the ``price_daily`` shape.

    Returns the cleaned frame plus human-readable issues. Rows without a usable
    date or a strictly positive close are dropped rather than stored as NULL —
    a price row that cannot price anything is noise.
    """
    issues: list[str] = []
    frame = raw.copy()

    # yfinance returns the trading day in the index.
    if not isinstance(frame.index, pd.RangeIndex) and "trade_date" not in {
        str(c).strip().lower() for c in frame.columns
    }:
        frame = frame.reset_index()

    columns = _resolve_columns(frame)
    missing = [key for key in ("trade_date", "close") if key not in columns]
    if missing:
        raise PriceSourceError(
            "missing_columns",
            f"cannot find {missing}; got {[str(c) for c in frame.columns][:12]}",
        )

    out = pd.DataFrame(index=frame.index)
    parsed = pd.to_datetime(frame[columns["trade_date"]], errors="coerce")
    out["trade_date"] = [None if pd.isna(v) else v.date() for v in parsed]
    for key in PRICE_NUMERIC_KEYS + ("volume",):
        out[key] = _numeric(frame[columns[key]]) if key in columns else None

    bad_date = int(sum(1 for v in out["trade_date"] if v is None))
    if bad_date:
        issues.append(f"{bad_date} row(s) dropped: unparseable trade_date")
    no_close = out["close"].isna() | (out["close"] <= 0)
    dropped = int((pd.Series([v is not None for v in out["trade_date"]], index=out.index) & no_close).sum())
    if dropped:
        issues.append(f"{dropped} row(s) dropped: missing or non-positive close")

    keep = pd.Series([v is not None for v in out["trade_date"]], index=out.index) & ~no_close
    out = out[keep].copy()
    if out.empty:
        raise PriceSourceError("no_valid_rows", "every row was rejected by validation")

    out["trade_date"] = [v.isoformat() for v in out["trade_date"]]
    duplicates = int(out["trade_date"].duplicated().sum())
    if duplicates:
        issues.append(f"{duplicates} duplicate trade_date(s); kept the last of each")
        out = out.drop_duplicates(subset=["trade_date"], keep="last")

    out["symbol"] = symbol
    out["source"] = source
    out["source_symbol"] = source_symbol or symbol
    out = out.sort_values("trade_date").reset_index(drop=True)
    return out[list(CANONICAL_COLUMNS)], issues


def find_jumps(frame: pd.DataFrame, threshold: float = JUMP_THRESHOLD) -> list[str]:
    """Report close-to-close moves large enough to be a contract roll artefact.

    Reported, never smoothed: on a continuous series a roll can look like a
    violent move, and silently "fixing" it would invent data.
    """
    closed = [_num(v) for v in frame["close"]]
    dates = list(frame["trade_date"])
    jumps: list[str] = []
    for i in range(1, len(closed)):
        previous, current = closed[i - 1], closed[i]
        if not previous or not current:
            continue
        change = current / previous - 1.0
        if abs(change) >= threshold:
            jumps.append(
                f"{dates[i]}: {change * 100:+.1f}% vs {dates[i - 1]} "
                f"({previous:.2f} -> {current:.2f}); possible contract roll"
            )
    return jumps


# --------------------------------------------------------------------------
# Numeric coercion + the idempotency key
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(round(number))


def payload_key(row: Mapping[str, Any]) -> str:
    """Identity of a stored price row, used to detect real changes.

    Numbers are routed through ``_num`` / ``_int`` and rounded, so a value read
    back from SQLite as an ``int`` compares equal to the same value held as a
    ``float`` in a freshly fetched frame — otherwise an idempotent re-run would
    report every row as updated.
    """
    parts: list[Any] = []
    for key in PRICE_NUMERIC_KEYS:
        value = _num(row.get(key))
        parts.append(None if value is None else round(value, 6))
    parts.append(_int(row.get("volume")))
    parts.append(row.get("source"))
    parts.append(row.get("source_symbol"))
    return json.dumps(parts, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def upsert_price_daily(
    con: sqlite3.Connection, frame: pd.DataFrame, batch_id: str
) -> dict[str, int]:
    """Idempotent write keyed on ``UNIQUE (symbol, trade_date)``."""
    symbol = str(frame["symbol"].iloc[0])
    existing = {
        str(row["trade_date"]): payload_key(dict(row))
        for row in con.execute(
            "SELECT trade_date, open, high, low, close, adj_close, volume, source, "
            "source_symbol FROM price_daily WHERE symbol = ?",
            (symbol,),
        )
    }
    counters = {"inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0}
    now = iso(utc_now())

    for _, row in frame.iterrows():
        record = {key: row.get(key) for key in CANONICAL_COLUMNS}
        trade_date = str(record["trade_date"])
        found = existing.get(trade_date)
        if found == payload_key(record):
            counters["unchanged"] += 1
            continue

        values = (
            _num(record.get("open")), _num(record.get("high")), _num(record.get("low")),
            _num(record.get("close")), _num(record.get("adj_close")),
            _int(record.get("volume")), str(record["source"]),
            str(record["source_symbol"]), batch_id,
        )
        if found is None:
            con.execute(
                "INSERT INTO price_daily (symbol, trade_date, open, high, low, close, "
                "adj_close, volume, source, source_symbol, ingest_batch_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, trade_date) + values + (now, now),
            )
            counters["inserted"] += 1
        else:
            con.execute(
                "UPDATE price_daily SET open=?, high=?, low=?, close=?, adj_close=?, "
                "volume=?, source=?, source_symbol=?, ingest_batch_id=?, updated_at=? "
                "WHERE symbol=? AND trade_date=?",
                values + (now, symbol, trade_date),
            )
            counters["updated"] += 1
    con.commit()
    return counters


def save_raw_file(raw: pd.DataFrame, source: str, symbol: str) -> Path | None:
    """Archive exactly what the source returned, under ``data/raw/prices/``."""
    RAW_PRICE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = symbol.replace("=", "_").replace("/", "_")
    path = RAW_PRICE_DIR / f"{source}_{safe}_{stamp}.csv"
    try:
        raw.to_csv(path)
    except Exception:  # pragma: no cover - defensive
        return None
    return path


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def _start_log(con: sqlite3.Connection, batch_id: str, symbol: str) -> None:
    con.execute(
        "INSERT INTO ingestion_log (batch_id, run_type, status, started_at, source_kind, "
        "markets_json) VALUES (?, 'price_update', 'running', ?, 'network', ?)",
        (batch_id, iso(utc_now()), json.dumps([symbol])),
    )
    con.commit()


def _finish_log(
    con: sqlite3.Connection,
    batch_id: str,
    status: str,
    *,
    counters: dict[str, int] | None = None,
    source_kind: str | None = None,
    source_file: str | None = None,
    source_file_sha256: str | None = None,
    rows_read: int = 0,
    rows_valid: int = 0,
    issues: list[str] | None = None,
    error: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    counters = counters or {}
    con.execute(
        "UPDATE ingestion_log SET status=?, finished_at=?, duration_ms=?, "
        "source_kind=COALESCE(?, source_kind), source_file=COALESCE(?, source_file), "
        "source_file_sha256=COALESCE(?, source_file_sha256), rows_read=?, rows_valid=?, "
        "rows_inserted=?, rows_updated=?, rows_unchanged=?, rows_rejected=?, "
        "issues_json=?, error_json=?, notes=? WHERE batch_id=?",
        (
            status, iso(utc_now()), duration_ms, source_kind, source_file,
            source_file_sha256, rows_read, rows_valid,
            counters.get("inserted", 0), counters.get("updated", 0),
            counters.get("unchanged", 0), counters.get("rejected", 0),
            json.dumps(issues or [], ensure_ascii=False),
            json.dumps(error, ensure_ascii=False) if error else None,
            REVISION_NOTE,
            batch_id,
        ),
    )
    con.commit()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _pick_fetcher(
    source: str, symbol: str, start: str | None, end: str | None, manual_csv: Path | None
) -> Callable[[], pd.DataFrame] | None:
    if source == "yfinance":
        return lambda: fetch_yfinance(symbol, start, end)
    if source == "sina":
        return lambda: fetch_sina(symbol, start, end)
    if source == "stooq":
        return lambda: fetch_stooq(symbol, start, end)
    if source == "manual_csv":
        return lambda: load_manual_csv(symbol, manual_csv)
    return None


def update_price(
    con: sqlite3.Connection,
    symbol: str = DEFAULT_PRICE_SYMBOL,
    start: str | None = None,
    end: str | None = None,
    sources: tuple[str, ...] = PRICE_SOURCES,
    manual_csv: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Refresh daily gold prices, degrading through ``sources`` in order.

    Always returns a summary. A ``status='failed'`` summary means every source
    was exhausted; ``error.attempts`` then holds one entry per source with its
    reason, so the failure is diagnosable without re-running.
    """
    started = time.time()
    batch_id = uuid.uuid4().hex[:12]
    attempts: list[dict[str, Any]] = []

    if not dry_run:
        _start_log(con, batch_id, symbol)

    for source in sources:
        fetcher = _pick_fetcher(source, symbol, start, end, manual_csv)
        if fetcher is None:
            attempts.append({"source": source, "reason": "unknown_source"})
            continue

        try:
            raw = fetcher()
        except PriceSourceError as exc:
            attempts.append({"source": source, "reason": exc.reason, "detail": exc.detail})
            continue
        except Exception as exc:  # defensive: yfinance and pandas both surprise
            attempts.append({
                "source": source, "reason": "unexpected_error",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            continue

        source_symbol = str(raw.attrs.get("source_symbol", symbol))
        try:
            frame, issues = normalize_frame(raw, source, symbol, source_symbol)
        except PriceSourceError as exc:
            attempts.append({"source": source, "reason": exc.reason, "detail": exc.detail})
            continue

        jumps = find_jumps(frame)
        if jumps:
            issues.append(f"{len(jumps)} large close-to-close move(s); see jump_candidates")
        if source == "stooq":
            issues.append(
                "stooq xauusd is SPOT gold, not the COMEX futures contract the COT report covers"
            )
        if source == "sina":
            issues.append(
                "sina GC is the front-month continuation; contract rolls are reported, not adjusted"
            )
        date_range = [str(frame["trade_date"].iloc[0]), str(frame["trade_date"].iloc[-1])]

        if dry_run:
            return {
                "batch_id": batch_id, "status": "dry_run", "symbol": symbol,
                "source": source, "source_symbol": source_symbol,
                "rows_read": len(raw), "rows_valid": len(frame),
                "date_range": date_range, "jump_candidates": jumps,
                "issues": issues, "attempts": attempts,
            }

        raw_file = save_raw_file(raw, source, symbol)
        sha = sha256_file(raw_file) if raw_file else None

        try:
            counters = upsert_price_daily(con, frame, batch_id)
        except Exception as exc:
            con.rollback()
            attempts.append({
                "source": source, "reason": "write_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            error = {"attempts": attempts, "message": "price write failed; COT data untouched"}
            _finish_log(
                con, batch_id, "failed",
                source_kind=source,
                source_file=str(raw_file) if raw_file else None,
                source_file_sha256=sha,
                rows_read=len(raw), rows_valid=len(frame),
                issues=issues, error=error,
                duration_ms=int((time.time() - started) * 1000),
            )
            return {
                "batch_id": batch_id, "status": "failed", "symbol": symbol,
                "error": error, "issues": issues,
            }

        _finish_log(
            con, batch_id, "ok",
            counters=counters, source_kind=source,
            source_file=str(raw_file) if raw_file else None,
            source_file_sha256=sha,
            rows_read=len(raw), rows_valid=len(frame),
            issues=issues,
            duration_ms=int((time.time() - started) * 1000),
        )
        return {
            "batch_id": batch_id, "status": "ok", "symbol": symbol,
            "source": source, "source_symbol": source_symbol,
            "source_file": str(raw_file) if raw_file else None,
            "source_file_sha256": sha,
            "rows_read": len(raw), "rows_valid": len(frame),
            "date_range": date_range, "counters": counters,
            "jump_candidates": jumps, "issues": issues, "attempts": attempts,
        }

    error = {
        "message": "every price source failed; the COT pipeline is unaffected",
        "attempts": attempts or [{"source": "none", "reason": "no sources configured"}],
        "sources_tried": list(sources),
    }
    if not dry_run:
        _finish_log(
            con, batch_id, "failed", error=error,
            duration_ms=int((time.time() - started) * 1000),
        )
    return {
        "batch_id": batch_id, "status": "failed", "symbol": symbol,
        "error": error, "attempts": attempts,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=DEFAULT_PRICE_SYMBOL)
    parser.add_argument("--start", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD")
    parser.add_argument("--source", action="append", default=None,
                        help=f"repeatable; one of {PRICE_SOURCES}")
    parser.add_argument("--manual-csv", default=None)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from ..db import init_db

    if not args.dry_run:
        init_db(args.db)
    con = open_con(args.db)
    try:
        summary = update_price(
            con,
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            sources=tuple(args.source) if args.source else PRICE_SOURCES,
            manual_csv=Path(args.manual_csv) if args.manual_csv else None,
            dry_run=args.dry_run,
        )
    finally:
        con.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    # A price failure is reported, not fatal: the COT database is intact.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())