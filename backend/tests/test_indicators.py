"""Indicator maths.

Every formula is pinned against a hand-computed value, and the two properties
that matter most operationally get their own tests: a trailing window must never
change when later rows are appended, and an unusable window must yield NULL with
a reason rather than 0 or an exception.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from backend.analysis.indicators import (
    ALERT_HIGH,
    ALERT_LOW,
    ALERT_NORMAL,
    CALCULATION_VERSION,
    DIV_CONFIRM_DOWN,
    DIV_CONFIRM_UP,
    DIV_DIVERGE_DOWN,
    DIV_DIVERGE_UP,
    DIV_UNKNOWN,
    R_FLAT_WINDOW,
    R_NO_PRICE,
    R_NO_PREV,
    R_ZERO_OI,
    IndicatorParams,
    align_price_to_reports,
    classify_alert,
    classify_divergence,
    compute_all,
    compute_changes,
    compute_cot_index,
    compute_net,
    compute_net_oi_pct,
    compute_net_streak,
    compute_percentile_rank,
    compute_returns,
    compute_trend,
)

MARKET = "088691"
LATEST_REPORT = "2026-10-06"


def _frame(rows: list[tuple[str, int, int, int]]) -> pd.DataFrame:
    """rows: (report_date, open_interest, mm_long, mm_short)."""
    return pd.DataFrame(
        [
            {
                "report_date": d,
                "published_at": d,
                "open_interest": oi,
                "mm_long": long_,
                "mm_short": short,
            }
            for d, oi, long_, short in rows
        ]
    )


def _weekly_frame(n: int, oi: int = 400000) -> pd.DataFrame:
    start = date(2015, 1, 6)
    rows = []
    for i in range(n):
        d = start + timedelta(days=7 * i)
        rows.append(
            (d.isoformat(), oi + i, 100000 + (i % 7) * 1000 + (i // 7) * 500, 20000 + (i % 5) * 700)
        )
    return _frame(rows)


# --------------------------------------------------------------------------
# A / B. Net position, split changes, share of open interest
# --------------------------------------------------------------------------


def test_net_and_split_changes_are_hand_computed():
    df = _frame(
        [
            ("2024-01-02", 1000, 100, 40),
            ("2024-01-09", 1000, 120, 40),
            ("2024-01-16", 1000, 110, 60),
        ]
    )
    assert list(compute_net(df)) == [60, 80, 50]

    changes = compute_changes(df)
    assert pd.isna(changes["d_net"].iloc[0]) and pd.isna(changes["d_long"].iloc[0])
    # Net fell 30 in the last week, but that is -10 long and +20 short: the split
    # is what tells "long liquidation" apart from "new shorts".
    assert (changes["d_net"].iloc[2], changes["d_long"].iloc[2], changes["d_short"].iloc[2]) == (
        -30,
        -10,
        20,
    )
    assert (changes["d_net"].iloc[1], changes["d_long"].iloc[1], changes["d_short"].iloc[1]) == (
        20,
        20,
        0,
    )


def test_net_oi_pct_and_zero_open_interest_is_invalid():
    df = _frame([("2024-01-02", 1000, 100, 40), ("2024-01-09", 0, 120, 40)])
    pct = compute_net_oi_pct(df)
    assert float(pct.iloc[0]) == pytest.approx(6.0)
    assert pd.isna(pct.iloc[1])


def test_zero_oi_reason_is_reported_through_compute_all():
    df = _frame([("2024-01-02", 0, 100, 40)])
    out = compute_all(df, None, IndicatorParams(), CALCULATION_VERSION)
    row = out[out["indicator_code"] == "net_oi_pct"].iloc[0]
    assert row["is_valid"] == 0
    assert row["invalid_reason"] == R_ZERO_OI
    assert pd.isna(row["value_num"])


# --------------------------------------------------------------------------
# C / D. Trailing-window rank and COT index
# --------------------------------------------------------------------------


def test_percentile_rank_hand_computed():
    values = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
    ranks, samples = compute_percentile_rank(values, window_rows=5, min_obs=3)
    pd.testing.assert_series_equal(
        ranks, pd.Series([pd.NA, pd.NA, 100.0, 100.0, 100.0], dtype="Float64")
    )
    pd.testing.assert_series_equal(samples, pd.Series([1, 2, 3, 4, 5], dtype="Int64"))

    smallest_last = pd.Series([10.0, 20.0, 30.0, 40.0, 5.0])
    ranks2, _ = compute_percentile_rank(smallest_last, window_rows=5, min_obs=3)
    assert float(ranks2.iloc[-1]) == pytest.approx(20.0)  # 1 of 5 values <= 5


def test_percentile_rank_is_insufficient_below_min_obs():
    values = pd.Series([1.0, 2.0, 3.0])
    ranks, samples = compute_percentile_rank(values, window_rows=10, min_obs=10)
    pd.testing.assert_series_equal(ranks, pd.Series([pd.NA] * 3, dtype="Float64"))
    pd.testing.assert_series_equal(samples, pd.Series([1, 2, 3], dtype="Int64"))


def test_cot_index_hand_computed_including_window_boundaries():
    values = pd.Series([40.0, 10.0, 25.0, 100.0, 70.0])
    index, samples = compute_cot_index(values, window_rows=3, min_obs=3)
    pd.testing.assert_series_equal(samples, pd.Series([1, 2, 3, 3, 3], dtype="Int64"))
    # window [40,10,25] -> (25-10)/(40-10) = 50
    # window [10,25,100] -> (100-10)/(100-10) = 100
    # window [25,100,70] -> (70-25)/(100-25) = 60
    pd.testing.assert_series_equal(
        index, pd.Series([pd.NA, pd.NA, 50.0, 100.0, 60.0], dtype="Float64")
    )


def test_flat_window_is_invalid_and_does_not_raise():
    values = pd.Series([5.0, 5.0, 5.0, 5.0])
    index, samples = compute_cot_index(values, window_rows=3, min_obs=3)
    pd.testing.assert_series_equal(index, pd.Series([pd.NA] * 4, dtype="Float64"))
    pd.testing.assert_series_equal(samples, pd.Series([1, 2, 3, 3], dtype="Int64"))


def test_flat_window_reason_survives_into_compute_all():
    df = _frame([(f"2024-{1 + i // 4:02d}-{1 + (i % 4) * 7:02d}", 1000, 100, 40) for i in range(40)])
    out = compute_all(df, None, IndicatorParams(), CALCULATION_VERSION)
    rows = out[out["indicator_code"] == "cot_index"]

    assert rows["value_num"].isna().all()  # flat -> NULL, never 0
    assert set(rows["invalid_reason"]) <= {R_FLAT_WINDOW, "insufficient_sample"}
    flat = rows[rows["invalid_reason"] == R_FLAT_WINDOW]
    # Only the windows that have reached their minimum sample size are reported
    # as flat; the 156-week window is still short of history.
    assert set(flat["window_key"]) == {"26w", "52w"}


# --------------------------------------------------------------------------
# Trailing windows must not change when later rows arrive
# --------------------------------------------------------------------------


def test_trailing_window_has_no_lookahead():
    """Appending future reports may not alter a single earlier indicator value."""
    prefix = _weekly_frame(200)
    full = _weekly_frame(260)
    params = IndicatorParams()

    short = compute_all(prefix, None, params, CALCULATION_VERSION)
    long_ = compute_all(full, None, params, CALCULATION_VERSION)

    keys = ["report_date", "indicator_code", "window_key"]
    columns = ["value_num", "is_valid", "invalid_reason", "sample_size"]
    short_indexed = short.set_index(keys).sort_index()
    long_indexed = (
        long_[long_["report_date"].isin(prefix["report_date"])]
        .set_index(keys)
        .sort_index()
    )
    assert len(short_indexed) == len(long_indexed)
    pd.testing.assert_frame_equal(
        short_indexed[columns], long_indexed[columns], check_dtype=False
    )


def test_cot_index_and_rank_series_are_unchanged_by_extension():
    base = [10.0 + (i % 9) * 3 for i in range(120)]
    extended = base + [999.0, 1.0, 500.0]

    base_rank, _ = compute_percentile_rank(pd.Series(base), window_rows=52, min_obs=32)
    ext_rank, _ = compute_percentile_rank(pd.Series(extended), window_rows=52, min_obs=32)
    pd.testing.assert_series_equal(base_rank, ext_rank.iloc[: len(base)], check_names=False)

    base_index, _ = compute_cot_index(pd.Series(base), window_rows=52, min_obs=32)
    ext_index, _ = compute_cot_index(pd.Series(extended), window_rows=52, min_obs=32)
    pd.testing.assert_series_equal(base_index, ext_index.iloc[: len(base)], check_names=False)


# --------------------------------------------------------------------------
# E. Price alignment, returns, trend, streaks, divergence
# --------------------------------------------------------------------------


def test_align_price_uses_publication_date_and_never_looks_ahead():
    price = pd.DataFrame(
        {
            "trade_date": ["2022-01-03", "2022-01-04", "2022-01-05"],
            "close": [1800.0, 1810.0, 1820.0],
        }
    )
    # A report published 2022-01-04 can only see the 01-04 close.
    assert align_price_to_reports(price, [date(2022, 1, 4), date(2022, 1, 5)]) == [1810.0, 1820.0]
    # Nothing was published yet, so there is no honest price.
    assert align_price_to_reports(price, [date(2022, 1, 2)]) == [None]


def test_align_price_with_no_price_frame_returns_none():
    assert align_price_to_reports(None, [date(2022, 1, 4)]) == [None]
    assert align_price_to_reports(pd.DataFrame(), [date(2022, 1, 4), date(2022, 1, 5)]) == [None, None]


def test_returns_and_trend_hand_computed():
    prices = [100.0, 110.0, 121.0, 121.0]
    returns = compute_returns(prices, window=1)
    assert returns[0] is None
    assert returns[1] == pytest.approx(0.1)
    assert returns[2] == pytest.approx(0.1)
    assert returns[3] == pytest.approx(0.0)

    trend = compute_trend(prices, window=1)
    assert trend == [None, 1, 1, 0]


def test_net_streak_hand_computed():
    d_net = pd.Series([pd.NA, 5.0, 3.0, -2.0, 4.0, 0.0, 1.0], dtype="Float64")
    streaks, reasons = compute_net_streak(d_net)
    pd.testing.assert_series_equal(
        streaks, pd.Series([pd.NA, 1, 2, 1, 1, pd.NA, 1], dtype="Int64")
    )
    assert reasons[0] == R_NO_PREV
    assert reasons[5] is not None  # a flat week breaks the streak


def test_divergence_quadrants():
    d_net = pd.Series([1.0, 1.0, -1.0, -1.0, 0.0])
    trends = [1, -1, -1, 1, 1]
    labels, _ = classify_divergence(d_net, trends)
    assert list(labels) == [
        DIV_CONFIRM_UP,
        DIV_DIVERGE_DOWN,
        DIV_CONFIRM_DOWN,
        DIV_DIVERGE_UP,
        DIV_UNKNOWN,
    ]


def test_divergence_without_price_is_unknown_with_reason():
    d_net = pd.Series([1.0, -1.0])
    labels, reasons = classify_divergence(d_net, [None, None])
    assert list(labels) == [DIV_UNKNOWN, DIV_UNKNOWN]
    assert reasons == [R_NO_PRICE, R_NO_PRICE]


def test_price_less_run_marks_price_indicators_invalid():
    out = compute_all(_weekly_frame(30), None, IndicatorParams(), CALCULATION_VERSION)
    price_codes = out[out["indicator_code"].isin(["ret", "trend", "divergence"])]
    assert not price_codes.empty
    assert (price_codes["is_valid"] == 0).all()
    assert set(price_codes["invalid_reason"]) == {R_NO_PRICE}


# --------------------------------------------------------------------------
# F. Alerts
# --------------------------------------------------------------------------


def test_alert_thresholds_are_configurable():
    ranks = pd.Series([95.0, 85.0, 5.0])
    default = IndicatorParams()
    labels_default, _ = classify_alert(ranks, default)
    assert list(labels_default) == [ALERT_HIGH, ALERT_NORMAL, ALERT_LOW]

    tweaked = IndicatorParams(alert_high_pct=80.0, alert_low_pct=50.0)
    labels_tweaked, _ = classify_alert(ranks, tweaked)
    assert list(labels_tweaked) == [ALERT_HIGH, ALERT_HIGH, ALERT_LOW]
    assert tweaked.params_hash != default.params_hash
    assert tweaked.to_json() != default.to_json()


def test_alert_without_enough_history_is_unknown():
    labels, reasons = classify_alert(pd.Series([None]), IndicatorParams())
    assert list(labels) == ["unknown"]
    assert reasons == ["insufficient_sample"]


def test_params_hash_is_stable_and_threshold_sensitive():
    a = IndicatorParams()
    b = IndicatorParams()
    assert a.params_hash == b.params_hash
    assert IndicatorParams(alert_high_pct=95.0).params_hash != a.params_hash
    # Window meaning is a parameter too.
    assert IndicatorParams(window_basis="calendar").params_hash != a.params_hash


# --------------------------------------------------------------------------
# Persisted results
# --------------------------------------------------------------------------


def test_indicator_row_count_for_the_stored_history(indicated, con):
    rows = con.execute(
        "SELECT COUNT(*) FROM cot_indicators WHERE report_type = ? AND market_code = ? "
        "AND calculation_version = ?",
        ("disaggregated_futures_only", MARKET, CALCULATION_VERSION),
    ).fetchone()[0]
    assert rows == 18 * 1061 == 19098


def test_invalid_indicators_store_null_and_a_reason(indicated, con):
    bad = con.execute(
        "SELECT COUNT(*) FROM cot_indicators WHERE is_valid = 0 AND value_num IS NOT NULL"
    ).fetchone()[0]
    assert bad == 0
    missing_reason = con.execute(
        "SELECT COUNT(*) FROM cot_indicators WHERE is_valid = 0 AND invalid_reason IS NULL"
    ).fetchone()[0]
    assert missing_reason == 0


def test_stored_net_and_cot_index_match_a_manual_recompute(indicated, con):
    rows = list(
        con.execute(
            "SELECT report_date, mm_long, mm_short FROM cot_weekly "
            "WHERE report_type = 'disaggregated_futures_only' AND market_code = ? "
            "ORDER BY report_date",
            (MARKET,),
        )
    )
    net = [r["mm_long"] - r["mm_short"] for r in rows]
    assert rows[-1]["report_date"] == LATEST_REPORT

    stored = {
        (r["indicator_code"], r["window_key"]): r["value_num"]
        for r in con.execute(
            "SELECT indicator_code, window_key, value_num FROM cot_indicators "
            "WHERE report_type = 'disaggregated_futures_only' AND market_code = ? "
            "AND report_date = ? AND calculation_version = ?",
            (MARKET, LATEST_REPORT, CALCULATION_VERSION),
        )
    }

    assert stored[("net", "")] == pytest.approx(net[-1], rel=1e-9)

    window = net[-52:]
    expected_index = 100.0 * (net[-1] - min(window)) / (max(window) - min(window))
    assert stored[("cot_index", "52w")] == pytest.approx(expected_index, rel=1e-6)

    window_156 = net[-156:]
    expected_rank = 100.0 * sum(1 for v in window_156 if v <= net[-1]) / len(window_156)
    assert stored[("pct_rank", "3y")] == pytest.approx(expected_rank, rel=1e-6)


def test_recompute_is_idempotent_for_one_version(indicated, con):
    from scripts.recompute_indicators import recompute

    before = con.execute("SELECT COUNT(*) FROM cot_indicators").fetchone()[0]
    recompute(con, "disaggregated_futures_only", [MARKET], IndicatorParams(), CALCULATION_VERSION)
    after = con.execute("SELECT COUNT(*) FROM cot_indicators").fetchone()[0]
    assert before == after == 18 * 1233