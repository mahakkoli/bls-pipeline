"""Ingest CES (Current Employment Statistics) data from the BLS Public
Data API into DuckDB.

CES publishes monthly employment, hours, and earnings by industry
(national level). A series ID is built by concatenating:

    CE + seasonal(1) + industry_code(8) + datatype_code(2)

    - "CE"           survey abbreviation
    - seasonal       S = seasonally adjusted, U = not seasonally adjusted
    - industry_code  8-digit NAICS "tabcode"; first 2 digits are the
                      supersector code, e.g. "05000000" = Total private
    - datatype_code  2-digit metric selector (employment, earnings, etc.)

Reference: https://download.bls.gov/pub/time.series/ce/ce.txt

SECTORS below are the major supersector-level totals BLS publishes at
the national level, not the full ~800-series industry breakdown BLS
tracks. Extend the dict with additional NAICS tabcodes as needed (see
https://download.bls.gov/pub/time.series/ce/ce.industry for the full
list).

CES is seasonally adjusted by default here (SEASONAL_CODE = "S") since
that's the convention for headline month-over-month employment change
figures; not-seasonally-adjusted data is available by passing "U".
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

SEASONAL_CODE = "S"

# industry_code (8-digit NAICS tabcode) -> industry name
SECTORS: dict[str, str] = {
    "00000000": "Total nonfarm",
    "05000000": "Total private",
    "10000000": "Mining and logging",
    "20000000": "Construction",
    "30000000": "Manufacturing",
    "40000000": "Trade, transportation, and utilities",
    "42000000": "Retail trade",
    "50000000": "Information",
    "55000000": "Financial activities",
    "60000000": "Professional and business services",
    "65000000": "Private education and health services",
    "70000000": "Leisure and hospitality",
    "80000000": "Other services",
    "90000000": "Government",
}

# datatype_code -> (column name, unit)
DATATYPES: dict[str, str] = {
    "01": "all_employees_thousands",
    "02": "avg_weekly_hours",
    "03": "avg_hourly_earnings",
    "11": "avg_weekly_earnings",
}

TABLE_NAME = "ces_employment"


def build_series_id(industry_code: str, datatype_code: str, seasonal: str = SEASONAL_CODE) -> str:
    return f"CE{seasonal}{industry_code}{datatype_code}"


def build_series_catalog(
    sectors: dict[str, str] = SECTORS,
    datatypes: dict[str, str] = DATATYPES,
    seasonal: str = SEASONAL_CODE,
) -> dict[str, dict[str, str]]:
    """Map series_id -> metadata needed to interpret its data points."""
    catalog: dict[str, dict[str, str]] = {}
    for industry_code, industry_name in sectors.items():
        for datatype_code, metric in datatypes.items():
            series_id = build_series_id(industry_code, datatype_code, seasonal)
            catalog[series_id] = {
                "industry_code": industry_code,
                "industry_name": industry_name,
                "supersector_code": industry_code[:2],
                "seasonal_code": seasonal,
                "datatype_code": datatype_code,
                "metric": metric,
            }
    return catalog


def normalize_records(
    api_results: list[dict[str, Any]],
    catalog: dict[str, dict[str, str]],
) -> pd.DataFrame:
    ingested_at = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []

    for series in api_results:
        series_id = series.get("seriesID", "")
        meta = catalog.get(series_id)
        if meta is None:
            continue  # unrecognized series, skip rather than guess at its meaning

        for point in series.get("data", []):
            raw_value = point.get("value")
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = None  # BLS uses non-numeric placeholders for suppressed data

            rows.append(
                {
                    "series_id": series_id,
                    "industry_code": meta["industry_code"],
                    "industry_name": meta["industry_name"],
                    "supersector_code": meta["supersector_code"],
                    "seasonal_code": meta["seasonal_code"],
                    "datatype_code": meta["datatype_code"],
                    "metric": meta["metric"],
                    "year": int(point.get("year")),
                    "period": point.get("period"),
                    "period_name": point.get("periodName"),
                    "value": value,
                    "footnote_codes": ",".join(
                        fn.get("code", "") for fn in point.get("footnotes", []) if fn.get("code")
                    ),
                    "ingested_at": ingested_at,
                }
            )

    return pd.DataFrame(rows)


def load_to_duckdb(df: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Load normalized CES rows into DuckDB, replacing any prior rows for
    the same (series_id, year, period) so reruns don't accumulate
    duplicates and preliminary values get overwritten by revisions."""
    con = get_connection(db_path)
    try:
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                series_id         VARCHAR,
                industry_code     VARCHAR,
                industry_name     VARCHAR,
                supersector_code  VARCHAR,
                seasonal_code     VARCHAR,
                datatype_code     VARCHAR,
                metric            VARCHAR,
                year              INTEGER,
                period            VARCHAR,
                period_name       VARCHAR,
                value             DOUBLE,
                footnote_codes    VARCHAR,
                ingested_at       TIMESTAMP
            )
            """
        )

        if df.empty:
            return 0

        con.register("new_rows", df)
        con.execute(
            f"""
            DELETE FROM {TABLE_NAME}
            WHERE (series_id, year, period) IN (
                SELECT series_id, year, period FROM new_rows
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

    catalog = build_series_catalog()
    series_ids = list(catalog.keys())
    print(f"Fetching {len(series_ids)} CES series for {start_year}-{end_year}...")

    api_results = fetch_series(series_ids, start_year, end_year, api_key)
    df = normalize_records(api_results, catalog)

    rows_loaded = load_to_duckdb(df, db_path)
    print(f"Loaded {rows_loaded} rows into {db_path}::{TABLE_NAME}")
    return rows_loaded


def main() -> None:
    current_year = datetime.now().year
    parser = argparse.ArgumentParser(description="Ingest BLS CES data into DuckDB")
    parser.add_argument("--start-year", type=int, default=current_year - 5)
    parser.add_argument("--end-year", type=int, default=current_year)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    run(args.start_year, args.end_year, args.db_path)


if __name__ == "__main__":
    main()
