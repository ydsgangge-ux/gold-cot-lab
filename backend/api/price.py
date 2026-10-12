"""Gold price read endpoints.

Price is an *auxiliary* source. When nothing has been ingested yet (or when the
only source failed, as happens offline), these endpoints still answer `200` with
``available: false`` instead of raising: the dashboard must be able to render
"price unavailable" rather than die with a 500 on a machine with no network.

Continuations of a front-month contract are not a tradable return series. The
``source`` / ``source_symbol`` fields are carried through untouched so that
later analysis can decide how to treat contract rolls.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query

from ..config import DEFAULT_PRICE_SYMBOL
from ..db import get_con

router = APIRouter(prefix="/api/price", tags=["price"])

PRICE_FIELDS = (
    "trade_date, open, high, low, close, adj_close, volume, source, source_symbol"
)


@router.get("/series")
def series(
    symbol: str = Query(DEFAULT_PRICE_SYMBOL),
    start: str | None = Query(None),
    end: str | None = Query(None),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    where = ["symbol = :symbol"]
    bound: dict[str, Any] = {"symbol": symbol}
    if start:
        where.append("trade_date >= :start")
        bound["start"] = start
    if end:
        where.append("trade_date <= :end")
        bound["end"] = end

    rows = [
        dict(r)
        for r in con.execute(
            f"SELECT {PRICE_FIELDS} FROM price_daily WHERE {' AND '.join(where)} "
            f"ORDER BY trade_date",
            bound,
        )
    ]
    if not rows:
        return {
            "available": False,
            "symbol": symbol,
            "count": 0,
            "date_range": None,
            "notes": ["no price rows stored yet; price-linked indicators stay invalid"],
            "series": [],
        }
    sources = sorted({r["source"] for r in rows})
    return {
        "available": True,
        "symbol": symbol,
        "count": len(rows),
        "date_range": [rows[0]["trade_date"], rows[-1]["trade_date"]],
        "sources": sources,
        "series": rows,
    }


@router.get("/latest")
def latest(
    symbol: str = Query(DEFAULT_PRICE_SYMBOL),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    row = con.execute(
        f"SELECT {PRICE_FIELDS} FROM price_daily WHERE symbol = ? "
        f"ORDER BY trade_date DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if row is None:
        return {"available": False, "symbol": symbol, "latest": None}
    return {"available": True, "symbol": symbol, "latest": dict(row)}