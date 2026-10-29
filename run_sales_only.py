import pandas as pd
from src.database import get_engine, create_vendor_summary, build_aggregates, get_row_count

engine = get_engine()

# Purani aadhi sales delete karo (agar bani ho toh)
with engine.begin() as conn:
    conn.exec_driver_sql("DROP TABLE IF EXISTS sales")

print("Loading sales with VendorNo -> vendornumber fix ...", flush=True)
total = 0
for i, chunk in enumerate(pd.read_csv("data/sales.csv", chunksize=100000, low_memory=False)):
    # saare column ke naam chhota + space hatake
    chunk.columns = [c.strip().lower().replace(" ", "") for c in chunk.columns]
    # VendorNo ko vendornumber banao
    if "vendorno" in chunk.columns:
        chunk = chunk.rename(columns={"vendorno": "vendornumber"})
    chunk.to_sql("sales", engine, if_exists="append", index=False)
    total += len(chunk)
    print(f"sales: {total} rows", flush=True)

print(f"DONE sales: {get_row_count('sales')} rows in DB", flush=True)

print("Creating vendor_summary ...", flush=True)
create_vendor_summary(engine, lambda d, t, m: print(m, flush=True))
print("vendor_summary:", get_row_count("vendor_summary"), flush=True)

print("Building aggregates ...", flush=True)
build_aggregates(engine, lambda d, t, m: print(m, flush=True))
print("ALL DONE!", flush=True)