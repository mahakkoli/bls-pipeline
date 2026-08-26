#!/usr/bin/env bash
set -euo pipefail

python -m ingestion.oews
python -m ingestion.ces
python -m ingestion.cpi
python -m ingestion.oews_historical

python verify_db.py

