"""Import correctness: raw fidelity, idempotency, revision safety, traceability."""

from __future__ import annotations

import json

import pytest

from backend.analysis.indicators import (
    CALCULATION_VERSION,
    INDICATOR_ROWS_PER_REPORT,
    IndicatorParams,
)
from backend.config import REPORT_TYPE_DISAGGREGATED
from backend.data.cftc_import import CHANGE_PAIRS, run_import
from backend.data.normalize import (
    build_raw_json,
    is_missing_token,
    parse_int_cell,
    read_seed_csv,
)
from backend.db import init_db, open_con

MARKET = "088691"
LATEST_REPORT = "2026-10-06"


def _fresh_db(tmp_path):
    db = tmp_path / "fresh.sqlite"
    init_db(db)
    return db


# --------------------------------------------------------------------------
# Raw fidelity
# --------------------------------------------------------------------------


def test_raw_json_keys_equal_source_headers(seed_csv, con):
    expected = set(read_seed_csv(seed_csv).columns)
    stored = {
        r["raw_json"]
        for r in con.execute("SELECT raw_json FROM cot_raw WHERE market_code = ? LIMIT 5", (MARKET,))
    }
    assert len(stored) == 5
    for blob in stored:
        assert set(json.loads(blob)) == expected
    # The inconsistent official spelling must survive verbatim.
    assert "Swap__Positions_Short_All" in expected
    assert "Swap_Positions_Long_All" in expected


def test_unmapped_columns_are_kept_only_in_the_raw_layer(con):
    weekly_columns = {r[1] for r in con.execute("PRAGMA table_info(cot_weekly)")}
    assert not any(c.startswith("change_in_") for c in weekly_columns)
    assert not any(c.startswith("pct_of_oi") for c in weekly_columns)
    assert "mm_long" in weekly_columns

    blob = json.loads(
        con.execute(
            "SELECT raw_json FROM cot_raw WHERE market_code = ? AND report_date = ?",
            (MARKET, LATEST_REPORT),
        ).fetchone()["raw_json"]
    )
    assert "Change_in_M_Money_Long_All" in blob
    assert "Pct_of_OI_M_Money_Long_All" in blob


def test_row_counts_and_market_breakdown(imported, con):
    assert imported["summary"]["rows_read"] == 1233
    assert imported["summary"]["rows_inserted"] == 1233
    counts = {
        r["market_code"]: r["n"]
        for r in con.execute(
            "SELECT market_code, COUNT(*) AS n FROM cot_weekly GROUP BY market_code"
        )
    }
    assert counts == {"088691": 1061, "088695": 166, "088LM1": 6}
    assert con.execute("SELECT COUNT(*) FROM cot_raw").fetchone()[0] == 1233


def test_no_duplicate_business_keys(con):
    dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT report_type, market_code, report_date "
        "FROM cot_weekly GROUP BY report_type, market_code, report_date HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0


def test_thousand_separator_and_zero_versus_missing(seed_csv, con):
    """`""` and `"0"` must stay distinguishable all the way into the database."""
    raw = read_seed_csv(seed_csv)
    empty_mask = raw["Traders_Prod_Merc_Long_All"].astype(str).str.strip() == ""
    zero_mask = (
        raw["Traders_Prod_Merc_Long_All"].astype(str).str.strip().str.replace(",", "", regex=False) == "0"
    )
    assert empty_mask.any() and zero_mask.any()
    assert all(parse_int_cell(v) is None for v in raw.loc[empty_mask, "Traders_Prod_Merc_Long_All"].head(5))
    assert all(parse_int_cell(v) == 0 for v in raw.loc[zero_mask, "Traders_Prod_Merc_Long_All"].head(5))
    assert is_missing_token("") and not is_missing_token("0")

    row = con.execute(
        "SELECT w.mm_long, r.raw_json, w.null_fields_json FROM cot_weekly w "
        "JOIN cot_raw r ON r.id = w.source_raw_id "
        "WHERE w.market_code = ? AND w.report_date = ?",
        (MARKET, LATEST_REPORT),
    ).fetchone()
    blob = json.loads(row["raw_json"])
    assert "," in blob["M_Money_Positions_Long_All"]
    assert blob["M_Money_Positions_Long_All"].replace(",", "") == str(row["mm_long"])
    assert row["mm_long"] == 127101

    # null_fields_json is exactly the set of numeric fields stored as NULL, which
    # is what keeps "missing" from being flattened into "zero".
    totals = con.execute(
        "SELECT SUM(traders_prod_long IS NULL) AS nulls, "
        "SUM(traders_prod_long = 0) AS zeros FROM cot_weekly"
    ).fetchone()
    assert totals["nulls"] > 0 and totals["zeros"] > 0

    for record in con.execute(
        "SELECT null_fields_json, open_interest, mm_long, mm_short, "
        "traders_prod_long, traders_tot FROM cot_weekly"
    ):
        missing = set(json.loads(record["null_fields_json"]))
        for field in ("open_interest", "mm_long", "mm_short", "traders_prod_long", "traders_tot"):
            assert (field in missing) == (record[field] is None), field


def test_trailing_whitespace_in_source_is_trimmed(seed_csv, con):
    """The export ships 'CMX ' with a trailing space; the stored layer must not."""
    raw_codes = set(read_seed_csv(seed_csv)["CFTC_Market_Code"].astype(str))
    assert "CMX " in raw_codes
    stored_codes = {
        r["exchange_code"]
        for r in con.execute("SELECT DISTINCT exchange_code FROM cot_weekly")
    }
    assert stored_codes == {"CMX", "FREX"}


def test_row_order_independence(seed_csv):
    """Same logical rows, different file order -> byte-identical raw payloads."""
    raw = read_seed_csv(seed_csv)
    original = sorted(build_raw_json(raw.iloc[i]) for i in range(len(raw)))
    shuffled = raw.sample(frac=1.0, random_state=7)
    reordered = sorted(build_raw_json(shuffled.iloc[i]) for i in range(len(shuffled)))
    assert original == reordered


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_idempotent_double_import(tmp_path, seed_csv):
    db = _fresh_db(tmp_path)
    con = open_con(db)
    try:
        first = run_import(con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED)
        before = dict(
            con.execute("SELECT market_code || report_date AS k, row_hash FROM cot_weekly")
        )
        second = run_import(con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED)
        after = dict(
            con.execute("SELECT market_code || report_date AS k, row_hash FROM cot_weekly")
        )

        from scripts.recompute_indicators import recompute

        recompute(con, REPORT_TYPE_DISAGGREGATED, None, IndicatorParams(), CALCULATION_VERSION)
        indicators_before = con.execute("SELECT COUNT(*) FROM cot_indicators").fetchone()[0]
        third = run_import(con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED)
        indicators_after = con.execute("SELECT COUNT(*) FROM cot_indicators").fetchone()[0]
        weekly_total = con.execute("SELECT COUNT(*) FROM cot_weekly").fetchone()[0]
    finally:
        con.close()

    assert first["rows_inserted"] == 1233
    assert second["rows_inserted"] == 0
    assert second["rows_unchanged"] == 1233
    assert third["rows_inserted"] == 0
    assert before == after
    assert weekly_total == 1233
    assert indicators_before == indicators_after == INDICATOR_ROWS_PER_REPORT * 1233


def test_revision_is_held_then_accepted_explicitly(tmp_path, seed_csv):
    db = _fresh_db(tmp_path)
    con = open_con(db)
    try:
        run_import(con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED)
        # Simulate an upstream change against a row already stored.
        con.execute(
            "UPDATE cot_weekly SET mm_long = 999999, row_hash = 'deliberately-stale' "
            "WHERE market_code = ? AND report_date = ?",
            (MARKET, LATEST_REPORT),
        )
        con.commit()

        held = run_import(con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED)
        value_after_hold = con.execute(
            "SELECT mm_long FROM cot_weekly WHERE market_code = ? AND report_date = ?",
            (MARKET, LATEST_REPORT),
        ).fetchone()["mm_long"]

        accepted = run_import(
            con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED, accept_revisions=True
        )
        value_after_accept = con.execute(
            "SELECT mm_long FROM cot_weekly WHERE market_code = ? AND report_date = ?",
            (MARKET, LATEST_REPORT),
        ).fetchone()["mm_long"]
    finally:
        con.close()

    assert held["status"] == "partial"
    assert held["rows_revised"] == 1
    assert held["rows_inserted"] == 0
    assert held["issues"]
    assert value_after_hold == 999999  # the incoming value was not written

    assert accepted["rows_updated"] == 1
    assert accepted["status"] == "ok"
    assert value_after_accept == 127101


# --------------------------------------------------------------------------
# Validation and traceability
# --------------------------------------------------------------------------


def test_validation_results_are_persisted_and_pct_other_is_excluded(con):
    log = con.execute(
        "SELECT validation_json FROM ingestion_log WHERE status = 'ok' "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert log is not None
    rules = {r["rule"]: r for r in json.loads(log["validation_json"])}
    assert len(rules) == 15
    assert all(r["passed"] for r in rules.values())

    counts = {
        r["market_code"]: r["n"]
        for r in con.execute("SELECT market_code, COUNT(*) AS n FROM cot_weekly GROUP BY market_code")
    }
    expected = sum(n - 1 for n in counts.values()) * len(CHANGE_PAIRS)
    assert rules["R11_change_recompute"]["checked"] == expected
    assert "string sorting" in rules["R11_change_recompute"]["note"]
    assert "Pct_of_Open_Interest_Other" in rules["R8_other_group_all_zero"]["note"]
    assert "R7_all_equals_old" in rules and "R9_oi_identity" in rules


def test_every_weekly_row_traces_back_to_its_raw_row(con):
    rows = list(
        con.execute(
            "SELECT w.report_date, w.open_interest, w.mm_long, w.mm_short, "
            "w.source_raw_id, r.raw_json, r.source_file, r.source_row_number "
            "FROM cot_weekly w JOIN cot_raw r ON r.id = w.source_raw_id "
            "WHERE w.market_code = ? ORDER BY w.report_date LIMIT 40",
            (MARKET,),
        )
    )
    assert len(rows) == 40
    for row in rows:
        blob = json.loads(row["raw_json"])
        assert int(blob["Open_Interest_All"].replace(",", "")) == row["open_interest"]
        assert int(blob["M_Money_Positions_Long_All"].replace(",", "")) == row["mm_long"]
        assert int(blob["M_Money_Positions_Short_All"].replace(",", "")) == row["mm_short"]
        assert row["source_file"].endswith(".csv")
        assert row["source_row_number"] >= 1


def test_dry_run_writes_nothing(tmp_path, seed_csv):
    db = _fresh_db(tmp_path)
    con = open_con(db)
    try:
        summary = run_import(
            con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED, dry_run=True
        )
        raw_count = con.execute("SELECT COUNT(*) FROM cot_raw").fetchone()[0]
        log_count = con.execute("SELECT COUNT(*) FROM ingestion_log").fetchone()[0]
    finally:
        con.close()
    assert summary["status"] == "dry_run"
    assert summary["rows_read"] == 1233
    assert raw_count == 0 and log_count == 0


@pytest.mark.parametrize("market", ["088691", "088695", "088LM1"])
def test_gold_markets_all_carry_the_expected_name_and_code(market, con):
    row = con.execute(
        "SELECT market_name, commodity_code FROM cot_weekly WHERE market_code = ? LIMIT 1",
        (market,),
    ).fetchone()
    assert row is not None
    assert "GOLD" in row["market_name"].upper()
    assert row["commodity_code"] == "088"