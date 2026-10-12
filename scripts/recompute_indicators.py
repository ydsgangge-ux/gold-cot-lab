"""Recompute indicators for stored COT history.

Usage:
    python scripts/recompute_indicators.py --version 1.0.0
    python scripts/recompute_indicators.py --market-code 088691 --report-type disaggregated_futures_only

Recomputing a version replaces that version's rows and leaves other versions
untouched, so an old result set stays available for comparison.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from backend.analysis.indicators import (  # noqa: E402
    CALCULATION_VERSION,
    IndicatorParams,
    compute_all,
)
from backend.config import DB_PATH, REPORT_TYPE_DISAGGREGATED  # noqa: E402
from backend.db import iso, open_con, utc_now  # noqa: E402

WEEKLY_READ_COLUMNS = (
    "report_type, market_code, report_date, published_at, open_interest, "
    "mm_long, mm_short"
)

INSERT_SQL = """
INSERT INTO cot_indicators
    (report_type, market_code, report_date, published_at, indicator_code, window_key,
     value_num, value_text, is_valid, invalid_reason, sample_size, params_json,
     params_hash, calculation_version, computed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def load_price(con, symbol: str):
    rows = list(
        con.execute(
            "SELECT trade_date, close FROM price_daily WHERE symbol = ? ORDER BY trade_date",
            (symbol,),
        )
    )
    if not rows:
        return None
    return pd.DataFrame([dict(r) for r in rows])


def recompute(
    con,
    report_type: str,
    market_codes: list[str] | None,
    params: IndicatorParams,
    version: str,
) -> dict:
    if market_codes:
        placeholders = ", ".join("?" for _ in market_codes)
        weekly_rows = list(
            con.execute(
                f"SELECT {WEEKLY_READ_COLUMNS} FROM cot_weekly "
                f"WHERE report_type = ? AND market_code IN ({placeholders}) "
                f"ORDER BY market_code, report_date",
                (report_type, *market_codes),
            )
        )
    else:
        weekly_rows = list(
            con.execute(
                f"SELECT {WEEKLY_READ_COLUMNS} FROM cot_weekly "
                f"WHERE report_type = ? ORDER BY market_code, report_date",
                (report_type,),
            )
        )
    if not weekly_rows:
        return {"report_type": report_type, "markets": [], "rows_written": 0}

    weekly = pd.DataFrame([dict(r) for r in weekly_rows])
    price = load_price(con, params.price_symbol)
    now = iso(utc_now())

    written = 0
    summary = []
    for market_code, group in weekly.groupby("market_code", sort=True):
        indicators = compute_all(group.reset_index(drop=True), price, params, version)
        con.execute(
            "DELETE FROM cot_indicators WHERE report_type = ? AND market_code = ? "
            "AND calculation_version = ?",
            (report_type, market_code, version),
        )
        payload = [
            (
                report_type,
                market_code,
                str(row.report_date),
                str(row.published_at),
                row.indicator_code,
                row.window_key,
                None if pd.isna(row.value_num) else float(row.value_num),
                None if row.value_text is None or pd.isna(row.value_text) else str(row.value_text),
                int(row.is_valid),
                row.invalid_reason,
                None if row.sample_size is None or pd.isna(row.sample_size) else int(row.sample_size),
                row.params_json,
                row.params_hash,
                row.calculation_version,
                now,
            )
            for row in indicators.itertuples()
        ]
        con.executemany(INSERT_SQL, payload)
        written += len(payload)
        valid = int((indicators["is_valid"] == 1).sum())
        summary.append(
            {
                "market_code": market_code,
                "reports": int(group.shape[0]),
                "indicator_rows": len(payload),
                "valid": valid,
                "invalid": len(payload) - valid,
                "invalid_reasons": indicators.loc[
                    indicators["is_valid"] == 0, "invalid_reason"
                ].value_counts().to_dict(),
            }
        )
    con.commit()
    return {"report_type": report_type, "rows_written": written, "markets": summary}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--report-type", default=REPORT_TYPE_DISAGGREGATED)
    parser.add_argument("--market-code", action="append", default=None)
    parser.add_argument("--version", default=CALCULATION_VERSION)
    parser.add_argument("--alert-high", type=float, default=None)
    parser.add_argument("--alert-low", type=float, default=None)
    args = parser.parse_args()

    params = IndicatorParams()
    if args.alert_high is not None or args.alert_low is not None:
        params = IndicatorParams(
            alert_high_pct=args.alert_high if args.alert_high is not None else params.alert_high_pct,
            alert_low_pct=args.alert_low if args.alert_low is not None else params.alert_low_pct,
        )

    con = open_con(args.db)
    try:
        result = recompute(con, args.report_type, args.market_code, params, args.version)
    finally:
        con.close()

    print(f"report_type   : {result['report_type']}")
    print(f"version       : {args.version}")
    print(f"params        : {params.to_json()}")
    print(f"params_hash   : {params.params_hash}")
    print(f"rows written  : {result['rows_written']}")
    for m in result["markets"]:
        print(
            f"  market {m['market_code']}: reports={m['reports']} "
            f"rows={m['indicator_rows']} valid={m['valid']} invalid={m['invalid']}"
        )
        if m["invalid_reasons"]:
            print(f"      invalid reasons: {m['invalid_reasons']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())