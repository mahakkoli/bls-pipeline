#!/usr/bin/env bash
set -euo pipefail

python -m ingestion.oews
python -m ingestion.ces
python -m ingestion.cpi
python -m ingestion.oews_historical

python -c "
import duckdb
conn = duckdb.connect('data/bls.duckdb')
tables = conn.execute('SHOW TABLES').fetchdf()
print('=== DB VERIFICATION ===')
for t in tables['name']:
    count = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {count} rows')
print('=== VERIFICATION COMPLETE ===')
"

