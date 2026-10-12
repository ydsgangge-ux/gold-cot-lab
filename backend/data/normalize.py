"""Parsing and field mapping: official CFTC export -> internal schema.

Two rules drive everything here:

* The raw layer keeps **every** official column under its original name, with
  the original string value, so ``""`` (missing) and ``"0"`` stay distinguishable.
* Only ``_All`` columns are mapped into the normalised layer. ``_Old`` / ``_Other``
  are a file layout artefact, not a semantic distinction, and the internal field
  names must not mirror the official ones (which are inconsistent: note
  ``Swap_Positions_Long_All`` uses one underscore while
  ``Swap__Positions_Short_All`` uses two).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from ..config import (
    PUBLISH_HOUR_ET,
    PUBLISH_MINUTE_ET,
    is_federal_holiday,
)

COT_DATE_FORMAT = "%Y %b %d %I:%M:%S %p"  # "2022 Aug 02 12:00:00 AM"
NY_TZ = ZoneInfo("America/New_York")

MISSING_TOKENS = {"", "-", "--", "NA", "N/A", "n/a", "null", "NULL", "None", "none"}

REPORT_DATE_COL = "Report_Date_as_YYYY_MM_DD"
MARKET_CODE_COL = "CFTC_Contract_Market_Code"


class ImportValidationError(Exception):
    """Raised when source data cannot be parsed. Carries sample evidence."""

    def __init__(self, rule: str, message: str, samples: Iterable[str] = ()) -> None:
        self.rule = rule
        self.samples = list(samples)[:10]
        detail = f"[{rule}] {message}"
        if self.samples:
            detail += " | samples: " + "; ".join(self.samples)
        super().__init__(detail)


# --------------------------------------------------------------------------
# Field mapping
# --------------------------------------------------------------------------

# official column name -> internal field name (normalised layer)
FIELD_MAP: dict[str, str] = {
    # identity / descriptive
    MARKET_CODE_COL: "market_code",
    "Market_and_Exchange_Names": "market_name",
    "Contract_Market_Name": "contract_name",
    "CFTC_Market_Code": "exchange_code",
    "CFTC_Region_Code": "region_code",
    "CFTC_Commodity_Code": "commodity_code",
    "Contract_Units": "contract_units",
    "YYYY Report Week WW": "report_week_label",
    "ID": "cftc_id",
    REPORT_DATE_COL: "report_date",
    # core positions (All only)
    "Open_Interest_All": "open_interest",
    "Prod_Merc_Positions_Long_All": "prod_merc_long",
    "Prod_Merc_Positions_Short_All": "prod_merc_short",
    "Swap_Positions_Long_All": "swap_long",
    "Swap__Positions_Short_All": "swap_short",       # double underscore in source
    "Swap__Positions_Spread_All": "swap_spread",     # double underscore in source
    "M_Money_Positions_Long_All": "mm_long",
    "M_Money_Positions_Short_All": "mm_short",
    "M_Money_Positions_Spread_All": "mm_spread",
    "Other_Rept_Positions_Long_All": "other_rept_long",
    "Other_Rept_Positions_Short_All": "other_rept_short",
    "Other_Rept_Positions_Spread_All": "other_rept_spread",
    "Tot_Rept_Positions_Long_All": "tot_rept_long",
    "Tot_Rept_Positions_Short_All": "tot_rept_short",
    "NonRept_Positions_Long_All": "nonrept_long",
    "NonRept_Positions_Short_All": "nonrept_short",
    # concentration (All group)
    "Conc_Gross_LE_4_TDR_Long_All": "conc_gross_le4_long",
    "Conc_Gross_LE_4_TDR_Short_All": "conc_gross_le4_short",
    "Conc_Gross_LE_8_TDR_Long_All": "conc_gross_le8_long",
    "Conc_Gross_LE_8_TDR_Short_All": "conc_gross_le8_short",
    "Conc_Net_LE_4_TDR_Long_All": "conc_net_le4_long",
    "Conc_Net_LE_4_TDR_Short_All": "conc_net_le4_short",
    "Conc_Net_LE_8_TDR_Long_All": "conc_net_le8_long",
    "Conc_Net_LE_8_TDR_Short_All": "conc_net_le8_short",
    # reportable trader counts (All group)
    "Traders_Tot_All": "traders_tot",
    "Traders_Prod_Merc_Long_All": "traders_prod_long",
    "Traders_Prod_Merc_Short_All": "traders_prod_short",
    "Traders_Swap_Long_All": "traders_swap_long",
    "Traders_Swap_Short_All": "traders_swap_short",
    "Traders_Swap_Spread_All": "traders_swap_spread",
    "Traders_M_Money_Long_All": "traders_mm_long",
    "Traders_M_Money_Short_All": "traders_mm_short",
    "Traders_M_Money_Spread_All": "traders_mm_spread",
    "Traders_Other_Rept_Long_All": "traders_other_long",
    "Traders_Other_Rept_Short_All": "traders_other_short",
    "Traders_Other_Rept_Spread_All": "traders_other_spread",
}

# Unmapped on purpose and kept only in cot_raw.raw_json:
#   - every ``_Old`` and ``_Other`` column (file layout, not semantics)
#   - every ``Change_in_*`` and ``Pct_of_OI_*`` column (exactly recomputable from
#     the ordered series; storing them twice invites silent divergence)
#   - ``FutOnly_or_Combined``, ``COMMODITY_*``, ``CFTC_SubGroup_Code`` (constant
#     or derivable; still preserved in the raw blob)

# Descriptive (non-numeric) internal fields.
TEXT_FIELDS = (
    "market_code",
    "market_name",
    "contract_name",
    "exchange_code",
    "region_code",
    "commodity_code",
    "contract_units",
    "report_week_label",
    "cftc_id",
)

# Numeric internal fields, in the order they appear in cot_weekly.
NUMERIC_FIELDS = (
    "open_interest",
    "prod_merc_long",
    "prod_merc_short",
    "swap_long",
    "swap_short",
    "swap_spread",
    "mm_long",
    "mm_short",
    "mm_spread",
    "other_rept_long",
    "other_rept_short",
    "other_rept_spread",
    "tot_rept_long",
    "tot_rept_short",
    "nonrept_long",
    "nonrept_short",
    "noncomm_long",
    "noncomm_short",
    "noncomm_spread",
    "comm_long",
    "comm_short",
    "conc_gross_le4_long",
    "conc_gross_le4_short",
    "conc_gross_le8_long",
    "conc_gross_le8_short",
    "conc_net_le4_long",
    "conc_net_le4_short",
    "conc_net_le8_long",
    "conc_net_le8_short",
    "traders_tot",
    "traders_prod_long",
    "traders_prod_short",
    "traders_swap_long",
    "traders_swap_short",
    "traders_swap_spread",
    "traders_mm_long",
    "traders_mm_short",
    "traders_mm_spread",
    "traders_other_long",
    "traders_other_short",
    "traders_other_spread",
)

# The subset that must be present for a row to be analytically useful.
REQUIRED_NUMERIC_FIELDS = (
    "open_interest",
    "mm_long",
    "mm_short",
    "prod_merc_long",
    "prod_merc_short",
    "swap_long",
    "swap_short",
)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def read_seed_csv(path: Path | str) -> pd.DataFrame:
    """Read an official CFTC export keeping every value as its original string.

    ``keep_default_na=False`` is essential: it keeps ``""`` as ``""`` instead of
    turning it into NaN, which is what lets the missing/zero distinction survive
    all the way into ``cot_raw.raw_json``.
    """
    df = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
        low_memory=False,
    )
    df.columns = [c.strip() for c in df.columns]
    return df


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_cftc_date(values: pd.Series) -> pd.Series:
    """Parse ``"2022 Aug 02 12:00:00 AM"`` into a ``date``.

    Deliberately strict: the explicit format means a change of upstream format
    raises instead of silently producing wrong orderings.
    """
    parsed = pd.to_datetime(values, format=COT_DATE_FORMAT, errors="coerce")
    bad = parsed.isna()
    if bool(bad.any()):
        samples = [
            f"row {i + 2}: {values.iloc[i]!r}"  # +2 = 1 header + 1-based index
            for i in bad.to_numpy().nonzero()[0][:10]
        ]
        raise ImportValidationError(
            "R1_date_format",
            f"{int(bad.sum())} value(s) do not match {COT_DATE_FORMAT!r}",
            samples,
        )
    return parsed.dt.date


def is_missing_token(value: Any) -> bool:
    """True for a genuinely absent value. Deliberately false for ``0`` and ``"0"``."""
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() in MISSING_TOKENS


def parse_int_cell(value: Any) -> int | None:
    """Parse one cell into an int, distinguishing missing from zero."""
    if is_missing_token(value):
        return None
    text = str(value).strip().replace(",", "")
    try:
        return int(text)
    except ValueError as exc:
        raise ImportValidationError(
            "R3_numeric", f"cannot parse {value!r} as an integer"
        ) from exc


def parse_cftc_int(values: pd.Series) -> pd.Series:
    """Vectorised integer parsing returning a nullable ``Int64`` series."""
    text = values.astype("string").str.strip()
    missing = text.isin(MISSING_TOKENS)
    cleaned = text.str.replace(",", "", regex=False)
    numeric = pd.to_numeric(cleaned.where(~missing), errors="coerce")
    bad = numeric.isna() & ~missing
    if bool(bad.any()):
        samples = [
            f"row {i + 2}: {values.iloc[i]!r}"
            for i in bad.to_numpy().nonzero()[0][:10]
        ]
        raise ImportValidationError(
            "R3_numeric",
            f"{int(bad.sum())} numeric value(s) are not integers",
            samples,
        )
    return numeric.round().astype("Int64")


# --------------------------------------------------------------------------
# Publication time
# --------------------------------------------------------------------------


def derive_published_at(report_date: date) -> datetime:
    """Derive the publication timestamp for a report date.

    This is a **rule-based derivation, not real data**: the CFTC normally
    releases, at ~15:30 ET on the Friday of the report week, the positions held
    as of the preceding Tuesday. Therefore the derived value is only a lower
    bound on when the report could have been visible.
    """
    weekday = report_date.weekday()  # 0=Mon ... 4=Fri
    delta = (4 - weekday) % 7
    if delta == 0:
        delta = 7  # a Friday report date belongs to the following week's release
    published = report_date + timedelta(days=delta)
    while published.weekday() >= 5 or is_federal_holiday(published):
        published += timedelta(days=1)
    local = datetime(
        published.year,
        published.month,
        published.day,
        PUBLISH_HOUR_ET,
        PUBLISH_MINUTE_ET,
        tzinfo=NY_TZ,
    )
    return local.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------


def build_raw_json(row: pd.Series) -> str:
    """Serialise the full original row, keyed by official column names.

    ``sort_keys=True`` plus ``ensure_ascii=False`` makes the output stable, so
    the same logical row always hashes the same regardless of column order.
    """
    payload = {str(k): ("" if v is None else str(v)) for k, v in row.items()}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def map_to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame with parsed dates plus every mapped internal column.

    Internal fields with no counterpart in this report type (the Legacy
    ``noncomm_*`` / ``comm_*`` slots) are created as all-missing so downstream
    code can address them uniformly.
    """
    out = pd.DataFrame(index=df.index)
    out["report_date"] = parse_cftc_date(df[REPORT_DATE_COL])
    out["market_code"] = df[MARKET_CODE_COL].astype("string").str.strip()

    for official, internal in FIELD_MAP.items():
        if internal in TEXT_FIELDS and internal != "market_code":
            if official in df.columns:
                out[internal] = df[official].astype("string").str.strip()
            else:
                out[internal] = pd.Series([pd.NA] * len(df), index=df.index, dtype="string")

    for official, internal in FIELD_MAP.items():
        if internal in NUMERIC_FIELDS:
            if official in df.columns:
                out[internal] = parse_cftc_int(df[official])
            else:
                out[internal] = pd.Series([pd.NA] * len(df), index=df.index, dtype="Int64")

    for field in NUMERIC_FIELDS:
        if field not in out.columns:
            out[field] = pd.Series([pd.NA] * len(df), index=df.index, dtype="Int64")

    return out


def missing_numeric_fields(row: pd.Series) -> list[str]:
    """Internal names of mapped numeric fields whose source value was missing."""
    return [f for f in NUMERIC_FIELDS if is_missing_token(row.get(f))]


def canonical_payload(row: pd.Series) -> dict[str, Any]:
    """Stable payload used to detect revisions, built from the mapped fields."""
    payload: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        value = row.get(field)
        payload[field] = None if is_missing_token(value) else str(value).strip()
    for field in NUMERIC_FIELDS:
        value = row.get(field)
        payload[field] = None if is_missing_token(value) else int(value)
    return payload


def payload_hash(payload: dict[str, Any]) -> str:
    return sha256_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def row_hash(row: pd.Series) -> str:
    return payload_hash(canonical_payload(row))