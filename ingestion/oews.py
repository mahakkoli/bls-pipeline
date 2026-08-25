"""Ingest OEWS (Occupational Employment and Wage Statistics) data from the
BLS Public Data API into DuckDB.

OEWS publishes one estimate per year (period "A01"), broken out by
metropolitan area and occupation. A series ID is built by concatenating:

    OE + U + area_type(1) + area_code(7) + industry_code(6) + occupation_code(6) + datatype(2)

    - "OE"          survey abbreviation
    - "U"           not seasonally adjusted (OEWS has no seasonal series)
    - area_type     M = metropolitan area, S = statewide, U = national
    - area_code     7-digit, zero-padded (CBSA code for metros)
    - industry_code 6-digit; "000000" = cross-industry (all industries)
    - occupation_code  6-digit SOC code with the dash removed, e.g. 151252
    - datatype_code 2-digit metric selector (employment, mean wage, etc.)

Reference: https://www.bls.gov/help/hlpforma.htm#OE

AREAS and OCCUPATIONS below are a small starter set, not the full BLS
taxonomy (BLS publishes ~400 metro areas and ~800 SOC occupation codes as
flat files on bls.gov, separate from the timeseries API). Extend these
dicts with additional codes as needed.
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

INDUSTRY_CODE_CROSS_INDUSTRY = "000000"

# area_code -> (area_name, area_type_code)
AREAS: dict[str, tuple[str, str]] = {
    "0000000": ("U.S. national", "N"),
    "35620": ("New York-Newark-Jersey City, NY-NJ-PA", "M"),
    "31080": ("Los Angeles-Long Beach-Anaheim, CA", "M"),
    "16980": ("Chicago-Naperville-Elgin, IL-IN-WI", "M"),
    "19100": ("Dallas-Fort Worth-Arlington, TX", "M"),
    "26420": ("Houston-The Woodlands-Sugar Land, TX", "M"),
    "47900": ("Washington-Arlington-Alexandria, DC-VA-MD-WV", "M"),
    "33100": ("Miami-Fort Lauderdale-Pompano Beach, FL", "M"),
    "12060": ("Atlanta-Sandy Springs-Alpharetta, GA", "M"),
    "38060": ("Phoenix-Mesa-Chandler, AZ", "M"),
    "41860": ("San Francisco-Oakland-Berkeley, CA", "M"),
    "42660": ("Seattle-Tacoma-Bellevue, WA", "M"),
    "14460": ("Boston-Cambridge-Newton, MA-NH", "M"),
}
# National series use a 7-zero area code with area_type "N" (areatype_code
# per oe.areatype: M = metro/nonmetro, N = national, S = state); metro
# (CBSA) codes are 5 digits, left-padded to 7 with zeros and area_type "M".

# occupation_code (SOC, no dash) -> occupation title
OCCUPATIONS: dict[str, str] = {
    "000000": "All occupations",
    "151252": "Software Developers",
    "151254": "Web Developers",
    "151211": "Computer Systems Analysts",
    "112021": "Marketing Managers",
    "131111": "Management Analysts",
    "292061": "Licensed Practical and Licensed Vocational Nurses",
    "251194": "Career/Technical Education Teachers, Postsecondary",
    "413021": "Insurance Sales Agents",
    "436014": "Secretaries and Administrative Assistants",
}

# datatype_code -> (column name, unit)
DATATYPES: dict[str, str] = {
    "01": "employment",
    "04": "annual_mean_wage",
    "13": "annual_median_wage",
    "03": "hourly_mean_wage",
    "08": "hourly_median_wage",
}

TABLE_NAME = "oews_wages"


def build_area_code(area_code: str) -> str:
    return area_code.zfill(7)


def build_series_id(area_code: str, area_type: str, occupation_code: str, datatype_code: str) -> str:
    return (
        "OE"
        "U"
        f"{area_type}"
        f"{build_area_code(area_code)}"
        f"{INDUSTRY_CODE_CROSS_INDUSTRY}"
        f"{occupation_code}"
        f"{datatype_code}"
    )


def build_series_catalog(
    areas: dict[str, tuple[str, str]] = AREAS,
    occupations: dict[str, str] = OCCUPATIONS,
    datatypes: dict[str, str] = DATATYPES,
) -> dict[str, dict[str, str]]:
    """Map series_id -> metadata needed to interpret its data points."""
    catalog: dict[str, dict[str, str]] = {}
    for area_code, (area_name, area_type) in areas.items():
        for occupation_code, occupation_title in occupations.items():
            for datatype_code, metric in datatypes.items():
                series_id = build_series_id(area_code, area_type, occupation_code, datatype_code)
                catalog[series_id] = {
                    "area_code": area_code,
                    "area_name": area_name,
                    "area_type": area_type,
                    "occupation_code": occupation_code,
                    "occupation_title": occupation_title,
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
                    "area_code": meta["area_code"],
                    "area_name": meta["area_name"],
                    "area_type": meta["area_type"],
                    "occupation_code": meta["occupation_code"],
                    "occupation_title": meta["occupation_title"],
                    "datatype_code": meta["datatype_code"],
                    "metric": meta["metric"],
                    "year": int(point.get("year")),
                    "period": point.get("period"),
                    "value": value,
                    "footnote_codes": ",".join(
                        fn.get("code", "") for fn in point.get("footnotes", []) if fn.get("code")
                    ),
                    "ingested_at": ingested_at,
                }
            )

    return pd.DataFrame(rows)


def load_to_duckdb(df: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Load normalized OEWS rows into DuckDB, replacing any prior rows for
    the same (series_id, year) so reruns don't accumulate duplicates."""
    con = get_connection(db_path)
    try:
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                series_id         VARCHAR,
                area_code         VARCHAR,
                area_name         VARCHAR,
                area_type         VARCHAR,
                occupation_code   VARCHAR,
                occupation_title  VARCHAR,
                datatype_code     VARCHAR,
                metric            VARCHAR,
                year              INTEGER,
                period            VARCHAR,
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
            WHERE (series_id, year) IN (
                SELECT series_id, year FROM new_rows
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
    print(f"Fetching {len(series_ids)} OEWS series for {start_year}-{end_year}...")

    api_results = fetch_series(series_ids, start_year, end_year, api_key)
    df = normalize_records(api_results, catalog)

    rows_loaded = load_to_duckdb(df, db_path)
    print(f"Loaded {rows_loaded} rows into {db_path}::{TABLE_NAME}")
    return rows_loaded


def main() -> None:
    current_year = datetime.now().year
    parser = argparse.ArgumentParser(description="Ingest BLS OEWS data into DuckDB")
    parser.add_argument("--start-year", type=int, default=current_year - 1)
    parser.add_argument("--end-year", type=int, default=current_year - 1)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    run(args.start_year, args.end_year, args.db_path)


if __name__ == "__main__":
    main()
