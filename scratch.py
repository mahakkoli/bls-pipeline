import duckdb
import pandas as pd

conn = duckdb.connect("data/bls.duckdb")

# Export OEWS
oews = conn.execute("SELECT * FROM oews_wages").fetchdf()
oews.to_csv("oews_export.csv", index=False)
print(f"OEWS exported: {len(oews)} rows")

# Export CES
ces = conn.execute("SELECT * FROM ces_employment").fetchdf()
ces.to_csv("ces_export.csv", index=False)
print(f"CES exported: {len(ces)} rows")

print("\nDone. Open oews_export.csv and ces_export.csv in Excel.")