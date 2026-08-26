"""Ingest CPI (Consumer Price Index) data from the BLS Public Data API
into DuckDB.

CPI-U publishes a monthly, not-seasonally-adjusted price index for the
US city average. Unlike OEWS/CES, series IDs here aren't built from
component codes — these are four of BLS's well-known standard CPI-U
item series, pulled directly:

    CUUR0000SA0  All items (overall inflation)
    CUUR0000SAH  Shelter
    CUUR0000SAM  Medical care
    CUUR0000SAF  Food

Reference: https://download.bls.gov/pub/time.series/cu/cu.txt
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from ingestion.bls_client import fetch_series
from ingestion.db import DEFAULT_DB_PATH, get_connection

DEFAULT_START_YEAR = 2019

# series_id -> human-readable category
SERIES: dict[str, str] = {
    "CUUR0000SA0": "All Items",
    "CUUR0000SAH": "Shelter",
    "CUUR0000SAM": "Medical Care",
    "CUUR0000SAF": "Food",
}

TABLE_NAME = "cpi_data"


def normalize_records(
    api_results: list[dict[str, Any]],
    series: dict[str, str] = SERIES,
) -> pd.DataFrame:
    ingested_at = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []

    for s in api_results:
        series_id = s.get("seriesID", "")
        category = series.get(series_id)
        if category is None:
            continue  # unrecognized series, skip rather than guess at its meaning

        for point in s.get("data", []):
            period = point.get("period", "")
            if not period.startswith("M") or period == "M13":
                continue  # skip the annual-average pseudo-period, keep real months only

            raw_value = point.get("value")
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = None  # BLS uses non-numeric placeholders for suppressed data

            rows.append(
                {
                    "series_id": series_id,
                    "category": category,
                    "year": int(point.get("year")),
                    "month": int(period[1:]),
                    "period_name": point.get("periodName"),
                    "value": value,
                    "ingested_at": ingested_at,
                }
            )

    return pd.DataFrame(rows)


def load_to_duckdb(df: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Load normalized CPI rows into DuckDB, replacing any prior rows for
    the same (series_id, year, month) so reruns don't accumulate
    duplicates and preliminary values get overwritten by revisions."""
    con = get_connection(db_path)
    try:
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                series_id    VARCHAR,
                category     VARCHAR,
                year         INTEGER,
                month        INTEGER,
                period_name  VARCHAR,
                value        DOUBLE,
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
            WHERE (series_id, year, month) IN (
                SELECT series_id, year, month FROM new_rows
            )
            """
        )
        con.execute(f"INSERT INTO {TABLE_NAME} SELECT * FROM new_rows")
        con.unregister("new_rows")

        return len(df)
    finally:
        con.close()


def run(start_year: int, end_year: int, db_path: Path = DEFAULT_DB_PATH) -> int:
    load_dotenv()
    api_key = os.environ.get("BLS_API_KEY")
    if not api_key:
        raise RuntimeError("BLS_API_KEY not set (expected in .env or the environment)")

    series_ids = list(SERIES.keys())
    print(f"Fetching {len(series_ids)} CPI series for {start_year}-{end_year}...")

    api_results = fetch_series(series_ids, start_year, end_year, api_key)
    df = normalize_records(api_results)

    rows_loaded = load_to_duckdb(df, db_path)

    if df.empty:
        print("Loaded 0 rows — no data returned")
    else:
        ym = df["year"] * 100 + df["month"]
        start_row = df.loc[ym.idxmin()]
        end_row = df.loc[ym.idxmax()]
        print(f"Loaded {rows_loaded} rows into {db_path}::{TABLE_NAME}")
        print(
            f"Date range: {int(start_row['year'])}-{int(start_row['month']):02d} "
            f"to {int(end_row['year'])}-{int(end_row['month']):02d}"
        )
        print(f"Categories loaded: {', '.join(sorted(df['category'].unique()))}")

    return rows_loaded


def main() -> None:
    current_year = datetime.now().year
    parser = argparse.ArgumentParser(description="Ingest BLS CPI data into DuckDB")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=current_year)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    run(args.start_year, args.end_year, args.db_path)


if __name__ == "__main__":
    main()
