"""Data-management endpoints.

Data management is not a side feature: it is the evidence page for the whole
tool. Every response here answers "where did this row come from, when was it
loaded, and what did the last run fail to do".

Writes are idempotent by construction — they call the same ``run_import`` /
``recompute`` used by the CLI scripts, so re-running a batch can never
duplicate rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..analysis.indicators import CALCULATION_VERSION, IndicatorParams
from ..config import DEFAULT_PRICE_SYMBOL, RAW_CFTC_DIR, REPORT_TYPE_DISAGGREGATED
from ..data.cftc_import import run_import
from ..data.price_client import PRICE_SOURCES, update_price
from ..db import get_con
from scripts.recompute_indicators import recompute

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Uploaded file names are sanitised: only the base name survives, and every
# character that is not a plain word character, dot or dash is replaced. A
# browser-supplied name must never be able to pick the destination directory.
UNSAFE_FILENAME = re.compile(r"[^\w.\-]+")

COUNTED_TABLES = (
    "cot_raw",
    "cot_weekly",
    "cot_indicators",
    "price_daily",
    "ingestion_log",
)

LOG_FIELDS = (
    "batch_id, run_type, status, started_at, finished_at, duration_ms, source_kind, "
    "source_file, source_file_sha256, report_types, rows_read, rows_valid, rows_inserted, "
    "rows_updated, rows_unchanged, rows_revised, rows_rejected, error_json, notes"
)


class ImportRequest(BaseModel):
    """Paths are resolved against the raw CFTC directory when relative."""

    paths: list[str] | None = Field(
        default=None, description="CSV paths; omit to import every file in data/raw/cftc"
    )
    report_type: str = REPORT_TYPE_DISAGGREGATED
    accept_revisions: bool = False
    dry_run: bool = False


class UploadRequest(BaseModel):
    """A CSV uploaded from the browser.

    The bytes travel as text in the body rather than as ``multipart/form-data``
    so that the project does not need ``python-multipart`` installed. The server
    writes them to ``data/raw/cftc/`` first, exactly like a manual copy, so the
    raw file is always archived before anything is parsed.
    """

    filename: str = Field(description="original file name, used for the archived copy")
    content: str = Field(description="the CSV file's text content")
    report_type: str = REPORT_TYPE_DISAGGREGATED
    accept_revisions: bool = False
    dry_run: bool = False


class PriceRefreshRequest(BaseModel):
    symbol: str = DEFAULT_PRICE_SYMBOL
    sources: list[str] | None = None
    recompute: bool = True


class ComputeRequest(BaseModel):
    report_type: str = REPORT_TYPE_DISAGGREGATED
    market_codes: list[str] | None = None
    version: str = CALCULATION_VERSION
    alert_high_pct: float | None = None
    alert_low_pct: float | None = None
    min_coverage: float | None = None


def _resolve_paths(paths: list[str] | None) -> list[Path]:
    if not paths:
        return sorted(RAW_CFTC_DIR.glob("*.csv"))
    resolved: list[Path] = []
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = RAW_CFTC_DIR / candidate
        if not candidate.exists():
            raise HTTPException(status_code=404, detail=f"file not found: {candidate}")
        resolved.append(candidate)
    return resolved


@router.get("/status")
def status(con: sqlite3.Connection = Depends(get_con)) -> dict[str, Any]:
    """One-screen truth about the stored data."""
    counts = {
        table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in COUNTED_TABLES
    }

    markets = [
        dict(r)
        for r in con.execute(
            "SELECT report_type, market_code, COUNT(*) AS reports, "
            "MIN(report_date) AS first_date, MAX(report_date) AS last_date, "
            "MAX(published_at) AS last_published_at "
            "FROM cot_weekly GROUP BY report_type, market_code "
            "ORDER BY report_type, market_code"
        )
    ]

    versions = [
        dict(r)
        for r in con.execute(
            "SELECT calculation_version, COUNT(*) AS rows, MAX(computed_at) AS computed_at "
            "FROM cot_indicators GROUP BY calculation_version ORDER BY calculation_version"
        )
    ]

    last_batch = con.execute(
        f"SELECT {LOG_FIELDS} FROM ingestion_log ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    last_error = con.execute(
        "SELECT batch_id, started_at, status, error_json FROM ingestion_log "
        "WHERE status = 'failed' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    last_error_record = dict(last_error) if last_error else None
    if last_error_record:
        # Returned decoded, for the same reason as ingestion_detail: the raw
        # string arrives double-escaped and is unreadable in the dashboard.
        raw_error = last_error_record.get("error_json")
        last_error_record["error_json"] = json.loads(raw_error) if raw_error else None

    return {
        "counts": counts,
        "markets": markets,
        "indicator_versions": versions,
        "price_symbols": [
            dict(r)
            for r in con.execute(
                "SELECT symbol, COUNT(*) AS rows, MIN(trade_date) AS first_date, "
                "MAX(trade_date) AS last_date FROM price_daily GROUP BY symbol"
            )
        ],
        "last_batch": dict(last_batch) if last_batch else None,
        "last_error": last_error_record,
    }


@router.get("/db/stats")
def db_stats(
    report_type: str = Query(REPORT_TYPE_DISAGGREGATED),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    """Structure checks that must stay true for the history to be trustworthy."""
    counts = {
        table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in COUNTED_TABLES
    }

    duplicate_keys = con.execute(
        "SELECT COUNT(*) FROM (SELECT report_type, market_code, report_date FROM cot_weekly "
        "GROUP BY report_type, market_code, report_date HAVING COUNT(*) > 1)"
    ).fetchone()[0]

    orphans = con.execute(
        "SELECT COUNT(*) FROM cot_weekly w "
        "LEFT JOIN cot_raw r ON r.id = w.source_raw_id WHERE r.id IS NULL"
    ).fetchone()[0]

    gaps: list[dict[str, Any]] = []
    market_rows = list(
        con.execute(
            "SELECT market_code, MAX(market_name) AS market_name FROM cot_weekly "
            "WHERE report_type = ? GROUP BY market_code ORDER BY market_code",
            (report_type,),
        )
    )
    for market in market_rows:
        market_code = market["market_code"]
        dates = [
            date.fromisoformat(r["report_date"])
            for r in con.execute(
                "SELECT report_date FROM cot_weekly WHERE report_type = ? AND market_code = ? "
                "ORDER BY report_date",
                (report_type, market_code),
            )
        ]
        missing = []
        for prev, cur in zip(dates, dates[1:]):
            if (cur - prev).days > 7:
                missing.append({"after": prev.isoformat(), "before": cur.isoformat(),
                                "gap_days": (cur - prev).days})
        gaps.append(
            {
                "market_code": market_code,
                # The code alone ("088691") tells a reader nothing, so the name
                # travels with it everywhere the market is listed.
                "market_name": market["market_name"],
                "reports": len(dates),
                "first_date": dates[0].isoformat() if dates else None,
                "last_date": dates[-1].isoformat() if dates else None,
                "gaps": missing[:50],
                "gap_count": len(missing),
            }
        )

    return {
        "report_type": report_type,
        "counts": counts,
        "integrity": {
            "duplicate_keys": duplicate_keys,
            "weekly_rows_without_raw": orphans,
        },
        "markets": gaps,
    }


@router.get("/ingestion")
def ingestion_list(
    limit: int = Query(20, ge=1, le=200),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    rows = [
        dict(r)
        for r in con.execute(
            f"SELECT {LOG_FIELDS} FROM ingestion_log ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
    ]
    return {"count": len(rows), "batches": rows}


@router.get("/ingestion/{batch_id}")
def ingestion_detail(
    batch_id: str,
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    row = con.execute(
        "SELECT * FROM ingestion_log WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown batch_id: {batch_id}")
    record = dict(row)
    # The *_json columns are returned decoded: the raw string would just be
    # double-escaped noise for the dashboard.
    for key in ("validation_json", "issues_json", "error_json", "markets_json", "params_json"):
        value = record.get(key)
        record[key] = json.loads(value) if value else None
    return record


@router.post("/import")
def import_files(
    request: ImportRequest,
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    paths = _resolve_paths(request.paths)
    if not paths:
        raise HTTPException(
            status_code=404,
            detail=f"no CSV found; pass paths or drop files into {RAW_CFTC_DIR}",
        )
    summaries = [
        run_import(
            con,
            path,
            report_type=request.report_type,
            dry_run=request.dry_run,
            accept_revisions=request.accept_revisions,
        )
        for path in paths
    ]
    return {
        "imported": len(summaries),
        "batches": [
            {
                "batch_id": s["batch_id"],
                "source_file": s["source_file"],
                "status": s["status"],
                "rows_read": s["rows_read"],
                "rows_inserted": s["rows_inserted"],
                "rows_updated": s["rows_updated"],
                "rows_unchanged": s["rows_unchanged"],
                "rows_revised": s["rows_revised"],
                "rows_by_market": s["rows_by_market"],
                "date_range": s["date_range"],
            }
            for s in summaries
        ],
    }


@router.post("/upload")
def upload_csv(
    request: UploadRequest,
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    """Accept a CSV chosen in the browser and run the normal import on it.

    The file is archived under ``data/raw/cftc/`` before parsing, so an uploaded
    file is indistinguishable from one the user copied in by hand: the raw layer
    keeps it, and every later re-run is idempotent against it.
    """
    if not request.content.strip():
        raise HTTPException(status_code=400, detail="the uploaded file is empty")

    safe_name = UNSAFE_FILENAME.sub("_", Path(request.filename).name) or "upload.csv"
    if not safe_name.lower().endswith(".csv"):
        safe_name += ".csv"
    RAW_CFTC_DIR.mkdir(parents=True, exist_ok=True)
    destination = RAW_CFTC_DIR / safe_name
    try:
        # CFTC exports are UTF-8; the browser hands the text back decoded the same
        # way, so writing UTF-8 here round-trips the original bytes.
        destination.write_text(request.content, encoding="utf-8", newline="")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot save file: {exc}") from exc

    summary = run_import(
        con,
        destination,
        report_type=request.report_type,
        dry_run=request.dry_run,
        accept_revisions=request.accept_revisions,
    )
    return {
        "imported": 1,
        "saved_to": str(destination),
        "batches": [
            {
                "batch_id": summary["batch_id"],
                "source_file": summary["source_file"],
                "status": summary["status"],
                "rows_read": summary["rows_read"],
                "rows_inserted": summary["rows_inserted"],
                "rows_updated": summary["rows_updated"],
                "rows_unchanged": summary["rows_unchanged"],
                "rows_revised": summary["rows_revised"],
                "rows_by_market": summary["rows_by_market"],
                "date_range": summary["date_range"],
            }
        ],
    }


@router.post("/compute-indicators")
def compute_indicators(
    request: ComputeRequest,
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    base = IndicatorParams()
    params = IndicatorParams(
        alert_high_pct=request.alert_high_pct if request.alert_high_pct is not None else base.alert_high_pct,
        alert_low_pct=request.alert_low_pct if request.alert_low_pct is not None else base.alert_low_pct,
        min_coverage=request.min_coverage if request.min_coverage is not None else base.min_coverage,
    )
    result = recompute(con, request.report_type, request.market_codes, params, request.version)
    return {
        **result,
        "version": request.version,
        "params": json.loads(params.to_json()),
        "params_hash": params.params_hash,
    }


@router.post("/refresh-price")
def refresh_price(
    request: PriceRefreshRequest,
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    """Fetch the gold price series, then optionally recompute the indicators.

    Recomputing in the same call is what makes this useful: the price-linked
    indicators were stored as invalid when no price existed, and only a recompute
    turns them into real values. A price failure is still a ``200`` — it never
    invalidates the COT data.
    """
    sources = tuple(request.sources) if request.sources else PRICE_SOURCES
    summary = update_price(con, symbol=request.symbol, sources=sources)

    recompute_result: dict[str, Any] | None = None
    if request.recompute and summary.get("status") == "ok":
        recompute_result = recompute(
            con, REPORT_TYPE_DISAGGREGATED, None, IndicatorParams(), CALCULATION_VERSION
        )

    return {"price": summary, "recompute": recompute_result}