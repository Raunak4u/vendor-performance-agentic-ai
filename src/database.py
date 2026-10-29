
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Callable

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "vendor_performance.db"

# Required columns for the CSV upload feature.
REQUIRED_COLUMNS = {
    "sales": ["vendornumber", "salesdollars", "salesquantity"],
    "purchases": ["vendornumber", "dollars", "quantity"],
    "vendors": ["vendornumber", "vendorname"],
    "products": ["brand", "description"],
}

ALLOWED_TABLES = {
    "sales",
    "purchases",
    "vendors",
    "products",
    "vendor_summary",
}


def get_engine() -> Engine:
    """Connect to the dashboard's existing SQLite database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    return create_engine(
        f"sqlite:///{DB_PATH.as_posix()}",
        connect_args={"timeout": 60},
        future=True,
    )


def table_exists(*args, **kwargs) -> bool:
    # flexible: table_exists("vendors") / table_exists(engine, "vendors") / table_exists(table_name="vendors")
    table_name = kwargs.get("table_name") or kwargs.get("table")
    engine = kwargs.get("engine")
    for a in args:
        if isinstance(a, str):
            table_name = a
        elif a is not None and engine is None:
            engine = a
    if table_name is None:
        raise TypeError("table_exists() missing 1 required positional argument: 'table_name'")
    if engine is None:
        engine = get_engine()
    # original logic below - check table
    """Return whether a table exists."""
    return table_name in inspect(engine).get_table_names()


def get_row_count(table_name: str) -> int:
    """Return the row count for a table in the dashboard database."""
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Unsupported table: {table_name}")

    engine = get_engine()
    try:
        if not table_exists(engine, table_name):
            return 0

        with engine.connect() as conn:
            result = conn.execute(
                text(f'SELECT COUNT(*) FROM "{table_name}"')
            )
            return int(result.scalar_one())
    finally:
        engine.dispose()


def _normalize_column_name(name: str) -> str:
    """Normalize CSV headings to match the existing SQLite schema."""
    value = str(name).strip().lower()
    value = re.sub(r"[^a-z0-9_]", "", value)
    return value


def load_csv(
    file_path: str,
    table_name: str,
    engine: Engine,
    progress_callback: Callable | None = None,
) -> None:
    """
    Import a CSV into a managed table in chunks.

    The destination table is replaced only when a valid CSV upload
    is deliberately submitted through the app.
    """
    if table_name not in {"sales", "purchases", "vendors", "products"}:
        raise ValueError(f"Unsupported upload table: {table_name}")

    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {path}")

    # Read headers first to validate the upload before replacing data.
    header = pd.read_csv(path, nrows=0)
    normalized = [_normalize_column_name(c) for c in header.columns]

    if not normalized or any(not c for c in normalized):
        raise ValueError("The CSV contains an empty or invalid column name.")

    if len(set(normalized)) != len(normalized):
        raise ValueError(
            "Two or more CSV columns become identical after normalization."
        )

    missing = set(REQUIRED_COLUMNS[table_name]) - set(normalized)
    if missing:
        raise ValueError(
            f"{table_name} CSV is missing required columns: "
            + ", ".join(sorted(missing))
        )

    chunksize = 100_000
    first_chunk = True

    for chunk in pd.read_csv(path, chunksize=chunksize):
        chunk.columns = [
            _normalize_column_name(c) for c in chunk.columns
        ]

        chunk.to_sql(
            table_name,
            con=engine,
            if_exists="replace" if first_chunk else "append",
            index=False,
            chunksize=10_000,
            method=None,
        )

        first_chunk = False

        if progress_callback is not None:
            try:
                progress_callback(
                    min(0.90, 0.90 * path.stat().st_size / max(
                        path.stat().st_size, 1
                    ))
                )
            except (TypeError, ValueError):
                # Progress reporting must not interrupt a valid import.
                pass

    if first_chunk:
        raise ValueError("The CSV contains no data rows.")


def create_vendor_summary(engine: Engine | None = None, progress_callback=None):
    if engine is None:
        engine = get_engine()
    """
    Rebuild vendor_summary from the sales and purchases tables.

    This operation changes only vendor_summary; it does not drop or
    replace the underlying sales or purchases tables.
    """
    required = {"sales", "purchases"}
    available = set(inspect(engine).get_table_names())

    if not required.issubset(available):
        missing = ", ".join(sorted(required - available))
        raise ValueError(f"Cannot build vendor summary. Missing: {missing}")

    sales_cols = {
        c["name"].lower()
        for c in inspect(engine).get_columns("sales")
    }
    purchase_cols = {
        c["name"].lower()
        for c in inspect(engine).get_columns("purchases")
    }

    for column in ("vendornumber", "vendorname", "salesdollars", "salesquantity"):
        if column not in sales_cols:
            raise ValueError(f"sales table is missing column: {column}")

    for column in ("vendornumber", "vendorname", "dollars", "quantity"):
        if column not in purchase_cols:
            raise ValueError(f"purchases table is missing column: {column}")

    # Aggregate the large source tables in SQLite rather than loading
    # millions of rows into Python memory.
    summary_sql = text("""
        CREATE TABLE vendor_summary_new AS
        WITH
        sales_agg AS (
            SELECT
                COALESCE(
                    NULLIF(TRIM(CAST(vendornumber AS TEXT)), ''),
                    LOWER(NULLIF(TRIM(vendorname), ''))
                ) AS vendor_key,
                MAX(vendornumber) AS vendornumber,
                MAX(vendorname) AS vendorname,
                SUM(COALESCE(salesdollars, 0)) AS total_sales,
                SUM(COALESCE(salesquantity, 0)) AS total_sales_qty
            FROM sales
            GROUP BY vendor_key
        ),
        purchase_agg AS (
            SELECT
                COALESCE(
                    NULLIF(TRIM(CAST(vendornumber AS TEXT)), ''),
                    LOWER(NULLIF(TRIM(vendorname), ''))
                ) AS vendor_key,
                MAX(vendornumber) AS vendornumber,
                MAX(vendorname) AS vendorname,
                SUM(COALESCE(dollars, 0)) AS total_purchase,
                SUM(COALESCE(quantity, 0)) AS total_purchase_qty
            FROM purchases
            GROUP BY vendor_key
        ),
        vendor_keys AS (
            SELECT vendor_key FROM sales_agg
            UNION
            SELECT vendor_key FROM purchase_agg
        ),
        combined AS (
            SELECT
                COALESCE(s.vendornumber, p.vendornumber) AS vendornumber,
                COALESCE(s.vendorname, p.vendorname) AS vendorname,
                COALESCE(p.total_purchase, 0) AS total_purchase,
                COALESCE(s.total_sales, 0) AS total_sales,
                COALESCE(p.total_purchase_qty, 0) AS total_purchase_qty,
                COALESCE(s.total_sales_qty, 0) AS total_sales_qty
            FROM vendor_keys k
            LEFT JOIN sales_agg s ON s.vendor_key = k.vendor_key
            LEFT JOIN purchase_agg p ON p.vendor_key = k.vendor_key
        )
        SELECT
            vendornumber,
            vendorname,
            total_purchase,
            total_sales,
            total_purchase_qty,
            total_sales_qty,
            total_sales - total_purchase AS gross_profit,
            CASE
                WHEN total_sales <> 0
                THEN (total_sales - total_purchase) * 100.0 / total_sales
                ELSE 0
            END AS profit_margin_pct
        FROM combined
    """)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS vendor_summary_new"))
        conn.execute(summary_sql)
        conn.execute(text("DROP TABLE IF EXISTS vendor_summary"))
        conn.execute(text(
            "ALTER TABLE vendor_summary_new RENAME TO vendor_summary"
        ))


def get_data_quality(engine: Engine | None = None):
    if engine is None:
        engine = get_engine()
    """Return a compact data-quality report for the dashboard."""
    report = []

    for table_name in sorted(
        {"sales", "purchases", "vendors", "products", "vendor_summary"}
    ):
        if not table_exists(engine, table_name):
            report.append({
                "table": table_name,
                "row_count": 0,
                "status": "Missing table",
            })
            continue

        with engine.connect() as conn:
            count = conn.execute(
                text(f'SELECT COUNT(*) FROM "{table_name}"')
            ).scalar_one()

        report.append({
            "table": table_name,
            "row_count": int(count),
            "status": "Available",
        })

    return pd.DataFrame(report)


def reset_database(engine: Engine | None = None) -> None:
    """
    Deliberately refuse destructive resets.

    Use the app's validated CSV upload workflow to replace a specific
    table. This prevents accidental deletion of the existing database.
    """
    raise RuntimeError(
        "Automatic database reset is disabled to protect existing data. "
        "Use the validated upload workflow for the table you intend to replace."
    )


# ------------------------------------------------------------------
# Dashboard compatibility helpers
# ------------------------------------------------------------------

def db_file_size() -> int:
    """Return the dashboard database size in bytes."""
    return DB_PATH.stat().st_size if DB_PATH.exists() else 0


def get_meta(key: str, default=None):
    """Read dashboard metadata, returning default if unavailable."""
    engine = get_engine()
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("""
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='app_meta'
            """)).first()
            if not exists:
                return default
            row = conn.execute(
                text("SELECT value FROM app_meta WHERE key=:key"),
                {"key": key},
            ).first()
            return row[0] if row else default
    finally:
        engine.dispose()


def set_meta(key: str, value) -> None:
    """Store dashboard metadata."""
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """))
            conn.execute(text("""
                INSERT INTO app_meta(key, value) VALUES (:key, :value)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """), {"key": key, "value": str(value)})
    finally:
        engine.dispose()


def missing_columns(headers, table_name: str) -> list[str]:
    """Return required CSV columns absent from a supplied header."""
    if table_name not in REQUIRED_COLUMNS:
        raise ValueError(f"Unsupported table: {table_name}")
    normalized = {_normalize_column_name(h) for h in headers}
    return sorted(set(REQUIRED_COLUMNS[table_name]) - normalized)


def build_aggregates(engine: Engine | None = None, progress_callback=None):
    if engine is None:
        engine = get_engine()
    """
    Build compact dashboard aggregates using SQLite.
    Raw sales and purchases are aggregated in SQL, not loaded into pandas.
    """
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    required = {"sales", "purchases"}
    missing = required - tables
    if missing:
        raise ValueError(
            "Cannot build aggregates; missing tables: "
            + ", ".join(sorted(missing))
        )

    def report(step, message):
        if progress_callback:
            try:
                progress_callback(step, 4, message)
            except TypeError:
                progress_callback(step / 4, message)

    # Use a transaction so the three tables are refreshed together.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """))

        # Detect columns case-insensitively to support common CSV schemas.
        sales_cols = {
            c["name"].lower() for c in inspect(engine).get_columns("sales")
        }
        purchase_cols = {
            c["name"].lower() for c in inspect(engine).get_columns("purchases")
        }

        def choose(columns, *candidates):
            for candidate in candidates:
                if candidate.lower() in columns:
                    return candidate
            raise ValueError(
                "Missing expected column. Tried: " + ", ".join(candidates)
            )

        sv = choose(sales_cols, "vendornumber", "vendorno")
        sn = choose(sales_cols, "vendorname")
        ss = choose(sales_cols, "store")
        sc = choose(sales_cols, "classification")
        sd = choose(sales_cols, "salesdate")
        sb = choose(sales_cols, "brand")
        sq = choose(sales_cols, "salesquantity")
        sx = choose(sales_cols, "salesdollars")

        pv = choose(purchase_cols, "vendornumber", "vendorno")
        pn = choose(purchase_cols, "vendorname")
        ps = choose(purchase_cols, "store")
        pc = choose(purchase_cols, "classification")
        pq = choose(purchase_cols, "quantity")
        px = choose(purchase_cols, "dollars")

        # Only interpolate identifiers taken from inspected table schemas.
        for name in (
            "agg_vendor_store_class",
            "agg_vendor_month",
            "agg_vendor_product",
        ):
            conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))

        report(0, "Building vendor/store/class aggregates")
        conn.execute(text(f"""
            CREATE TABLE agg_vendor_store_class AS
            WITH
            s AS (
                SELECT
                    CAST("{sv}" AS TEXT) AS vendor_no,
                    MAX("{sn}") AS vendor_name,
                    CAST("{ss}" AS TEXT) AS store,
                    CAST("{sc}" AS TEXT) AS classification,
                    SUM(COALESCE("{sx}", 0)) AS sales,
                    SUM(COALESCE("{sq}", 0)) AS sales_qty
                FROM sales
                GROUP BY CAST("{sv}" AS TEXT), "{ss}", "{sc}"
            ),
            p AS (
                SELECT
                    CAST("{pv}" AS TEXT) AS vendor_no,
                    CAST("{ps}" AS TEXT) AS store,
                    CAST("{pc}" AS TEXT) AS classification,
                    SUM(COALESCE("{px}", 0)) AS purchases,
                    SUM(COALESCE("{pq}", 0)) AS purchase_qty
                FROM purchases
                GROUP BY CAST("{pv}" AS TEXT), "{ps}", "{pc}"
            )
            SELECT
                COALESCE(s.vendor_no, p.vendor_no) AS vendor_no,
                s.vendor_name,
                COALESCE(s.store, p.store) AS store,
                COALESCE(s.classification, p.classification) AS classification,
                COALESCE(s.sales, 0) AS sales,
                COALESCE(p.purchases, 0) AS purchases,
                COALESCE(s.sales, 0) - COALESCE(p.purchases, 0) AS profit,
                COALESCE(s.sales_qty, 0) AS sales_qty,
                COALESCE(p.purchase_qty, 0) AS purchase_qty
            FROM s
            LEFT JOIN p ON s.vendor_no=p.vendor_no
                AND s.store=p.store
                AND s.classification=p.classification
            UNION ALL
            SELECT
                p.vendor_no, NULL, p.store, p.classification,
                0, p.purchases, -p.purchases, 0, p.purchase_qty
            FROM p
            LEFT JOIN s ON s.vendor_no=p.vendor_no
                AND s.store=p.store
                AND s.classification=p.classification
            WHERE s.vendor_no IS NULL
        """))

        report(1, "Building vendor/month aggregates")
        conn.execute(text(f"""
            CREATE TABLE agg_vendor_month AS
            SELECT
                CAST("{sv}" AS TEXT) AS vendor_no,
                MAX("{sn}") AS vendor_name,
                strftime('%Y-%m', "{sd}") AS month,
                SUM(COALESCE("{sx}", 0)) AS sales,
                SUM(COALESCE("{sq}", 0)) AS sales_qty
            FROM sales
            GROUP BY CAST("{sv}" AS TEXT), strftime('%Y-%m', "{sd}")
        """))

        report(2, "Building vendor/product aggregates")
        conn.execute(text(f"""
            CREATE TABLE agg_vendor_product AS
            SELECT
                CAST("{sv}" AS TEXT) AS vendor_no,
                MAX("{sn}") AS vendor_name,
                CAST("{sb}" AS TEXT) AS brand,
                SUM(COALESCE("{sx}", 0)) AS sales,
                SUM(COALESCE("{sq}", 0)) AS sales_qty
            FROM sales
            GROUP BY CAST("{sv}" AS TEXT), CAST("{sb}" AS TEXT)
        """))

        conn.execute(text("""
            INSERT INTO app_meta(key, value) VALUES
                ('aggregates_stale', '0'),
                ('aggregates_built_at', datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """))

    report(3, "Analytics tables built successfully")
