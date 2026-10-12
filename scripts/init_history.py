"""Initialise the history database from official CFTC export files.

Usage:
    python scripts/init_history.py --csv "<path>"            # import (idempotent)
    python scripts/init_history.py --csv "<path>" --dry-run  # validate only, no writes
    python scripts/init_history.py                            # import every data/raw/cftc/*.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import DB_PATH, RAW_CFTC_DIR, REPORT_TYPE_DISAGGREGATED  # noqa: E402
from backend.data.cftc_import import run_import  # noqa: E402
from backend.db import init_db, open_con  # noqa: E402


def print_report(summary: dict, verbose: bool = True) -> None:
    print("=" * 78)
    print(f"batch          : {summary['batch_id']}")
    print(f"source         : {summary['source_file']}")
    print(f"sha256         : {summary['source_file_sha256'][:16]}...")
    print(f"report_type    : {summary['report_type']}")
    print(f"rows read      : {summary['rows_read']}")
    print(f"date range     : {summary['date_range'][0]} -> {summary['date_range'][1]}")
    print("rows by market :")
    for market, count in summary["rows_by_market"].items():
        print(f"    {market:>8}  {count}")
    print(f"status         : {summary['status']}")
    print(
        "counters       : "
        f"inserted={summary['rows_inserted']} updated={summary['rows_updated']} "
        f"unchanged={summary['rows_unchanged']} revised={summary['rows_revised']}"
    )
    print("-" * 78)
    print("validation:")
    for r in summary["validations"]:
        flag = "PASS" if r["passed"] else f"FAIL({r['count']})"
        print(
            f"  [{r['level']:>5}] {r['rule']:<32} {flag:<12} "
            f"checked={r['checked']}"
        )
        if not r["passed"]:
            for s in r["samples"][:5]:
                print(f"           - {s}")
        elif verbose and r.get("note"):
            print(f"           note: {r['note'][:140]}")
    if summary.get("issues"):
        print("-" * 78)
        print("issues:")
        for issue in summary["issues"][:10]:
            print(f"  - {issue}")
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", action="append", default=None,
                        help="official export file(s); repeatable")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and report without writing anything")
    parser.add_argument("--accept-revisions", action="store_true",
                        help="overwrite stored rows when source values differ")
    parser.add_argument("--report-type", default=REPORT_TYPE_DISAGGREGATED)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    if args.csv:
        paths = [Path(p) for p in args.csv]
    else:
        paths = sorted(RAW_CFTC_DIR.glob("*.csv"))
    if not paths:
        print(f"no CSV found. pass --csv or drop files into {RAW_CFTC_DIR}")
        return 2

    if not args.dry_run:
        init_db(args.db)

    con = open_con(args.db)
    exit_code = 0
    try:
        for path in paths:
            if not path.exists():
                print(f"missing file: {path}")
                exit_code = 2
                continue
            summary = run_import(
                con,
                path,
                report_type=args.report_type,
                dry_run=args.dry_run,
                accept_revisions=args.accept_revisions,
            )
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, default=str))
            else:
                print_report(summary)
            if summary["status"] == "partial":
                exit_code = max(exit_code, 1)
    finally:
        con.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())