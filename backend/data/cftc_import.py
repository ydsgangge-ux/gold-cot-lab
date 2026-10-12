"""CFTC import pipeline: validation, idempotent upsert, revision detection.

Idempotency contract: importing the same file twice must leave ``cot_weekly``
row count, every ``cot_weekly`` payload column (``last_seen_at`` excluded) and
every computed indicator unchanged, with ``rows_inserted == 0`` on the second run.

Revision policy defaults to ``log_and_hold``: when a source value differs from
what is already stored, the old value is *not* overwritten and the batch is
marked ``partial``, so an unexpected upstream change can never silently corrupt
the database.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import sqlite3

from ..config import REPORT_TYPE_DISAGGREGATED, is_federal_holiday  # noqa: F401
from ..db import iso, utc_now
from .normalize import (
    FIELD_MAP,
    MARKET_CODE_COL,
    MISSING_TOKENS,
    NUMERIC_FIELDS,
    REPORT_DATE_COL,
    TEXT_FIELDS,
    ImportValidationError,
    build_raw_json,
    canonical_payload,
    derive_published_at,
    is_missing_token,
    map_to_canonical,
    parse_cftc_date,
    parse_cftc_int,
    payload_hash,
    read_seed_csv,
    sha256_file,
)

REVISION_POLICIES = ("log_and_hold", "accept")

RAW_COLUMNS = (
    "report_type", "market_code", "report_date", "published_at",
    "published_at_source", "source_kind", "source_file", "source_file_sha256",
    "source_row_number", "sources_json", "raw_json", "raw_hash", "cftc_id",
    "ingest_batch_id", "first_seen_at", "last_seen_at",
)

WEEKLY_COLUMNS = (
    ("report_type",), ("market_code",), ("market_name",), ("contract_name",),
    ("exchange_code",), ("region_code",), ("commodity_code",), ("contract_units",),
    ("report_date",), ("published_at",), ("published_at_source",),
    ("report_week_label",),
) + tuple((f,) for f in NUMERIC_FIELDS) + (
    ("null_fields_json",), ("row_hash",), ("source_raw_id",),
    ("ingest_batch_id",), ("created_at",), ("updated_at",),
)
WEEKLY_COLUMNS = tuple(c for group in WEEKLY_COLUMNS for c in group)

# --------------------------------------------------------------------------
# Columns referenced by the recomputation checks
# --------------------------------------------------------------------------

# Change_in_<name>_All  ->  <name>_All
CHANGE_PAIRS: dict[str, str] = {
    "Change_in_Open_Interest_All": "Open_Interest_All",
    "Change_in_Prod_Merc_Long_All": "Prod_Merc_Positions_Long_All",
    "Change_in_Prod_Merc_Short_All": "Prod_Merc_Positions_Short_All",
    "Change_in_Swap_Long_All": "Swap_Positions_Long_All",
    "Change_in_Swap_Short_All": "Swap__Positions_Short_All",
    "Change_in_Swap_Spread_All": "Swap__Positions_Spread_All",
    "Change_in_M_Money_Long_All": "M_Money_Positions_Long_All",
    "Change_in_M_Money_Short_All": "M_Money_Positions_Short_All",
    "Change_in_M_Money_Spread_All": "M_Money_Positions_Spread_All",
    "Change_in_Other_Rept_Long_All": "Other_Rept_Positions_Long_All",
    "Change_in_Other_Rept_Short_All": "Other_Rept_Positions_Short_All",
    "Change_in_Other_Rept_Spread_All": "Other_Rept_Positions_Spread_All",
    "Change_in_Tot_Rept_Long_All": "Tot_Rept_Positions_Long_All",
    "Change_in_Tot_Rept_Short_All": "Tot_Rept_Positions_Short_All",
    "Change_in_NonRept_Long_All": "NonRept_Positions_Long_All",
    "Change_in_NonRept_Short_All": "NonRept_Positions_Short_All",
}

# Pct_of_OI_<name>_All  ->  <name>_All
PCT_PAIRS: dict[str, str] = {
    "Pct_of_OI_Prod_Merc_Long_All": "Prod_Merc_Positions_Long_All",
    "Pct_of_OI_Prod_Merc_Short_All": "Prod_Merc_Positions_Short_All",
    "Pct_of_OI_Swap_Long_All": "Swap_Positions_Long_All",
    "Pct_of_OI_Swap_Short_All": "Swap__Positions_Short_All",
    "Pct_of_OI_Swap_Spread_All": "Swap__Positions_Spread_All",
    "Pct_of_OI_M_Money_Long_All": "M_Money_Positions_Long_All",
    "Pct_of_OI_M_Money_Short_All": "M_Money_Positions_Short_All",
    "Pct_of_OI_M_Money_Spread_All": "M_Money_Positions_Spread_All",
    "Pct_of_OI_Other_Rept_Long_All": "Other_Rept_Positions_Long_All",
    "Pct_of_OI_Other_Rept_Short_All": "Other_Rept_Positions_Short_All",
    "Pct_of_OI_Other_Rept_Spread_All": "Other_Rept_Positions_Spread_All",
    "Pct_of_OI_Tot_Rept_Long_All": "Tot_Rept_Positions_Long_All",
    "Pct_of_OI_Tot_Rept_Short_All": "Tot_Rept_Positions_Short_All",
    "Pct_of_OI_NonRept_Long_All": "NonRept_Positions_Long_All",
    "Pct_of_OI_NonRept_Short_All": "NonRept_Positions_Short_All",
}

PCT_TOLERANCE = 0.1
# The source stores percentages with one decimal, and it truncates rather than
# rounds (e.g. 20.1501 is published as 20.1). Comparing against an independently
# recomputed value therefore has to allow one full display unit; a genuine
# error in this column would be far larger than 0.1.

# Fields that exist in the normalised schema but have no counterpart in the
# Disaggregated report; they stay NULL until the Legacy report is imported.
LEGACY_RESERVED_FIELDS = (
    "noncomm_long", "noncomm_short", "noncomm_spread", "comm_long", "comm_short",
)

# Market code -> the contract name the code must correspond to.
EXPECTED_MARKET_NAMES = {
    "088691": "GOLD - COMMODITY EXCHANGE INC.",
    "088695": "MICRO GOLD - COMMODITY EXCHANGE INC.",
    "088LM1": "GOLD -1 TROY OUNCE  - COINBASE DERIVATIVES, LLC",
}


def _rule(
    name: str,
    level: str,
    checked: int,
    violations: list[str],
    note: str = "",
) -> dict[str, Any]:
    return {
        "rule": name,
        "level": level,
        "passed": not violations,
        "checked": checked,
        "count": len(violations),
        "samples": violations[:10],
        "note": note,
    }


def _to_int(value: Any) -> int | None:
    if is_missing_token(value):
        return None
    return int(str(value).strip().replace(",", ""))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(raw: pd.DataFrame, canon: pd.DataFrame) -> list[dict[str, Any]]:
    """Run every structural and informational check and return the report.

    Rules R1 (date format) and R3 (numeric parse) are enforced earlier by
    raising; the remaining checks here are recorded rather than fatal, so a
    single odd week cannot block the whole history from loading.
    """
    results: list[dict[str, Any]] = []

    # R1 - date format parsed successfully (reaching here means it did).
    results.append(_rule("R1_date_format", "error", len(raw), [], COT_FMT_NOTE))

    # R2 - one report per (market, report_date) inside this batch.
    dup_mask = canon.duplicated(subset=["market_code", "report_date"], keep=False)
    dups = [
        f"market={canon.iloc[i]['market_code']} date={canon.iloc[i]['report_date']}"
        for i in dup_mask.to_numpy().nonzero()[0]
    ]
    results.append(_rule("R2_report_date_unique", "error", len(canon), dups))

    # R3 - all mapped numeric fields parse as integers (enforced earlier).
    results.append(_rule("R3_numeric_parse", "error", len(raw), [], "enforced during mapping"))

    # R4 - missing vs zero are distinguished.
    r4_fields = [f for f in NUMERIC_FIELDS if f not in LEGACY_RESERVED_FIELDS]
    missing_counts = {f: 0 for f in r4_fields}
    zero_counts = {f: 0 for f in r4_fields}
    for _, row in canon.iterrows():
        for field in r4_fields:
            value = row[field]
            if is_missing_token(value):
                missing_counts[field] += 1
            elif int(value) == 0:
                zero_counts[field] += 1
    non_key_missing = {k: v for k, v in missing_counts.items() if v}
    results.append(
        _rule(
            "R4_missing_vs_zero",
            "warn",
            len(canon) * len(r4_fields),
            [],
            note=json.dumps(
                {
                    "fields_with_missing": non_key_missing,
                    "total_missing": sum(missing_counts.values()),
                    "total_zero": sum(zero_counts.values()),
                    "excluded_legacy_reserved": list(LEGACY_RESERVED_FIELDS),
                }
            ),
        )
    )

    # R5 - a market code always maps to exactly one market name, and vice versa.
    code_to_names: dict[str, set[str]] = {}
    name_to_codes: dict[str, set[str]] = {}
    for _, row in canon.iterrows():
        code_to_names.setdefault(str(row["market_code"]), set()).add(str(row["market_name"]))
        name_to_codes.setdefault(str(row["market_name"]), set()).add(str(row["market_code"]))
    conflicts = [
        f"{code} -> {sorted(names)}"
        for code, names in code_to_names.items()
        if len(names) > 1
    ] + [
        f"{name} -> {sorted(codes)}"
        for name, codes in name_to_codes.items()
        if len(codes) > 1
    ]
    results.append(_rule("R5_market_code_name_consistency", "warn", len(canon), conflicts))

    # R6 - gold commodity code rows must actually be gold contracts.
    violations = []
    for _, row in canon.iterrows():
        if str(row["commodity_code"]).strip() == "088" and "GOLD" not in str(row["market_name"]).upper():
            violations.append(f"{row['market_code']} {row['market_name']}")
    results.append(_rule("R6_gold_commodity_matches_name", "warn", len(canon), violations))

    # R7 - the All group equals the Old group.
    old_pairs = [(f"{k}_All", f"{k}_Old") for k in _ALL_GROUP_STEMS]
    diffs = []
    checked = 0
    for all_col, old_col in old_pairs:
        if all_col not in raw.columns or old_col not in raw.columns:
            continue
        checked += len(raw)
        a = parse_cftc_int(raw[all_col])
        b = parse_cftc_int(raw[old_col])
        mismatch = (a.fillna(-1) != b.fillna(-1)).to_numpy().nonzero()[0]
        for i in mismatch[:5]:
            diffs.append(f"row {i + 2} {all_col} != {old_col}")
    results.append(
        _rule("R7_all_equals_old", "warn", checked, diffs, "no crop-year split for gold")
    )

    # R8 - every _Other numeric column is zero, except Pct_of_Open_Interest_Other
    # (a CFTC artefact that is always 100; excluding it avoids 1061 phantom rows).
    other_cols = [
        c for c in raw.columns
        if c.endswith("_Other") and c != "Pct_of_Open_Interest_Other"
    ]
    violations = []
    checked = 0
    for col in other_cols:
        checked += len(raw)
        parsed = parse_cftc_int(raw[col])
        nonzero = (parsed.fillna(0) != 0).to_numpy().nonzero()[0]
        for i in nonzero[:5]:
            violations.append(f"row {i + 2} {col}={raw[col].iloc[i]!r}")
    results.append(
        _rule("R8_other_group_all_zero", "warn", checked, violations,
              "excluded Pct_of_Open_Interest_Other")
    )

    # R9 - open interest equals reportable + non-reportable, on both sides.
    oi = parse_cftc_int(raw["Open_Interest_All"])
    trl = parse_cftc_int(raw["Tot_Rept_Positions_Long_All"])
    trs = parse_cftc_int(raw["Tot_Rept_Positions_Short_All"])
    nrl = parse_cftc_int(raw["NonRept_Positions_Long_All"])
    nrs = parse_cftc_int(raw["NonRept_Positions_Short_All"])
    bad = ((oi - (trl + nrl)).abs() > 0) | ((oi - (trs + nrs)).abs() > 0)
    results.append(
        _rule("R9_oi_identity", "warn", len(raw),
              [f"row {i + 2}" for i in bad.to_numpy().nonzero()[0][:10]])
    )

    # R10 - total reportable decomposes into the category columns (no double counting).
    expected_long = (
        parse_cftc_int(raw["Prod_Merc_Positions_Long_All"])
        + parse_cftc_int(raw["Swap_Positions_Long_All"])
        + parse_cftc_int(raw["Swap__Positions_Spread_All"])
        + parse_cftc_int(raw["M_Money_Positions_Long_All"])
        + parse_cftc_int(raw["M_Money_Positions_Spread_All"])
        + parse_cftc_int(raw["Other_Rept_Positions_Long_All"])
        + parse_cftc_int(raw["Other_Rept_Positions_Spread_All"])
    )
    bad = (trl - expected_long).abs() > 0
    results.append(
        _rule("R10_tot_rept_decomposition", "warn", len(raw),
              [f"row {i + 2}" for i in bad.to_numpy().nonzero()[0][:10]])
    )

    # R11 / R12 - Change_in_* and Pct_of_OI_* are exactly recomputable from the
    # ordered series. They live only in raw_json, so this check keeps them honest.
    results.extend(_check_recomputable(raw, canon))

    # R13 - ID equals YYMMDD + market code + F.
    violations = []
    for i, (_, row) in enumerate(canon.iterrows()):
        rid = str(row.get("cftc_id") or "")
        expected = row["report_date"].strftime("%y%m%d") + str(row["market_code"]) + "F"
        if rid != expected:
            violations.append(f"row {i + 2} id={rid!r} expected={expected!r}")
    results.append(_rule("R13_id_consistency", "warn", len(canon), violations))

    # R14 - published_at must be strictly after report_date.
    violations = []
    for i, (_, row) in enumerate(canon.iterrows()):
        if pd.Timestamp(row["published_at"]).date() <= row["report_date"]:
            violations.append(f"row {i + 2} {row['published_at']} <= {row['report_date']}")
    results.append(_rule("R14_published_after_report", "warn", len(canon), violations))

    # R15 - no duplicate business keys inside the batch.
    violations = [
        f"market={canon.iloc[i]['market_code']} date={canon.iloc[i]['report_date']}"
        for i in canon.duplicated(subset=["report_type", "market_code", "report_date"])
        .to_numpy()
        .nonzero()[0]
    ]
    results.append(_rule("R15_no_duplicate_keys", "error", len(canon), violations))

    return results


COT_FMT_NOTE = "%Y %b %d %I:%M:%S %p"


_ALL_GROUP_STEMS = (
    "Open_Interest",
    "Prod_Merc_Positions_Long",
    "Prod_Merc_Positions_Short",
    "Swap_Positions_Long",
    "Swap__Positions_Short",
    "Swap__Positions_Spread",
    "M_Money_Positions_Long",
    "M_Money_Positions_Short",
    "M_Money_Positions_Spread",
    "Other_Rept_Positions_Long",
    "Other_Rept_Positions_Short",
    "Other_Rept_Positions_Spread",
    "Tot_Rept_Positions_Long",
    "Tot_Rept_Positions_Short",
    "NonRept_Positions_Long",
    "NonRept_Positions_Short",
)


def _int_list(series: pd.Series) -> list[int | None]:
    """Nullable Int64 -> plain Python list with None for missing values."""
    return [None if pd.isna(v) else int(v) for v in series]


def _check_recomputable(
    raw: pd.DataFrame, canon: pd.DataFrame
) -> list[dict[str, Any]]:
    """Verify Change_in_* and Pct_of_OI_* against values recomputed in date order."""
    work = canon[["market_code", "report_date"]].copy()
    work["_src"] = range(len(work))

    change_checked = 0
    change_violations: list[str] = []
    for market, group in work.groupby("market_code", sort=True):
        order = group.sort_values("report_date")["_src"].tolist()
        for change_col, value_col in CHANGE_PAIRS.items():
            reported = _int_list(parse_cftc_int(raw[change_col]))
            values = _int_list(parse_cftc_int(raw[value_col]))
            for pos in range(1, len(order)):
                cur, prev = order[pos], order[pos - 1]
                change_checked += 1
                if reported[cur] is None or values[cur] is None or values[prev] is None:
                    continue
                if reported[cur] != values[cur] - values[prev]:
                    if len(change_violations) < 10:
                        change_violations.append(
                            f"market={market} {change_col} at {canon.iloc[cur]['report_date']}"
                        )

    pct_checked = 0
    pct_violations: list[str] = []
    oi = parse_cftc_int(raw["Open_Interest_All"]).astype("Float64")
    for pct_col, value_col in PCT_PAIRS.items():
        values = parse_cftc_int(raw[value_col]).astype("Float64")
        reported = pd.to_numeric(
            raw[pct_col].astype("string").str.strip().str.replace(",", "", regex=False),
            errors="coerce",
        )
        recomputed = values / oi * 100.0
        pct_checked += int(recomputed.notna().sum())
        delta = (recomputed - reported).abs()
        bad = (delta > PCT_TOLERANCE).fillna(False).to_numpy().nonzero()[0]
        for i in bad[:3]:
            if len(pct_violations) < 10:
                pct_violations.append(
                    f"{pct_col} at {canon.iloc[i]['report_date']}: "
                    f"reported={reported.iloc[i]} recomputed={recomputed.iloc[i]:.4f}"
                )

    return [
        _rule("R11_change_recompute", "warn", change_checked, change_violations,
              "requires date-ordered series; string sorting would report false mismatches"),
        _rule("R12_pct_recompute", "warn", pct_checked, pct_violations,
              f"tolerance {PCT_TOLERANCE} for 1-decimal source rounding"),
    ]


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------


def build_rows(
    raw: pd.DataFrame,
    canon: pd.DataFrame,
    report_type: str,
    source_file: str,
    source_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn the parsed frame into cot_raw / cot_weekly row dicts."""
    raw_rows: list[dict[str, Any]] = []
    weekly_rows: list[dict[str, Any]] = []
    now = iso(utc_now())

    for pos in range(len(raw)):
        raw_row = raw.iloc[pos]
        can_row = canon.iloc[pos]
        report_date = can_row["report_date"]
        published_at = derive_published_at(report_date)
        raw_json = build_raw_json(raw_row)
        market_code = str(can_row["market_code"])
        source_row_number = pos + 1

        raw_rows.append(
            {
                "report_type": report_type,
                "market_code": market_code,
                "report_date": report_date.isoformat(),
                "published_at": iso(published_at),
                "published_at_source": "derived",
                "source_kind": "seed_csv",
                "source_file": source_file,
                "source_file_sha256": source_sha256,
                "source_row_number": source_row_number,
                "sources_json": json.dumps(
                    [{"file": source_file, "sha256": source_sha256, "row": source_row_number}],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "raw_json": raw_json,
                "raw_hash": payload_hash({"raw": raw_json}),
                "cftc_id": str(can_row.get("cftc_id") or ""),
                "ingest_batch_id": "",
                "first_seen_at": now,
                "last_seen_at": now,
            }
        )

        weekly: dict[str, Any] = {
            "report_type": report_type,
            "market_code": market_code,
            "report_date": report_date.isoformat(),
            "published_at": iso(published_at),
            "published_at_source": "derived",
            "null_fields_json": json.dumps(
                sorted(f for f in NUMERIC_FIELDS if is_missing_token(can_row.get(f))),
                separators=(",", ":"),
            ),
            "ingest_batch_id": "",
            "created_at": now,
            "updated_at": now,
        }
        for field in TEXT_FIELDS:
            value = can_row.get(field)
            weekly[field] = None if is_missing_token(value) else str(value).strip()
        for field in NUMERIC_FIELDS:
            value = can_row.get(field)
            weekly[field] = None if is_missing_token(value) else int(value)
        weekly["row_hash"] = payload_hash(canonical_payload(can_row))
        weekly_rows.append(weekly)

    return raw_rows, weekly_rows


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _upsert_raw(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert or refresh the raw layer; returns counters keyed by business key id map."""
    inserted = updated = unchanged = 0
    existing = {
        (r["report_type"], r["market_code"], r["report_date"]): dict(r)
        for r in con.execute(
            "SELECT id, report_type, market_code, report_date, raw_hash, sources_json "
            "FROM cot_raw"
        )
    }
    for row in rows:
        key = (row["report_type"], row["market_code"], row["report_date"])
        payload = dict(row)
        if key not in existing:
            cols = ", ".join(RAW_COLUMNS)
            placeholders = ", ".join(f":{c}" for c in RAW_COLUMNS)
            con.execute(
                f"INSERT INTO cot_raw ({cols}) VALUES ({placeholders})", payload
            )
            inserted += 1
            continue
        prior = existing[key]
        sources = json.loads(prior["sources_json"] or "[]")
        incoming = json.loads(payload["sources_json"])
        merged = {json.dumps(s, sort_keys=True): s for s in sources}
        for s in incoming:
            merged[json.dumps(s, sort_keys=True)] = s
        merged_json = json.dumps(
            sorted(merged.values(), key=lambda s: json.dumps(s, sort_keys=True)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if prior["raw_hash"] != payload["raw_hash"]:
            con.execute(
                "UPDATE cot_raw SET published_at=:published_at, raw_json=:raw_json, "
                "raw_hash=:raw_hash, sources_json=:sources_json, last_seen_at=:last_seen_at, "
                "ingest_batch_id=:ingest_batch_id WHERE id=:id",
                {**payload, "sources_json": merged_json, "id": prior["id"]},
            )
            updated += 1
        elif prior["sources_json"] != merged_json:
            con.execute(
                "UPDATE cot_raw SET sources_json=:sources_json, last_seen_at=:last_seen_at "
                "WHERE id=:id",
                {"sources_json": merged_json, "last_seen_at": payload["last_seen_at"],
                 "id": prior["id"]},
            )
            unchanged += 1
        else:
            con.execute(
                "UPDATE cot_raw SET last_seen_at=:last_seen_at WHERE id=:id",
                {"last_seen_at": payload["last_seen_at"], "id": prior["id"]},
            )
            unchanged += 1
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


def _raw_id_map(con: sqlite3.Connection) -> dict[tuple[str, str, str], int]:
    return {
        (r["report_type"], r["market_code"], r["report_date"]): r["id"]
        for r in con.execute("SELECT id, report_type, market_code, report_date FROM cot_raw")
    }


def _upsert_weekly(
    con: sqlite3.Connection,
    rows: list[dict[str, Any]],
    id_map: dict[tuple[str, str, str], int],
    batch_id: str,
    accept_revisions: bool,
) -> tuple[dict[str, int], list[str]]:
    counters = {"inserted": 0, "updated": 0, "unchanged": 0, "revised": 0}
    issues: list[str] = []
    existing = {
        (r["report_type"], r["market_code"], r["report_date"]): dict(r)
        for r in con.execute(
            "SELECT id, report_type, market_code, report_date, row_hash FROM cot_weekly"
        )
    }
    cols = ", ".join(WEEKLY_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in WEEKLY_COLUMNS)
    update_cols = [c for c in WEEKLY_COLUMNS if c not in (
        "report_type", "market_code", "report_date", "created_at")]

    for row in rows:
        key = (row["report_type"], row["market_code"], row["report_date"])
        payload = dict(row)
        payload["ingest_batch_id"] = batch_id
        payload["source_raw_id"] = id_map[key]
        if key not in existing:
            con.execute(
                f"INSERT INTO cot_weekly ({cols}) VALUES ({placeholders})", payload
            )
            counters["inserted"] += 1
            continue
        prior = existing[key]
        if prior["row_hash"] == payload["row_hash"]:
            counters["unchanged"] += 1
            continue
        if not accept_revisions:
            counters["revised"] += 1
            issues.append(
                f"revision held for {key[1]} {key[2]}: stored row_hash={prior['row_hash'][:12]} "
                f"incoming={payload['row_hash'][:12]}"
            )
            continue
        assignments = ", ".join(f"{c}=:{c}" for c in update_cols)
        con.execute(
            f"UPDATE cot_weekly SET {assignments} WHERE id=:row_id",
            {**payload, "row_id": prior["id"]},
        )
        counters["updated"] += 1
    return counters, issues


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run_import(
    con: sqlite3.Connection,
    csv_path: Path | str,
    report_type: str = REPORT_TYPE_DISAGGREGATED,
    dry_run: bool = False,
    accept_revisions: bool = False,
    source_label: str | None = None,
) -> dict[str, Any]:
    """Import one official export file. Idempotent when the content is unchanged."""
    started = time.time()
    csv_path = Path(csv_path)
    batch_id = uuid.uuid4().hex[:12]
    source_label = source_label or csv_path.name

    raw = read_seed_csv(csv_path)
    source_sha256 = sha256_file(csv_path)

    canon = map_to_canonical(raw)
    canon["report_type"] = report_type
    published = [derive_published_at(d) for d in canon["report_date"]]
    canon["published_at"] = published

    validations = validate(raw, canon)

    fatal = [v for v in validations if v["level"] == "error" and not v["passed"]]
    if fatal:
        raise ImportValidationError(
            fatal[0]["rule"],
            f"{len(fatal)} blocking validation failure(s)",
            fatal[0]["samples"],
        )

    raw_rows, weekly_rows = build_rows(raw, canon, report_type, source_label, source_sha256)

    summary: dict[str, Any] = {
        "batch_id": batch_id,
        "source_file": source_label,
        "source_file_sha256": source_sha256,
        "report_type": report_type,
        "dry_run": dry_run,
        "rows_read": len(raw),
        "markets": sorted(canon["market_code"].unique().tolist()),
        "rows_by_market": canon["market_code"].value_counts().sort_index().to_dict(),
        "date_range": [
            canon["report_date"].min().isoformat(),
            canon["report_date"].max().isoformat(),
        ],
        "validations": validations,
    }

    if dry_run:
        summary.update(
            {"status": "dry_run", "rows_inserted": 0, "rows_updated": 0,
             "rows_unchanged": 0, "rows_revised": 0, "rows_rejected": 0}
        )
        return summary

    now = iso(utc_now())
    con.execute(
        "INSERT INTO ingestion_log (batch_id, run_type, status, started_at, source_kind, "
        "source_file, source_file_sha256, report_types, markets_json, rows_read, rows_valid) "
        "VALUES (?, ?, 'running', ?, 'seed_csv', ?, ?, ?, ?, ?, ?)",
        (
            batch_id, "seed_import", now, source_label, source_sha256,
            json.dumps([report_type]), json.dumps(summary["markets"]),
            len(raw), len(canon),
        ),
    )
    con.commit()

    try:
        raw_counters = _upsert_raw(con, raw_rows)
        id_map = _raw_id_map(con)
        weekly_counters, issues = _upsert_weekly(
            con, weekly_rows, id_map, batch_id, accept_revisions
        )
        con.commit()
    except Exception as exc:  # pragma: no cover - defensive
        con.rollback()
        _finish_log(con, batch_id, status="failed", error={"type": type(exc).__name__,
                                                           "message": str(exc)})
        raise

    status = "ok"
    if weekly_counters["revised"] and not accept_revisions:
        status = "partial"

    summary.update(
        {
            "status": status,
            "rows_inserted": weekly_counters["inserted"],
            "rows_updated": weekly_counters["updated"],
            "rows_unchanged": weekly_counters["unchanged"],
            "rows_revised": weekly_counters["revised"],
            "rows_rejected": 0,
            "raw_inserted": raw_counters["inserted"],
            "raw_updated": raw_counters["updated"],
        }
    )
    if issues:
        summary["issues"] = issues

    _finish_log(
        con,
        batch_id,
        status=status,
        counters={**weekly_counters, "raw_inserted": raw_counters["inserted"]},
        validations=validations,
        issues=issues,
        markets=summary["markets"],
        report_type=report_type,
        duration_ms=int((time.time() - started) * 1000),
    )
    summary["duration_ms"] = int((time.time() - started) * 1000)
    return summary


def _finish_log(
    con: sqlite3.Connection,
    batch_id: str,
    status: str,
    counters: dict[str, int] | None = None,
    validations: list[dict[str, Any]] | None = None,
    issues: list[str] | None = None,
    markets: list[str] | None = None,
    report_type: str | None = None,
    error: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    counters = counters or {}
    con.execute(
        "UPDATE ingestion_log SET status=?, finished_at=?, duration_ms=?, "
        "rows_inserted=?, rows_updated=?, rows_unchanged=?, rows_revised=?, "
        "validation_json=?, issues_json=?, error_json=?, markets_json=COALESCE(?, markets_json), "
        "report_types=COALESCE(?, report_types), notes=? WHERE batch_id=?",
        (
            status,
            iso(utc_now()),
            duration_ms,
            counters.get("inserted", 0),
            counters.get("updated", 0),
            counters.get("unchanged", 0),
            counters.get("revised", 0),
            json.dumps(validations or [], ensure_ascii=False),
            json.dumps(issues or [], ensure_ascii=False),
            json.dumps(error, ensure_ascii=False) if error else None,
            json.dumps(markets, ensure_ascii=False) if markets else None,
            json.dumps([report_type], ensure_ascii=False) if report_type else None,
            f"raw_inserted={counters.get('raw_inserted', 0)}",
            batch_id,
        ),
    )
    con.commit()


def seed_raw_dir(con: sqlite3.Connection, raw_dir: Path | str, **kwargs: Any) -> list[dict[str, Any]]:
    """Import every CSV sitting in the raw CFTC directory, oldest file first."""
    paths = sorted(Path(raw_dir).glob("*.csv"))
    return [run_import(con, p, **kwargs) for p in paths]