import pandas as pd

for year in [2019, 2024]:
    filename = f"data/oews_historical/national_M{year}_dl.xlsx"
    print(f"\n=== {year} COLUMNS ===")
    df = pd.read_excel(filename, nrows=1)
    print(df.columns.tolist())