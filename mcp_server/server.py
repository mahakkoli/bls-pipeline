"""MCP server exposing the BLS DuckDB warehouse to agent clients.

Runs over stdio for local development. Holds a single persistent DuckDB
connection to data/bls.duckdb, opened once at process startup, and
exposes three tools for an LLM client to explore the schema and query
the data: search_metadata, get_series_values, and query_database.
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from mcp_server.prompts import SYSTEM_PROMPT

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "bls.duckdb"

load_dotenv(PROJECT_ROOT / ".env")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("Warning: ANTHROPIC_API_KEY not set in .env or environment", flush=True)

DATA_TABLES = ("oews_wages", "ces_employment")

# information_schema has no notion of column comments here (the ingestion
# scripts don't set any), so column meaning is documented by hand and
# joined onto the live schema at query time.
COLUMN_DESCRIPTIONS: dict[str, str] = {
    "series_id": "BLS series identifier this row was sourced from",
    "datatype_code": "BLS numeric code for the metric, e.g. '01' = employment",
    "metric": "Human-readable name of the value being measured, e.g. 'employment', 'annual_mean_wage'",
    "year": "Calendar year of the observation",
    "period": "BLS period code: 'A01' for OEWS annual estimates, 'M01'-'M12' for CES months",
    "period_name": "Human-readable period, e.g. 'July' (CES only)",
    "value": "The numeric measurement itself; units depend on the `metric` column",
    "footnote_codes": "Comma-separated BLS footnote codes, e.g. 'P' for preliminary; empty string if none",
    "ingested_at": "UTC timestamp when this row was loaded by the ingestion pipeline",
    "area_code": "BLS CBSA code (7-digit, zero-padded), or '0000000' for national",
    "area_name": "Human-readable metro area name, or 'U.S. national'",
    "area_type": "BLS area-type code: 'M' = metro, 'N' = national",
    "occupation_code": "6-digit SOC occupation code with the dash removed",
    "occupation_title": "Human-readable occupation name",
    "industry_code": "8-digit NAICS 'tabcode'; the first 2 digits are the supersector code",
    "industry_name": "Human-readable industry/supersector name",
    "supersector_code": "2-digit CES supersector code (first 2 digits of industry_code)",
    "seasonal_code": "'S' = seasonally adjusted, 'U' = not seasonally adjusted",
}

# DuckDB file is single-writer; ingestion scripts and this server should
# not run concurrently against the same data/bls.duckdb file.
db = duckdb.connect(str(DB_PATH), read_only=True)
db_lock = threading.Lock()

# Surfaced to clients via the MCP `instructions` field (part of the
# initialize response) so a connecting LLM gets this guidance without the
# host application having to hardcode it separately.
mcp = MCPServer(name="bls-analyst", instructions=SYSTEM_PROMPT)


def _json_safe(value: Any) -> Any:
    """Coerce a DuckDB scalar into something json.dumps can handle."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@mcp.tool(
    description=(
        "Use this tool FIRST before writing any SQL query. Returns the full schema of "
        "available BLS tables including table names, column names, column descriptions, "
        "and example values. Call this whenever you are unsure what tables or columns "
        "exist, what valid values look like, or how the data is structured."
    )
)
def search_metadata() -> dict[str, Any]:
    try:
        tables: dict[str, list[dict[str, Any]]] = {}
        with db_lock:
            for table in DATA_TABLES:
                columns = db.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_name = ?
                    ORDER BY ordinal_position
                    """,
                    [table],
                ).fetchall()

                column_info = []
                for column_name, data_type in columns:
                    samples = db.execute(
                        f'SELECT DISTINCT "{column_name}" FROM {table} '
                        f'WHERE "{column_name}" IS NOT NULL LIMIT 3'
                    ).fetchall()
                    column_info.append(
                        {
                            "name": column_name,
                            "type": data_type,
                            "description": COLUMN_DESCRIPTIONS.get(column_name, ""),
                            "example_values": [_json_safe(row[0]) for row in samples],
                        }
                    )
                tables[table] = column_info

        return {"tables": tables}
    except Exception as exc:
        return {"error": f"search_metadata failed: {exc}"}


@mcp.tool(
    description=(
        "Use this tool to look up the exact BLS label for a location, occupation, or "
        "industry before querying. Users will type natural language like 'Chicago' or "
        "'data engineer' — this tool maps those to the exact values stored in the "
        "database. Always call this before filtering by location, occupation, or "
        "industry in a query_database call."
    )
)
def get_series_values(field: str, search_term: str) -> dict[str, Any]:
    try:
        with db_lock:
            column_rows = db.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_name IN (?, ?)
                """,
                list(DATA_TABLES),
            ).fetchall()

        tables_with_field = [t for t, c in column_rows if c == field]
        if not tables_with_field:
            valid_fields = sorted({c for _, c in column_rows})
            return {
                "error": f"Unknown field '{field}'. Valid fields are: {valid_fields}",
            }

        matches: list[dict[str, Any]] = []
        with db_lock:
            for table in tables_with_field:
                rows = db.execute(
                    f'SELECT DISTINCT "{field}" FROM {table} '
                    f'WHERE "{field}" ILIKE ? LIMIT 10',
                    [f"%{search_term}%"],
                ).fetchall()
                for (value,) in rows:
                    matches.append({"table": table, "value": _json_safe(value)})

        matches = matches[:10]
        return {"field": field, "search_term": search_term, "matches": matches}
    except Exception as exc:
        return {"error": f"get_series_values failed: {exc}"}


@mcp.tool(
    description=(
        "Executes a SQL query against the BLS DuckDB database and returns results as "
        "structured data. Use this to retrieve wages, employment figures, trends, and "
        "rankings. You may call this multiple times per user question to build a "
        "complete answer. Always use search_metadata and get_series_values before "
        "constructing your SQL."
    )
)
def query_database(sql: str) -> dict[str, Any]:
    try:
        cleaned = sql.strip().rstrip(";")
        # Wrapping as a subquery both enforces the row cap via LIMIT pushdown
        # and rejects non-SELECT statements (DDL/DML) as a syntax error.
        wrapped = f"SELECT * FROM ({cleaned}) AS _query_database_result LIMIT 100"

        with db_lock:
            cursor = db.execute(wrapped)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()

        results = [
            {col: _json_safe(val) for col, val in zip(columns, row)} for row in rows
        ]
        return {"row_count": len(results), "rows": results}
    except Exception as exc:
        return {"error": f"Query failed: {exc}"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
