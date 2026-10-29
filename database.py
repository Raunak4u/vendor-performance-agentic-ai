"""
src/database.py
Vendor Performance Intelligence - database layer (SQLite).

Design rules
------------
* The big tables (sales ~12.8M rows, purchases ~2.4M rows) are NEVER loaded into pandas.
  Everything the dashboard needs comes from small pre-aggregated tables
  (vendor_summary + agg_* tables) that are built ONCE with SQL GROUP BY.
* NullPool: no connection is kept open between calls, so the dashboard can never hold
  a lock that blocks the upload pipeline.
* Every connection uses timeout=30 / busy_timeout=30000 and WAL journal mode.
* CSV loading goes into a temporary table and is swapped in with one short transaction,
  so a failed or half-finished upload never damages the live data.

Public API (same names as before):
    get_engine, load_csv, create_vendor_summary, get_data_quality,
    get_row_count, table_exists, reset_database, REQUIRED_COLUMNS
New helpers:
    build_aggregates, missing_columns, get_meta, set_meta, db_file_size

CLI:
    python -m src.database summary       # rebuild vendor_summary from purchases + sales
    python -m src.database aggregates    # build the agg_* tables (one-time, a few minutes)
    python -m src.database all
    python -m src.database counts
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

# ----------------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("VENDOR_DB_PATH", str(BASE_DIR / "vendor_performance.db")))
SQLITE_TIMEOUT = 30  # seconds

BIG_TABLES = {"sales", "purchases"}
AGG_TABLES = [
    "agg_vendor_store_class",
    "agg_vendor_month",
    "agg_vendor_product",
]
ALL_KNOWN_TABLES = ["purchases", "sales", "vendors", "products", "vendor_summary", "app_meta"] + AGG_TABLES

# Lower-case column names a CSV must contain before it is accepted.
REQUIRED_COLUMNS: dict[str, list[str]] = {
    "vendors": ["vendornumber", "vendorname"],
    "products": ["brand", "description"],
    "purchases": ["vendornumber", "vendorname", "brand", "quantity", "dollars"],
    "sales": ["vendorno", "vendorname", "brand", "salesquantity", "salesdollars", "salesdate"],
}

# Column-name aliases (all lower-case) treated as the same column.
_ALIASES = {
    "vendor_no": "vendorno",
    "vendornumber": "vendorno",
    "vendor_number": "vendorno",
    "vendor_name": "vendorname",
}

ProgressCB = Optional[Callable[..., None]]


# ----------------------------------------------------------------------------
# Connections
# ----------------------------------------------------------------------------
def get_engine():
    """SQLAlchemy engine for the SQLite DB. NullPool => nothing stays open between calls."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{DB_PATH.as_posix()}",
        poolclass=NullPool,
        connect_args={"timeout": SQLITE_TIMEOUT, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # pragma: no cover - trivial
        cur = dbapi_connection.cursor()
        try:
            cur.execute(f"PRAGMA busy_timeout={SQLITE_TIMEOUT * 1000}")
            try:
                cur.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

    return engine


def _db_file(engine=None) -> str:
    """Path of the SQLite file behind an engine (falls back to DB_PATH)."""
    try:
        if engine is not None and getattr(engine, "url", None) is not None:
            database = engine.url.database
            if database:
                return str(database)
    except Exception:
        pass
    return str(DB_PATH)


def _connect(engine=None) -> sqlite3.Connection:
    """Short-lived plain sqlite3 connection (timeout=30, WAL, busy_timeout)."""
    conn = sqlite3.connect(_db_file(engine), timeout=SQLITE_TIMEOUT, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout={SQLITE_TIMEOUT * 1000}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-131072")  # ~128 MB page cache
    return conn


def _ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
        raise ValueError(f"Unsafe table/column name: {name!r}")
    return str(name)


def db_file_size(engine=None) -> int:
    try:
        return Path(_db_file(engine)).stat().st_size
    except OSError:
        return 0


# ----------------------------------------------------------------------------
# Small metadata table (row counts, build times, stale flags)
# ----------------------------------------------------------------------------
def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")


def set_meta(key: str, value, engine=None) -> None:
    conn = _connect(engine)
    try:
        _ensure_meta(conn)
        conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def get_meta(key: str, default=None, engine=None):
    conn = _connect(engine)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
        ).fetchone()
        if not exists:
            return default
        row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Table helpers
# ----------------------------------------------------------------------------
def table_exists(table_name: str, engine=None) -> bool:
    conn = _connect(engine)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{_ident(table)}")').fetchall()]


def _pick(columns: Iterable[str], *candidates: str) -> Optional[str]:
    """Return the real column name matching one of the candidates (case-insensitive)."""
    lookup = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lookup:
            return lookup[cand.lower()]
    return None


def get_row_count(table_name: str, exact: bool = False, engine=None) -> int:
    """
    Row count that does NOT scan the 12.8M-row sales table.
    Order: stored meta value -> MAX(rowid) (instant) -> COUNT(*) (only if exact=True or small table).
    """
    conn = _connect(engine)
    try:
        t = _ident(table_name)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
            return 0
        if t in BIG_TABLES and not exact:
            meta_ok = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
            ).fetchone()
            if meta_ok:
                row = conn.execute("SELECT value FROM app_meta WHERE key=?", (f"rows_{t}",)).fetchone()
                if row and str(row[0]).isdigit():
                    return int(row[0])
            row = conn.execute(f'SELECT MAX(_rowid_) FROM "{t}"').fetchone()
            return int(row[0] or 0)
        row = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def missing_columns(columns: Iterable[str], table_name: str) -> list[str]:
    """Required (lower-case) columns that are absent from `columns`. Aliases are honoured."""
    required = REQUIRED_COLUMNS.get(table_name, [])
    have = {_ALIASES.get(str(c).strip().lower(), str(c).strip().lower()) for c in columns}
    return [c for c in required if _ALIASES.get(c, c) not in have]


# ----------------------------------------------------------------------------
# CSV -> SQLite (safe swap, real progress)
# ----------------------------------------------------------------------------
def _clean_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    chunk.columns = [str(c).strip() for c in chunk.columns]
    for col in chunk.columns:
        series = chunk[col]
        if series.dtype == object or pd.api.types.is_string_dtype(series):
            try:
                chunk[col] = series.str.strip()
            except Exception:
                pass
    return chunk


def load_csv(
    csv_path: str,
    table_name: str,
    engine=None,
    progress_callback: ProgressCB = None,
    chunksize: int = 100_000,
) -> int:
    """
    Stream a CSV into SQLite and replace `table_name`.
    The data is written to `<table>__loading` first; the live table is swapped in with a
    single short transaction, so readers/the dashboard are never blocked for long.
    progress_callback(progress=<0-100>, rows_loaded=<int>) is called after every chunk
    (progress is based on the real bytes read from the file).
    Returns the number of rows loaded.
    """
    table = _ident(table_name)
    tmp_table = f"{table}__loading"
    csv_path = str(csv_path)
    total_bytes = max(os.path.getsize(csv_path), 1)

    conn = _connect(engine)
    rows = 0
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{tmp_table}"')
        conn.commit()

        with open(csv_path, "rb") as handle:
            reader = pd.read_csv(handle, chunksize=chunksize, low_memory=False)
            for chunk in reader:
                chunk = _clean_chunk(chunk)
                chunk.to_sql(tmp_table, conn, if_exists="append", index=False)
                rows += len(chunk)
                if progress_callback:
                    try:
                        pct = min(99.0, handle.tell() / total_bytes * 100.0)
                    except Exception:
                        pct = 0.0
                    progress_callback(progress=pct, rows_loaded=rows)

        if rows == 0:
            raise ValueError("The CSV file contains no data rows.")

        # Atomic swap (short write lock).
        conn.commit()
        conn.isolation_level = None  # manual transaction control
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.execute(f'ALTER TABLE "{tmp_table}" RENAME TO "{table}"')
            _ensure_meta(conn)
            conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)", (f"rows_{table}", str(rows)))
            if table in BIG_TABLES or table in ("vendors", "products"):
                conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('aggregates_stale', '1')")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if progress_callback:
            progress_callback(progress=100.0, rows_loaded=rows)
        return rows
    except Exception:
        try:
            conn.isolation_level = ""
            conn.execute(f'DROP TABLE IF EXISTS "{tmp_table}"')
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# vendor_summary  (129 rows, built from two GROUP BY scans)
# ----------------------------------------------------------------------------
def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def create_vendor_summary(engine=None, progress_callback: ProgressCB = None, build_aggs: bool = False) -> int:
    """
    Rebuild vendor_summary from purchases + sales using SQL aggregation only.
    Columns: VendorNumber, VendorName, total_sales, total_purchase, gross_profit,
             profit_margin_pct, total_purchase_qty, total_sales_qty, sell_through_pct, avg_price
    """
    conn = _connect(engine)
    try:
        pc = _columns(conn, "purchases")
        sc = _columns(conn, "sales")
        if not pc or not sc:
            raise RuntimeError("Both 'purchases' and 'sales' tables are required to build vendor_summary.")

        p_vendor = _pick(pc, "VendorNumber", "VendorNo")
        p_name = _pick(pc, "VendorName")
        p_dollars = _pick(pc, "Dollars")
        p_qty = _pick(pc, "Quantity")
        s_vendor = _pick(sc, "VendorNo", "VendorNumber")
        s_name = _pick(sc, "VendorName")
        s_dollars = _pick(sc, "SalesDollars")
        s_qty = _pick(sc, "SalesQuantity")
        if not all([p_vendor, p_dollars, p_qty, s_vendor, s_dollars, s_qty]):
            raise RuntimeError("purchases/sales are missing vendor, dollars or quantity columns.")

        if progress_callback:
            progress_callback(0, 3, "Aggregating purchases by vendor")
        p_name_sql = f'MAX("{p_name}")' if p_name else "NULL"
        purchases = pd.read_sql_query(
            f'SELECT "{p_vendor}" AS vendor_no, {p_name_sql} AS p_name, '
            f'SUM("{p_dollars}") AS total_purchase, SUM("{p_qty}") AS total_purchase_qty '
            f'FROM purchases GROUP BY "{p_vendor}"',
            conn,
        )
        if progress_callback:
            progress_callback(1, 3, "Aggregating sales by vendor (largest table - please wait)")
        s_name_sql = f'MAX("{s_name}")' if s_name else "NULL"
        sales = pd.read_sql_query(
            f'SELECT "{s_vendor}" AS vendor_no, {s_name_sql} AS s_name, '
            f'SUM("{s_dollars}") AS total_sales, SUM("{s_qty}") AS total_sales_qty '
            f'FROM sales GROUP BY "{s_vendor}"',
            conn,
        )
        if progress_callback:
            progress_callback(2, 3, "Writing vendor_summary")

        for df in (purchases, sales):
            df["vendor_no"] = _num(df["vendor_no"])
        purchases = purchases.dropna(subset=["vendor_no"])
        sales = sales.dropna(subset=["vendor_no"])

        merged = purchases.merge(sales, on="vendor_no", how="outer")
        merged["VendorName"] = merged["s_name"].where(merged["s_name"].notna(), merged["p_name"])
        for col in ("total_sales", "total_purchase", "total_sales_qty", "total_purchase_qty"):
            merged[col] = _num(merged[col]).fillna(0.0)

        merged["gross_profit"] = merged["total_sales"] - merged["total_purchase"]
        merged["profit_margin_pct"] = (
            merged["gross_profit"] / merged["total_sales"].where(merged["total_sales"] != 0) * 100.0
        ).fillna(0.0)
        merged["sell_through_pct"] = (
            merged["total_sales_qty"] / merged["total_purchase_qty"].where(merged["total_purchase_qty"] != 0) * 100.0
        ).fillna(0.0)
        merged["avg_price"] = (
            merged["total_sales"] / merged["total_sales_qty"].where(merged["total_sales_qty"] != 0)
        ).fillna(0.0)
        merged["VendorNumber"] = merged["vendor_no"].astype("int64")

        # Fill any missing names from the vendors table.
        if _columns(conn, "vendors"):
            vcols = _columns(conn, "vendors")
            vn, vname = _pick(vcols, "VendorNumber"), _pick(vcols, "VendorName")
            if vn and vname:
                names = pd.read_sql_query(f'SELECT "{vn}" AS vendor_no, "{vname}" AS v_name FROM vendors', conn)
                names["vendor_no"] = _num(names["vendor_no"])
                names = names.dropna(subset=["vendor_no"]).drop_duplicates("vendor_no")
                merged = merged.merge(names, on="vendor_no", how="left")
                merged["VendorName"] = merged["VendorName"].where(merged["VendorName"].notna(), merged["v_name"])

        out = merged[
            [
                "VendorNumber", "VendorName", "total_sales", "total_purchase", "gross_profit",
                "profit_margin_pct", "total_purchase_qty", "total_sales_qty", "sell_through_pct", "avg_price",
            ]
        ].sort_values("total_sales", ascending=False)

        out.to_sql("vendor_summary", conn, if_exists="replace", index=False)
        conn.commit()
        _ensure_meta(conn)
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES ('summary_built_at', ?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        conn.commit()
        if progress_callback:
            progress_callback(3, 3, "vendor_summary ready")
    finally:
        conn.close()

    if build_aggs:
        build_aggregates(engine, progress_callback)
    return len(out)


# ----------------------------------------------------------------------------
# Aggregate tables used by the dashboard (built once, tiny)
# ----------------------------------------------------------------------------
def _month_from_dates(values: pd.Series) -> pd.Series:
    """Convert raw date strings (any common format) to 'YYYY-MM'."""
    s = values.astype("object").where(values.notna(), None)
    try:
        parsed = pd.to_datetime(pd.Series(s), errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(pd.Series(s), errors="coerce")
    return parsed.dt.strftime("%Y-%m")


def build_aggregates(engine=None, progress_callback: ProgressCB = None) -> None:
    """
    Build agg_vendor_store_class, agg_vendor_month, agg_vendor_product with SQL GROUP BY.
    Slow once (the sales table is scanned 3 times), instant to read afterwards.
    progress_callback(done_steps, total_steps, message)
    """
    total = 7
    conn = _connect(engine)

    def tick(done: int, message: str) -> None:
        if progress_callback:
            progress_callback(done, total, message)

    try:
        pc, sc = _columns(conn, "purchases"), _columns(conn, "sales")
        if not pc or not sc:
            raise RuntimeError("Both 'purchases' and 'sales' tables are required.")

        p = {k: _pick(pc, *v) for k, v in {
            "vendor": ("VendorNumber", "VendorNo"), "store": ("Store",), "cls": ("Classification",),
            "dollars": ("Dollars",), "qty": ("Quantity",), "date": ("PODate", "ReceivingDate", "InvoiceDate"),
            "brand": ("Brand",), "desc": ("Description",),
        }.items()}
        s = {k: _pick(sc, *v) for k, v in {
            "vendor": ("VendorNo", "VendorNumber"), "store": ("Store",), "cls": ("Classification",),
            "dollars": ("SalesDollars",), "qty": ("SalesQuantity",), "date": ("SalesDate",),
            "brand": ("Brand",), "desc": ("Description",),
        }.items()}
        for label, mapping, need in (
            ("purchases", p, ("vendor", "dollars", "qty")),
            ("sales", s, ("vendor", "dollars", "qty")),
        ):
            absent = [n for n in need if not mapping.get(n)]
            if absent:
                raise RuntimeError(f"{label} table is missing columns for: {', '.join(absent)}")

        def col(mapping, key, fallback="'unknown'"):
            return f'"{mapping[key]}"' if mapping.get(key) else fallback

        # ---- 1/2 vendor x store x classification ----------------------------------------
        tick(0, "Aggregating purchases by vendor / store / classification")
        pv = pd.read_sql_query(
            f'SELECT {col(p, "vendor")} AS vendor_no, {col(p, "store")} AS store, {col(p, "cls")} AS classification, '
            f'SUM({col(p, "dollars")}) AS purchases, SUM({col(p, "qty")}) AS purchase_qty '
            f"FROM purchases GROUP BY 1, 2, 3",
            conn,
        )
        tick(1, "Aggregating sales by vendor / store / classification (largest table)")
        sv = pd.read_sql_query(
            f'SELECT {col(s, "vendor")} AS vendor_no, {col(s, "store")} AS store, {col(s, "cls")} AS classification, '
            f'SUM({col(s, "dollars")}) AS sales, SUM({col(s, "qty")}) AS sales_qty '
            f"FROM sales GROUP BY 1, 2, 3",
            conn,
        )
        for df in (pv, sv):
            df["vendor_no"] = _num(df["vendor_no"])
            df["store"] = df["store"].astype(str)
            df["classification"] = df["classification"].astype(str)
        vsc = pv.dropna(subset=["vendor_no"]).merge(
            sv.dropna(subset=["vendor_no"]), on=["vendor_no", "store", "classification"], how="outer"
        )
        for c in ("sales", "sales_qty", "purchases", "purchase_qty"):
            vsc[c] = _num(vsc[c]).fillna(0.0)
        vsc["vendor_no"] = vsc["vendor_no"].astype("int64")

        # ---- 3/4 vendor x month ---------------------------------------------------------
        tick(2, "Aggregating purchases by vendor / month")
        if p.get("date"):
            pm = pd.read_sql_query(
                f'SELECT {col(p, "vendor")} AS vendor_no, {col(p, "date")} AS d, '
                f'SUM({col(p, "dollars")}) AS purchases, SUM({col(p, "qty")}) AS purchase_qty '
                f"FROM purchases GROUP BY 1, 2",
                conn,
            )
        else:
            pm = pd.DataFrame(columns=["vendor_no", "d", "purchases", "purchase_qty"])
        tick(3, "Aggregating sales by vendor / month (largest table)")
        if s.get("date"):
            sm = pd.read_sql_query(
                f'SELECT {col(s, "vendor")} AS vendor_no, {col(s, "date")} AS d, '
                f'SUM({col(s, "dollars")}) AS sales, SUM({col(s, "qty")}) AS sales_qty '
                f"FROM sales GROUP BY 1, 2",
                conn,
            )
        else:
            sm = pd.DataFrame(columns=["vendor_no", "d", "sales", "sales_qty"])

        def to_monthly(df: pd.DataFrame, value_cols: list[str]) -> pd.DataFrame:
            if df.empty:
                return pd.DataFrame(columns=["vendor_no", "month"] + value_cols)
            df = df.copy()
            df["vendor_no"] = _num(df["vendor_no"])
            df["month"] = _month_from_dates(df["d"])
            df = df.dropna(subset=["vendor_no", "month"])
            for c in value_cols:
                df[c] = _num(df[c]).fillna(0.0)
            return df.groupby(["vendor_no", "month"], as_index=False)[value_cols].sum()

        vm = to_monthly(pm, ["purchases", "purchase_qty"]).merge(
            to_monthly(sm, ["sales", "sales_qty"]), on=["vendor_no", "month"], how="outer"
        )
        for c in ("sales", "sales_qty", "purchases", "purchase_qty"):
            if c not in vm.columns:
                vm[c] = 0.0
            vm[c] = _num(vm[c]).fillna(0.0)
        vm["vendor_no"] = vm["vendor_no"].astype("int64") if len(vm) else vm["vendor_no"]

        # ---- 5/6 vendor x product (brand) -----------------------------------------------
        tick(4, "Aggregating purchases by vendor / brand")
        pdesc = f'MAX({col(p, "desc", "NULL")})'
        pp = pd.read_sql_query(
            f'SELECT {col(p, "vendor")} AS vendor_no, {col(p, "brand", "0")} AS brand, {pdesc} AS p_desc, '
            f'SUM({col(p, "dollars")}) AS purchases, SUM({col(p, "qty")}) AS purchase_qty '
            f"FROM purchases GROUP BY 1, 2",
            conn,
        )
        tick(5, "Aggregating sales by vendor / brand (largest table)")
        sdesc = f'MAX({col(s, "desc", "NULL")})'
        sp = pd.read_sql_query(
            f'SELECT {col(s, "vendor")} AS vendor_no, {col(s, "brand", "0")} AS brand, {sdesc} AS s_desc, '
            f'SUM({col(s, "dollars")}) AS sales, SUM({col(s, "qty")}) AS sales_qty '
            f"FROM sales GROUP BY 1, 2",
            conn,
        )
        for df in (pp, sp):
            df["vendor_no"] = _num(df["vendor_no"])
            df["brand"] = df["brand"].astype(str)
        vp = pp.dropna(subset=["vendor_no"]).merge(
            sp.dropna(subset=["vendor_no"]), on=["vendor_no", "brand"], how="outer"
        )
        vp["description"] = vp["s_desc"].where(vp["s_desc"].notna(), vp["p_desc"]).fillna("(unnamed)")
        for c in ("sales", "sales_qty", "purchases", "purchase_qty"):
            vp[c] = _num(vp[c]).fillna(0.0)
        vp["vendor_no"] = vp["vendor_no"].astype("int64")
        vp = vp[["vendor_no", "brand", "description", "sales", "sales_qty", "purchases", "purchase_qty"]]

        # ---- 7 write ------------------------------------------------------------------
        tick(6, "Writing aggregate tables")
        vsc.to_sql("agg_vendor_store_class", conn, if_exists="replace", index=False)
        vm.to_sql("agg_vendor_month", conn, if_exists="replace", index=False)
        vp.to_sql("agg_vendor_product", conn, if_exists="replace", index=False)
        conn.execute('CREATE INDEX IF NOT EXISTS ix_avsc_vendor ON agg_vendor_store_class(vendor_no)')
        conn.execute('CREATE INDEX IF NOT EXISTS ix_avm_vendor ON agg_vendor_month(vendor_no)')
        conn.execute('CREATE INDEX IF NOT EXISTS ix_avp_vendor ON agg_vendor_product(vendor_no)')
        _ensure_meta(conn)
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('aggregates_stale', '0')")
        conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('aggregates_built_at', ?)", (now,))
        for t in BIG_TABLES:
            row = conn.execute(f'SELECT MAX(_rowid_) FROM "{t}"').fetchone()
            conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)", (f"rows_{t}", str(int(row[0] or 0))))
        conn.commit()
        tick(7, "Aggregates ready")
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Data quality (sampled - never a full scan)
# ----------------------------------------------------------------------------
_DATE_COLUMNS = {
    "purchases": ["PODate", "ReceivingDate", "InvoiceDate", "PayDate"],
    "sales": ["SalesDate"],
}


def get_data_quality(engine=None, sample_size: int = 200_000, tables: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Null / duplicate / invalid-date check on the first `sample_size` rows of each table.
    Row counts are exact-but-fast (see get_row_count). Returns a DataFrame.
    """
    tables = tables or ["vendors", "products", "purchases", "sales", "vendor_summary"]
    records = []
    conn = _connect(engine)
    try:
        for t in tables:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                continue
            sample = pd.read_sql_query(f'SELECT * FROM "{_ident(t)}" LIMIT {int(sample_size)}', conn)
            cells = max(sample.shape[0] * sample.shape[1], 1)
            null_cells = int(sample.isna().sum().sum())
            dupes = int(sample.duplicated().sum())
            invalid_dates = 0
            lookup = {c.lower(): c for c in sample.columns}
            for dcol in _DATE_COLUMNS.get(t, []):
                real = lookup.get(dcol.lower())
                if real is None:
                    continue
                raw = sample[real].dropna()
                if raw.empty:
                    continue
                try:
                    parsed = pd.to_datetime(raw, errors="coerce", format="mixed")
                except (TypeError, ValueError):
                    parsed = pd.to_datetime(raw, errors="coerce")
                invalid_dates += int(parsed.isna().sum())
            records.append(
                {
                    "Table": t,
                    "Rows": get_row_count(t, engine=engine),
                    "Rows Checked": int(len(sample)),
                    "Null Cells": null_cells,
                    "Null %": round(null_cells / cells * 100.0, 3),
                    "Duplicate Rows": dupes,
                    "Invalid Dates": invalid_dates,
                }
            )
    finally:
        conn.close()
    return pd.DataFrame(records)


# ----------------------------------------------------------------------------
# Reset (explicit use only - the dashboard never calls this)
# ----------------------------------------------------------------------------
def reset_database(engine=None) -> bool:
    """Drop all known tables. DESTRUCTIVE - only call on purpose."""
    conn = _connect(engine)
    try:
        for t in ALL_KNOWN_TABLES:
            conn.execute(f'DROP TABLE IF EXISTS "{_ident(t)}"')
        conn.commit()
        return True
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _cli_progress(*args, **kwargs) -> None:
    if len(args) >= 3:
        print(f"[{args[0]}/{args[1]}] {args[2]}", flush=True)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "help"
    eng = get_engine()
    try:
        if command in ("summary", "all"):
            print(f"vendor_summary rebuilt: {create_vendor_summary(eng, _cli_progress)} vendors")
        if command in ("aggregates", "all"):
            build_aggregates(eng, _cli_progress)
            print("Aggregate tables built.")
        if command == "counts":
            for name in ["vendors", "products", "purchases", "sales", "vendor_summary"] + AGG_TABLES:
                print(f"{name:26s} {get_row_count(name, engine=eng):>12,}")
        if command not in ("summary", "aggregates", "all", "counts"):
            print(__doc__)
    finally:
        eng.dispose()
