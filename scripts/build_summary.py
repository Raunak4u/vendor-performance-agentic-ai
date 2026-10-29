"""
build_summary.py  -  STEP 3: build the small, fast vendor.db the app will use.

Why this script exists (simple English):
  The big inventory.db has 12.8 million sales rows. Searching it for every question
  would be slow, and joining sales to purchases row-by-row makes purchase dollars
  get counted many times (the "join inflation" bug in the old notebook).

What it does:
  1. Opens inventory.db READ-ONLY (it can never change your raw data).
  2. Adds up SALES by vendor + brand.       (separately)
  3. Adds up PURCHASES by vendor + brand.   (separately)
  4. Adds up FREIGHT from vendor_invoice by vendor only.
  5. Merges the small results in pandas, then calculates profit and ratios.
  6. Saves 4 small tables into vendor.db:
        vendor_sales_summary  (vendor x brand)
        monthly_vendor_sales  (month x vendor x classification)
        vendor_freight        (one row per vendor; the source of truth for freight)
        meta                  (latest sales date, build time, ...)
  7. Checks the totals against your expected KPIs and prints warnings if they differ.

Run:  python scripts/build_summary.py
"""
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# KPIs you expect for 2024 (USD). Used only to WARN if the data looks different.
EXPECTED = {"sales": 343_600_000, "purchases": 321_900_000, "profit": 21_650_000, "margin": 6.3}
TOLERANCE = 0.01          # 1% allowed difference for money values
MARGIN_TOLERANCE = 0.1    # 0.1 percentage points allowed for margin

# ------------------------------------------------------------------ SQL
# Each table is added up ON ITS OWN. They are never joined together in SQL.
SALES_SQL = """
SELECT VendorNo AS VendorNumber, VendorName, Brand,
       MAX(Description)    AS Description,
       MAX(Classification) AS Classification,
       SUM(SalesDollars)   AS TotalSalesDollars,
       SUM(SalesQuantity)  AS TotalSalesQuantity,
       SUM(ExciseTax)      AS TotalExciseTax
FROM sales
GROUP BY VendorNo, VendorName, Brand
"""

PURCHASES_SQL = """
SELECT VendorNumber, VendorName, Brand,
       MAX(Description)    AS Description,
       MAX(Classification) AS Classification,
       SUM(Dollars)        AS TotalPurchaseDollars,
       SUM(Quantity)       AS TotalPurchaseQuantity
FROM purchases
GROUP BY VendorNumber, VendorName, Brand
"""

# Freight comes ONLY from vendor_invoice, one row per vendor.
FREIGHT_SQL = """
SELECT VendorNumber, VendorName,
       SUM(Freight) AS FreightCost,
       COUNT(*)     AS InvoiceCount,
       SUM(Dollars) AS InvoiceDollars
FROM vendor_invoice
GROUP BY VendorNumber, VendorName
"""

# Month comes from SalesDate. FirstDate/LastDate are only used to find the data range.
MONTHLY_SQL = """
SELECT strftime('%Y-%m', SalesDate) AS Month,
       VendorNo AS VendorNumber,
       Classification,
       SUM(SalesDollars)  AS SalesDollars,
       SUM(SalesQuantity) AS SalesQuantity,
       MIN(SalesDate)     AS FirstDate,
       MAX(SalesDate)     AS LastDate
FROM sales
GROUP BY Month, VendorNo, Classification
"""


# -------------------------------------------------------------- helpers
def resolve_path(value: str) -> Path:
    """Relative paths in .env start at the project folder."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def money(x: float) -> str:
    return f"${x:,.0f}"


def timed_query(conn, label: str, sql: str) -> pd.DataFrame:
    t0 = time.time()
    print(f"   {label} ...", end=" ", flush=True)
    df = pd.read_sql_query(sql, conn)
    print(f"{len(df):,} rows ({time.time() - t0:.0f}s)")
    return df


def to_number(df: pd.DataFrame, cols) -> pd.DataFrame:
    """Make sure id columns are numbers; drop rows where they are missing."""
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    bad = df[cols].isna().any(axis=1).sum()
    if bad:
        print(f"   WARNING: dropped {bad} rows with a missing {'/'.join(cols)}")
    df = df.dropna(subset=cols).copy()
    for c in cols:
        df[c] = df[c].astype("int64")
    return df


def pick_vendor_names(frames) -> pd.DataFrame:
    """One name per VendorNumber: the spelling with the most dollars behind it."""
    parts = []
    for df, dollar_col in frames:
        part = df[["VendorNumber", "VendorName", dollar_col]].copy()
        part["w"] = part[dollar_col].abs()
        parts.append(part[["VendorNumber", "VendorName", "w"]])
    names = pd.concat(parts).dropna(subset=["VendorName"])
    names = names.groupby(["VendorNumber", "VendorName"], as_index=False)["w"].sum()
    names = names.sort_values("w", ascending=False).drop_duplicates("VendorNumber")
    return names[["VendorNumber", "VendorName"]]


# ------------------------------------------------------------- building
def build_tables(conn):
    """Read the raw database and return (summary, monthly, freight, meta)."""
    print("Step A - adding up each raw table separately:")
    sales = to_number(timed_query(conn, "sales     by vendor+brand", SALES_SQL), ["VendorNumber", "Brand"])
    purch = to_number(timed_query(conn, "purchases by vendor+brand", PURCHASES_SQL), ["VendorNumber", "Brand"])
    freight = to_number(timed_query(conn, "freight   by vendor    ", FREIGHT_SQL), ["VendorNumber"])
    monthly = timed_query(conn, "sales     by month+vendor", MONTHLY_SQL)

    # --- one clean name per vendor
    names = pick_vendor_names([(sales, "TotalSalesDollars"), (purch, "TotalPurchaseDollars"),
                               (freight, "InvoiceDollars")])

    # --- collapse name variants: now exactly one row per VendorNumber + Brand per table
    s = sales.groupby(["VendorNumber", "Brand"], as_index=False).agg(
        Description=("Description", "first"), Classification=("Classification", "first"),
        TotalSalesDollars=("TotalSalesDollars", "sum"), TotalSalesQuantity=("TotalSalesQuantity", "sum"),
        TotalExciseTax=("TotalExciseTax", "sum"))
    p = purch.groupby(["VendorNumber", "Brand"], as_index=False).agg(
        Description=("Description", "first"), Classification=("Classification", "first"),
        TotalPurchaseDollars=("TotalPurchaseDollars", "sum"), TotalPurchaseQuantity=("TotalPurchaseQuantity", "sum"))
    f = freight.groupby("VendorNumber", as_index=False).agg(
        FreightCost=("FreightCost", "sum"), InvoiceCount=("InvoiceCount", "sum"),
        InvoiceDollars=("InvoiceDollars", "sum"))

    # --- Step B: merge the SMALL tables in pandas (outer = keep pairs that appear in only one)
    print("\nStep B - merging in pandas (outer join on VendorNumber + Brand)")
    before = {"sales": s["TotalSalesDollars"].sum(), "purchases": p["TotalPurchaseDollars"].sum()}
    summary = s.merge(p, on=["VendorNumber", "Brand"], how="outer", suffixes=("_s", "_p"), indicator=True)
    summary["Description"] = summary["Description_s"].fillna(summary["Description_p"])
    summary["Classification"] = summary["Classification_s"].fillna(summary["Classification_p"])
    has_purchases = summary["_merge"].isin(["both", "right_only"])
    sales_on_purchased = summary.loc[has_purchases, "TotalSalesDollars"].sum()
    for c in ["TotalSalesDollars", "TotalSalesQuantity", "TotalExciseTax",
              "TotalPurchaseDollars", "TotalPurchaseQuantity"]:
        summary[c] = summary[c].fillna(0.0)
    summary = summary.merge(names, on="VendorNumber", how="left")
    summary["VendorName"] = summary["VendorName"].fillna("Vendor " + summary["VendorNumber"].astype(str))

    # --- Step C: spread each vendor's freight over its brand rows (by purchase dollars)
    # so that SUM(FreightCost) over a vendor's rows equals the invoice freight exactly.
    vend_purch = summary.groupby("VendorNumber")["TotalPurchaseDollars"].transform("sum")
    vend_rows = summary.groupby("VendorNumber")["Brand"].transform("count")
    share = (summary["TotalPurchaseDollars"] / vend_purch.where(vend_purch > 0)).fillna(1.0 / vend_rows)
    summary = summary.merge(f[["VendorNumber", "FreightCost"]].rename(columns={"FreightCost": "VendorFreight"}),
                            on="VendorNumber", how="left")
    summary["FreightCost"] = summary["VendorFreight"].fillna(0.0) * share
    allocated_freight = summary["FreightCost"].sum()

    # --- Step D: calculated columns (money stays unrounded so totals are exact)
    sd, pd_ = summary["TotalSalesDollars"], summary["TotalPurchaseDollars"]
    sq, pq = summary["TotalSalesQuantity"], summary["TotalPurchaseQuantity"]
    summary["GrossProfit"] = sd - pd_
    summary["ProfitMargin"] = (summary["GrossProfit"] / sd.where(sd != 0) * 100).round(4)   # already a PERCENT
    summary["NetAfterFreight"] = summary["GrossProfit"] - summary["FreightCost"]
    summary["SalesToPurchaseRatio"] = (sd / pd_.where(pd_ != 0)).round(4)
    summary["StockTurnover"] = (sq / pq.where(pq != 0)).round(4)
    summary["Classification"] = summary["Classification"].astype("Int64")

    cols = ["VendorNumber", "VendorName", "Brand", "Description", "Classification",
            "TotalPurchaseQuantity", "TotalPurchaseDollars", "TotalSalesQuantity", "TotalSalesDollars",
            "TotalExciseTax", "FreightCost", "GrossProfit", "ProfitMargin", "NetAfterFreight",
            "SalesToPurchaseRatio", "StockTurnover"]
    summary = summary[cols].sort_values("TotalSalesDollars", ascending=False).reset_index(drop=True)

    # --- monthly table
    bad_dates = monthly["Month"].isna()
    if bad_dates.any():
        print(f"   WARNING: {int(monthly.loc[bad_dates, 'SalesDollars'].sum()):,} sales dollars have no valid SalesDate "
              "and are left out of monthly_vendor_sales")
    first_date, last_date = monthly["FirstDate"].min(), monthly["LastDate"].max()
    monthly = to_number(monthly.dropna(subset=["Month"]), ["VendorNumber"])
    monthly = monthly.merge(names, on="VendorNumber", how="left")
    monthly["VendorName"] = monthly["VendorName"].fillna("Vendor " + monthly["VendorNumber"].astype(str))
    monthly["Classification"] = pd.to_numeric(monthly["Classification"], errors="coerce").astype("Int64")
    monthly = monthly[["Month", "VendorNumber", "VendorName", "Classification", "SalesDollars", "SalesQuantity"]]
    monthly = monthly.sort_values(["Month", "SalesDollars"], ascending=[True, False]).reset_index(drop=True)

    # --- freight table (one row per vendor)
    freight_out = f.merge(names, on="VendorNumber", how="left")
    freight_out["VendorName"] = freight_out["VendorName"].fillna("Vendor " + freight_out["VendorNumber"].astype(str))
    freight_out = freight_out[["VendorNumber", "VendorName", "FreightCost", "InvoiceCount", "InvoiceDollars"]]

    meta = {
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "currency": "USD",
        "first_sales_date": str(first_date),
        "max_sales_date": str(last_date),       # "last 3 months" is counted back from THIS date
        "anchor_month": str(last_date)[:7],
    }
    checks = {"before": before, "sales_on_purchased": sales_on_purchased,
              "allocated_freight": allocated_freight, "invoice_freight": f["FreightCost"].sum()}
    return summary, monthly, freight_out, meta, checks


# ------------------------------------------------------------ validation
def validate(summary, monthly, checks) -> bool:
    """Print the KPI check. Returns False only if an INTERNAL consistency check fails."""
    sales_t = summary["TotalSalesDollars"].sum()
    purch_t = summary["TotalPurchaseDollars"].sum()
    profit_t = sales_t - purch_t
    margin_t = profit_t / sales_t * 100 if sales_t else float("nan")   # SUM method, never AVG

    print("\n" + "=" * 74)
    print("KPI VALIDATION (calculated from the summary vs. your expected values)")
    print("=" * 74)
    print(f"{'KPI':<18}{'Calculated':>18}{'Expected':>18}{'Diff':>9}   Status")
    print("-" * 74)
    warnings = []
    for label, key, value in [("Total Sales", "sales", sales_t), ("Total Purchases", "purchases", purch_t),
                              ("Gross Profit", "profit", profit_t)]:
        diff = (value - EXPECTED[key]) / EXPECTED[key]
        ok = abs(diff) <= TOLERANCE
        print(f"{label:<18}{money(value):>18}{money(EXPECTED[key]):>18}{diff:>+9.2%}   {'OK' if ok else 'WARNING'}")
        if not ok:
            warnings.append(label)
    mdiff = margin_t - EXPECTED["margin"]
    ok = abs(mdiff) <= MARGIN_TOLERANCE
    print(f"{'Margin %':<18}{margin_t:>17.2f}%{EXPECTED['margin']:>17.2f}%{mdiff:>+8.2f}p   {'OK' if ok else 'WARNING'}")
    if not ok:
        warnings.append("Margin %")

    print("\nINTERNAL CHECKS (these must pass)")
    internal_ok = True
    drift_s = abs(checks["before"]["sales"] - sales_t)
    drift_p = abs(checks["before"]["purchases"] - purch_t)
    for label, good in [("Merge did not change sales total", drift_s < 1),
                        ("Merge did not change purchase total (no join inflation)", drift_p < 1),
                        ("Monthly sales == summary sales", abs(monthly["SalesDollars"].sum() - sales_t) < 1),
                        ("Allocated freight == invoice freight (vendors with purchases)",
                         abs(summary["FreightCost"].sum() - checks["allocated_freight"]) < 1)]:
        print(f"   [{'OK' if good else 'FAIL'}] {label}")
        internal_ok &= good
    unalloc = checks["invoice_freight"] - checks["allocated_freight"]
    if abs(unalloc) >= 1:
        print(f"   note: {money(unalloc)} of freight belongs to vendors with no sales/purchase rows. "
              "It is kept in vendor_freight only.")

    if warnings:
        print(f"\nWARNING: {', '.join(warnings)} differ from expected by more than the allowed tolerance.")
        print("How to read this:")
        print("   - Rows loaded OK in step 2? Check the row counts printed by build_db.py first.")
        print(f"   - Sales on vendor+brand pairs that ALSO have purchases: {money(checks['sales_on_purchased'])}")
        print("     (if THIS matches your expected sales, your old report left out sales of unpurchased items).")
        print("   The tables are still saved. Totals above are exactly what the raw data adds up to.")
    return internal_ok


# ---------------------------------------------------------------- saving
def save_db(out_path: Path, tables: dict) -> None:
    """Write to a temporary file first, then swap it in, so a crash never leaves a broken vendor.db."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    for name, df in tables.items():
        df.to_sql(name, conn, if_exists="replace", index=False)
    conn.execute("CREATE INDEX idx_vss_vendor ON vendor_sales_summary(VendorNumber)")
    conn.execute("CREATE INDEX idx_vss_brand  ON vendor_sales_summary(Brand)")
    conn.execute("CREATE INDEX idx_mvs_month  ON monthly_vendor_sales(Month)")
    conn.execute("CREATE INDEX idx_mvs_vendor ON monthly_vendor_sales(VendorNumber)")
    conn.commit()
    conn.close()
    try:
        os.replace(tmp, out_path)
    except PermissionError:
        print(f"ERROR: cannot overwrite {out_path}. Close the Streamlit app (Ctrl+C) and run again.")
        raise SystemExit(1)


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("DB_PATH") or not os.getenv("SUMMARY_DB_PATH"):
        print("ERROR: DB_PATH and SUMMARY_DB_PATH must be set in your .env file.")
        return 1
    raw_path = resolve_path(os.environ["DB_PATH"])
    out_path = resolve_path(os.environ["SUMMARY_DB_PATH"])
    if not raw_path.exists():
        print(f"ERROR: {raw_path} not found. Run first:  python scripts/build_db.py")
        return 1

    print(f"Raw database    : {raw_path}  (opened read-only)\nSummary database: {out_path}\n")
    conn = sqlite3.connect(raw_path.resolve().as_uri() + "?mode=ro", uri=True)   # read-only = safe
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in ("sales", "purchases", "vendor_invoice") if t not in have]
    if missing:
        print(f"ERROR: missing tables in the raw database: {missing}. Run build_db.py again.")
        return 1

    t0 = time.time()
    summary, monthly, freight, meta, checks = build_tables(conn)
    conn.close()

    ok = validate(summary, monthly, checks)
    if not ok:
        print("\nINTERNAL CHECK FAILED - vendor.db was NOT written. Please send me this output.")
        return 1

    meta_df = pd.DataFrame({"key": list(meta), "value": list(meta.values())})
    save_db(out_path, {"vendor_sales_summary": summary, "monthly_vendor_sales": monthly,
                       "vendor_freight": freight, "meta": meta_df})

    print("\n" + "=" * 74)
    print(f"Saved {out_path.name}: {out_path.stat().st_size / 1e6:.1f} MB in {time.time() - t0:.0f}s")
    print(f"   vendor_sales_summary : {len(summary):,} rows")
    print(f"   monthly_vendor_sales : {len(monthly):,} rows")
    print(f"   vendor_freight       : {len(freight):,} rows")
    print(f"   Sales dates          : {meta['first_sales_date']} to {meta['max_sales_date']}")
    print("\nTop 5 vendors by sales:")
    top = summary.groupby(["VendorNumber", "VendorName"], as_index=False)[["TotalSalesDollars", "GrossProfit"]].sum()
    top = top.sort_values("TotalSalesDollars", ascending=False).head(5)
    for _, r in top.iterrows():
        print(f"   {r['VendorName'][:34]:<36}{money(r['TotalSalesDollars']):>16}   profit {money(r['GrossProfit'])}")
    print("\nNext: Step 4 (app/db.py and app/tools.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
