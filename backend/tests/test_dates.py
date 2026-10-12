"""Date handling: parsing, publication-time derivation and point-in-time reads.

The dates are where a COT tool is easiest to get quietly wrong, so they get the
most tests: the source uses a long-form date that sorts alphabetically, the
publication date has to be *derived*, and a read must never see a report that
was not public yet.
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
import pytest

from backend.data.normalize import (
    REPORT_DATE_COL,
    ImportValidationError,
    derive_published_at,
    parse_cftc_date,
    read_seed_csv,
)
from backend.db import iso, pit_clause, resolve_as_of


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parse_cftc_date_accepts_official_long_form():
    values = pd.Series(["2022 Aug 02 12:00:00 AM", "2006 Jun 13 12:00:00 AM"])
    assert list(parse_cftc_date(values)) == [date(2022, 8, 2), date(2006, 6, 13)]


def test_parse_cftc_date_rejects_iso_form():
    """The column is *named* YYYY_MM_DD but holds a long-form value; ISO input
    must fail loudly rather than be silently re-interpreted."""
    with pytest.raises(ImportValidationError) as excinfo:
        parse_cftc_date(pd.Series(["2022-08-02"]))
    assert excinfo.value.rule == "R1_date_format"


def test_string_sort_is_not_chronological(seed_csv):
    """Regression: sorting the raw source column alphabetically ≠ chronologically.

    This is exactly why the stored layer normalises to ISO and every read orders
    by the stored date, never by the source string.
    """
    raw = read_seed_csv(seed_csv)
    values = raw[REPORT_DATE_COL].astype(str)
    dates = list(parse_cftc_date(raw[REPORT_DATE_COL]))

    by_string = list(values.sort_values())
    by_date = [d.isoformat() for d in sorted(dates)]
    assert by_string != by_date

    # A concrete demonstration: the alphabetically-last value is not the newest.
    newest = parse_cftc_date(pd.Series([values.sort_values().iloc[-1]]))[0]
    assert newest != max(dates)


def test_stored_report_dates_sort_chronologically(con):
    stored = [
        r["report_date"]
        for r in con.execute(
            "SELECT report_date FROM cot_weekly WHERE market_code = '088691' "
            "ORDER BY report_date"
        )
    ]
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in stored)
    assert stored == sorted(stored)
    assert stored[0] == "2006-06-13"


# --------------------------------------------------------------------------
# Publication time
# --------------------------------------------------------------------------


def test_published_at_tuesday_maps_to_same_week_friday():
    # 2026-10-06 is a Tuesday; the report goes out Friday 2026-10-09 15:30 ET.
    assert derive_published_at(date(2026, 10, 6)).isoformat() == "2026-10-09T19:30:00+00:00"


def test_published_at_shifts_off_a_holiday_friday():
    # 2025-07-04 (Friday) is Independence Day, so the release slides to Monday.
    assert derive_published_at(date(2025, 7, 1)).isoformat() == "2025-07-07T19:30:00+00:00"
    # 2026-12-25 (Friday) is Christmas, and it also crosses the DST boundary.
    assert derive_published_at(date(2026, 12, 22)).isoformat() == "2026-12-28T20:30:00+00:00"


def test_published_at_never_lands_on_a_weekend():
    for month in range(1, 13):
        published = derive_published_at(date(2024, month, 2))
        assert published.weekday() < 5


def test_dst_boundary_offset_is_1930z_in_summer_and_2030z_in_winter():
    summer = derive_published_at(date(2024, 6, 4))   # EDT, UTC-4
    winter = derive_published_at(date(2024, 12, 3))  # EST, UTC-5
    assert summer.strftime("%H:%M") == "19:30"
    assert winter.strftime("%H:%M") == "20:30"


def test_every_stored_report_was_published_after_its_report_date(con):
    rows = list(
        con.execute(
            "SELECT report_date, published_at, published_at_source FROM cot_weekly"
        )
    )
    assert len(rows) == 1233
    assert {r["published_at_source"] for r in rows} == {"derived"}
    for row in rows:
        assert row["published_at"] > row["report_date"], row


# --------------------------------------------------------------------------
# Point-in-time
# --------------------------------------------------------------------------


def _latest_visible(con, market_code: str, as_of: str, pit_mode: str) -> str:
    moment = resolve_as_of(as_of)
    clause, extra = pit_clause(pit_mode)
    row = con.execute(
        f"SELECT report_date FROM cot_weekly w "
        f"WHERE w.report_type = 'disaggregated_futures_only' AND w.market_code = :code "
        f"AND {clause} ORDER BY w.report_date DESC LIMIT 1",
        {"code": market_code, "as_of": iso(moment), **extra},
    ).fetchone()
    assert row is not None
    return row["report_date"]


def test_as_of_excludes_rows_not_yet_public(con):
    """2021-12-29 sits between the report date (12-28) and its release (12-31).

    Under the default mode the 12-28 report must be invisible; only the
    ``report_date`` mode — explicitly flagged as forward-biased — sees it.
    """
    assert _latest_visible(con, "088691", "2021-12-29", "published_at") == "2021-12-21"
    assert _latest_visible(con, "088691", "2021-12-29", "report_date") == "2021-12-28"
    # The conservative mode is at least as strict as the derived publication date.
    assert _latest_visible(con, "088691", "2021-12-29", "conservative") == "2021-12-21"


def test_as_of_returns_the_last_report_at_or_before_the_moment(con):
    assert _latest_visible(con, "088691", "2022-01-01", "published_at") == "2021-12-28"
    assert _latest_visible(con, "088691", "2026-10-10", "published_at") == "2026-10-06"
    # Nothing is visible before the first report was ever published.
    with pytest.raises(AssertionError):
        _latest_visible(con, "088691", "2006-01-01", "published_at")


def test_pit_clause_rejects_unknown_mode():
    with pytest.raises(ValueError):
        pit_clause("whatever")


def test_resolve_as_of_defaults_to_now_and_parses_z_suffix():
    assert resolve_as_of(None).tzinfo is not None
    assert resolve_as_of("2022-01-01T00:00:00Z").isoformat() == "2022-01-01T00:00:00+00:00"
    assert resolve_as_of("2022-01-01").isoformat() == "2022-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        resolve_as_of("not-a-date")