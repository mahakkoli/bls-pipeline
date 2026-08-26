#!/usr/bin/env bash
set -euo pipefail

python -m ingestion.oews && python -m ingestion.ces
