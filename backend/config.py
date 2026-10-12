"""Central configuration: paths, report types, indicator defaults, federal holidays."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
RAW_CFTC_DIR = RAW_DIR / "cftc"
RAW_PRICE_DIR = RAW_DIR / "prices"
DB_PATH = DATA_DIR / "gold_cot.sqlite"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"

# --------------------------------------------------------------------------
# Report types
# --------------------------------------------------------------------------

REPORT_TYPE_DISAGGREGATED = "disaggregated_futures_only"
REPORT_TYPE_LEGACY = "legacy_futures_only"
REPORT_TYPES = (REPORT_TYPE_DISAGGREGATED, REPORT_TYPE_LEGACY)

# SOCRATA dataset id for Disaggregated Futures Only (official API endpoint).
CFTC_DISAGGREGATED_RESOURCE = "72hh-3qpy"
CFTC_API_BASE = "https://publicreporting.cftc.gov/resource"

# Markets shipped in the seed export.
DEFAULT_MARKET_CODE = "088691"  # GOLD - COMMODITY EXCHANGE INC.
DEFAULT_PRICE_SYMBOL = "GC=F"

# The launcher's preferred listen port. Deliberately not 8000: that port is the
# default of too many dev tools, and a collision would silently move the app to
# a port the user was never told about.
DEFAULT_PORT = 9527

# --------------------------------------------------------------------------
# Indicator defaults (user-configurable; thresholds never enter calculation_version)
# --------------------------------------------------------------------------

DEFAULT_PCT_RANK_WINDOWS_YEARS = (3, 5)
DEFAULT_COT_INDEX_WINDOWS_WEEKS = (26, 52, 156)
DEFAULT_RETURN_WINDOWS_WEEKS = (1, 4, 12)
DEFAULT_TREND_WINDOWS_WEEKS = (4, 12)
DEFAULT_MIN_COVERAGE = 0.60
DEFAULT_ALERT_HIGH_PCT = 90.0
DEFAULT_ALERT_LOW_PCT = 10.0
# Window meaning is "last N reports", not N calendar weeks.
DEFAULT_WINDOW_BASIS = "rows"

# Calendar-year windows are converted to report counts using this factor.
# 52 weekly reports per year; the 3y window is 156 rows, the 5y window is 260.
REPORTS_PER_YEAR = 52

# --------------------------------------------------------------------------
# Publication timing
# --------------------------------------------------------------------------

# CFTC publishes the report for a given Tuesday at ~15:30 ET on the same week's Friday.
PUBLISH_HOUR_ET = 15
PUBLISH_MINUTE_ET = 30


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th `weekday` (0=Mon) of a month. n is 1-based."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _observed(d: date) -> date:
    """Apply the US federal observed rule: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def federal_holidays(year: int) -> set[date]:
    """US federal holidays (observed dates) for one calendar year.

    A holiday observed on Jan 1 of `year` may fall in December of `year - 1`,
    and the reverse; callers that need a contiguous window should union the
    results of adjacent years.
    """
    days = [
        _observed(date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),          # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),          # Washington's Birthday
        _last_weekday(year, 5, 0),            # Memorial Day
        _observed(date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),          # Labor Day
        _nth_weekday(year, 10, 0, 2),         # Columbus Day
        _observed(date(year, 11, 11)),        # Veterans Day
        _nth_weekday(year, 11, 3, 4),         # Thanksgiving
        _observed(date(year, 12, 25)),        # Christmas Day
    ]
    if year >= 2021:
        days.append(_observed(date(year, 6, 19)))  # Juneteenth
    return {d for d in days if d.year == year}


def is_federal_holiday(d: date) -> bool:
    """True if `d` is an observed US federal holiday.

    Holidays observed near a year boundary are looked up in the neighbouring
    year so that e.g. Jan 1 falling on a Saturday (observed Dec 31) is caught.
    """
    for y in (d.year - 1, d.year, d.year + 1):
        if d in federal_holidays(y):
            return True
    return False