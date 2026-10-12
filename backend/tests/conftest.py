"""Shared fixtures.

The seed export used here is the real CFTC file, not a synthetic stub: these
tests exist to pin behaviour against the real quirks (long-form dates, thousand
separators, trailing spaces, shuffled row order, empty vs zero).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.analysis.indicators import CALCULATION_VERSION, IndicatorParams
from backend.config import RAW_CFTC_DIR, REPORT_TYPE_DISAGGREGATED
from backend.data.cftc_import import run_import
from backend.db import init_db, open_con


@pytest.fixture(scope="session")
def seed_csv() -> Path:
    files = sorted(RAW_CFTC_DIR.glob("*.csv"))
    if not files:
        pytest.skip(f"no seed export found in {RAW_CFTC_DIR}")
    return files[0]


@pytest.fixture(scope="session")
def imported(tmp_path_factory: pytest.TempPathFactory, seed_csv: Path) -> dict:
    """A freshly imported database, shared by every read-only test."""
    db = tmp_path_factory.mktemp("goldcot") / "test.sqlite"
    init_db(db)
    con = open_con(db)
    try:
        summary = run_import(con, seed_csv, report_type=REPORT_TYPE_DISAGGREGATED)
    finally:
        con.close()
    return {"db": db, "csv": seed_csv, "summary": summary}


@pytest.fixture()
def con(imported: dict):
    connection = open_con(imported["db"])
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def indicated(imported: dict) -> dict:
    """The same database with the default indicator version computed."""
    from scripts.recompute_indicators import recompute

    connection = open_con(imported["db"])
    try:
        recompute(
            connection,
            REPORT_TYPE_DISAGGREGATED,
            None,
            IndicatorParams(),
            CALCULATION_VERSION,
        )
    finally:
        connection.close()
    return imported