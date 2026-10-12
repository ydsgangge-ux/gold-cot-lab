"""Indicator computation.

Contract for every indicator:

* Windows are **trailing** (current report included). Using the full sample
  would leak future data into historical values and is never done here.
* A window means "the last N reports", not N calendar weeks (``window_basis``).
* An invalid result is written as ``value_num = NULL`` + ``is_valid = 0`` +
  ``invalid_reason``. Never 0, never NaN, never an exception that aborts the
  whole batch.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable

import pandas as pd

from ..config import (
    DEFAULT_ALERT_HIGH_PCT,
    DEFAULT_ALERT_LOW_PCT,
    DEFAULT_COT_INDEX_WINDOWS_WEEKS,
    DEFAULT_MIN_COVERAGE,
    DEFAULT_PCT_RANK_WINDOWS_YEARS,
    DEFAULT_PRICE_SYMBOL,
    DEFAULT_RETURN_WINDOWS_WEEKS,
    DEFAULT_TREND_WINDOWS_WEEKS,
    DEFAULT_WINDOW_BASIS,
    REPORTS_PER_YEAR,
)

CALCULATION_VERSION = "1.0.0"

# invalid_reason vocabulary
R_INSUFFICIENT = "insufficient_sample"
R_FLAT_WINDOW = "flat_window"
R_ZERO_OI = "zero_oi"
R_NO_PRICE = "no_price"
R_NO_PREV = "no_prev"
R_FLAT_CHANGE = "flat_change"

# Divergence / alert labels
DIV_CONFIRM_UP = "趋势与仓位一致（上涨增仓）"
DIV_DIVERGE_UP = "价格与仓位背离（上涨减仓）"
DIV_CONFIRM_DOWN = "趋势与仓位一致（下跌减仓）"
DIV_DIVERGE_DOWN = "价格与仓位背离（下跌增仓）"
DIV_UNKNOWN = "unknown"

ALERT_HIGH = "高位拥挤观察"
ALERT_LOW = "低位拥挤观察"
ALERT_NORMAL = "normal"
ALERT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class IndicatorParams:
    """User-configurable parameters.

    Thresholds belong here (and in ``params_json`` / ``params_hash``), **not** in
    ``calculation_version``: adjusting a threshold does not redefine an indicator.
    """

    pct_rank_windows_years: tuple[int, ...] = DEFAULT_PCT_RANK_WINDOWS_YEARS
    cot_index_windows_weeks: tuple[int, ...] = DEFAULT_COT_INDEX_WINDOWS_WEEKS
    return_windows_weeks: tuple[int, ...] = DEFAULT_RETURN_WINDOWS_WEEKS
    trend_windows_weeks: tuple[int, ...] = DEFAULT_TREND_WINDOWS_WEEKS
    min_coverage: float = DEFAULT_MIN_COVERAGE
    alert_high_pct: float = DEFAULT_ALERT_HIGH_PCT
    alert_low_pct: float = DEFAULT_ALERT_LOW_PCT
    window_basis: str = DEFAULT_WINDOW_BASIS
    price_symbol: str = DEFAULT_PRICE_SYMBOL
    pit_mode: str = "published_at"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @property
    def params_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()[:16]

    def window_rows(self, window_key: str) -> int:
        if window_key.endswith("y"):
            return int(window_key[:-1]) * REPORTS_PER_YEAR
        return int(window_key.rstrip("w"))

    def min_obs(self, window_rows: int) -> int:
        return math.ceil(window_rows * self.min_coverage)


def _min_obs_needed(params: IndicatorParams, window_rows: int) -> int:
    return max(2, params.min_obs(window_rows))


# --------------------------------------------------------------------------
# A. Net position and weekly change
# --------------------------------------------------------------------------


def compute_net(df: pd.DataFrame) -> pd.Series:
    """Net managed-money position: long - short."""
    long_ = pd.to_numeric(df["mm_long"], errors="coerce")
    short_ = pd.to_numeric(df["mm_short"], errors="coerce")
    return long_ - short_


def compute_changes(df: pd.DataFrame) -> pd.DataFrame:
    """Weekly deltas, split into long and short legs.

    Splitting matters: a rising net can come from new longs *or* from short
    covering, and those are different behaviours.
    """
    net = compute_net(df)
    long_ = pd.to_numeric(df["mm_long"], errors="coerce")
    short_ = pd.to_numeric(df["mm_short"], errors="coerce")
    return pd.DataFrame(
        {
            "d_net": net.diff(),
            "d_long": long_.diff(),
            "d_short": short_.diff(),
        },
        index=df.index,
    )


def compute_net_oi_pct(df: pd.DataFrame) -> pd.Series:
    """Net position as a share of total open interest, in percent."""
    net = compute_net(df)
    oi = pd.to_numeric(df["open_interest"], errors="coerce")
    return net / oi.where(oi > 0) * 100.0


# --------------------------------------------------------------------------
# C / D. Trailing-window rank and COT index
# --------------------------------------------------------------------------


def compute_percentile_rank(
    values: pd.Series, window_rows: int, min_obs: int
) -> tuple[pd.Series, pd.Series]:
    """Percentile of the current value inside its trailing window.

    Rank uses ``<=`` (the current value counts itself), so a series that is
    monotonically rising yields 100 at the last point.
    """
    arr = [None if pd.isna(v) else float(v) for v in values]
    ranks: list[float | None] = []
    samples: list[int] = []
    for i, current in enumerate(arr):
        window = arr[max(0, i - window_rows + 1): i + 1]
        valid = [v for v in window if v is not None]
        samples.append(len(valid))
        if current is None or len(valid) < min_obs:
            ranks.append(None)
            continue
        ranks.append(100.0 * sum(1 for v in valid if v <= current) / len(valid))
    return pd.Series(ranks, index=values.index, dtype="Float64"), pd.Series(
        samples, index=values.index, dtype="Int64"
    )


def compute_cot_index(
    values: pd.Series, window_rows: int, min_obs: int
) -> tuple[pd.Series, pd.Series]:
    """COT Index: 100 * (v - min) / (max - min) over the trailing window.

    A flat window (max == min) is reported as *invalid*, not as a division by
    zero and not as a silently propagated NaN.
    """
    arr = [None if pd.isna(v) else float(v) for v in values]
    out: list[float | None] = []
    samples: list[int] = []
    for i, current in enumerate(arr):
        window = arr[max(0, i - window_rows + 1): i + 1]
        valid = [v for v in window if v is not None]
        samples.append(len(valid))
        if current is None or len(valid) < min_obs:
            out.append(None)
            continue
        lo, hi = min(valid), max(valid)
        out.append(None if hi == lo else 100.0 * (current - lo) / (hi - lo))
    return pd.Series(out, index=values.index, dtype="Float64"), pd.Series(
        samples, index=values.index, dtype="Int64"
    )


# --------------------------------------------------------------------------
# E. Price trend, streaks and divergence
# --------------------------------------------------------------------------


def align_price_to_reports(
    price: pd.DataFrame,
    published_dates: Iterable[date],
) -> list[float | None]:
    """Take, for each report, the last close available on or before its

    publication date. Using the report date itself would leak information that
    did not exist yet; using the publication date is the earliest honest point.
    """
    if price is None or price.empty:
        return [None] * len(list(published_dates))
    frame = price.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    frame = frame.sort_values("trade_date")
    dates = frame["trade_date"].tolist()
    closes = frame["close"].tolist()
    out: list[float | None] = []
    for pub in published_dates:
        idx = bisect.bisect_right(dates, pub) - 1
        if idx < 0 or closes[idx] is None or pd.isna(closes[idx]):
            out.append(None)
        else:
            out.append(float(closes[idx]))
    return out


def compute_returns(prices: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(prices)):
        if i - window < 0 or prices[i] is None or prices[i - window] in (None, 0):
            out.append(None)
        else:
            out.append(prices[i] / prices[i - window] - 1.0)
    return out


def compute_trend(prices: list[float | None], window: int) -> list[int | None]:
    """+1 up / -1 down / 0 flat, comparing window-start and current close."""
    out: list[int | None] = []
    for i in range(len(prices)):
        if i - window < 0 or prices[i] is None or prices[i - window] is None:
            out.append(None)
            continue
        delta = prices[i] - prices[i - window]
        out.append(0 if delta == 0 else (1 if delta > 0 else -1))
    return out


def compute_net_streak(d_net: pd.Series) -> tuple[pd.Series, list[str | None]]:
    """Consecutive weeks the net position has moved in the same direction."""
    arr = [None if pd.isna(v) else float(v) for v in d_net]
    streaks: list[int | None] = []
    reasons: list[str | None] = []
    prev_sign = 0
    prev_streak = 0
    for i, value in enumerate(arr):
        if value is None or value == 0:
            streaks.append(None)
            reasons.append(R_NO_PREV if i == 0 else R_FLAT_CHANGE)
            prev_sign, prev_streak = 0, 0
            continue
        sign = 1 if value > 0 else -1
        if i == 0:
            streaks.append(None)
            reasons.append(R_NO_PREV)
            prev_sign, prev_streak = sign, 1
            continue
        if sign == prev_sign:
            prev_streak += 1
        else:
            prev_streak = 1
        prev_sign = sign
        streaks.append(prev_streak)
        reasons.append(None)
    return pd.Series(streaks, index=d_net.index, dtype="Int64"), reasons


def classify_divergence(
    d_net: pd.Series, trends: list[int | None]
) -> tuple[pd.Series, list[str | None]]:
    """Four-quadrant comparison of price direction against net-position change.

    Divergence only means "worth watching"; it is not a reversal signal.
    """
    labels: list[str] = []
    reasons: list[str | None] = []
    for i, trend in enumerate(trends):
        change = d_net.iloc[i]
        if trend is None or pd.isna(change) or change == 0:
            labels.append(DIV_UNKNOWN)
            reasons.append(R_NO_PRICE if trend is None else R_FLAT_CHANGE)
            continue
        up = change > 0
        if trend > 0:
            labels.append(DIV_CONFIRM_UP if up else DIV_DIVERGE_UP)
        elif trend < 0:
            labels.append(DIV_CONFIRM_DOWN if not up else DIV_DIVERGE_DOWN)
        else:
            labels.append(DIV_UNKNOWN)
        reasons.append(None)
    return pd.Series(labels, index=d_net.index, dtype="string"), reasons


def classify_alert(
    pct_rank: pd.Series, params: IndicatorParams
) -> tuple[pd.Series, list[str | None]]:
    labels: list[str] = []
    reasons: list[str | None] = []
    for value in pct_rank:
        if pd.isna(value):
            labels.append(ALERT_UNKNOWN)
            reasons.append(R_INSUFFICIENT)
            continue
        if value > params.alert_high_pct:
            labels.append(ALERT_HIGH)
        elif value < params.alert_low_pct:
            labels.append(ALERT_LOW)
        else:
            labels.append(ALERT_NORMAL)
        reasons.append(None)
    return pd.Series(labels, index=pct_rank.index, dtype="string"), reasons


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _emit(
    rows: list[dict[str, Any]],
    *,
    report_date: date,
    published_at: date,
    indicator_code: str,
    window_key: str,
    value_num: float | None = None,
    value_text: str | None = None,
    is_valid: bool,
    invalid_reason: str | None,
    sample_size: int | None,
    params: IndicatorParams,
    calc_version: str,
) -> None:
    rows.append(
        {
            "report_date": report_date.isoformat(),
            "published_at": published_at.isoformat(),
            "indicator_code": indicator_code,
            "window_key": window_key,
            "value_num": value_num,
            "value_text": value_text,
            "is_valid": 1 if is_valid else 0,
            "invalid_reason": invalid_reason,
            "sample_size": sample_size,
            "params_json": params.to_json(),
            "params_hash": params.params_hash,
            "calculation_version": calc_version,
        }
    )


def compute_all(
    weekly: pd.DataFrame,
    price: pd.DataFrame | None = None,
    params: IndicatorParams | None = None,
    calc_version: str = CALCULATION_VERSION,
) -> pd.DataFrame:
    """Compute every first-version indicator for one (report_type, market).

    Returns a long-format frame ready to be written to ``cot_indicators``.
    """
    params = params or IndicatorParams()
    if weekly.empty:
        return pd.DataFrame(
            columns=[
                "report_date", "published_at", "indicator_code", "window_key",
                "value_num", "value_text", "is_valid", "invalid_reason",
                "sample_size", "params_json", "params_hash", "calculation_version",
            ]
        )

    df = weekly.sort_values("report_date").reset_index(drop=True)
    report_dates = [pd.Timestamp(d).date() for d in df["report_date"]]
    published = [pd.Timestamp(p).date() for p in df["published_at"]]

    net = compute_net(df)
    changes = compute_changes(df)
    net_oi_pct = compute_net_oi_pct(df)
    oi = pd.to_numeric(df["open_interest"], errors="coerce")

    prices = align_price_to_reports(price, published) if price is not None else None
    trend_windows = params.trend_windows_weeks
    trends: dict[int, list[int | None]] = (
        {w: compute_trend(prices, w) for w in trend_windows} if prices is not None else {}
    )

    streak, streak_reasons = compute_net_streak(changes["d_net"])

    # Divergence uses the shortest trend window; one row per report.
    div_window = trend_windows[0] if trend_windows else 4
    div_labels, div_reasons = classify_divergence(
        changes["d_net"], trends.get(div_window, [None] * len(df))
    )

    rank_windows: dict[str, tuple[pd.Series, pd.Series]] = {}
    for years in params.pct_rank_windows_years:
        key = f"{years}y"
        rank_windows[key] = compute_percentile_rank(
            net, params.window_rows(key), _min_obs_needed(params, params.window_rows(key))
        )
    primary_rank_key = f"{params.pct_rank_windows_years[0]}y"
    alert_labels, alert_reasons = classify_alert(rank_windows[primary_rank_key][0], params)

    index_windows: dict[str, tuple[pd.Series, pd.Series]] = {}
    for weeks in params.cot_index_windows_weeks:
        key = f"{weeks}w"
        window_rows = params.window_rows(key)
        index_windows[key] = compute_cot_index(
            net, window_rows, _min_obs_needed(params, window_rows)
        )

    return_windows: dict[str, list[float | None]] = {
        f"{w}w": (compute_returns(prices, w) if prices is not None else [None] * len(df))
        for w in params.return_windows_weeks
    }

    rows: list[dict[str, Any]] = []
    for i, report_date in enumerate(report_dates):
        published_at = published[i]

        def simple(code: str, series: pd.Series, reason: str | None = None) -> None:
            value = series.iloc[i]
            valid = not pd.isna(value)
            _emit(
                rows,
                report_date=report_date,
                published_at=published_at,
                indicator_code=code,
                window_key="",
                value_num=None if not valid else round(float(value), 6),
                is_valid=valid,
                invalid_reason=None if valid else reason,
                sample_size=None,
                params=params,
                calc_version=calc_version,
            )

        simple("net", net)
        simple("d_net", changes["d_net"], R_NO_PREV)
        simple("d_long", changes["d_long"], R_NO_PREV)
        simple("d_short", changes["d_short"], R_NO_PREV)

        # net_oi_pct has its own reason when open interest is unusable.
        oi_value = oi.iloc[i]
        pct_value = net_oi_pct.iloc[i]
        if not pd.isna(pct_value):
            reason = None
        elif pd.isna(oi_value) or oi_value <= 0:
            reason = R_ZERO_OI
        else:
            reason = R_INSUFFICIENT
        _emit(
            rows,
            report_date=report_date,
            published_at=published_at,
            indicator_code="net_oi_pct",
            window_key="",
            value_num=None if pd.isna(pct_value) else round(float(pct_value), 6),
            is_valid=not pd.isna(pct_value),
            invalid_reason=reason,
            sample_size=None,
            params=params,
            calc_version=calc_version,
        )

        # net_streak
        streak_value = streak.iloc[i]
        _emit(
            rows,
            report_date=report_date,
            published_at=published_at,
            indicator_code="net_streak",
            window_key="",
            value_num=None if pd.isna(streak_value) else float(streak_value),
            is_valid=not pd.isna(streak_value),
            invalid_reason=streak_reasons[i],
            sample_size=None,
            params=params,
            calc_version=calc_version,
        )

        # percentile rank per window
        for key, (series, samples) in rank_windows.items():
            value = series.iloc[i]
            _emit(
                rows,
                report_date=report_date,
                published_at=published_at,
                indicator_code="pct_rank",
                window_key=key,
                value_num=None if pd.isna(value) else round(float(value), 6),
                is_valid=not pd.isna(value),
                invalid_reason=None if not pd.isna(value) else R_INSUFFICIENT,
                sample_size=int(samples.iloc[i]),
                params=params,
                calc_version=calc_version,
            )

        # COT index per window
        for key, (series, samples) in index_windows.items():
            window_rows = params.window_rows(key)
            needed = _min_obs_needed(params, window_rows)
            value = series.iloc[i]
            if not pd.isna(value):
                reason = None
            elif int(samples.iloc[i]) < needed:
                reason = R_INSUFFICIENT
            else:
                reason = R_FLAT_WINDOW
            _emit(
                rows,
                report_date=report_date,
                published_at=published_at,
                indicator_code="cot_index",
                window_key=key,
                value_num=None if pd.isna(value) else round(float(value), 6),
                is_valid=not pd.isna(value),
                invalid_reason=reason,
                sample_size=int(samples.iloc[i]),
                params=params,
                calc_version=calc_version,
            )

        # price returns
        for key, series in return_windows.items():
            value = series[i]
            if prices is None:
                reason = R_NO_PRICE
            elif value is None:
                reason = R_NO_PRICE if prices[i] is None else R_INSUFFICIENT
            else:
                reason = None
            _emit(
                rows,
                report_date=report_date,
                published_at=published_at,
                indicator_code="ret",
                window_key=key,
                value_num=None if value is None else round(float(value), 8),
                is_valid=value is not None,
                invalid_reason=reason,
                sample_size=None,
                params=params,
                calc_version=calc_version,
            )

        for weeks in trend_windows:
            key = f"{weeks}w"
            value = trends.get(weeks, [None] * len(df))[i]
            _emit(
                rows,
                report_date=report_date,
                published_at=published_at,
                indicator_code="trend",
                window_key=key,
                value_num=None if value is None else float(value),
                is_valid=value is not None,
                invalid_reason=None if value is not None else (
                    R_NO_PRICE if prices is None else R_INSUFFICIENT
                ),
                sample_size=None,
                params=params,
                calc_version=calc_version,
            )

        # divergence and alert
        div_label = div_labels.iloc[i]
        _emit(
            rows,
            report_date=report_date,
            published_at=published_at,
            indicator_code="divergence",
            window_key=f"{div_window}w",
            value_text=str(div_label),
            is_valid=str(div_label) != DIV_UNKNOWN,
            invalid_reason=div_reasons[i],
            sample_size=None,
            params=params,
            calc_version=calc_version,
        )
        alert_label = alert_labels.iloc[i]
        _emit(
            rows,
            report_date=report_date,
            published_at=published_at,
            indicator_code="alert",
            window_key=primary_rank_key,
            value_text=str(alert_label),
            is_valid=str(alert_label) != ALERT_UNKNOWN,
            invalid_reason=alert_reasons[i],
            sample_size=None,
            params=params,
            calc_version=calc_version,
        )

    return pd.DataFrame(rows)


# The number of indicator rows produced per report for the default parameters.
INDICATOR_ROWS_PER_REPORT = 18