"""Print a summary of every table in data/bls.duckdb: name and row count.

Run at the end of build.sh so Railway's build logs show exactly what
ended up in the database after every deploy.
"""

import duckdb

from ingestion.db import DEFAULT_DB_PATH

conn = duckdb.connect(str(DEFAULT_DB_PATH), read_only=True)
tables = conn.execute("SHOW TABLES").fetchdf()

for table in tables["name"]:
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"{table}: {count} rows")

conn.close()
