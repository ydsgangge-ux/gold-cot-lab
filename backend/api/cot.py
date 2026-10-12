"""COT read endpoints.

Every date filter goes through ``resolve_as_of`` + ``pit_clause`` (the single
point-in-time implementation in ``backend.db``) so that no handler can invent its
own notion of "visible as of". ``report_type`` is mandatory on all of them: the
Disaggregated and Legacy reports define different trader categories, and their
series must never be concatenated.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..analysis.indicators import CALCULATION_VERSION, DIV_UNKNOWN
from ..config import REPORT_TYPE_DISAGGREGATED
from ..db import get_con, iso, pit_clause, resolve_as_of

router = APIRouter(prefix="/api/cot", tags=["cot"])

TEXT_INDICATORS = {"divergence", "alert"}

WEEKLY_FIELDS = (
    "report_date, published_at, published_at_source, market_name, exchange_code, "
    "open_interest, mm_long, mm_short, mm_spread, prod_merc_long, prod_merc_short, "
    "swap_long, swap_short, swap_spread, other_rept_long, other_rept_short, "
    "tot_rept_long, tot_rept_short, nonrept_long, nonrept_short, "
    "traders_tot, traders_mm_long, traders_mm_short"
)


def _validate_pit(pit_mode: str) -> None:
    if pit_mode not in ("published_at", "conservative", "report_date"):
        raise HTTPException(status_code=400, detail=f"invalid pit_mode: {pit_mode}")


def _params(as_of: str | None, pit_mode: str) -> tuple[str, dict[str, Any], str]:
    try:
        moment = resolve_as_of(as_of)
        clause, extra = pit_clause(pit_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    params: dict[str, Any] = {"as_of": iso(moment), **extra}
    return clause, params, iso(moment)


def _indicator_key(code: str, window_key: str) -> str:
    if code in TEXT_INDICATORS:
        return code
    return f"{code}_{window_key}" if window_key else code


def _fetch_series(
    con: sqlite3.Connection,
    report_type: str,
    market_code: str,
    clause: str,
    params: dict[str, Any],
    start: str | None,
    end: str | None,
    calc_version: str | None,
    codes: list[str] | None,
) -> list[dict[str, Any]]:
    where = [f"w.report_type = :report_type", "w.market_code = :market_code", clause]
    bound: dict[str, Any] = {**params, "report_type": report_type, "market_code": market_code}
    if start:
        where.append("w.report_date >= :start")
        bound["start"] = start
    if end:
        where.append("w.report_date <= :end")
        bound["end"] = end

    weekly = [
        dict(r)
        for r in con.execute(
            f"SELECT {WEEKLY_FIELDS} FROM cot_weekly w "
            f"WHERE {' AND '.join(where)} ORDER BY w.report_date",
            bound,
        )
    ]
    if not weekly:
        return []

    ind_where = [
        "report_type = :report_type",
        "market_code = :market_code",
        "report_date BETWEEN :first AND :last",
    ]
    ind_bound: dict[str, Any] = {
        "report_type": report_type,
        "market_code": market_code,
        "first": weekly[0]["report_date"],
        "last": weekly[-1]["report_date"],
    }
    if calc_version:
        ind_where.append("calculation_version = :calc_version")
        ind_bound["calc_version"] = calc_version
    if codes:
        placeholders = ", ".join(f":c{i}" for i in range(len(codes)))
        ind_where.append(f"indicator_code IN ({placeholders})")
        ind_bound.update({f"c{i}": c for i, c in enumerate(codes)})

    indicators: dict[tuple[str, str], dict[str, Any]] = {}
    for r in con.execute(
        f"SELECT report_date, indicator_code, window_key, value_num, value_text, "
        f"is_valid, invalid_reason, sample_size, calculation_version FROM cot_indicators "
        f"WHERE {' AND '.join(ind_where)}",
        ind_bound,
    ):
        key = (r["report_date"], _indicator_key(r["indicator_code"], r["window_key"]))
        # When several versions exist, keep the highest.
        prior = indicators.get(key)
        if prior is None or str(r["calculation_version"]) > str(prior["_version"]):
            indicators[key] = {
                "value": r["value_num"] if r["value_num"] is not None else r["value_text"],
                "is_valid": r["is_valid"],
                "invalid_reason": r["invalid_reason"],
                "sample_size": r["sample_size"],
                "_version": r["calculation_version"],
            }

    series: list[dict[str, Any]] = []
    for row in weekly:
        record: dict[str, Any] = {
            "report_date": row["report_date"],
            "published_at": row["published_at"],
            "published_at_source": row["published_at_source"],
            "market_name": row["market_name"],
            "exchange_code": row["exchange_code"],
            "open_interest": row["open_interest"],
            "mm_long": row["mm_long"],
            "mm_short": row["mm_short"],
        }
        record["net"] = (
            None if row["mm_long"] is None or row["mm_short"] is None
            else row["mm_long"] - row["mm_short"]
        )
        for column in WEEKLY_FIELDS.split(", "):
            if column not in record and column not in ("report_date", "published_at",
                                                       "published_at_source", "market_name",
                                                       "exchange_code"):
                record[column] = row[column]
        for (report_date, key), payload in indicators.items():
            if report_date == row["report_date"]:
                record[key] = payload["value"]
                record[f"{key}__state"] = {
                    "is_valid": payload["is_valid"],
                    "invalid_reason": payload["invalid_reason"],
                    "sample_size": payload["sample_size"],
                }
        series.append(record)
    return series


@router.get("/overview")
def overview(
    report_type: str = Query(REPORT_TYPE_DISAGGREGATED),
    market_code: str = Query(...),
    as_of: str | None = Query(None),
    pit_mode: str = Query("published_at"),
    calc_version: str | None = Query(None),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    """Latest visible snapshot, with the data date and the release date kept apart."""
    _validate_pit(pit_mode)
    clause, params, moment = _params(as_of, pit_mode)

    row = con.execute(
        f"SELECT {WEEKLY_FIELDS} FROM cot_weekly w "
        f"WHERE w.report_type = :report_type AND w.market_code = :market_code AND {clause} "
        f"ORDER BY w.report_date DESC LIMIT 1",
        {**params, "report_type": report_type, "market_code": market_code},
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no report visible for market_code={market_code} as_of={moment} "
                   f"pit_mode={pit_mode}",
        )
    latest = dict(row)

    indicators: dict[str, Any] = {}
    for r in con.execute(
        "SELECT indicator_code, window_key, value_num, value_text, is_valid, "
        "invalid_reason, sample_size, calculation_version FROM cot_indicators "
        "WHERE report_type = ? AND market_code = ? AND report_date = ?",
        (report_type, market_code, latest["report_date"]),
    ):
        key = _indicator_key(r["indicator_code"], r["window_key"])
        prior = indicators.get(key)
        if prior is None or str(r["calculation_version"]) > str(prior["version"]):
            indicators[key] = {
                "value": r["value_num"] if r["value_num"] is not None else r["value_text"],
                "is_valid": r["is_valid"],
                "invalid_reason": r["invalid_reason"],
                "sample_size": r["sample_size"],
                "version": r["calculation_version"],
            }

    previous_net = con.execute(
        "SELECT mm_long, mm_short FROM cot_weekly WHERE report_type = ? AND market_code = ? "
        "AND report_date < ? ORDER BY report_date DESC LIMIT 1",
        (report_type, market_code, latest["report_date"]),
    ).fetchone()

    net = None
    if latest["mm_long"] is not None and latest["mm_short"] is not None:
        net = latest["mm_long"] - latest["mm_short"]
    prior_net = None
    if previous_net and previous_net["mm_long"] is not None and previous_net["mm_short"] is not None:
        prior_net = previous_net["mm_long"] - previous_net["mm_short"]

    return {
        "report_type": report_type,
        "market_code": market_code,
        "market_name": latest["market_name"],
        # The two dates users must not confuse:
        "data_as_of": latest["report_date"],
        "published_at": latest["published_at"],
        "published_at_source": latest["published_at_source"],
        "as_of": moment,
        "pit_mode": pit_mode,
        "latest_version": calc_version or CALCULATION_VERSION,
        "position": {
            "open_interest": latest["open_interest"],
            "mm_long": latest["mm_long"],
            "mm_short": latest["mm_short"],
            "net": net,
            "net_change": None if net is None or prior_net is None else net - prior_net,
            "net_oi_pct": indicators.get("net_oi_pct", {}).get("value"),
            "net_streak": indicators.get("net_streak", {}).get("value"),
        },
        "indicators": indicators,
        "notes": [
            "published_at is derived from the report date (Friday 15:30 ET), not an "
            "official release timestamp; treat it as a lower bound of real visibility.",
            "percentile rank and COT index use trailing windows (last N reports).",
        ],
    }


@router.get("/series")
def series(
    report_type: str = Query(REPORT_TYPE_DISAGGREGATED),
    market_code: str = Query(...),
    start: str | None = Query(None),
    end: str | None = Query(None),
    as_of: str | None = Query(None),
    pit_mode: str = Query("published_at"),
    calc_version: str | None = Query(None),
    codes: str | None = Query(None, description="comma separated indicator codes"),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    _validate_pit(pit_mode)
    clause, params, moment = _params(as_of, pit_mode)
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None
    data = _fetch_series(
        con, report_type, market_code, clause, params, start, end, calc_version, code_list
    )
    return {
        "report_type": report_type,
        "market_code": market_code,
        "as_of": moment,
        "pit_mode": pit_mode,
        "count": len(data),
        "series": data,
    }


@router.get("/structure")
def structure(
    report_type: str = Query(REPORT_TYPE_DISAGGREGATED),
    market_code: str = Query(...),
    start: str | None = Query(None),
    end: str | None = Query(None),
    as_of: str | None = Query(None),
    pit_mode: str = Query("published_at"),
    con: sqlite3.Connection = Depends(get_con),
) -> dict[str, Any]:
    """Long / short / net for each trader category, on one shared timeline."""
    _validate_pit(pit_mode)
    clause, params, moment = _params(as_of, pit_mode)
    where = ["report_type = :report_type", "market_code = :market_code", clause]
    bound: dict[str, Any] = {**params, "report_type": report_type, "market_code": market_code}
    if start:
        where.append("report_date >= :start")
        bound["start"] = start
    if end:
        where.append("report_date <= :end")
        bound["end"] = end

    rows = list(
        con.execute(
            "SELECT report_date, published_at, open_interest, "
            "prod_merc_long, prod_merc_short, swap_long, swap_short, swap_spread, "
            "mm_long, mm_short, mm_spread, other_rept_long, other_rept_short, "
            "tot_rept_long, tot_rept_short, nonrept_long, nonrept_short "
            "FROM cot_weekly "
            f"WHERE {' AND '.join(where)} ORDER BY report_date",
            bound,
        )
    )

    def net_of(long_key: str, short_key: str, row: sqlite3.Row) -> int | None:
        if row[long_key] is None or row[short_key] is None:
            return None
        return row[long_key] - row[short_key]

    categories = {
        "managed_money": ("mm_long", "mm_short"),
        "producer": ("prod_merc_long", "prod_merc_short"),
        "swap": ("swap_long", "swap_short"),
        "other_reportables": ("other_rept_long", "other_rept_short"),
        "nonreportable": ("nonrept_long", "nonrept_short"),
    }
    data = []
    for row in rows:
        entry: dict[str, Any] = {
            "report_date": row["report_date"],
            "published_at": row["published_at"],
            "open_interest": row["open_interest"],
        }
        for name, (long_key, short_key) in categories.items():
            entry[f"{name}_long"] = row[long_key]
            entry[f"{name}_short"] = row[short_key]
            entry[f"{name}_net"] = net_of(long_key, short_key, row)
        data.append(entry)

    return {
        "report_type": report_type,
        "market_code": market_code,
        "as_of": moment,
        "pit_mode": pit_mode,
        "count": len(data),
        "structure": data,
    }