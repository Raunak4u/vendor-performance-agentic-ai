"""
build_db.py  -  STEP 2: load the raw CSV / Excel files into one SQLite database.

What it does (in simple words):
  1. Reads DB_PATH and DATA_DIR from the .env file (no hardcoded paths).
  2. Looks inside the data folder and finds your 6 files by keyword
     (Sales, Purchases, PurchasePrices, BegInv, EndInv, VendorInvoice).
  3. Cleans the column names (removes spaces, fixes upper/lower case).
  4. Loads each file into its own table, 50,000 rows at a time,
     so even the 1.5 GB sales file does not fill your RAM.
  5. Prints the row count of every table and compares it with the expected count.

Run:
    python scripts/build_db.py                 # load everything
    python scripts/build_db.py --only sales    # reload just one table
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# The project folder = the folder that contains scripts/ and .env
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHUNK_SIZE = 50_000                     # rows read and written at a time
SUPPORTED = {".csv", ".xlsx", ".xls"}   # file types we can read
MIN_FREE_GB = 10                        # warn if the drive has less free space

# Expected columns and row counts for each table.
TABLES = {
    "sales": {
        "rows": 12_825_363,
        "columns": ["InventoryId", "Store", "Brand", "Description", "Size", "SalesQuantity",
                    "SalesDollars", "SalesPrice", "SalesDate", "Volume", "Classification",
                    "ExciseTax", "VendorNo", "VendorName"],
    },
    "purchases": {
        "rows": 2_372_474,
        "columns": ["InventoryId", "Store", "Brand", "Description", "Size", "VendorNumber",
                    "VendorName", "PONumber", "PODate", "ReceivingDate", "InvoiceDate",
                    "PayDate", "PurchasePrice", "Quantity", "Dollars", "Classification"],
    },
    "purchase_prices": {
        "rows": 12_261,
        "columns": ["Brand", "Description", "Price", "Size", "Volume", "Classification",
                    "PurchasePrice", "VendorNumber", "VendorName"],
    },
    "begin_inventory": {
        "rows": 206_529,
        "columns": ["InventoryId", "Store", "City", "Brand", "Description", "Size",
                    "onHand", "Price", "startDate"],
    },
    "end_inventory": {
        "rows": 224_489,
        "columns": ["InventoryId", "Store", "City", "Brand", "Description", "Size",
                    "onHand", "Price", "endDate"],
    },
    "vendor_invoice": {
        "rows": 5_543,
        "columns": ["VendorNumber", "VendorName", "InvoiceDate", "PONumber", "PODate",
                    "PayDate", "Quantity", "Dollars", "Freight", "Approval"],
    },
}


# ----------------------------------------------------------------- helpers
def resolve_path(value: str) -> Path:
    """Turn a path from .env into a full path. Relative paths start at the project folder."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def detect_table(filename: str):
    """Guess which table a file belongs to, using keywords in its name."""
    name = re.sub(r"[^a-z0-9]", "", Path(filename).stem.lower())   # 'Sales_FINAL.csv' -> 'salesfinal'
    if "summary" in name:                       # e.g. vendor_summary.csv is NOT raw data
        return None
    if "purchaseprice" in name:                 # must be checked before 'purchase'
        return "purchase_prices"
    if "invoice" in name:
        return "vendor_invoice"
    if name.startswith("beg") and "inv" in name:
        return "begin_inventory"
    if name.startswith("end") and "inv" in name:
        return "end_inventory"
    if "purchase" in name:
        return "purchases"
    if "sales" in name:
        return "sales"
    return None


def find_files(data_dir: Path) -> dict:
    """Return {table_name: file_path}. Folders, zip files and Excel temp files are skipped."""
    candidates = {}
    for f in data_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in SUPPORTED:
            continue                                        # skips folders and .zip/.pdf/.png
        if f.name.startswith(("~$", ".")):
            continue                                        # Excel temp files
        table = detect_table(f.name)
        if table:
            candidates.setdefault(table, []).append(f)

    chosen = {}
    for table, files in candidates.items():
        # Prefer .csv (faster), then the biggest file
        files.sort(key=lambda p: (p.suffix.lower() != ".csv", -p.stat().st_size))
        chosen[table] = files[0]
        for extra in files[1:]:
            print(f"   note: ignoring {extra.name} (using {files[0].name} for '{table}')")
    return chosen


def clean_frame(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Strip spaces from column names, fix their spelling/case, trim vendor names."""
    wanted = {c.lower(): c for c in TABLES[table]["columns"]}
    names = [str(c).replace("\ufeff", "").strip() for c in df.columns]
    df.columns = [wanted.get(n.lower(), n) for n in names]   # e.g. 'store' -> 'Store'
    if "VendorName" in df.columns:
        df["VendorName"] = df["VendorName"].str.strip()
    return df


def write_chunk(df: pd.DataFrame, table: str, conn, first: bool) -> None:
    """Write one chunk. The first chunk replaces any old table, the rest are appended."""
    df.to_sql(table, conn, if_exists="replace" if first else "append",
              index=False, chunksize=CHUNK_SIZE)


def check_columns(df: pd.DataFrame, table: str) -> None:
    missing = [c for c in TABLES[table]["columns"] if c not in df.columns]
    extra = [c for c in df.columns if c not in TABLES[table]["columns"]]
    if missing:
        print(f"   WARNING: missing columns in '{table}': {missing}")
    if extra:
        print(f"   note: extra columns kept in '{table}': {extra}")


def load_csv(path: Path, table: str, conn) -> None:
    """Read a CSV in chunks. Tries UTF-8 first, then Latin-1 if the file has odd characters."""
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            first, total, t0 = True, 0, time.time()
            reader = pd.read_csv(path, chunksize=CHUNK_SIZE, encoding=encoding,
                                 dtype={"Size": str})        # keep sizes like '1.75L' as text
            for i, chunk in enumerate(reader, start=1):
                chunk = clean_frame(chunk, table)
                if first:
                    check_columns(chunk, table)
                write_chunk(chunk, table, conn, first)
                first = False
                total += len(chunk)
                if i % 10 == 0:                              # progress every 500,000 rows
                    print(f"   ... {total:,} rows loaded ({time.time() - t0:.0f}s)")
            return
        except UnicodeDecodeError:
            print(f"   encoding {encoding} failed, trying another one...")
    raise RuntimeError(f"Could not read {path.name} with any known encoding")


def load_excel(path: Path, table: str, conn) -> None:
    """Excel files are read fully (Excel max is about 1 million rows), then written in chunks."""
    df = clean_frame(pd.read_excel(path, dtype={"Size": str}), table)
    check_columns(df, table)
    for i in range(0, max(len(df), 1), CHUNK_SIZE):
        write_chunk(df.iloc[i:i + CHUNK_SIZE], table, conn, first=(i == 0))


def count_rows(conn, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


# -------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="Load raw files into SQLite")
    parser.add_argument("--only", nargs="+", choices=list(TABLES),
                        help="load only these tables (default: all)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("DB_PATH"):
        print("ERROR: DB_PATH is missing. Add this line to your .env file:  DB_PATH=data/inventory.db")
        return 1
    db_path = resolve_path(os.environ["DB_PATH"])
    data_dir = resolve_path(os.getenv("DATA_DIR", "data"))

    if not data_dir.is_dir():
        print(f"ERROR: data folder not found: {data_dir}\nCreate it and copy your 6 files into it.")
        return 1

    print(f"Data folder : {data_dir}\nDatabase    : {db_path}\n")
    files = find_files(data_dir)
    wanted = args.only or list(TABLES)
    missing = [t for t in wanted if t not in files]

    print("Files detected:")
    for t in wanted:
        print(f"   {t:<16} <- {files[t].name if t in files else 'NOT FOUND'}")
    if missing:
        print(f"\nWARNING: no file found for: {', '.join(missing)}")
    if not any(t in files for t in wanted):
        print("Nothing to load. Check the file names inside the data folder.")
        return 1

    db_path.parent.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(db_path.parent).free / 1e9
    if free_gb < MIN_FREE_GB:
        print(f"\nWARNING: only {free_gb:.1f} GB free on this drive. The database can need several GB.")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = MEMORY")   # faster bulk loading
    conn.execute("PRAGMA synchronous = OFF")       # (safe here: you can simply re-run the script)

    results, t_start = [], time.time()
    for table in wanted:
        if table not in files:
            continue
        path = files[table]
        print(f"\nLoading '{table}' from {path.name} ...")
        t0 = time.time()
        try:
            if path.suffix.lower() == ".csv":
                load_csv(path, table, conn)
            else:
                load_excel(path, table, conn)
        except Exception as exc:                   # show the problem and carry on with other tables
            print(f"   ERROR while loading {table}: {exc}")
            results.append((table, None, TABLES[table]["rows"], time.time() - t0))
            continue
        n = count_rows(conn, table)
        results.append((table, n, TABLES[table]["rows"], time.time() - t0))
        print(f"   done: {n:,} rows in {time.time() - t0:.0f}s")
    conn.close()

    # ---- final report
    print("\n" + "=" * 70)
    print(f"{'Table':<17}{'Rows loaded':>14}{'Expected':>14}   Status")
    print("-" * 70)
    problems = len(missing)
    for table, n, expected, _ in results:
        if n is None:
            status, shown = "FAILED", "-"
            problems += 1
        elif n == 0:
            status, shown = "EMPTY - check the file", f"{n:,}"
            problems += 1
        elif abs(n - expected) / expected <= 0.01:
            status, shown = "OK", f"{n:,}"
        else:
            status, shown = "differs from expected", f"{n:,}"
        print(f"{table:<17}{shown:>14}{expected:>14,}   {status}")
    print("=" * 70)
    print(f"Total time: {(time.time() - t_start) / 60:.1f} minutes")
    if problems:
        print("Some tables need attention (see above). Fix and re-run with:  --only <table>")
        return 1
    print("All tables loaded. Next: python scripts/build_summary.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
