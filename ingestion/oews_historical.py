"""Load historical OEWS "National" flat files into DuckDB.

ingestion/oews.py pulls the current year via BLS's timeseries API, but
that API only ever exposes the latest published vintage (see
PROGRESS.md) — it has no history. BLS does publish prior years, just
not through that API: as downloadable "National" Excel workbooks, one
per year, at https://www.bls.gov/oes/tables.htm. This script loads
those files (saved locally to data/oews_historical/) to backfill
multi-year history the API alone can't provide.

Each national workbook covers the full BLS occupation hierarchy (total
-> major -> minor -> broad -> detailed) for every industry combined, at
the U.S. national level only. We keep just the detailed-occupation rows,
which is the finest-grained, most directly comparable level.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ingestion.db import DEFAULT_DB_PATH, get_connection

HISTORICAL_DIR = Path(__file__).resolve().parent.parent / "data" / "oews_historical"
FILENAME_PATTERN = re.compile(r"national_M(\d{4})_dl\.xlsx$")

# The national workbooks use area code 99 for their one U.S.-national row
# (not the 7-digit CBSA-style codes ingestion/oews.py's timeseries-API
# series IDs use) — confirmed directly against all 6 files, every row in
# every file is 99.
NATIONAL_AREA_CODE = 99

KEEP_COLUMNS = [
    "area",
    "area_title",
    "occ_code",
    "occ_title",
    "o_group",
    "tot_emp",
    "a_mean",
    "a_median",
    "a_pct10",
    "a_pct25",
    "a_pct75",
    "a_pct90",
]
NUMERIC_COLUMNS = [
    "tot_emp",
    "a_mean",
    "a_median",
    "a_pct10",
    "a_pct25",
    "a_pct75",
    "a_pct90",
]

TABLE_NAME = "oews_historical"


def discover_files() -> list[tuple[int, Path]]:
    """Find national_M<year>_dl.xlsx files and pull the year from each
    filename, rather than hardcoding a year range."""
    found = []
    for path in sorted(HISTORICAL_DIR.glob("national_M*_dl.xlsx")):
        match = FILENAME_PATTERN.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    return found


def load_year(year: int, path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    df.columns = [c.lower() for c in df.columns]

    df = df[df["area"] == NATIONAL_AREA_CODE]
    df = df[df["o_group"] == "detailed"]

    df = df[KEEP_COLUMNS].copy()
    df["year"] = year

    for col in NUMERIC_COLUMNS:
        # BLS uses '*' (not published) and '#' (wage >= $999,999.99 /
        # withheld) as suppression markers in these columns; coercing
        # turns both into NaN, which we load as NULL.
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def normalize_records() -> pd.DataFrame:
    files = discover_files()
    if not files:
        raise FileNotFoundError(
            f"No national_M<year>_dl.xlsx files found in {HISTORICAL_DIR}"
        )

    frames = [load_year(year, path) for year, path in files]
    df = pd.concat(frames, ignore_index=True)
    df["ingested_at"] = datetime.now(timezone.utc)
    return df


def load_to_duckdb(df: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Load normalized rows into DuckDB, replacing any prior rows for the
    same (occ_code, year) so reruns don't accumulate duplicates."""
    con = get_connection(db_path)
    try:
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                area         INTEGER,
                area_title   VARCHAR,
                occ_code     VARCHAR,
                occ_title    VARCHAR,
                o_group      VARCHAR,
                tot_emp      DOUBLE,
                a_mean       DOUBLE,
                a_median     DOUBLE,
                a_pct10      DOUBLE,
                a_pct25      DOUBLE,
                a_pct75      DOUBLE,
                a_pct90      DOUBLE,
                year         INTEGER,
                ingested_at  TIMESTAMP
            )
            """
        )

        if df.empty:
            return 0

        con.register("new_rows", df)
        con.execute(
            f"""
            DELETE FROM {TABLE_NAME}
            WHERE (occ_code, year) IN (
                SELECT occ_code, year FROM new_rows
            )
            """
        )
        con.execute(f"INSERT INTO {TABLE_NAME} SELECT * FROM new_rows")
        con.unregister("new_rows")

        return len(df)
    finally:
        con.close()


def run(db_path: Path = DEFAULT_DB_PATH) -> int:
    df = normalize_records()
    rows_loaded = load_to_duckdb(df, db_path)

    print(f"Loaded {rows_loaded} rows into {db_path}::{TABLE_NAME}")
    print(f"Year range: {df['year'].min()}-{df['year'].max()}")
    print(f"Distinct occupations: {df['occ_code'].nunique()}")

    return rows_loaded


if __name__ == "__main__":
    run()
