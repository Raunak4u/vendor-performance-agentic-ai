import pandas as pd
from sqlalchemy import create_engine, text
import os
import gc

DATA_FOLDER = "data"
DB_FILE = "vendor_performance.db"
CHUNKSIZE = 100000

engine = create_engine(
    f"sqlite:///{DB_FILE}",
    connect_args={"timeout": 60}
)


def normalize_columns(df):
    df.columns = [
        str(c).strip().lower().replace(" ", "_")
        for c in df.columns
    ]
    return df


def load_csv_chunked(file_name, table_name, rename_map=None):
    file_path = os.path.join(DATA_FOLDER, file_name)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"CSV file not found: {file_path}")

    print(f"\nLoading {file_path} -> {table_name}")

    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))

    total = 0
    first = True

    for chunk in pd.read_csv(
        file_path,
        chunksize=CHUNKSIZE,
        low_memory=False
    ):
        chunk = normalize_columns(chunk)

        if rename_map:
            chunk = chunk.rename(columns=rename_map)

        # Use a moderate insert batch to avoid huge SQL statements.
        chunk.to_sql(
            table_name,
            engine,
            if_exists="replace" if first else "append",
            index=False,
            chunksize=2000,
            method=None
        )

        total += len(chunk)
        first = False

        print(f"  Loaded: {total:,} rows")

        del chunk
        gc.collect()

    if first:
        raise ValueError(f"No data rows found in {file_path}")

    print(f"Completed {table_name}: {total:,} rows")
    return total


def create_vendor_summary():
    print("\nCreating vendor_summary...")

    sql = """
    CREATE TABLE vendor_summary AS
    WITH purchase_agg AS (
        SELECT
            vendornumber,
            SUM(quantity) AS total_purchase_qty,
            SUM(dollars) AS total_purchase
        FROM purchases
        GROUP BY vendornumber
    ),
    sales_agg AS (
        SELECT
            vendornumber,
            SUM(salesquantity) AS total_sales_qty,
            SUM(salesdollars) AS total_sales
        FROM sales
        GROUP BY vendornumber
    )
    SELECT
        v.vendornumber,
        v.vendorname,
        COALESCE(p.total_purchase, 0) AS total_purchase,
        COALESCE(s.total_sales, 0) AS total_sales,
        COALESCE(p.total_purchase_qty, 0) AS total_purchase_qty,
        COALESCE(s.total_sales_qty, 0) AS total_sales_qty,
        COALESCE(s.total_sales, 0)
            - COALESCE(p.total_purchase, 0) AS gross_profit,
        CASE
            WHEN COALESCE(s.total_sales, 0) = 0 THEN 0
            ELSE (
                COALESCE(s.total_sales, 0)
                - COALESCE(p.total_purchase, 0)
            ) * 100.0 / s.total_sales
        END AS profit_margin_pct
    FROM vendors v
    LEFT JOIN purchase_agg p
        ON v.vendornumber = p.vendornumber
    LEFT JOIN sales_agg s
        ON v.vendornumber = s.vendornumber
    """

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS vendor_summary"))
        conn.execute(text(sql))

        # Add indexes to improve common vendor lookups.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_purchases_vendor "
            "ON purchases(vendornumber)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_sales_vendor "
            "ON sales(vendornumber)"
        ))

    print("vendor_summary created successfully!")


def show_kpis():
    print("\n--- Overall KPIs ---")

    kpis = pd.read_sql("""
        SELECT
            SUM(total_purchase) AS total_purchase,
            SUM(total_sales) AS total_sales,
            SUM(gross_profit) AS total_profit,
            CASE
                WHEN SUM(total_sales) = 0 THEN 0
                ELSE SUM(gross_profit) * 100.0 / SUM(total_sales)
            END AS overall_margin_pct
        FROM vendor_summary
    """, engine)

    print(kpis.to_string(index=False))

    print("\n--- Top 5 Vendors by Profit ---")
    top = pd.read_sql("""
        SELECT
            vendorname,
            total_purchase,
            total_sales,
            gross_profit,
            ROUND(profit_margin_pct, 2) AS margin_pct
        FROM vendor_summary
        ORDER BY gross_profit DESC
        LIMIT 5
    """, engine)
    print(top.to_string(index=False))

    print("\n--- 5 Lowest-Profit Vendors ---")
    losses = pd.read_sql("""
        SELECT
            vendorname,
            total_purchase,
            total_sales,
            gross_profit,
            ROUND(profit_margin_pct, 2) AS margin_pct
        FROM vendor_summary
        WHERE gross_profit < 0
        ORDER BY gross_profit ASC
        LIMIT 5
    """, engine)
    print(losses.to_string(index=False))

    output_file = "vendor_summary.csv"
    pd.read_sql(
        "SELECT * FROM vendor_summary ORDER BY gross_profit DESC",
        engine
    ).to_csv(output_file, index=False)

    print(f"\nExported: {output_file}")


if __name__ == "__main__":
    try:
        required_files = [
            "purchases.csv",
            "sales.csv",
            "vendors.csv",
            "products.csv"
        ]

        missing = [
            f for f in required_files
            if not os.path.exists(os.path.join(DATA_FOLDER, f))
        ]

        if missing:
            raise FileNotFoundError(
                "Missing files in data folder: " + ", ".join(missing)
            )

        # Create a fresh database on each run.
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)
            print(f"Deleted old {DB_FILE}")

        load_csv_chunked("purchases.csv", "purchases")

        load_csv_chunked(
            "sales.csv",
            "sales",
            rename_map={"vendorno": "vendornumber"}
        )

        load_csv_chunked("vendors.csv", "vendors")
        load_csv_chunked("products.csv", "products")

        create_vendor_summary()
        show_kpis()

        print(f"\nDone! Database saved as {DB_FILE}")

    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        raise

    finally:
        engine.dispose()
