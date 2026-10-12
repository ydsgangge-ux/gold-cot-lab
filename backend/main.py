"""FastAPI entry point for Gold COT Lab.

Route order matters: the ``/api`` routers are registered first, and the static
frontend is mounted at ``/`` last. Mounting ``StaticFiles`` first would swallow
every API prefix.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .analysis.indicators import CALCULATION_VERSION, IndicatorParams
from .api import admin, cot, price
from .config import FRONTEND_DIST, REPORT_TYPES
from .db import get_con, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Gold COT Lab",
    description="Local research tool for CFTC gold futures positioning.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cot.router)
app.include_router(price.router)
app.include_router(admin.router)


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, Any]:
    return {"status": "ok", "version": app.version, "calculation_version": CALCULATION_VERSION}


@app.get("/api/meta/filters", tags=["meta"])
def filters(con: sqlite3.Connection = Depends(get_con)) -> dict[str, Any]:
    """What the dashboard is allowed to offer in its global filter bar."""
    present = {
        r["report_type"]
        for r in con.execute("SELECT DISTINCT report_type FROM cot_weekly")
    }
    markets = [
        dict(r)
        for r in con.execute(
            "SELECT report_type, market_code, market_name, exchange_code, COUNT(*) AS reports, "
            "MIN(report_date) AS first_date, MAX(report_date) AS last_date, "
            "MAX(published_at) AS last_published_at "
            "FROM cot_weekly GROUP BY report_type, market_code, market_name, exchange_code "
            "ORDER BY report_type, market_code"
        )
    ]
    versions = [
        r["calculation_version"]
        for r in con.execute(
            "SELECT DISTINCT calculation_version FROM cot_indicators "
            "ORDER BY calculation_version"
        )
    ]
    latest_published = con.execute("SELECT MAX(published_at) FROM cot_weekly").fetchone()[0]
    params = IndicatorParams()

    return {
        "report_types": [
            {"code": code, "available": code in present} for code in REPORT_TYPES
        ],
        "markets": markets,
        "latest_published_at": latest_published,
        "calculation_versions": versions,
        "price_symbols": [
            dict(r)
            for r in con.execute(
                "SELECT symbol, COUNT(*) AS rows, MIN(trade_date) AS first_date, "
                "MAX(trade_date) AS last_date FROM price_daily GROUP BY symbol"
            )
        ],
        "effective_params": {
            "params": params.to_json(),
            "params_hash": params.params_hash,
            "calculation_version": CALCULATION_VERSION,
        },
        "pit_modes": [
            {"code": "published_at", "label": "按公开时间（默认 · 只看当时真能看到的）"},
            {"code": "conservative", "label": "保守口径（报告日再往后推 7 天）"},
            {"code": "report_date", "label": "按报告日（会看到未来 · 仅研究对照）"},
        ],
    }


if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
else:  # pragma: no cover - depends on whether `npm run build` has been run

    @app.get("/", tags=["meta"])
    def frontend_missing() -> dict[str, Any]:
        return {
            "status": "api-only",
            "message": "frontend/dist not found; run `npm run build` in frontend/",
            "docs": "/docs",
        }