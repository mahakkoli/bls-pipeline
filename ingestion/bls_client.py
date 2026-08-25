"""Shared client for the BLS Public Data API v2 (timeseries endpoint).

Used by both the OEWS and CES ingestion scripts, since they hit the same
API with the same batching/auth/retry rules.

Docs: https://www.bls.gov/developers/api_signature_v2.htm
"""

from __future__ import annotations

import time
from typing import Any

import requests

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# Registered (keyed) API limits: 50 series per request, 20-year span per
# request, 500 requests/day. Unregistered requests are capped much lower,
# so BLS_API_KEY is effectively required for any real pull.
MAX_SERIES_PER_REQUEST = 50
MAX_YEAR_SPAN = 20

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_series(
    series_ids: list[str],
    start_year: int,
    end_year: int,
    api_key: str,
) -> list[dict[str, Any]]:
    """Fetch one or more BLS series, batching to respect API limits.

    Returns the concatenated list of series result objects from
    `Results.series` across all batches, e.g.:
        [{"seriesID": "...", "data": [{"year": "2024", "period": "A01", ...}]}, ...]
    """
    if end_year - start_year + 1 > MAX_YEAR_SPAN:
        raise ValueError(
            f"Year span {start_year}-{end_year} exceeds BLS API limit of "
            f"{MAX_YEAR_SPAN} years per request; split the request."
        )

    all_series: list[dict[str, Any]] = []
    for batch in _chunk(series_ids, MAX_SERIES_PER_REQUEST):
        all_series.extend(_fetch_batch(batch, start_year, end_year, api_key))
    return all_series


def _fetch_batch(
    series_ids: list[str],
    start_year: int,
    end_year: int,
    api_key: str,
) -> list[dict[str, Any]]:
    payload = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
        "registrationkey": api_key,
    }

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                BLS_API_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            body = response.json()

            status = body.get("status")
            if status != "REQUEST_SUCCEEDED":
                messages = "; ".join(body.get("message", [])) or "no message"
                raise RuntimeError(f"BLS API request failed ({status}): {messages}")

            return body.get("Results", {}).get("series", [])

        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError(
        f"BLS API request failed after {MAX_RETRIES} attempts"
    ) from last_error
