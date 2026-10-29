from pathlib import Path
from src.database import get_engine, load_csv, create_vendor_summary, build_aggregates, get_row_count

DATA = Path("data")
engine = get_engine()

for table in ["vendors", "products", "purchases", "sales"]:
    path = DATA / f"{table}.csv"
    print(f"Loading {table} from {path} ...", flush=True)
    load_csv(str(path), table, engine, lambda p, r: print(f"{table}: {r} rows", flush=True))
    print(f"DONE {table}: {get_row_count(table)} rows in DB", flush=True)

print("Creating vendor_summary ...", flush=True)
create_vendor_summary(engine, lambda d, t, m: print(m, flush=True))
print("vendor_summary:", get_row_count("vendor_summary"), flush=True)

print("Building aggregates ...", flush=True)
build_aggregates(engine, lambda d, t, m: print(m, flush=True))
print("ALL DONE!", flush=True)