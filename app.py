"""
Vendor Performance Intelligence - Executive Dashboard + Agentic AI layer
Run:  streamlit run app.py

Performance contract
--------------------
* On start-up only vendor_summary (129 rows), vendors and products are read.
* sales / purchases (millions of rows) are NEVER read into pandas. Charts use the small
  agg_* tables created once by src.database.build_aggregates().
* KPI cards are always GLOBAL (from vendor_summary). Filters only affect tables/charts.
"""
from __future__ import annotations

import copy
import html as _html
import io
import itertools
import json
import math
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import text

try:  # optional: lets you keep API keys in a .env file
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

from src.database import (
    DB_PATH,
    REQUIRED_COLUMNS,  # noqa: F401  (kept so the old import contract still works)
    build_aggregates,
    create_vendor_summary,
    db_file_size,
    get_data_quality,
    get_engine,
    get_meta,
    get_row_count,
    load_csv,
    missing_columns,
    table_exists,
)


# ----------------------------------------------------------------------------
# AUTO-BOOTSTRAP FROM data/ - for Streamlit Cloud
# ----------------------------------------------------------------------------
try:
    if get_row_count('vendor_summary') != 129:
        _data = DB_PATH.parent / 'data'
        _need = {'vendors': _data / 'vendors.csv', 'products': _data / 'products.csv', 'vendor_summary': _data / 'vendor_summary.csv'}
        if all(f.is_file() for f in _need.values()):
            try:
                DB_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            print('Auto-bootstrap: loading pre-built vendor_summary (452M full data) ...')
            _eng = get_engine()
            try:
                for _tbl in ['vendors', 'products', 'vendor_summary']:
                    load_csv(str(_need[_tbl]), _tbl, _eng)
                try:
                    build_aggregates(_eng)
                except Exception as _be:
                    print(f'build_aggregates skipped: {_be}')
            finally:
                _eng.dispose()
            print('Auto-bootstrap done')
except Exception as _e:
    print(f'Auto-bootstrap skipped: {_e}')

# ----------------------------------------------------------------------------
# Page config + constants
# ----------------------------------------------------------------------------
st.set_page_config(page_title="Vendor Performance Intelligence", layout="wide", initial_sidebar_state="expanded")

NAVY = "#142238"
BLUE = "#2A5298"
TEAL = "#1B998B"
RED = "#C0392B"
AMBER = "#E69F00"
GREEN = "#2E8B57"
PALETTE = ["#2A5298", "#1B998B", "#E69F00", "#C0392B", "#6C5CE7", "#4DA3D9", "#8E9AAF", "#2E8B57"]

EXPECTED = {"sales": 452_062_952, "purchases": 321_900_766, "profit": 130_162_186, "margin": 28.79, "vendors": 129}
DATA_DIR = DB_PATH.parent / "data"
_PLOT_KEYS = itertools.count()  # re-created on every rerun (module is re-executed)

FILTER_DEFAULTS = {
    "f_search": "",
    "f_profit": "All vendors",
    "f_margin": (-100.0, 100.0),
    "f_minsales": 0.0,
    "f_store": [],
    "f_class": [],
    "f_months": None,
}
PROFIT_OPTIONS = ["All vendors", "Profitable only", "Loss-making only", "Low margin (below 10%)"]

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, .stApp { font-family: 'Inter','Segoe UI',Roboto,Arial,sans-serif; }
.stApp { background: #F4F7FB; }
.block-container { padding-top: 1rem; padding-bottom: 2rem; max-width: 1500px; }
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer { visibility: hidden; }

/* Sidebar */
section[data-testid="stSidebar"] { background: #142238; }
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
section[data-testid="stSidebar"] label p,
section[data-testid="stSidebar"] .stMarkdown p,
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color: #D5DEEC !important; }
.sb-brand { display:flex; align-items:center; gap:10px; padding:4px 0 14px 0; border-bottom:1px solid rgba(255,255,255,.12); margin-bottom:14px; }
.sb-logo { width:38px; height:38px; border-radius:9px; background:linear-gradient(135deg,#2A5298,#1B998B); color:#fff; font-weight:800; font-size:.85rem; display:flex; align-items:center; justify-content:center; letter-spacing:.5px; }
.sb-brand-text { color:#fff; font-weight:700; font-size:.95rem; line-height:1.1; }
.sb-brand-sub { color:#9FB2CF; font-weight:500; font-size:.7rem; }
.sb-title { color:#9FB2CF; font-size:.7rem; font-weight:700; letter-spacing:1.4px; text-transform:uppercase; margin:14px 0 6px 0; }
.sb-badge { display:inline-block; background:#C0392B; color:#fff; border-radius:999px; font-size:.72rem; font-weight:700; padding:1px 9px; margin-left:6px; }
.sb-alert { background:rgba(255,255,255,.06); border-left:3px solid #E69F00; border-radius:6px; padding:6px 9px; margin-bottom:6px; }
.sb-alert.crit { border-left-color:#E5534B; }
.sb-alert-name { color:#fff; font-size:.78rem; font-weight:600; }
.sb-alert-why { color:#B9C7DD; font-size:.7rem; }

/* Header */
.vpi-header { background:linear-gradient(120deg,#0B1B33 0%,#16335E 55%,#2A5298 100%); border-radius:14px; padding:18px 24px; display:flex; align-items:center; gap:16px; box-shadow:0 6px 18px rgba(20,34,56,.25); margin-bottom:12px; }
.vpi-logo { width:54px; height:54px; border-radius:12px; background:linear-gradient(135deg,#1B998B,#2A5298); border:1px solid rgba(255,255,255,.35); color:#fff; font-weight:800; font-size:1.15rem; letter-spacing:1px; display:flex; align-items:center; justify-content:center; }
.vpi-title { color:#fff; font-size:1.55rem; font-weight:800; letter-spacing:.2px; line-height:1.15; }
.vpi-sub { color:#B8C9E4; font-size:.85rem; font-weight:500; margin-top:2px; }
.vpi-meta { margin-left:auto; text-align:right; color:#B8C9E4; font-size:.78rem; line-height:1.5; }
.vpi-meta b { color:#fff; }

/* KPI cards */
.kpi { background:#fff; border-radius:12px; padding:14px 18px; box-shadow:0 2px 10px rgba(20,34,56,.08); border-left:6px solid #2A5298; height:100%; }
.kpi-label { color:#5B6B86; font-size:.72rem; font-weight:700; letter-spacing:1.1px; text-transform:uppercase; }
.kpi-value { color:#142238; font-size:1.75rem; font-weight:800; line-height:1.25; margin:2px 0; white-space:nowrap; }
.kpi-delta { color:#5B6B86; font-size:.8rem; font-weight:500; }
.kpi-delta b { color:#142238; }

/* Filter strip */
.fstrip { background:#fff; border-radius:10px; padding:10px 16px; margin:12px 0 14px 0; box-shadow:0 1px 6px rgba(20,34,56,.07); display:flex; flex-wrap:wrap; gap:8px 26px; align-items:center; font-size:.85rem; color:#32476B; }
.fstrip b { color:#142238; }
.fstrip .pill { background:#E8EEF8; color:#16335E; border-radius:999px; padding:2px 11px; font-weight:700; }

/* Cards */
div[data-testid="stVerticalBlockBorderWrapper"] { background:#fff; border-radius:12px !important; box-shadow:0 2px 10px rgba(20,34,56,.07); }
.card-title { color:#142238; font-size:.95rem; font-weight:700; margin:2px 0 4px 0; }
.card-sub { color:#6B7A94; font-size:.75rem; margin-bottom:4px; }
.insight { background:#fff; border-radius:12px; padding:12px 16px; box-shadow:0 2px 10px rgba(20,34,56,.07); border-top:3px solid #1B998B; color:#32476B; font-size:.85rem; height:100%; }
.insight b { color:#142238; }
h4 { color:#142238; font-weight:700; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap:4px; background:#fff; padding:6px; border-radius:10px; box-shadow:0 1px 6px rgba(20,34,56,.07); }
.stTabs [data-baseweb="tab"] { height:38px; padding:0 14px; border-radius:8px; font-weight:600; }
.stTabs [data-baseweb="tab"] p { color:#32476B; font-weight:600; font-size:.86rem; }
.stTabs [aria-selected="true"] { background:#142238; }
.stTabs [aria-selected="true"] p { color:#fff; }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display:none; }

.vpi-footer { margin-top:26px; padding:12px 4px; border-top:1px solid #D7E0EE; color:#6B7A94; font-size:.78rem; display:flex; flex-wrap:wrap; gap:6px 22px; }
.vpi-footer b { color:#142238; }

/* Icons, badges, bullets, sidebar answer */
.kpi-head { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.kpi-icon { width:34px; height:34px; border-radius:9px; display:flex; align-items:center; justify-content:center; flex:none; }
.kpi-icon svg { width:18px; height:18px; stroke:#fff; fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
.badge { display:inline-block; border-radius:999px; padding:2px 11px; font-size:.75rem; font-weight:700; color:#fff; }
.b-red { background:#C0392B; } .b-yellow { background:#E69F00; } .b-green { background:#27AE60; } .b-grey { background:#8E9AAF; }
.ai-bullet { background:#F4F7FB; border-left:4px solid #1B998B; border-radius:8px; padding:8px 12px; margin:6px 0; color:#32476B; font-size:.88rem; }
.ai-bullet b { color:#142238; }
.sb-answer { background:rgba(255,255,255,.08); border-left:3px solid #1B998B; border-radius:6px; padding:8px 10px; margin:8px 0; color:#E8EEF8; font-size:.8rem; line-height:1.45; }
.sb-answer b { color:#fff; }

@media print {
  section[data-testid="stSidebar"], header[data-testid="stHeader"], .stButton, .stDownloadButton, [data-testid="stChatInput"] { display:none !important; }
  .stApp { background:#fff; }
  .block-container { max-width:100%; padding:0; }
  .vpi-header { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
</style>
"""


# ----------------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------------
def esc(x) -> str:
    return _html.escape(str(x), quote=True)


def money(x) -> str:
    if x is None or pd.isna(x):
        return "-"
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def money_short(x) -> str:
    if x is None or pd.isna(x):
        return "-"
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def pct(x, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "-"
    if x < -1000:  # e.g. the -476,000% edge case: readable instead of a 7-digit number
        return "below -1,000%"
    return f"{x:.{digits}f}%"


def label_class(v) -> str:
    s = str(v)
    return f"Class {s}" if s.replace(".0", "").isdigit() else s


def label_store(v) -> str:
    s = str(v)
    return f"Store {s}" if s.replace(".0", "").isdigit() else s


_ST_VER = tuple(int(p) for p in re.findall(r"\d+", st.__version__)[:2]) if hasattr(st, "__version__") else (1, 40)


def _stretch(preferred_new: bool) -> dict:
    if preferred_new:
        return {"width": "stretch"} if _ST_VER >= (1, 50) else {"use_container_width": True}
    return {"use_container_width": True} if _ST_VER < (1, 50) else {"width": "stretch"}


def show_df(data, height: int | None = None) -> None:
    kwargs = {"hide_index": True}
    if height:
        kwargs["height"] = height
    try:
        st.dataframe(data, **_stretch(True), **kwargs)
    except Exception:
        st.dataframe(data, use_container_width=True, **kwargs)


def show_plot(fig, height: int = 360) -> None:
    fig.update_layout(
        height=height,
        template="plotly_white",
        font=dict(family="Inter, Segoe UI, Arial, sans-serif", size=12, color="#1B2A41"),
        margin=dict(l=10, r=10, t=36, b=10),
        paper_bgcolor="white",
        plot_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    key = f"plot_{next(_PLOT_KEYS)}"
    try:
        st.plotly_chart(fig, key=key, **_stretch(True))
    except Exception:
        st.plotly_chart(fig, key=key, use_container_width=True)


def styled(df: pd.DataFrame, money_cols=(), pct_cols=(), int_cols=(), dec_cols=()):
    fmt = {}
    for c in money_cols:
        if c in df.columns:
            fmt[c] = "${:,.0f}"
    for c in pct_cols:
        if c in df.columns:
            fmt[c] = "{:.2f}%"
    for c in int_cols:
        if c in df.columns:
            fmt[c] = "{:,.0f}"
    for c in dec_cols:
        if c in df.columns:
            fmt[c] = "{:,.1f}"
    return df.style.format(fmt, na_rep="-")


def color_cells(styler, column: str, mapping: dict):
    def _c(v):
        colr = mapping.get(v)
        return f"background-color:{colr}; color:#fff; font-weight:600;" if colr else ""

    fn = getattr(styler, "map", None) or getattr(styler, "applymap")
    return fn(_c, subset=[column])


GRADE_COLORS = {"A+": "#1E8449", "A": "#27AE60", "B+": "#2E86C1", "B": "#5DADE2", "C": "#E69F00", "D": "#C0392B"}
RISK_COLORS = {"Low": "#27AE60", "Medium": "#E69F00", "High": "#E67E22", "Critical": "#C0392B"}
BAND_COLORS = {"Green": "#27AE60", "Yellow": "#E69F00", "Red": "#C0392B"}
DELAY_COLORS = {"Low": "#27AE60", "Medium": "#E69F00", "High": "#C0392B"}


def card(title: str, subtitle: str = ""):
    box = st.container(border=True)
    with box:
        st.markdown(f"<div class='card-title'>{title}</div>", unsafe_allow_html=True)
        if subtitle:
            st.markdown(f"<div class='card-sub'>{subtitle}</div>", unsafe_allow_html=True)
    return box


def hbar(labels, values, title: str, color: str = BLUE, as_money: bool = True) -> go.Figure:
    labels = list(labels)
    values = list(values)
    texts = [money_short(v) if as_money else f"{v:,.2f}" for v in values]
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker_color=color, text=texts, textposition="auto"))
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(title=dict(text=title, font=dict(size=13)), showlegend=False)
    return fig


# ----------------------------------------------------------------------------
# Data access (all small tables, all cached)
# ----------------------------------------------------------------------------
def run_query(sql: str) -> pd.DataFrame:
    engine = get_engine()
    try:
        with engine.connect() as conn:
            return pd.read_sql_query(text(sql), conn)
    finally:
        engine.dispose()


def data_version() -> float:
    """Changes whenever the database file changes -> invalidates the caches."""
    latest = 0.0
    for suffix in ("", "-wal"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            latest = max(latest, p.stat().st_mtime)
    return latest


SUMMARY_RENAMES = {
    "vendornumber": "vendor_no", "vendorno": "vendor_no", "vendor_number": "vendor_no", "vendor_no": "vendor_no",
    "vendorname": "vendor_name", "vendor_name": "vendor_name",
    "total_sales": "sales", "sales": "sales",
    "total_purchase": "purchases", "total_purchases": "purchases", "purchases": "purchases",
    "gross_profit": "profit", "profit": "profit",
    "profit_margin_pct": "margin_given", "profit_margin": "margin_given",
    "total_sales_qty": "sales_qty", "total_purchase_qty": "purchase_qty",
    "sell_through_pct": "sell_through", "avg_price": "avg_price",
}


def _numcol(df: pd.DataFrame, name: str, default: float = 0.0) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype="float64")


def add_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Vendor Score = Margin*0.4 + Relative Sales*0.3 + Sell-Through*0.3 (always on ALL vendors)."""
    df = df.copy()
    top_sales = df["sales"].max() if len(df) else 0
    df["rel_sales"] = (df["sales"] / top_sales * 100.0) if top_sales and top_sales > 0 else 0.0
    df["score"] = (
        df["margin"].clip(0, 100) * 0.4 + df["rel_sales"].clip(0, 100) * 0.3 + df["sell_through"].clip(0, 100) * 0.3
    )
    p = df["score"].rank(pct=True, method="average") if len(df) else df["score"]
    df["grade"] = np.select(
        [p > 0.90, p > 0.75, p > 0.55, p > 0.35, p > 0.15],
        ["A+", "A", "B+", "B", "C"],
        default="D",
    )
    df.loc[df["margin"] < 0, "grade"] = "D"
    df["risk"] = np.select(
        [df["margin"] < 0, (df["margin"] < 10) | (df["sell_through"] < 20), (df["margin"] < 20) | (df["sell_through"] < 50)],
        ["Critical", "High", "Medium"],
        default="Low",
    )
    return df


def prepare_summary(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    df.columns = [SUMMARY_RENAMES.get(str(c).strip().lower(), str(c).strip().lower()) for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]
    if "vendor_no" not in df.columns:
        return pd.DataFrame()
    df["vendor_no"] = pd.to_numeric(df["vendor_no"], errors="coerce")
    df = df.dropna(subset=["vendor_no"])
    df["vendor_no"] = df["vendor_no"].astype("int64")
    df["vendor_name"] = df["vendor_name"].fillna("Unknown").astype(str) if "vendor_name" in df.columns else "Unknown"
    df["sales"] = _numcol(df, "sales")
    df["purchases"] = _numcol(df, "purchases")
    df["sales_qty"] = _numcol(df, "sales_qty")
    df["purchase_qty"] = _numcol(df, "purchase_qty")
    df["profit"] = _numcol(df, "profit") if "profit" in df.columns else df["sales"] - df["purchases"]
    df["margin"] = np.where(df["sales"] > 0, df["profit"] / df["sales"].where(df["sales"] > 0, 1) * 100.0, 0.0)
    if "sell_through" in df.columns:
        st_series = _numcol(df, "sell_through")
        if len(st_series) and st_series.quantile(0.95) <= 1.5:  # stored as a fraction
            st_series = st_series * 100.0
        df["sell_through"] = st_series
    else:
        df["sell_through"] = np.where(df["purchase_qty"] > 0, df["sales_qty"] / df["purchase_qty"].where(df["purchase_qty"] > 0, 1) * 100.0, 0.0)
    df["avg_price"] = _numcol(df, "avg_price") if "avg_price" in df.columns else np.where(df["sales_qty"] > 0, df["sales"] / df["sales_qty"].where(df["sales_qty"] > 0, 1), 0.0)
    keep = ["vendor_no", "vendor_name", "sales", "purchases", "profit", "margin", "sales_qty", "purchase_qty", "sell_through", "avg_price"]
    df = df[keep].drop_duplicates("vendor_no").reset_index(drop=True)
    return add_scores(df)


@st.cache_data(ttl=300, show_spinner=False)
def load_vendor_summary(version: float) -> pd.DataFrame:
    if not table_exists("vendor_summary"):
        return pd.DataFrame()
    return prepare_summary(run_query("SELECT * FROM vendor_summary LIMIT 5000"))


@st.cache_data(ttl=300, show_spinner=False)
def load_vendors(version: float) -> pd.DataFrame:
    if not table_exists("vendors"):
        return pd.DataFrame()
    df = run_query("SELECT * FROM vendors LIMIT 5000")
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "vendornumber" in df.columns:
        df = df.rename(columns={"vendornumber": "vendor_no"})
        df["vendor_no"] = pd.to_numeric(df["vendor_no"], errors="coerce")
        df = df.dropna(subset=["vendor_no"]).drop_duplicates("vendor_no")
        df["vendor_no"] = df["vendor_no"].astype("int64")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def load_products(version: float) -> pd.DataFrame:
    if not table_exists("products"):
        return pd.DataFrame()
    df = run_query("SELECT * FROM products LIMIT 100000")
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


@st.cache_data(ttl=300, show_spinner=False)
def load_agg(name: str, version: float) -> pd.DataFrame:
    if name not in ("agg_vendor_store_class", "agg_vendor_month", "agg_vendor_product"):
        return pd.DataFrame()
    if not table_exists(name):
        return pd.DataFrame()
    df = run_query(f"SELECT * FROM {name} LIMIT 500000")
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "vendor_no" in df.columns:
        df["vendor_no"] = pd.to_numeric(df["vendor_no"], errors="coerce")
        df = df.dropna(subset=["vendor_no"])
        df["vendor_no"] = df["vendor_no"].astype("int64")
    for c in ("store", "classification", "brand"):
        if c in df.columns:
            df[c] = df[c].astype(str)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def load_status(version: float) -> dict:
    return {
        "stale": str(get_meta("aggregates_stale", "0")) == "1",
        "built_at": get_meta("aggregates_built_at", None),
        "has_sales": table_exists("sales"),
        "has_purchases": table_exists("purchases"),
    }


@st.cache_data(ttl=3600, show_spinner=False)
def load_row_counts(version: float) -> dict:
    return {
        "sales": get_row_count("sales"),
        "purchases": get_row_count("purchases"),
        "vendors": get_row_count("vendors"),
        "products": get_row_count("products"),
    }


@st.cache_data(ttl=300, show_spinner=False)
def summary_quality(version: float) -> dict:
    raw = run_query("SELECT * FROM vendor_summary LIMIT 5000") if table_exists("vendor_summary") else pd.DataFrame()
    if raw.empty:
        return {"nulls": 0, "dupes": 0, "invalid": 0, "rows": 0}
    cols = {c.lower(): c for c in raw.columns}
    key = cols.get("vendornumber") or cols.get("vendorno") or raw.columns[0]
    sales_col = cols.get("total_sales")
    invalid = int((pd.to_numeric(raw[sales_col], errors="coerce") < 0).sum()) if sales_col else 0
    return {
        "nulls": int(raw.isna().sum().sum()),
        "dupes": int(raw.duplicated(subset=[key]).sum()),
        "invalid": invalid,
        "rows": int(len(raw)),
    }


# ----------------------------------------------------------------------------
# Supply chain: delay analytics (one-time SQL aggregate over purchases)
# ----------------------------------------------------------------------------
DELAY_TABLE = "agg_vendor_delay"
SLA_OPTIONS = [7, 10, 14]
_COL_ALIASES = {
    "vendor": ["vendornumber", "vendorno", "vendorid", "vendor"],
    "po": ["podate", "purchaseorderdate", "orderdate"],
    "recv": ["receivingdate", "receiveddate", "receivedate", "deliverydate"],
}


def _norm(name) -> str:
    """Lower-case and strip everything but letters/digits so PODate == po_date == PO Date."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def table_columns(table: str) -> dict:
    """normalised column name -> real column name, whatever case/underscores the CSV used."""
    if not table_exists(table):
        return {}
    info = run_query(f'PRAGMA table_info("{table}")')
    return {_norm(n): n for n in info["name"].tolist()}


def build_delay_table() -> tuple:
    """One SQL GROUP BY over purchases -> small vendor x month lead-time table. Returns (ok, message)."""
    cols = table_columns("purchases")

    def pick(key: str):
        return next((cols[a] for a in _COL_ALIASES[key] if a in cols), None)

    vcol, pcol, rcol = pick("vendor"), pick("po"), pick("recv")
    if not (vcol and pcol and rcol):
        return False, "The purchases table needs a vendor column plus PO date and receiving date columns (for example VendorNumber, PODate, ReceivingDate)."
    sql = f"""
        SELECT vendor_no, month,
               COUNT(*) AS n,
               AVG(lead) AS avg_lead,
               AVG(lead * lead) AS avg_sq,
               SUM(lead > 7) AS gt7,
               SUM(lead > 10) AS gt10,
               SUM(lead > 14) AS gt14
        FROM (
            SELECT "{vcol}" AS vendor_no,
                   strftime('%Y-%m', "{pcol}") AS month,
                   julianday("{rcol}") - julianday("{pcol}") AS lead
            FROM purchases
            WHERE "{pcol}" IS NOT NULL AND "{rcol}" IS NOT NULL
        )
        WHERE lead IS NOT NULL AND lead >= 0 AND lead < 365 AND month IS NOT NULL
        GROUP BY vendor_no, month
    """
    df = run_query(sql)
    if df.empty:
        return False, "No usable PO / receiving dates found. Dates must look like YYYY-MM-DD."
    engine = get_engine()
    try:
        df.to_sql(DELAY_TABLE, engine, if_exists="replace", index=False)
    finally:
        engine.dispose()
    return True, f"Built {len(df):,} vendor-month rows from the purchases table."


@st.cache_data(ttl=300, show_spinner=False)
def load_delay_monthly(version: float) -> pd.DataFrame:
    if not table_exists(DELAY_TABLE):
        return pd.DataFrame()
    df = run_query(f"SELECT * FROM {DELAY_TABLE} LIMIT 200000")
    df.columns = [str(c).strip().lower() for c in df.columns]
    if df.empty or "vendor_no" not in df.columns:
        return pd.DataFrame()
    df["vendor_no"] = pd.to_numeric(df["vendor_no"], errors="coerce")
    df = df.dropna(subset=["vendor_no"])
    df["vendor_no"] = df["vendor_no"].astype("int64")
    df["month"] = df["month"].astype(str)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def load_product_totals(version: float) -> pd.DataFrame:
    """Product x brand totals over ALL vendors (cached, from the small agg table)."""
    return product_frame(load_agg("agg_vendor_product", version))


# ----------------------------------------------------------------------------
# Business logic
# ----------------------------------------------------------------------------
def totals(df: pd.DataFrame) -> dict:
    sales = float(df["sales"].sum()) if len(df) else 0.0
    purchases = float(df["purchases"].sum()) if len(df) else 0.0
    profit = float(df["profit"].sum()) if len(df) else 0.0
    return {
        "sales": sales,
        "purchases": purchases,
        "profit": profit,
        "margin": (profit / sales * 100.0) if sales else 0.0,
        "vendors": int(len(df)),
    }


def apply_filters(vs: pd.DataFrame, vsc: pd.DataFrame, f: dict):
    """Returns (filtered vendor frame, store/class rows of the filtered vendors)."""
    base = vs.copy()
    scoped = vsc
    if (f["stores"] or f["classes"]) and not vsc.empty:
        mask = pd.Series(True, index=vsc.index)
        if f["stores"]:
            mask &= vsc["store"].isin(f["stores"])
        if f["classes"]:
            mask &= vsc["classification"].isin(f["classes"])
        scoped = vsc[mask]
        agg = scoped.groupby("vendor_no", as_index=False)[["sales", "sales_qty", "purchases", "purchase_qty"]].sum()
        base = base.drop(columns=["sales", "sales_qty", "purchases", "purchase_qty", "profit", "margin", "sell_through"]).merge(
            agg, on="vendor_no", how="inner"
        )
        base["profit"] = base["sales"] - base["purchases"]
        base["margin"] = np.where(base["sales"] > 0, base["profit"] / base["sales"].where(base["sales"] > 0, 1) * 100.0, 0.0)
        base["sell_through"] = np.where(
            base["purchase_qty"] > 0, base["sales_qty"] / base["purchase_qty"].where(base["purchase_qty"] > 0, 1) * 100.0, 0.0
        )
    term = f["search"].strip().lower()
    if term:
        base = base[base["vendor_name"].str.lower().str.contains(re.escape(term), na=False) | base["vendor_no"].astype(str).str.contains(re.escape(term))]
    option = f["profit"]
    if option == "Profitable only":
        base = base[base["profit"] > 0]
    elif option == "Loss-making only":
        base = base[base["profit"] < 0]
    elif option.startswith("Low margin"):
        base = base[base["margin"] < 10]
    lo, hi = f["margin"]
    if (lo, hi) != FILTER_DEFAULTS["f_margin"]:
        base = base[(base["margin"] >= lo) & (base["margin"] <= hi)]
    if f["minsales"] > 0:
        base = base[base["sales"] >= f["minsales"]]
    base = base.reset_index(drop=True)
    if not vsc.empty:
        scoped = scoped[scoped["vendor_no"].isin(base["vendor_no"])]
    return base, scoped


def alerts_frame(vs: pd.DataFrame) -> pd.DataFrame:
    a = vs[(vs["margin"] < 0) | (vs["sell_through"] < 20) | (vs["profit"] < -10_000)].copy()
    reasons = []
    for r in a.itertuples():
        why = []
        if r.profit < -10_000:
            why.append(f"loss {money_short(r.profit)}")
        elif r.margin < 0:
            why.append(f"negative margin {r.margin:.1f}%")
        if r.sell_through < 20:
            why.append(f"sell-through {r.sell_through:.0f}%")
        reasons.append(", ".join(why))
    a["why"] = reasons
    a["critical"] = (a["margin"] < 0) | (a["profit"] < -10_000)
    return a.sort_values(["critical", "profit"], ascending=[False, True])


def anomalies_frame(df: pd.DataFrame) -> pd.DataFrame:
    a = df[(df["margin"] < 0) | (df["margin"] > 60) | (df["sell_through"] < 20)].copy()
    reasons = []
    for r in a.itertuples():
        why = []
        if r.margin < 0:
            why.append("Margin < 0%")
        if r.margin > 60:
            why.append("Margin > 60%")
        if r.sell_through < 20:
            why.append("Sell-through < 20%")
        reasons.append("; ".join(why))
    a["reason"] = reasons
    return a


def vendor_table(df: pd.DataFrame, include_rank: bool = False) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "Vendor No": df["vendor_no"],
            "Vendor": df["vendor_name"],
            "Sales": df["sales"],
            "Purchases": df["purchases"],
            "Profit": df["profit"],
            "Margin %": df["margin"],
            "Sell-Through %": df["sell_through"],
            "Score": df["score"],
            "Grade": df["grade"],
            "Risk": df["risk"],
        }
    )
    if "risk_score" in df.columns:
        out["Risk Score"] = df["risk_score"]
        out["Risk Band"] = df["risk_band"]
    if include_rank:
        out.insert(0, "Rank", range(1, len(out) + 1))
    return out.reset_index(drop=True)


def render_vendor_table(df: pd.DataFrame, include_rank: bool = False, height: int = 420) -> None:
    table = vendor_table(df.head(1000), include_rank)
    s = styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %", "Sell-Through %"], dec_cols=["Score", "Risk Score"])
    s = color_cells(s, "Grade", GRADE_COLORS)
    s = color_cells(s, "Risk", RISK_COLORS)
    if "Risk Band" in table.columns:
        s = color_cells(s, "Risk Band", BAND_COLORS)
    show_df(s, height=height)


# ----------------------------------------------------------------------------
# Risk, delay, anomaly, forecast, Pareto, inventory, email and insight logic
# ----------------------------------------------------------------------------
RISK_WEIGHTS = {"margin": 35.0, "delay": 25.0, "unsold": 20.0, "consistency": 20.0}
RISK_RED, RISK_YELLOW = 60.0, 35.0
DELAY_COLS = ["orders", "avg_lead", "std_lead", "late_pct", "trend", "pred_lead", "delay_score", "delay_risk"]


def delay_scores(dm: pd.DataFrame, sla: int = 10) -> pd.DataFrame:
    """Per-vendor lead-time statistics and a simple next-month prediction.

    pred_lead = weighted mean of the last 3 months + linear trend (days/month).
    delay_score (0-100) = 50% late share + 30% predicted lead vs portfolio median + 20% volatility.
    """
    cols = ["vendor_no"] + DELAY_COLS
    if dm is None or dm.empty:
        return pd.DataFrame(columns=cols)
    late_col = {7: "gt7", 10: "gt10", 14: "gt14"}.get(int(sla), "gt10")
    rows = []
    for vno, g in dm.sort_values("month").groupby("vendor_no"):
        n = g["n"].to_numpy(dtype=float)
        total = n.sum()
        if total <= 0:
            continue
        lead = g["avg_lead"].to_numpy(dtype=float)
        avg = float((lead * n).sum() / total)
        var = float((g["avg_sq"].to_numpy(dtype=float) * n).sum() / total - avg ** 2)
        late = float(g[late_col].to_numpy(dtype=float).sum() / total * 100.0)
        slope, pred = 0.0, float(lead[-1])
        if len(g) >= 3:
            x = np.arange(len(g), dtype=float)
            slope = float(np.polyfit(x, lead, 1, w=np.sqrt(n))[0])
            pred = max(float(np.average(lead[-3:], weights=n[-3:])) + slope, 0.0)
        rows.append({"vendor_no": int(vno), "orders": int(total), "avg_lead": avg, "std_lead": math.sqrt(max(var, 0.0)),
                     "late_pct": late, "trend": slope, "pred_lead": pred})
    if not rows:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(rows)
    med = float(out["avg_lead"].median())
    if not med > 0:
        med = 1.0
    lead_idx = ((out["pred_lead"] / med - 1.0) * 100.0).clip(0, 100)
    vol_idx = (out["std_lead"] / out["avg_lead"].where(out["avg_lead"] > 0, 1.0) * 100.0).clip(0, 100)
    out["delay_score"] = (0.5 * out["late_pct"].clip(0, 100) + 0.3 * lead_idx + 0.2 * vol_idx).round(1)
    pr = out["delay_score"].rank(pct=True)
    out["delay_risk"] = np.select([pr > 0.80, (out["pred_lead"] > sla) & (pr > 0.50), pr > 0.50], ["High", "High", "Medium"], default="Low")
    return out[cols]


def risk_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Vendor Risk Score 0-100 (higher = riskier) = weighted Margin + Delay + Unsold(return proxy) + Consistency.

    Components that cannot be measured (no delay table, fewer than 3 months) are left out and the
    remaining weights are re-normalised, so a vendor is never penalised for missing data.
    """
    df = df.copy()
    comp = pd.DataFrame(index=df.index)
    comp["margin"] = ((40.0 - df["margin"]) / 40.0 * 100.0).clip(0, 100)
    comp["delay"] = pd.to_numeric(df["delay_score"], errors="coerce") if "delay_score" in df.columns else np.nan
    comp["unsold"] = (100.0 - df["sell_through"]).clip(0, 100)
    comp["consistency"] = (pd.to_numeric(df["sales_cv"], errors="coerce") * 100.0).clip(0, 100) if "sales_cv" in df.columns else np.nan
    w = pd.Series(RISK_WEIGHTS)
    wmat = comp.notna().astype(float).mul(w, axis=1)
    num = comp.fillna(0.0).mul(wmat).sum(axis=1)
    den = wmat.sum(axis=1).replace(0, np.nan)
    score = (num / den).fillna(0.0)
    df["risk_score"] = score.round(1)
    df["risk_band"] = np.select([score >= RISK_RED, score >= RISK_YELLOW], ["Red", "Yellow"], default="Green")
    for k in comp.columns:
        df[f"rk_{k}"] = comp[k]
    return df


def enrich_vendors(vs: pd.DataFrame, vm: pd.DataFrame, dm: pd.DataFrame, sla: int = 10, n_months: int = 0) -> pd.DataFrame:
    """Adds delay stats, consistency and the 0-100 risk score to the vendor summary (never mutates the cache)."""
    df = vs.copy()
    ds = delay_scores(dm, sla)
    if ds.empty:
        for c in DELAY_COLS:
            df[c] = "n/a" if c == "delay_risk" else np.nan
    else:
        df = df.merge(ds, on="vendor_no", how="left")
        df["delay_risk"] = df["delay_risk"].fillna("n/a")
    if vm is not None and not vm.empty and n_months >= 3:
        g = vm.groupby("vendor_no")["sales"]
        mean = g.mean()
        cv = (g.std(ddof=0) / mean.where(mean > 0)).rename("sales_cv").reset_index()
        df = df.merge(cv, on="vendor_no", how="left")
    else:
        df["sales_cv"] = np.nan
    return risk_scores(df)


def profit_drop_frame(vm: pd.DataFrame, names: pd.DataFrame, month=None, threshold: float = 30.0, min_prev: float = 1000.0):
    """Vendors whose profit fell by more than `threshold` % vs the previous month.

    Returns (frame, previous_month, current_month). Needs at least 2 months of monthly data."""
    cols = ["vendor_no", "vendor_name", "prev_profit", "profit", "drop_pct"]
    empty = pd.DataFrame(columns=cols)
    if vm is None or vm.empty:
        return empty, None, None
    m = vm.assign(month=vm["month"].astype(str))
    for _c in ["sales", "purchases"]:
        if _c not in m.columns:
            m[_c] = 0
    m["profit"] = m["sales"] - m["purchases"]
    months = sorted(m["month"].unique())
    if len(months) < 2:
        return empty, None, None
    cur = month if month in months else months[-1]
    i = months.index(cur)
    if i == 0:
        return empty, None, cur
    prev = months[i - 1]
    a = m[m["month"] == prev].groupby("vendor_no")["profit"].sum().rename("prev_profit")
    b = m[m["month"] == cur].groupby("vendor_no")["profit"].sum().rename("profit")
    d = pd.concat([a, b], axis=1).fillna(0.0).reset_index()
    d = d[d["prev_profit"] >= min_prev].copy()
    d["drop_pct"] = (d["prev_profit"] - d["profit"]) / d["prev_profit"] * 100.0
    d = d[d["drop_pct"] > threshold].sort_values("drop_pct", ascending=False)
    d = d.merge(names[["vendor_no", "vendor_name"]], on="vendor_no", how="left")
    d["vendor_name"] = d["vendor_name"].fillna("Unknown")
    return d[cols].reset_index(drop=True), prev, cur


def forecast_sales(monthly: pd.DataFrame, method: str = "Blend (linear + moving average)", horizon: int = 3) -> dict:
    """Next `horizon` months of sales from a linear trend, a 3-month moving average, or their blend."""
    y = monthly["sales"].to_numpy(dtype=float)
    n = len(y)
    periods = pd.PeriodIndex(monthly["month"].astype(str), freq="M")
    last = periods[-1]
    labels = [str(last + k) for k in range(1, horizon + 1)]
    x = np.arange(n, dtype=float)
    slope, intercept = (np.polyfit(x, y, 1) if n >= 2 else (0.0, float(y[-1])))
    lin = np.array([intercept + slope * (n - 1 + k) for k in range(1, horizon + 1)])
    window = min(3, n)
    hist, ma = list(y), []
    for _ in range(horizon):
        v = float(np.mean(hist[-window:]))
        ma.append(v)
        hist.append(v)
    ma = np.array(ma)
    if method == "Linear trend":
        pred = lin
    elif method == "3-month moving average":
        pred = ma
    else:
        pred = (lin + ma) / 2.0
    pred = np.clip(pred, 0, None)
    resid = float(np.std(y - (intercept + slope * x), ddof=1)) if n > 2 else 0.0
    return {"months": labels, "pred": pred, "lower": np.clip(pred - resid, 0, None), "upper": pred + resid,
            "slope": float(slope), "resid": resid, "n": n}


def pareto_frame(df: pd.DataFrame, value: str = "purchases") -> pd.DataFrame:
    """Vendors ranked by `value` with cumulative share; `vital` = the vendors needed to reach 80%."""
    cols = ["rank", "vendor_no", "vendor_name", value, "share", "cum_share", "vital"]
    d = df[df[value] > 0][["vendor_no", "vendor_name", value]].sort_values(value, ascending=False).reset_index(drop=True)
    if d.empty:
        return pd.DataFrame(columns=cols)
    d["share"] = d[value] / d[value].sum() * 100.0
    d["cum_share"] = d["share"].cumsum()
    d["vital"] = (d["cum_share"] - d["share"]) < 80.0
    d["rank"] = np.arange(1, len(d) + 1)
    return d[cols]


def inventory_frame(pf: pd.DataFrame, n_months: int, low_days: int = 15, over_days: int = 180, min_units: int = 10) -> pd.DataFrame:
    """Stock status per product.

    The database has no stock-on-hand column, so stock is ESTIMATED as units purchased - units sold
    (opening inventory is unknown). Cover days = stock / average daily units sold."""
    if pf is None or pf.empty:
        return pd.DataFrame()
    inv = pf[["brand", "description", "sales", "purchases", "sales_qty", "purchase_qty"]].copy()
    days = max(int(n_months), 1) * 30.0
    inv["stock"] = (inv["purchase_qty"] - inv["sales_qty"]).clip(lower=0)
    inv["daily_units"] = inv["sales_qty"] / days
    daily_safe = inv["daily_units"].where(inv["daily_units"] > 0, 1.0)
    inv["cover_days"] = np.where(inv["daily_units"] > 0, inv["stock"] / daily_safe, np.inf)
    pq_safe = inv["purchase_qty"].where(inv["purchase_qty"] > 0, 1.0)
    inv["avg_cost"] = np.where(inv["purchase_qty"] > 0, inv["purchases"] / pq_safe, 0.0)
    inv["stock_value"] = inv["stock"] * inv["avg_cost"]
    low = (inv["sales_qty"] >= min_units) & (inv["cover_days"] < low_days)
    over = (inv["stock"] > 0) & ((inv["cover_days"] > over_days) | (inv["sales_qty"] <= 0))
    inv["status"] = np.select([low, over], ["Low stock", "Overstock"], default="OK")
    return inv


def loss_frame(vs: pd.DataFrame) -> pd.DataFrame:
    """Loss-making vendors with divide-by-zero-safe margin labels (e.g. the -476,000% edge case)."""
    d = vs[vs["profit"] < 0].sort_values("profit").copy()

    def label(r) -> str:
        if r["sales"] <= 0:
            return "n/a (no sales)"
        if r["margin"] < -1000:
            return "below -1,000%"
        return f"{r['margin']:.1f}%"

    d["margin_label"] = d.apply(label, axis=1) if len(d) else []
    d["margin_chart"] = d["margin"].clip(-100, 100)
    return d


def _fmt_margin(margin: float, sales: float) -> str:
    if sales <= 0:
        return "n/a (no sales recorded)"
    if margin < -1000:
        return "below -1,000%"
    return f"{margin:.1f}%"


def build_email_drafts(loss: pd.DataFrame, vendors: pd.DataFrame, sender: str = "Procurement & Vendor Management") -> list:
    """Draft (never sent) alert emails for loss-making vendors."""
    emails = {}
    if vendors is not None and not vendors.empty and "vendor_no" in vendors.columns:
        ecol = next((c for c in vendors.columns if "email" in str(c).lower()), None)
        if ecol:
            emails = dict(zip(vendors["vendor_no"], vendors[ecol]))
    drafts = []
    for r in loss.head(200).itertuples():
        to = emails.get(r.vendor_no)
        to = to if isinstance(to, str) and "@" in to else "[vendor email not on file]"
        subject = f"Action required: negative margin on {r.vendor_name} account"
        body = (
            f"Dear {r.vendor_name} team,\n\n"
            "During our periodic vendor performance review we found that trading with you is currently loss-making for us:\n\n"
            f"  - Total sales of your products : {money(r.sales)}\n"
            f"  - Total purchase cost          : {money(r.purchases)}\n"
            f"  - Gross profit / (loss)        : {money(r.profit)}\n"
            f"  - Profit margin                : {_fmt_margin(r.margin, r.sales)}\n\n"
            "We would like to schedule a call within the next 7 days to discuss:\n"
            "  1. Revised purchase prices or volume discounts\n"
            "  2. Return / buy-back terms for slow-moving stock\n"
            "  3. A joint plan to bring the margin back above zero\n\n"
            "Please reply with your availability.\n\n"
            f"Regards,\n{sender}"
        )
        drafts.append({"vendor": str(r.vendor_name), "vendor_no": int(r.vendor_no), "to": to, "subject": subject, "body": body})
    return drafts


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", str(s))


def auto_insights(vs: pd.DataFrame, vm: pd.DataFrame, dead_capital: float = 0.0) -> list:
    """Five data-driven bullets: best vendor, worst vendor, profit drop, risk vendors, action needed."""
    if vs.empty:
        return []
    best = vs.loc[vs["profit"].idxmax()]
    worst = vs.loc[vs["profit"].idxmin()]
    out = [
        f"<b>Best vendor:</b> {esc(best['vendor_name'])} earns <b>{money_short(best['profit'])}</b> profit at a "
        f"<b>{pct(best['margin'], 1)}</b> margin (grade {esc(best['grade'])})."
    ]
    if worst["profit"] < 0:
        out.append(
            f"<b>Worst vendor:</b> {esc(worst['vendor_name'])} is losing <b>{money_short(-worst['profit'])}</b> "
            f"(margin {esc(_fmt_margin(worst['margin'], worst['sales']))}) on {money_short(worst['sales'])} of sales."
        )
    else:
        out.append(
            f"<b>Worst vendor:</b> {esc(worst['vendor_name'])} has the lowest profit at <b>{money_short(worst['profit'])}</b> "
            f"({pct(worst['margin'], 1)} margin). No vendor is loss-making."
        )
    drops, prev, cur = profit_drop_frame(vm, vs)
    if prev is None or cur is None:
        out.append("<b>Profit drop:</b> needs at least 2 months of monthly data. Build the analytics tables to enable it.")
    else:
        mm = vm.assign(month=vm["month"].astype(str))
        if "purchases" not in mm.columns:
            mm["purchases"] = 0
        if "sales" not in mm.columns:
            mm["sales"] = 0
        mp = (mm["sales"] - mm["purchases"]).groupby(mm["month"]).sum()
        p0, p1 = float(mp.get(prev, 0.0)), float(mp.get(cur, 0.0))
        chg = (p1 - p0) / abs(p0) * 100.0 if p0 else 0.0
        tail = f" Largest: {esc(drops.iloc[0]['vendor_name'])} ({drops.iloc[0]['drop_pct']:.0f}% down)." if len(drops) else ""
        out.append(
            f"<b>Profit drop:</b> portfolio profit moved <b>{chg:+.1f}%</b> from {esc(prev)} to {esc(cur)}; "
            f"<b>{len(drops)}</b> vendors fell by more than 30%.{tail}"
        )
    red = vs[vs["risk_band"] == "Red"].sort_values("risk_score", ascending=False)
    if red.empty:
        out.append("<b>Risk vendors:</b> no vendor is in the Red band (risk score 60+).")
    else:
        names = ", ".join(f"{esc(r.vendor_name)} ({r.risk_score:.0f})" for r in red.head(3).itertuples())
        out.append(f"<b>Risk vendors:</b> <b>{len(red)}</b> vendors are in the Red band (risk score 60+). Highest: {names}.")
    actions = []
    losers = vs[vs["profit"] < 0]
    if len(losers):
        actions.append(f"renegotiate prices with {len(losers)} loss-making vendors (combined loss {money_short(-losers['profit'].sum())})")
    high_delay = int((vs["delay_risk"] == "High").sum()) if "delay_risk" in vs.columns else 0
    if high_delay:
        actions.append(f"agree delivery SLAs with {high_delay} high delay-risk vendors")
    if dead_capital > 0:
        actions.append(f"liquidate or return {money_short(dead_capital)} of dead stock")
    if actions:
        out.append("<b>Action needed:</b> " + "; ".join(actions[:3]) + ".")
    else:
        out.append("<b>Action needed:</b> no urgent action. Keep monitoring margins and delivery times.")
    return out


# ----------------------------------------------------------------------------
# Exports (cached so they are not rebuilt on every rerun)
# ----------------------------------------------------------------------------
def export_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = vendor_table(df)
    return out.round(2)


@st.cache_data(show_spinner=False, ttl=300)
def make_csv(df: pd.DataFrame) -> bytes:
    return export_frame(df).to_csv(index=False).encode("utf-8")


@st.cache_data(show_spinner=False, ttl=300)
def make_excel(df: pd.DataFrame, summary_rows: list) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export_frame(df).to_excel(writer, sheet_name="Filtered Vendors", index=False)
        pd.DataFrame(summary_rows, columns=["Metric", "Global", "Filtered"]).to_excel(writer, sheet_name="Summary", index=False)
        for sheet in writer.sheets.values():
            for col_cells in sheet.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells[:60])
                sheet.column_dimensions[col_cells[0].column_letter].width = min(max(width + 2, 10), 42)
    return buffer.getvalue()


@st.cache_data(show_spinner=False, ttl=300)
def make_pdf(summary_rows: list, top: pd.DataFrame, bottom: pd.DataFrame, filter_text: str, risk_line: str = "") -> bytes:
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm, topMargin=11 * mm, bottomMargin=10 * mm)
    base = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=base["Title"], fontSize=17, leading=20, textColor=colors.HexColor("#142238"), alignment=0, spaceAfter=0)
    small = ParagraphStyle("s", parent=base["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#5B6B86"))
    head = ParagraphStyle("h", parent=base["Heading3"], fontSize=10, leading=12, textColor=colors.HexColor("#142238"), spaceBefore=4, spaceAfter=3)
    navy = colors.HexColor("#142238")

    def short(s, n=26):
        s = str(s)
        return s if len(s) <= n else s[: n - 1] + "..."

    kpi_data = [["Metric", "Global", "Filtered"]] + [[short(c, 30) for c in r] for r in summary_rows]
    kpi_tbl = Table(kpi_data, colWidths=[38 * mm, 36 * mm, 36 * mm])
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FB")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D7E0EE")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    chart_w, chart_h = 150 * mm, 52 * mm
    drawing = Drawing(chart_w, chart_h)
    if len(top):
        chart = HorizontalBarChart()
        chart.x, chart.y, chart.width, chart.height = 62 * mm, 6 * mm, chart_w - 68 * mm, chart_h - 12 * mm
        values = [round(v / 1e6, 2) for v in top["profit"].tolist()][::-1]
        chart.data = [values]
        chart.categoryAxis.categoryNames = [short(n, 24) for n in top["vendor_name"].tolist()][::-1]
        chart.categoryAxis.labels.fontSize = 6.5
        chart.valueAxis.labels.fontSize = 6.5
        chart.valueAxis.valueMin = min(0, min(values))
        chart.valueAxis.valueMax = max(0, max(values))
        chart.bars[0].fillColor = colors.HexColor("#2A5298")
        drawing.add(chart)

    def vendor_block(df, heading):
        rows = [["Vendor", "Sales", "Profit", "Margin"]]
        for r in df.itertuples():
            rows.append([short(r.vendor_name), money_short(r.sales), money_short(r.profit), f"{r.margin:.1f}%"])
        t = Table(rows, colWidths=[52 * mm, 24 * mm, 24 * mm, 18 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A5298")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D7E0EE")),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return [Paragraph(heading, head), t]

    top_row = Table([[kpi_tbl, drawing]], colWidths=[116 * mm, chart_w])
    top_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    lower = Table([[vendor_block(top.head(5), f"Top {min(5, len(top))} vendors by profit"), vendor_block(bottom.head(5), f"Bottom {min(5, len(bottom))} vendors by profit")]], colWidths=[125 * mm, 125 * mm])
    lower.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    story = [
        Paragraph("Vendor Performance Intelligence - One-Page Executive Summary", title),
        Paragraph(f"Generated {datetime.now():%d %b %Y %H:%M} | Filters: {filter_text}", small),
        *([Paragraph(risk_line, small)] if risk_line else []),
        Spacer(1, 5 * mm),
        top_row,
        Spacer(1, 3 * mm),
        lower,
    ]
    doc.build(story)
    return buffer.getvalue()


# ----------------------------------------------------------------------------
# Long-running actions with real progress
# ----------------------------------------------------------------------------
def run_aggregate_build() -> None:
    bar = st.progress(0.0, text="Starting...")
    ok = False

    def cb(done, total, message):
        bar.progress(min(done / max(total, 1), 1.0), text=f"Step {min(done + 1, total)}/{total}: {message}")

    engine = get_engine()
    try:
        build_aggregates(engine, cb)
        ok = True
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not build analytics tables: {exc}")
    finally:
        engine.dispose()
    if ok:
        st.cache_data.clear()
        st.session_state.last_refresh = datetime.now()
        st.rerun()


def make_pipeline_report(tables: list, started: float, source: str) -> dict:
    """Row counts read back from the database AFTER loading - proof that the data really was written."""
    counts_ = {}
    for t in ("vendors", "products", "purchases", "sales", "vendor_summary"):
        if table_exists(t):
            try:
                counts_[t] = int(get_row_count(t))
            except Exception:  # noqa: BLE001
                counts_[t] = -1
    return {
        "tables": list(tables), "counts": counts_, "db_bytes": int(db_file_size()),
        "seconds": time.time() - started, "finished": datetime.now(), "source": source,
    }


def render_pipeline_report() -> None:
    """Persistent result of the last pipeline run (survives the st.rerun that refreshes the dashboard)."""
    rep = st.session_state.get("pipeline_report")
    if not rep:
        return
    with st.container(border=True):
        st.success(
            f"Pipeline finished {rep['finished']:%d %b %Y %H:%M:%S} in {rep['seconds']:.0f}s (source: {rep['source']}). "
            f"Database size: {rep['db_bytes'] / 1e6:,.1f} MB."
        )
        table = pd.DataFrame({"Table": list(rep["counts"].keys()), "Rows in database": ["n/a" if v < 0 else f"{v:,}" for v in rep["counts"].values()]})
        show_df(table)
        st.button("Dismiss", key="btn_dismiss_report", on_click=lambda: st.session_state.pop("pipeline_report", None))


def db_diagnostics() -> None:
    """Shown on the empty screen so a 0-byte / table-less database is obvious instead of mysterious."""
    try:
        size = int(db_file_size())
    except Exception:  # noqa: BLE001
        size = 0
    st.caption(f"Database file: {DB_PATH} | exists: {Path(DB_PATH).exists()} | size: {size:,} bytes")
    if size == 0:
        st.error(
            "The database file is empty (0 bytes), so no tables exist yet. Load your CSV files below. "
            "If it goes back to 0 bytes after a run, check that the folder is writable and that no other program "
            "(DB Browser, a second Streamlit run, antivirus) has the file open."
        )
        return
    try:
        tables = run_query("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        st.caption("Tables found: " + (", ".join(tables["name"].tolist()) if len(tables) else "none"))
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Could not read the table list: {exc}")


def execute_pipeline(chosen: dict) -> None:
    """chosen: table -> Streamlit UploadedFile (upload) or Path (file in the data/ folder, no size limit)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    bar = st.progress(0.0)
    status = st.empty()
    order = [t for t in ("vendors", "products", "purchases", "sales") if t in chosen]
    n = len(order)
    ok = False
    from_folder = all(isinstance(chosen[t], Path) for t in order) if order else False
    engine = get_engine()
    try:
        for i, table in enumerate(order):
            upload = chosen[table]
            from_disk = isinstance(upload, Path)
            tmp = upload if from_disk else DATA_DIR / f"_upload_{table}.csv"
            if not from_disk:
                status.info(f"{i + 1}/{n} - {table}: saving {upload.name} to disk...")
                with open(tmp, "wb") as out:
                    shutil.copyfileobj(upload, out, 1024 * 1024)
            try:
                header = list(pd.read_csv(tmp, nrows=0).columns)
                missing = missing_columns(header, table)
                if missing:
                    raise ValueError(f"{upload.name} is missing required columns for '{table}': {', '.join(missing)}")

                def cb(progress=0.0, rows_loaded=0, _i=i, _t=table):
                    bar.progress(min(0.6 * (_i + progress / 100.0) / n, 0.6))
                    status.info(f"{_i + 1}/{n} - {_t}: loaded {rows_loaded:,} rows ({progress:.0f}%)")

                load_csv(str(tmp), table, engine, cb)
                status.info(f"{i + 1}/{n} - {table}: done, {get_row_count(table):,} rows now in the database.")
            finally:
                if not from_disk:
                    tmp.unlink(missing_ok=True)

        if table_exists("purchases") and table_exists("sales"):
            def cb_sum(done, total, message):
                bar.progress(0.6 + 0.1 * done / max(total, 1))
                status.info(f"Rebuilding vendor_summary - {message}")

            create_vendor_summary(engine, cb_sum)

            def cb_agg(done, total, message):
                bar.progress(0.7 + 0.3 * done / max(total, 1))
                status.info(f"Building analytics tables {min(done + 1, total)}/{total} - {message}")

            build_aggregates(engine, cb_agg)

        rep = make_pipeline_report(order, started, "data/ folder" if from_folder else "upload")
        if rep["db_bytes"] <= 0:
            raise RuntimeError(f"{DB_PATH} is still 0 bytes after loading. Check folder permissions and close any other program using the file.")
        if table_exists("purchases") and table_exists("sales") and rep["counts"].get("vendor_summary", 0) <= 0:
            raise RuntimeError("vendor_summary has 0 rows after the pipeline. Check that purchases.csv and sales.csv contain data and share the same vendor numbers.")
        st.session_state.pipeline_report = rep
        bar.progress(1.0)
        status.success("Pipeline complete.")
        ok = True
    except Exception as exc:  # noqa: BLE001
        status.error(f"Pipeline failed: {exc}")
    finally:
        engine.dispose()
    if ok:
        st.cache_data.clear()
        st.session_state.last_refresh = datetime.now()
        st.rerun()


def render_pipeline(has_data: bool) -> None:
    with st.expander("Data Pipeline - upload new CSV files", expanded=not has_data):
        if has_data:
            st.warning("Data already exists, upload will replace it. Only the tables you upload are replaced.")
        else:
            st.info("No data found yet. Upload the CSV files to create the database.")
        labels = {
            "vendors": "Vendors CSV (vendors.csv)",
            "products": "Products CSV (products.csv)",
            "purchases": "Purchases CSV (purchases.csv)",
            "sales": "Sales CSV (sales.csv)",
        }
        cols = st.columns(2)
        chosen = {}
        for i, (table, label) in enumerate(labels.items()):
            with cols[i % 2]:
                f = st.file_uploader(label, type=["csv"], key=f"up_{table}")
                if f is not None:
                    chosen[table] = f
        confirmed = True
        if has_data:
            confirmed = st.checkbox("I understand the uploaded tables will replace the existing data", key="pipeline_confirm")
        if st.button("Run pipeline", key="btn_run_pipeline", disabled=(not chosen) or (not confirmed)):
            execute_pipeline(chosen)

        st.markdown("**Or load straight from the data/ folder** (no upload size limit, best for the large sales.csv)")
        found = {t: DATA_DIR / f"{t}.csv" for t in labels if (DATA_DIR / f"{t}.csv").exists()}
        if found:
            st.caption("Found: " + ", ".join(f"{p.name} ({p.stat().st_size / 1e6:,.1f} MB)" for p in found.values()))
        else:
            st.caption(f"No CSV files found in {DATA_DIR}. Copy vendors.csv, products.csv, purchases.csv and sales.csv there.")
        if st.button("Run pipeline from data/ folder", key="btn_run_folder", disabled=(not found) or (not confirmed)):
            execute_pipeline(found)
        st.caption(
            "Streamlit limits browser uploads to 200 MB per file by default. For very large files use the data/ folder button above, "
            "or run `python -m src.database all` from the terminal after copying the files into data/."
        )


# ----------------------------------------------------------------------------
# Callbacks
# ----------------------------------------------------------------------------
def clear_filters() -> None:
    for key, value in FILTER_DEFAULTS.items():
        st.session_state[key] = copy.deepcopy(value)


def refresh_data() -> None:
    st.cache_data.clear()
    st.session_state.last_refresh = datetime.now()


def queue_question(question: str) -> None:
    st.session_state["pending_q"] = question


# ----------------------------------------------------------------------------
# Rule-based AI analyst (no external API)
# ----------------------------------------------------------------------------
SUGGESTIONS = [
    "Which vendor is most profitable?",
    "Show loss making vendors",
    "Top 5 loss-making vendors kaun hain?",
    "Which vendor has highest margin?",
    "Show high risk vendors",
    "Which vendors have delay risk?",
    "Which vendors have margin below 10%?",
    "Forecast sales for next 3 months",
    "Pareto 80/20 vendors",
    "Top 5 products of DIAGEO",
    "Show dead stock",
    "Give me key insights",
]


def find_vendor(name: str, vs: pd.DataFrame):
    name = name.lower().strip(" ?.!\"'")
    if not name:
        return None
    names = vs["vendor_name"].str.lower()
    hit = names.str.contains(re.escape(name), na=False)
    if hit.any():
        return vs[hit].sort_values("sales", ascending=False).iloc[0]
    close = get_close_matches(name, names.tolist(), n=1, cutoff=0.5)
    if close:
        return vs[names == close[0]].iloc[0]
    for token in name.split():
        if len(token) > 3:
            hit = names.str.contains(re.escape(token), na=False)
            if hit.any():
                return vs[hit].sort_values("sales", ascending=False).iloc[0]
    return None


def product_frame(vp):
    # agg_vendor_product me description/purchases nahi hai, isliye bana lo
    if 'description' not in vp.columns:
        vp['description'] = vp['brand'] if 'brand' in vp.columns else ''
    if 'purchases' not in vp.columns:
        vp['purchases'] = 0
    if 'purchase_qty' not in vp.columns:
        vp['purchase_qty'] = 0
    if 'sales' not in vp.columns:
        vp['sales'] = 0
    if 'sales_qty' not in vp.columns:
        vp['sales_qty'] = 0
    pf = vp.groupby(['brand', 'description'], as_index=False)[['sales', 'sales_qty', 'purchases', 'purchase_qty']].sum()
    return pf
    pf = vp.groupby(["brand", "description"], as_index=False)[["sales", "sales_qty", "purchases", "purchase_qty"]].sum()
    pf["profit"] = pf["sales"] - pf["purchases"]
    pf["margin"] = np.where(pf["sales"] > 0, pf["profit"] / pf["sales"].where(pf["sales"] > 0, 1) * 100.0, 0.0)
    return pf


def ai_answer(question: str, ctx) -> dict:
    q = question.lower().strip()
    vs, vp = ctx.vs, ctx.vp
    no_products = {"text": "Product-level analytics are not built yet. Use 'Build analytics tables' first.", "table": None, "fig": None}

    # 1) top N products of <vendor>
    m = re.search(r"top\s*(\d+)?\s*products?\s+(?:of|for|from|by|in)\s+(.+)", q)
    if m:
        if vp.empty:
            return no_products
        n = int(m.group(1) or 10)
        vendor = find_vendor(m.group(2), vs)
        if vendor is None:
            return {"text": f"I could not find a vendor matching '{m.group(2).strip()}'.", "table": None, "fig": None}
        rows = vp[vp["vendor_no"] == vendor["vendor_no"]].copy()
        if "purchases" not in rows.columns:
            rows["purchases"] = 0
        rows["profit"] = rows["sales"] - rows["purchases"]
        rows = rows.sort_values("sales", ascending=False).head(n)
        def _safe_product_col(frame, candidates, default=0):
            for candidate in candidates:
                if candidate in frame.columns:
                    return frame[candidate]
            return pd.Series(default, index=frame.index)
        product_names = _safe_product_col(rows, ["description", "product_description", "product_name", "product", "product_desc"], "Unknown Product").astype(str)
        brand_values = _safe_product_col(rows, ["brand", "brand_name"], "Unknown Brand").astype(str)
        sales_values = pd.to_numeric(_safe_product_col(rows, ["sales", "total_sales", "revenue"], 0), errors="coerce").fillna(0)
        purchase_values = pd.to_numeric(_safe_product_col(rows, ["purchases", "purchase", "total_purchases", "cost"], 0), errors="coerce").fillna(0)
        profit_values = pd.to_numeric(_safe_product_col(rows, ["profit", "total_profit"], 0), errors="coerce").fillna(0)
        table = pd.DataFrame({"Product": product_names, "Brand": brand_values, "Sales": sales_values, "Purchases": purchase_values, "Profit": profit_values})
        fig = hbar(product_names.str.slice(0, 32), rows["sales"], f"Top {len(rows)} products by sales - {vendor['vendor_name']}")
        return {"text": f"Top {len(rows)} products of {vendor['vendor_name']} by sales.", "table": table, "fig": fig}
    if re.search(r"top\s*\d*\s*products?", q):
        if vp.empty:
            return no_products
        n = int(re.search(r"top\s*(\d+)", q).group(1)) if re.search(r"top\s*(\d+)", q) else 10
        pf = product_frame(vp).sort_values("sales", ascending=False).head(n)
        table = pd.DataFrame({"Product": pf["description"], "Brand": pf["brand"], "Sales": pf["sales"], "Profit": pf["profit"]})
        return {"text": f"Top {len(pf)} products across all vendors by sales.", "table": table, "fig": hbar(pf["description"].str.slice(0, 32), pf["sales"], "Top products by sales")}

    # 2) dead stock
    if "dead stock" in q or "dead-stock" in q or "unsold" in q:
        if vp.empty:
            return no_products
        pf = product_frame(vp)
        dead = pf[(pf["purchases"] > 0) & (pf["sales"] <= 0)].sort_values("purchases", ascending=False)
        if dead.empty:
            return {"text": "No dead stock found - every purchased product has sales.", "table": None, "fig": None}
        table = pd.DataFrame({"Product": dead["description"], "Brand": dead["brand"], "Purchased $": dead["purchases"], "Units Purchased": dead["purchase_qty"]}).head(50)
        return {
            "text": f"{len(dead):,} products were purchased but have zero sales, tying up {money(dead['purchases'].sum())}.",
            "table": table,
            "fig": hbar(dead["description"].head(15).str.slice(0, 32), dead["purchases"].head(15), "Largest dead-stock positions", RED),
        }

    # 3) margin threshold
    m = re.search(r"margin\s*(?:is\s*)?(below|under|less than|lower than|<|above|over|greater than|more than|higher than|>)\s*(-?\d+(?:\.\d+)?)", q)
    if m:
        limit = float(m.group(2))
        below = m.group(1) in ("below", "under", "less than", "lower than", "<")
        rows = vs[vs["margin"] < limit] if below else vs[vs["margin"] > limit]
        rows = rows.sort_values("margin", ascending=below)
        word = "below" if below else "above"
        if rows.empty:
            return {"text": f"No vendors have a margin {word} {limit:g}%.", "table": None, "fig": None}
        return {
            "text": f"{len(rows)} vendors have a profit margin {word} {limit:g}%.",
            "table": vendor_table(rows.head(50))[["Vendor", "Sales", "Profit", "Margin %", "Sell-Through %", "Risk"]],
            "fig": hbar(rows["vendor_name"].head(15).str.slice(0, 30), rows["margin"].head(15), f"Margin % ({word} {limit:g}%)", RED if below else GREEN, as_money=False),
        }

    # 4) loss making
    if re.search(r"loss|losing|negative profit|unprofitable", q):
        rows = vs[vs["profit"] < 0].sort_values("profit")
        if rows.empty:
            return {"text": "No vendors are loss-making.", "table": None, "fig": None}
        return {
            "text": f"{len(rows)} vendors are loss-making with a combined loss of {money(rows['profit'].sum())}. Worst: {rows.iloc[0]['vendor_name']} ({money(rows.iloc[0]['profit'])}).",
            "table": vendor_table(rows.head(50))[["Vendor", "Sales", "Purchases", "Profit", "Margin %", "Risk"]],
            "fig": hbar(rows["vendor_name"].head(15).str.slice(0, 30), rows["profit"].head(15), "Loss-making vendors (profit)", RED),
        }

    # 5) low sell-through
    if "sell-through" in q or "sell through" in q or "sellthrough" in q:
        rows = vs[vs["sell_through"] < 20].sort_values("sell_through")
        return {
            "text": f"{len(rows)} vendors have sell-through below 20%.",
            "table": vendor_table(rows.head(50))[["Vendor", "Sales", "Profit", "Sell-Through %", "Risk"]] if len(rows) else None,
            "fig": hbar(rows["vendor_name"].head(15).str.slice(0, 30), rows["sell_through"].head(15), "Sell-through %", AMBER, as_money=False) if len(rows) else None,
        }

    # 6) most / least / top vendors
    if re.search(r"most|best|highest|top|least|worst|lowest|bottom|biggest|largest", q) and re.search(r"vendor|supplier|profit|sales|margin", q):
        ascending = bool(re.search(r"least|worst|lowest|bottom|smallest", q))
        metric = "profit"
        if "margin" in q:
            metric = "margin"
        elif "sales" in q and "profit" not in q:
            metric = "sales"
        n = int(re.search(r"(?:top|bottom)\s*(\d+)", q).group(1)) if re.search(r"(?:top|bottom)\s*(\d+)", q) else 5
        rows = vs.sort_values(metric, ascending=ascending).head(n)
        first = rows.iloc[0]
        as_money = metric != "margin"
        value = money(first[metric]) if as_money else pct(first[metric])
        phrase = {"profit": "profit", "sales": "sales", "margin": "profit margin"}[metric]
        kind = "lowest" if ascending else "highest"
        extra = [f"sales {money(first['sales'])}"] if metric != "sales" else []
        if metric != "margin":
            extra.append(f"margin {pct(first['margin'])}")
        detail = f" ({', '.join(extra)})" if extra else ""
        return {
            "text": f"{first['vendor_name']} has the {kind} {phrase}: {value}{detail}.",
            "table": vendor_table(rows)[["Vendor", "Sales", "Profit", "Margin %", "Grade"]],
            "fig": hbar(rows["vendor_name"].str.slice(0, 30), rows[metric], f"{'Bottom' if ascending else 'Top'} {len(rows)} vendors by {phrase}", RED if ascending else BLUE, as_money=as_money),
        }

    # 7) overall summary
    if re.search(r"total|overall|summary|kpi|overview|how many vendors", q):
        t = totals(vs)
        return {
            "text": f"Across {t['vendors']} vendors: sales {money(t['sales'])}, purchases {money(t['purchases'])}, profit {money(t['profit'])}, margin {pct(t['margin'])}.",
            "table": pd.DataFrame({"Metric": ["Sales", "Purchases", "Profit", "Margin", "Vendors"], "Value": [money(t["sales"]), money(t["purchases"]), money(t["profit"]), pct(t["margin"]), f"{t['vendors']}"]}),
            "fig": None,
        }

    return {
        "text": "I can answer questions about vendors, margins, products and stock. Try one of the suggested questions above, or ask for example: "
        "'Top 5 products of <vendor>', 'Vendors with margin below 15%', 'Least profitable vendors', 'Show dead stock', 'Total sales'.",
        "table": None,
        "fig": None,
    }


# ----------------------------------------------------------------------------
# Agentic AI layer: SQL tool-calling agent.
# Default = rule-based planner (English + Hinglish). Optional = any OpenAI-compatible LLM.
# Every answer is computed by a real SQL query on the vendor data - nothing is invented.
# ----------------------------------------------------------------------------
AGENT_COLS = [
    "vendor_no", "vendor_name", "sales", "purchases", "profit", "margin", "sell_through", "score", "grade", "risk",
    "risk_score", "risk_band", "avg_lead", "pred_lead", "late_pct", "delay_score", "delay_risk",
]
AGENT_SCHEMA = """Table vendor_kpi (one row per vendor):
  vendor_no INTEGER, vendor_name TEXT, sales REAL, purchases REAL, profit REAL (= sales - purchases),
  margin REAL (profit margin in %), sell_through REAL (% of purchased units sold), score REAL, grade TEXT (A+,A,B+,B,C,D),
  risk TEXT (Critical/High/Medium/Low), risk_score REAL (0-100, higher = riskier), risk_band TEXT (Green/Yellow/Red),
  avg_lead REAL (average days from PO to receiving), pred_lead REAL (predicted next-month lead days),
  late_pct REAL (% of orders later than the SLA), delay_score REAL (0-100), delay_risk TEXT (High/Medium/Low/n/a)
Table vendor_month (one row per vendor and month):
  vendor_no INTEGER, vendor_name TEXT, month TEXT ('YYYY-MM'), sales REAL, purchases REAL, profit REAL
Currency is USD."""

_HINGLISH = [
    (r"\b(?:kaun|kon|kis|kaunsa|kaunse)\s*(?:sa|se|si)?\b", " which "),
    (r"\bkitn[eai]\b", " how many "),
    (r"\bsabse\s*(?:zyada|jyada|jada|upar|badhiya|accha|acha|bada|bade)\b", " highest "),
    (r"\bsabse\s*(?:kam|kum|neeche|niche|bura|kharab|chhota|chote)\b", " lowest "),
    (r"\b(?:nuksan|nuksaan|ghata|ghate|ghaate)\b", " loss "),
    (r"\b(?:munafa|munafaa|fayda|faida|labh)\b", " profit "),
    (r"\b(?:bikri|bikree|vikray)\b", " sales "),
    (r"\b(?:kharid|kharidari|khareed)\b", " purchases "),
    (r"\b(?:dikhao|dikha|dikhaiye|batao|bataiye|bata do|btao)\b", " show "),
    (r"\b(?:jokhim|khatra)\b", " risk "),
    (r"\b(?:deri|der|vilamb)\b", " delay "),
    (r"\b(?:agle|aane wale|bhavishya|anumaan)\b", " next "),
    (r"\b(?:wale|wala|vale|waale|wali)\b", " "),
    (r"\b(?:hain|hai|ho|ka|ki|ke|ko|mein|kya|aur)\b", " "),
]
_METRICS = {
    "margin": ("margin", "profit margin", False), "profit": ("profit", "profit", True), "sales": ("sales", "sales", True),
    "purchase": ("purchases", "purchases", True), "risk": ("risk_score", "risk score", False),
    "score": ("score", "vendor score", False), "sell": ("sell_through", "sell-through", False),
}
_FORBIDDEN_SQL = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b", re.I)


def normalize_question(q: str) -> str:
    s = " " + q.lower().strip() + " "
    for pattern, repl in _HINGLISH:
        s = re.sub(pattern, repl, s)
    return re.sub(r"\s+", " ", s).strip()


def agent_connection(ctx) -> sqlite3.Connection:
    """In-memory SQLite copy of the (small) vendor tables, so the agent can run real SQL safely."""
    conn = sqlite3.connect(":memory:")
    base = ctx.vs
    base[[c for c in AGENT_COLS if c in base.columns]].to_sql("vendor_kpi", conn, index=False)
    vm = getattr(ctx, "vm_all", None)
    if vm is not None and not vm.empty:
        m = vm.merge(base[["vendor_no", "vendor_name"]], on="vendor_no", how="left")
        m = m.assign(month=m["month"].astype(str), profit=m["sales"] - m["purchases"])
        m[["vendor_no", "vendor_name", "month", "sales", "purchases", "profit"]].to_sql("vendor_month", conn, index=False)
    return conn


def _top_n(nq: str, default: int = 5) -> int:
    m = re.search(r"(?:top|bottom|worst|best|first|last)\s*(\d{1,2})\b", nq) or re.search(r"\b(\d{1,2})\s*(?:vendors?|suppliers?)\b", nq)
    return max(1, min(int(m.group(1)), 50)) if m else default


def _auto_fig(df: pd.DataFrame, title: str = ""):
    """Horizontal bar chart of the first numeric column against the first text column."""
    if df is None or len(df) < 2 or len(df.columns) < 2:
        return None
    label = df.columns[0]
    nums = [c for c in df.columns[1:] if pd.api.types.is_numeric_dtype(df[c])]
    if not nums or pd.api.types.is_numeric_dtype(df[label]):
        return None
    col = nums[0]
    return hbar(df[label].astype(str).str.slice(0, 30).head(15), df[col].head(15), title or col, BLUE, as_money=col in ("Sales", "Purchases", "Profit"))


def _sql_result(text_: str, df: pd.DataFrame, sql: str, fig=None) -> dict:
    return {"text": text_, "table": df, "fig": fig, "sql": sql}


def rule_agent(question: str, ctx, conn):
    """Plan -> pick a tool -> run SQL -> phrase the answer from the returned rows. None = not my question."""
    q = question.lower().strip()
    nq = normalize_question(question)
    if re.search(r"product|brand|dead stock|unsold|\bsku\b|\bitems?\b", nq):
        return None  # product questions are handled by the product analyst below
    n = _top_n(nq)

    def run(sql: str) -> pd.DataFrame:
        return pd.read_sql_query(sql, conn)

    # 1) auto insights
    if re.search(r"insight|key finding|highlights?|overview", nq):
        lines = [strip_html(x) for x in auto_insights(ctx.vs, ctx.vm_all)]
        return {"text": "\n\n".join(f"- {x}" for x in lines), "table": None, "fig": None, "sql": "-- computed from vendor_kpi and vendor_month (see Auto-Insights on the Dashboard tab)"}

    # 2) forecast
    if re.search(r"forecast|predict\w* sales|sales predict|projection|\bnext \d* ?months?\b", nq):
        if ctx.vm_all.empty:
            return {"text": "Monthly data is not built yet. Use 'Build analytics tables' first.", "table": None, "fig": None, "sql": None}
        sql = "SELECT month AS Month, SUM(sales) AS Sales FROM vendor_month GROUP BY month ORDER BY month"
        hist = run(sql)
        try:
            fc = forecast_sales(hist.rename(columns={"Month": "month", "Sales": "sales"}))
        except Exception:  # noqa: BLE001
            return {"text": "The month labels could not be parsed as dates, so a forecast is not possible.", "table": hist, "fig": None, "sql": sql}
        table = pd.DataFrame({"Month": fc["months"], "Forecast Sales": fc["pred"]})
        text_ = f"Based on {fc['n']} months of history, forecast sales are " + ", ".join(f"{m}: {money_short(v)}" for m, v in zip(fc["months"], fc["pred"])) + "."
        return _sql_result(text_, table, sql, hbar(table["Month"], table["Forecast Sales"], "Sales forecast (next 3 months)", TEAL))

    # 3) profit drop
    if re.search(r"profit (?:drop|decline|fall|down)|dropped|declin", nq):
        drops, prev, cur = profit_drop_frame(ctx.vm_all, ctx.vs)
        if cur is None or prev is None:
            return {"text": "At least 2 months of monthly data are needed to detect profit drops.", "table": None, "fig": None, "sql": None}
        if drops.empty:
            return {"text": f"No vendor's profit fell by more than 30% from {prev} to {cur}.", "table": None, "fig": None, "sql": None}
        table = pd.DataFrame({"Vendor": drops["vendor_name"], "Profit": drops["profit"], "Previous Profit": drops["prev_profit"], "Drop %": drops["drop_pct"]}).head(n)
        return {"text": f"{len(drops)} vendors lost more than 30% of their profit from {prev} to {cur}. Largest drop: {drops.iloc[0]['vendor_name']} ({drops.iloc[0]['drop_pct']:.0f}%).",
                "table": table, "fig": _auto_fig(table[["Vendor", "Drop %"]], "Profit drop % vs previous month"),
                "sql": "SELECT vendor_name, month, SUM(profit) AS profit FROM vendor_month GROUP BY vendor_no, month  -- then month-over-month change"}

    # 4) pareto
    if re.search(r"pareto|80 ?/ ?20|80 ?%|vital few", nq):
        sql = (
            "WITH r AS (SELECT vendor_name, purchases, SUM(purchases) OVER (ORDER BY purchases DESC) AS cum, "
            "SUM(purchases) OVER () AS tot FROM vendor_kpi WHERE purchases > 0) "
            "SELECT vendor_name AS Vendor, purchases AS Purchases, ROUND(cum * 100.0 / tot, 1) AS \"Cumulative %\" "
            "FROM r WHERE cum - purchases < 0.8 * tot ORDER BY purchases DESC"
        )
        df = run(sql)
        total = int(pd.read_sql_query("SELECT COUNT(*) AS c FROM vendor_kpi WHERE purchases > 0", conn)["c"].iloc[0])
        text_ = f"{len(df)} of {total} vendors ({len(df) / max(total, 1) * 100:.0f}%) account for 80% of total purchase value."
        return _sql_result(text_, df, sql, _auto_fig(df, "Vendors driving 80% of purchases"))

    # 5) delay
    if re.search(r"delay|late deliver|lead time|slow deliver", nq):
        if "delay_score" not in ctx.vs.columns or ctx.vs["delay_score"].notna().sum() == 0:
            return {"text": "Delay analytics are not built yet. Open Control Tower > Delay Prediction and click 'Build delay analytics'.", "table": None, "fig": None, "sql": None}
        sql = (f"SELECT vendor_name AS Vendor, ROUND(avg_lead, 1) AS \"Avg Lead Days\", ROUND(pred_lead, 1) AS \"Predicted Lead Days\", "
               f"ROUND(late_pct, 1) AS \"Late %\", delay_score AS \"Delay Score\", delay_risk AS \"Delay Risk\" "
               f"FROM vendor_kpi WHERE delay_score IS NOT NULL ORDER BY delay_score DESC LIMIT {n}")
        df = run(sql)
        high = int(pd.read_sql_query("SELECT COUNT(*) AS c FROM vendor_kpi WHERE delay_risk = 'High'", conn)["c"].iloc[0])
        text_ = f"{high} vendors are High delay risk. Highest delay score: {df.iloc[0]['Vendor']} ({df.iloc[0]['Delay Score']:.0f}, predicted lead {df.iloc[0]['Predicted Lead Days']:.1f} days)."
        return _sql_result(text_, df, sql, _auto_fig(df[["Vendor", "Delay Score"]], "Delay score"))

    # 6) risk
    if re.search(r"\brisk", nq):
        only_red = bool(re.search(r"\bred\b|high risk|critical", nq))
        where = "WHERE risk_band = 'Red' " if only_red else ""
        sql = (f"SELECT vendor_name AS Vendor, risk_score AS \"Risk Score\", risk_band AS \"Risk Band\", margin AS \"Margin %\", "
               f"sell_through AS \"Sell-Through %\", profit AS Profit FROM vendor_kpi {where}ORDER BY risk_score DESC LIMIT {n}")
        df = run(sql)
        counts_ = run("SELECT risk_band, COUNT(*) AS c FROM vendor_kpi GROUP BY risk_band").set_index("risk_band")["c"].to_dict()
        if df.empty:
            return _sql_result("No vendors match that risk filter.", df, sql)
        text_ = (f"Risk bands: {counts_.get('Red', 0)} Red, {counts_.get('Yellow', 0)} Yellow, {counts_.get('Green', 0)} Green. "
                 f"Riskiest vendor: {df.iloc[0]['Vendor']} (score {df.iloc[0]['Risk Score']:.0f}).")
        return _sql_result(text_, df, sql, _auto_fig(df[["Vendor", "Risk Score"]], "Risk score (0-100)"))

    # 7) loss making
    if re.search(r"\bloss|losing|negative profit|unprofitable", nq) and not re.search(r"margin\s*(?:below|under|above|over|<|>)", nq):
        sql = (f"SELECT vendor_name AS Vendor, sales AS Sales, purchases AS Purchases, profit AS Profit, margin AS \"Margin %\", "
               f"risk_band AS \"Risk Band\" FROM vendor_kpi WHERE profit < 0 ORDER BY profit ASC LIMIT {n}")
        df = run(sql)
        agg = run("SELECT COUNT(*) AS c, COALESCE(SUM(profit), 0) AS s FROM vendor_kpi WHERE profit < 0")
        count, total = int(agg["c"].iloc[0]), float(agg["s"].iloc[0])
        if count == 0:
            return _sql_result("No vendors are loss-making.", df, sql)
        asked_count = bool(re.search(r"how many", nq))
        text_ = (f"{count} vendors are loss-making with a combined loss of {money(total)}." if asked_count or n >= count else
                 f"{count} vendors are loss-making (combined loss {money(total)}). The {len(df)} worst are shown; #1 is {df.iloc[0]['Vendor']} at {money(df.iloc[0]['Profit'])}.")
        return _sql_result(text_, df, sql, _auto_fig(df[["Vendor", "Profit"]], "Loss-making vendors (profit)"))

    # 8) highest / lowest metric
    if re.search(r"highest|lowest|best|worst|top|bottom|most|least|biggest|largest|smallest", nq) and re.search(r"vendor|supplier|margin|profit|sales|purchase|score|sell", nq):
        key = next((k for k in ("risk", "margin", "sell", "purchase", "score", "sales", "profit") if k in nq), "profit")
        col, phrase, _money = _METRICS[key]
        asc = bool(re.search(r"lowest|worst|bottom|least|smallest", nq))
        if key == "risk":
            asc = bool(re.search(r"lowest|least|safest|best", nq))
        sql = (f"SELECT vendor_name AS Vendor, sales AS Sales, purchases AS Purchases, profit AS Profit, margin AS \"Margin %\", "
               f"sell_through AS \"Sell-Through %\", score AS \"Vendor Score\", risk_score AS \"Risk Score\", grade AS Grade "
               f"FROM vendor_kpi ORDER BY {col} {'ASC' if asc else 'DESC'} LIMIT {n}")
        df = run(sql)
        first = df.iloc[0]
        shown = {"profit": money(first["Profit"]), "sales": money(first["Sales"]), "purchases": money(first["Purchases"]),
                 "margin": pct(first["Margin %"]), "risk": f"{first['Risk Score']:.0f}",
                 "sell": pct(first["Sell-Through %"]), "score": f"{first['Vendor Score']:.1f}"}.get(key, "")
        kind = "lowest" if asc else "highest"
        text_ = f"{first['Vendor']} has the {kind} {phrase}" + (f": {shown}." if shown else ".")
        label_col = {"margin": "Margin %", "risk": "Risk Score", "profit": "Profit", "sales": "Sales", "purchase": "Purchases", "sell": "Sell-Through %", "score": "Vendor Score"}.get(key, "Profit")
        return _sql_result(text_, df, sql, _auto_fig(df[["Vendor", label_col]], f"{'Bottom' if asc else 'Top'} {len(df)} vendors by {phrase}"))

    # 9) one vendor
    m = re.search(r"(?:about|profile|details?|performance(?: of)?|info(?:rmation)? (?:on|about))\s+(.+)", q)
    if m:
        vendor = find_vendor(m.group(1), ctx.vs)
        if vendor is not None:
            sql = ("SELECT vendor_name AS Vendor, sales AS Sales, purchases AS Purchases, profit AS Profit, margin AS \"Margin %\", "
                   "sell_through AS \"Sell-Through %\", grade AS Grade, risk_score AS \"Risk Score\", risk_band AS \"Risk Band\", "
                   f"delay_risk AS \"Delay Risk\" FROM vendor_kpi WHERE vendor_no = {int(vendor['vendor_no'])}")
            df = run(sql)
            r = df.iloc[0]
            text_ = (f"{r['Vendor']}: sales {money(r['Sales'])}, profit {money(r['Profit'])} ({pct(r['Margin %'])} margin), "
                     f"grade {r['Grade']}, risk score {r['Risk Score']:.0f} ({r['Risk Band']}).")
            return _sql_result(text_, df, sql)
    return None


def _llm_chat(cfg: dict, messages: list, timeout: int = 45) -> str:
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps({"model": cfg["model"], "messages": messages, "temperature": 0}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]


def llm_config():
    """First configured provider wins. Set GROQ_API_KEY, GEMINI_API_KEY or OPENAI_API_KEY (env var, .env or secrets).
    Optional overrides: LLM_MODEL, LLM_BASE_URL. Generic: LLM_API_KEY + LLM_BASE_URL + LLM_MODEL."""
    def get(name: str):
        v = os.getenv(name)
        if v:
            return v
        try:
            return st.secrets.get(name)
        except Exception:  # noqa: BLE001  (no secrets file)
            return None

    providers = [
        ("GROQ_API_KEY", "Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        ("GEMINI_API_KEY", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
        ("OPENAI_API_KEY", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ]
    for env, name, base, model in providers:
        key = get(env)
        if key:
            return {"key": key, "name": name, "base": get("LLM_BASE_URL") or base, "model": get("LLM_MODEL") or model}
    key, base, model = get("LLM_API_KEY"), get("LLM_BASE_URL"), get("LLM_MODEL")
    if key and base and model:
        return {"key": key, "name": "Custom", "base": base, "model": model}
    return None


def _extract_sql(text_: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text_, re.S | re.I)
    return (m.group(1) if m else text_).strip().rstrip(";").strip()


def _safe_select(sql: str) -> bool:
    s = sql.strip().lower()
    return (s.startswith("select") or s.startswith("with")) and ";" not in s and not _FORBIDDEN_SQL.search(s)


def llm_agent(question: str, conn, cfg: dict):
    """LLM writes ONE read-only SQL query -> we run it -> LLM phrases the answer from the returned rows only."""
    system = (
        "You translate questions about vendor performance (English or Hinglish) into ONE SQLite SELECT statement.\n"
        f"{AGENT_SCHEMA}\nRules: return only the SQL (no prose), read-only, add LIMIT 50 unless aggregating, "
        "use readable column aliases, margin and sell_through are already percentages."
    )
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    df, sql = None, ""
    for _ in range(2):
        raw = _llm_chat(cfg, msgs)
        sql = _extract_sql(raw)
        err = "only a single read-only SELECT is allowed"
        if _safe_select(sql):
            try:
                df = pd.read_sql_query(sql, conn)
                break
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
        msgs += [{"role": "assistant", "content": raw}, {"role": "user", "content": f"That failed: {err}. Return only a corrected single SELECT statement."}]
    if df is None:
        return None
    sample = json.dumps(df.head(15).to_dict("records"), default=str)
    answer = _llm_chat(cfg, [
        {"role": "system", "content": "You are a vendor analytics assistant. Answer in 2-3 short sentences in the same language style as the question (English or Hinglish). Use ONLY numbers present in the data provided. Money is in USD."},
        {"role": "user", "content": f"Question: {question}\nSQL result ({len(df)} rows, first 15 shown): {sample}"},
    ])
    return {"text": answer.strip(), "table": df.head(100), "fig": _auto_fig(df), "sql": sql}


def agent_answer(question: str, ctx) -> dict:
    """Entry point used by the sidebar and the AI Agent tab."""
    conn = agent_connection(ctx)
    try:
        cfg = llm_config()
        if cfg:
            try:
                res = llm_agent(question, conn, cfg)
                if res:
                    res["mode"] = f"LLM ({cfg['name']}) + SQL"
                    return res
            except Exception:  # noqa: BLE001  (network / quota / bad key -> fall back to rules)
                pass
        res = rule_agent(question, ctx, conn)
        if res is not None:
            res["mode"] = "Rule-based SQL agent"
            return res
    finally:
        conn.close()
    res = ai_answer(question, ctx)
    res.setdefault("sql", None)
    res["mode"] = "Rule-based analyst"
    return res


def safe_agent_answer(question: str, ctx) -> dict:
    """Never let a bad question or a network hiccup crash the dashboard."""
    try:
        return agent_answer(question, ctx)
    except Exception as exc:  # noqa: BLE001
        return {"text": f"Sorry, I could not answer that ({exc}). Try rephrasing, e.g. 'Top 5 loss-making vendors'.", "table": None, "fig": None, "sql": None, "mode": "error"}


# ----------------------------------------------------------------------------
# Tabs
# ----------------------------------------------------------------------------
def empty_message() -> None:
    st.info("No vendors match the current filters. Use 'Clear all filters' in the sidebar.")


def aggregates_message() -> None:
    st.info("This view needs the analytics tables. Click 'Build analytics tables' at the top of the page (one-time).")


def monthly_frame(vm: pd.DataFrame, vendor_ids) -> pd.DataFrame:
    if vm.empty:
        return pd.DataFrame()
    rows = vm[vm["vendor_no"].isin(vendor_ids)]
    if "purchases" not in rows.columns: rows["purchases"] = 0
    if "sales" not in rows.columns: rows["sales"] = 0
    out = rows.groupby("month", as_index=False)[["sales", "purchases"]].sum().sort_values("month")
    out["profit"] = out["sales"] - out["purchases"]
    return out


def tab_overview(ctx) -> None:
    fv = ctx.fv
    if fv.empty:
        return empty_message()
    t = ctx.ftot
    top5_share = fv.nlargest(5, "sales")["sales"].sum() / t["sales"] * 100 if t["sales"] else 0
    losers = fv[fv["profit"] < 0]
    sized = fv[fv["sales"] >= fv["sales"].median()]
    best_margin = sized.sort_values("margin", ascending=False).iloc[0] if len(sized) else fv.iloc[0]
    insights = [
        f"<b>Concentration:</b> the top 5 vendors generate <b>{top5_share:.1f}%</b> of the filtered sales ({money_short(fv.nlargest(5, 'sales')['sales'].sum())}).",
        (f"<b>Loss exposure:</b> <b>{len(losers)}</b> vendors are loss-making with a combined loss of <b>{money_short(losers['profit'].sum())}</b>."
         if len(losers) else "<b>Loss exposure:</b> no loss-making vendors in the current selection."),
        f"<b>Margin leader:</b> <b>{esc(best_margin['vendor_name'])}</b> earns <b>{best_margin['margin']:.1f}%</b> margin on {money_short(best_margin['sales'])} of sales (above-median sales vendors only).",
    ]
    cols = st.columns(3)
    for col, text_ in zip(cols, insights):
        col.markdown(f"<div class='insight'>{text_}</div>", unsafe_allow_html=True)
    st.write("")

    c1, c2 = st.columns(2)
    with c1:
        with card("Monthly Sales vs Purchases", "Pre-aggregated monthly totals for the filtered vendors"):
            monthly = monthly_frame(ctx.vm, fv["vendor_no"])
            if monthly.empty:
                aggregates_message()
            else:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["sales"], name="Sales", mode="lines+markers", line=dict(color=BLUE, width=3)))
                fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["purchases"], name="Purchases", mode="lines+markers", line=dict(color=AMBER, width=3)))
                fig.update_xaxes(type="category")
                fig.update_yaxes(tickprefix="$", separatethousands=True)
                show_plot(fig, 340)
    with c2:
        with card("Top 10 Vendors by Profit"):
            top = fv.nlargest(10, "profit")
            show_plot(hbar(top["vendor_name"].str.slice(0, 30), top["profit"], "", GREEN), 340)

    c3, c4 = st.columns(2)
    with c3:
        with card("Bottom 10 Vendors by Profit", "Loss-making vendors appear in red"):
            bottom = fv.nsmallest(10, "profit")
            fig = hbar(bottom["vendor_name"].str.slice(0, 30), bottom["profit"], "", RED)
            fig.update_traces(marker_color=[RED if v < 0 else AMBER for v in bottom["profit"]])
            show_plot(fig, 340)
    with c4:
        with card("Sales by Classification"):
            if ctx.scope.empty:
                aggregates_message()
            else:
                cls = ctx.scope.groupby("classification", as_index=False)["sales"].sum()
                cls["label"] = cls["classification"].map(label_class)
                fig = px.pie(cls, names="label", values="sales", hole=0.55, color_discrete_sequence=PALETTE)
                fig.update_traces(textinfo="label+percent", hovertemplate="%{label}: $%{value:,.0f}<extra></extra>")
                show_plot(fig, 340)

    with card("Vendor Summary", f"Showing {min(len(fv), 1000):,} of {len(fv):,} filtered vendors, sorted by sales"):
        render_vendor_table(fv.sort_values("sales", ascending=False), height=420)


def tab_scorecard(ctx) -> None:
    fv, vs = ctx.fv, ctx.vs
    if fv.empty:
        return empty_message()
    with card("Vendor Scorecard", "Score = Margin x 0.4 + Relative Sales x 0.3 + Sell-Through x 0.3. Grades are percentile based "
                                  "(A+ top 10%, A next 15%, B+ 20%, B 20%, C 20%, D bottom 15%; negative margin is always D). "
                                  "Risk: Critical = negative margin, High = margin < 10% or sell-through < 20%, Medium = margin < 20% or sell-through < 50%."):
        render_vendor_table(fv.sort_values("score", ascending=False), include_rank=True, height=460)

    render_comparison(ctx, "cmp")


def tab_deepdive(ctx) -> None:
    # --- AUTO FIX for NameError: view ---
    view = None
    try:
        _df = ctx.get('df') if isinstance(ctx, dict) else getattr(ctx, 'df', None)
        import pandas as _pd
        view = _df.copy() if _df is not None else _pd.DataFrame()
    except Exception:
        import pandas as _pd
        view = _pd.DataFrame()
    vs, vm, vp = ctx.vs, ctx.vm, ctx.vp
    ordered = vs.sort_values("sales", ascending=False)
    labels = {int(r.vendor_no): f"{r.vendor_name} (#{r.vendor_no})" for r in ordered.itertuples()}
    ids = list(labels.keys())
    vendor_id = st.selectbox("Select vendor", ids, format_func=lambda i: labels[i], key="dd_vendor")
    row = vs[vs["vendor_no"] == vendor_id].iloc[0]
    rank = int((vs["sales"] > row["sales"]).sum()) + 1

    cols = st.columns(6)
    metrics = [
        ("Total Sales", money_short(row["sales"])), ("Total Purchases", money_short(row["purchases"])),
        ("Gross Profit", money_short(row["profit"])), ("Profit Margin", pct(row["margin"])),
        ("Sell-Through", pct(row["sell_through"])), ("Vendor Score", f"{row['score']:.1f} ({row['grade']})"),
    ]
    for col, (label, value) in zip(cols, metrics):
        col.metric(label, value)
    st.caption(f"Sales rank #{rank} of {len(vs)} vendors | Risk level: {row['risk']} | Average selling price: ${row['avg_price']:,.2f}")

    c1, c2 = st.columns(2)
    with c1:
        with card("Monthly Trend", "From pre-aggregated monthly table"):
            monthly = monthly_frame(vm, [vendor_id])
            if monthly.empty:
                aggregates_message()
            else:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["sales"], name="Sales", mode="lines+markers", line=dict(color=BLUE, width=3)))
                fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["purchases"], name="Purchases", mode="lines+markers", line=dict(color=AMBER, width=3)))
                fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["profit"], name="Profit", mode="lines+markers", line=dict(color=GREEN, width=3)))
                fig.update_xaxes(type="category")
                fig.update_yaxes(tickprefix="$", separatethousands=True)
                show_plot(fig, 340)
    with c2:
        with card("Vendor vs Portfolio Average"):
            fig = go.Figure()
            cats = ["Margin %", "Sell-Through %", "Score"]
            fig.add_trace(go.Bar(name=row["vendor_name"], x=cats, y=[row["margin"], row["sell_through"], row["score"]], marker_color=BLUE))
            fig.add_trace(go.Bar(name="Average vendor", x=cats, y=[vs["margin"].mean(), vs["sell_through"].mean(), vs["score"].mean()], marker_color="#B8C4D9"))
            fig.update_layout(barmode="group")
            show_plot(fig, 340)

    with card("Top 10 Products", "By sales, from the product aggregate table"):
        if vp.empty:
            aggregates_message()
        else:
            rows = vp[vp["vendor_no"] == vendor_id].copy()
            if "purchases" not in rows.columns:
                rows["purchases"] = 0
            rows["profit"] = rows["sales"] - rows["purchases"]
            rows["margin"] = np.where(rows["sales"] > 0, rows["profit"] / rows["sales"].where(rows["sales"] > 0, 1) * 100, 0.0)
            rows = rows.sort_values("sales", ascending=False).head(10)
            if rows.empty:
                st.info("No product data for this vendor.")
            else:
                def _safe_product_col(frame, candidates, default=0):
                    for candidate in candidates:
                        if candidate in frame.columns:
                            return frame[candidate]
                    return pd.Series(default, index=frame.index)
                product_names = _safe_product_col(rows, ["description", "product_description", "product_name", "product", "product_desc"], "Unknown Product").astype(str)
                brand_values = _safe_product_col(rows, ["brand", "brand_name"], "Unknown Brand").astype(str)
                sales_values = pd.to_numeric(_safe_product_col(rows, ["sales", "total_sales", "revenue"], 0), errors="coerce").fillna(0)
                purchase_values = pd.to_numeric(_safe_product_col(rows, ["purchases", "purchase", "total_purchases", "cost"], 0), errors="coerce").fillna(0)
                profit_values = pd.to_numeric(_safe_product_col(rows, ["profit", "total_profit"], 0), errors="coerce").fillna(0)
                margin_values = pd.to_numeric(_safe_product_col(rows, ["margin", "margin_pct", "margin_percent"], 0), errors="coerce").fillna(0)
                if "purchases" not in view.columns:
                    view["purchases"] = 0
                if "sales" not in view.columns:
                    view["sales"] = 0
                if "profit" not in view.columns:
                    view["profit"] = view["sales"] - view["purchases"]
                if "margin" not in view.columns:
                    view["margin"] = 0
                if "sales_qty" not in view.columns:
                    view["sales_qty"] = 0
                if "brand" not in view.columns:
                    view["brand"] = "Unknown"
                if "description" not in view.columns:
                    view["description"] = "Unknown"
                table = pd.DataFrame({"Product": product_names, "Brand": brand_values, "Sales": sales_values, "Purchases": purchase_values, "Profit": profit_values, "Margin %": margin_values})
                a, b = st.columns([3, 2])
                with a:
                    show_df(styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %"]))
                with b:
                    show_plot(hbar(product_names.str.slice(0, 28), rows["sales"], "", BLUE), 330)

    vendors = ctx.vendors
    if not vendors.empty and "vendor_no" in vendors.columns:
        info = vendors[vendors["vendor_no"] == vendor_id]
        if not info.empty:
            r = info.iloc[0]
            parts = [str(r[c]) for c in ("address", "city", "state", "zipcode") if c in info.columns and pd.notna(r[c])]
            if parts:
                st.caption("Vendor address: " + ", ".join(parts))


def tab_products(ctx) -> None:
    fv = ctx.fv
    if ctx.vp.empty:
        return aggregates_message()
    if fv.empty:
        return empty_message()
    pf = ctx.pf
    if pf.empty:
        return st.info("No product data for the filtered vendors.")
    catalog = len(ctx.products)
    m1, m2, m3 = st.columns(3)
    m1.metric("Products with activity", f"{len(pf):,}")
    m2.metric("Product catalog size", f"{catalog:,}" if catalog else "n/a")
    m3.metric("Products with zero sales", f"{int(((pf['purchases'] > 0) & (pf['sales'] <= 0)).sum()):,}")
    st.caption("Product figures follow the vendor filters. Store and classification filters apply to vendor/store/classification views only.")

    with card("Product Search"):
        term = st.text_input("Search product or brand", key="prod_search", placeholder="e.g. Vodka, 1234, Jack Daniel")
        view = pf
        if term.strip():
            t = term.strip().lower()
            view = pf[pf["description"].str.lower().str.contains(re.escape(t), na=False) | pf["brand"].str.lower().str.contains(re.escape(t), na=False)]
        view = view.sort_values("sales", ascending=False).head(1000)
        if "purchases" not in view.columns:
            view["purchases"] = 0
    if "sales" not in view.columns:
        view["sales"] = 0
    if "profit" not in view.columns:
        view["profit"] = view["sales"] - view["purchases"]
    if "margin" not in view.columns:
        view["margin"] = 0
    if "sales_qty" not in view.columns:
        view["sales_qty"] = 0
    if "brand" not in view.columns:
        view["brand"] = "Unknown"
    if "description" not in view.columns:
        view["description"] = "Unknown"
    table = pd.DataFrame({"Brand": view["brand"], "Product": view["description"], "Sales": view["sales"], "Purchases": view["purchases"], "Profit": view["profit"], "Margin %": view["margin"], "Units Sold": view["sales_qty"]})
    show_df(styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %"], int_cols=["Units Sold"]), height=380)

    c1, c2 = st.columns(2)
    with c1:
        with card("Top 20 Products by Sales"):
            top = pf.nlargest(20, "sales")
            show_plot(hbar(top["description"].str.slice(0, 30), top["sales"], "", BLUE), 560)
    with c2:
        with card("Top 20 Products by Profit"):
            if "profit" not in pf.columns:
                if "revenue" in pf.columns and "cost" in pf.columns:
                    pf["profit"] = pf["revenue"] - pf["cost"]
                elif "sales" in pf.columns and "cost" in pf.columns:
                    pf["profit"] = pf["sales"] - pf["cost"]
                elif "amount" in pf.columns and "cost" in pf.columns:
                    pf["profit"] = pf["amount"] - pf["cost"]
                else:
                    pf["profit"] = 0
            top = pf.nlargest(20, "profit")
            show_plot(hbar(top["description"].str.slice(0, 30), top["profit"], "", GREEN), 560)

    c3, c4 = st.columns(2)
    with c3:
        with card("Dead Stock", "Purchased but zero sales"):
            dead = pf[(pf["purchases"] > 0) & (pf["sales"] <= 0)].sort_values("purchases", ascending=False)
            if dead.empty:
                st.success("No dead stock in the current selection.")
            else:
                st.metric("Capital tied up in dead stock", money(dead["purchases"].sum()))
                table = pd.DataFrame({"Brand": dead["brand"], "Product": dead["description"], "Purchased $": dead["purchases"], "Units": dead["purchase_qty"]}).head(500)
                show_df(styled(table, money_cols=["Purchased $"], int_cols=["Units"]), height=330)
    with c4:
        with card("Sales vs Profit", "Colour = margin %, top 3,000 products by sales"):
            sample = pf.nlargest(3000, "sales")
            if "margin" not in sample.columns:
                import numpy as _np
                sample = sample.copy()
                sample["margin"] = _np.where(sample["sales"] != 0, sample["profit"] / sample["sales"] * 100, 0)
            fig = px.scatter(sample, x="sales", y="profit", color="margin", color_continuous_scale="RdYlGn", range_color=(-20, 60), hover_name="description", hover_data={"sales": ":$,.0f", "profit": ":$,.0f", "margin": ":.1f"})
            fig.update_traces(marker=dict(size=7, opacity=0.8))
            fig.update_layout(coloraxis_colorbar=dict(title="Margin %"))
            show_plot(fig, 400)


def tab_store_category(ctx) -> None:
    scope = ctx.scope
    if scope.empty:
        return aggregates_message()
    if ctx.fv.empty:
        return empty_message()

    def summarise(key: str) -> pd.DataFrame:
        g = scope.groupby(key, as_index=False)[["sales", "purchases", "sales_qty", "purchase_qty"]].sum()
        g["profit"] = g["sales"] - g["purchases"]
        g["margin"] = np.where(g["sales"] > 0, g["profit"] / g["sales"].where(g["sales"] > 0, 1) * 100, 0.0)
        return g.sort_values("profit", ascending=False)

    stores = summarise("store")
    with card("Store-wise Profit", f"{len(stores):,} stores | profit = sales - purchases received at the store"):
        a, b = st.columns([2, 3])
        with a:
            table = pd.DataFrame({"Store": stores["store"].map(label_store), "Sales": stores["sales"], "Purchases": stores["purchases"], "Profit": stores["profit"], "Margin %": stores["margin"]}).head(1000)
            show_df(styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %"]), height=420)
        with b:
            top = stores.head(25)
            fig = go.Figure(go.Bar(x=top["store"].map(label_store), y=top["profit"], marker_color=[GREEN if v >= 0 else RED for v in top["profit"]]))
            fig.update_xaxes(type="category", tickangle=-45)
            fig.update_yaxes(tickprefix="$", separatethousands=True)
            fig.update_layout(title=dict(text="Top 25 stores by profit", font=dict(size=13)))
            show_plot(fig, 420)

    classes = summarise("classification")
    with card("Classification-wise Performance"):
        a, b = st.columns([3, 2])
        with a:
            table = pd.DataFrame({"Classification": classes["classification"].map(label_class), "Sales": classes["sales"], "Purchases": classes["purchases"], "Profit": classes["profit"], "Margin %": classes["margin"], "Units Sold": classes["sales_qty"]})
            show_df(styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %"], int_cols=["Units Sold"]))
        with b:
            fig = go.Figure(go.Bar(x=classes["classification"].map(label_class), y=classes["profit"], marker_color=BLUE, text=[money_short(v) for v in classes["profit"]], textposition="auto"))
            fig.update_yaxes(tickprefix="$", separatethousands=True)
            fig.update_layout(title=dict(text="Profit by classification", font=dict(size=13)))
            show_plot(fig, 320)


def tab_ai(ctx) -> None:
    cfg = llm_config()
    mode = (f"LLM mode ({cfg['name']}): the model writes the SQL and the answer is phrased only from the returned rows"
            if cfg else "Rule-based SQL agent (no API key found). Add GROQ_API_KEY, GEMINI_API_KEY or OPENAI_API_KEY to a .env file to switch on LLM mode")
    st.markdown(
        f"<div class='card-sub'>{esc(mode)}. Ask in English or Hinglish. Every answer is computed by a SQL query on the full "
        "portfolio (all vendors), regardless of sidebar filters.</div>", unsafe_allow_html=True)
    st.session_state.setdefault("chat", [])

    pending = st.session_state.pop("pending_q", None)
    if pending:
        st.session_state.chat.append({"role": "user", "text": pending})
        st.session_state.chat.append({"role": "assistant", **safe_agent_answer(pending, ctx)})

    cols = st.columns(3)
    for i, suggestion in enumerate(SUGGESTIONS):
        cols[i % 3].button(suggestion, key=f"sg_{i}", on_click=queue_question, args=(suggestion,))

    if not st.session_state.chat:
        st.info("Ask a question below or click a suggestion.")
    for msg in st.session_state.chat:
        with st.chat_message(msg["role"]):
            st.markdown(str(msg["text"]).replace("$", "\\$"))
            if msg.get("table") is not None:
                table = msg["table"]
                money_cols = [c for c in table.columns if c in ("Sales", "Purchases", "Profit", "Purchased $", "Loss", "Stock Value", "Previous Profit", "Forecast Sales")]
                pct_cols = [c for c in table.columns if str(c).endswith("%")]
                int_cols = [c for c in table.columns if c in ("Units Purchased",)]
                show_df(styled(table, money_cols=money_cols, pct_cols=pct_cols, int_cols=int_cols))
            if msg.get("fig") is not None:
                show_plot(msg["fig"], 340)
            if msg.get("sql"):
                with st.expander("SQL used"):
                    st.code(str(msg["sql"]), language="sql")
            if msg.get("mode") and msg["role"] == "assistant":
                st.caption(msg["mode"])

    with st.form("ai_form", clear_on_submit=True):
        prompt = st.text_input("Ask a question", placeholder="e.g. Top 5 loss-making vendors kaun hain?", key="ai_input")
        asked = st.form_submit_button("Ask")
    if asked and prompt.strip():
        st.session_state.chat.append({"role": "user", "text": prompt.strip()})
        st.session_state.chat.append({"role": "assistant", **safe_agent_answer(prompt.strip(), ctx)})
        st.session_state.chat = st.session_state.chat[-30:]
        st.rerun()


def tab_reports(ctx) -> None:
    fv, vs, kp, fk = ctx.fv, ctx.vs, ctx.kpi, ctx.ftot

    # ---- downloads -------------------------------------------------------------
    with card("Downloads", "All exports use the current sidebar filters"):
        c1, c2, c3 = st.columns(3)
        c1.download_button("Download Filtered CSV", make_csv(fv), "vendors_filtered.csv", "text/csv", key="dl_csv")
        c2.download_button("Download Excel (2 sheets)", make_excel(fv, ctx.summary_rows), "vendor_report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_xlsx")
        if fv.empty:
            c3.button("One-Page PDF", disabled=True, key="dl_pdf_off")
        else:
            bands = vs["risk_band"].value_counts()
            risk_line = f"Risk summary (all vendors): {int(bands.get('Red', 0))} Red | {int(bands.get('Yellow', 0))} Yellow | {int(bands.get('Green', 0))} Green. Loss-making vendors: {int((vs['profit'] < 0).sum())}."
            pdf = make_pdf(ctx.summary_rows, fv.nlargest(5, "profit")[["vendor_name", "sales", "profit", "margin"]], fv.nsmallest(5, "profit")[["vendor_name", "sales", "profit", "margin"]], ctx.filter_text, risk_line)
            c3.download_button("Download One-Page PDF", pdf, "vendor_summary.pdf", "application/pdf", key="dl_pdf")

    # ---- email alert simulation ---------------------------------------------------
    with card("Email Alert Simulation", "Generates draft emails for loss-making vendors. Nothing is actually sent."):
        loss = vs[vs["profit"] < 0].sort_values("profit")
        st.caption(f"{len(loss)} loss-making vendors (all vendors, not only the filtered ones).")
        if st.button("Send Alert to Loss-Making Vendors", key="btn_send_alerts", disabled=loss.empty):
            st.session_state.alert_drafts = build_email_drafts(loss, ctx.vendors)
            st.session_state.alert_time = datetime.now()
        drafts = st.session_state.get("alert_drafts")
        if drafts:
            st.success(f"Simulation complete: {len(drafts)} draft emails generated at {st.session_state.get('alert_time', datetime.now()):%H:%M:%S}. No email was sent.")
            idx = st.selectbox("Preview draft", list(range(len(drafts))), format_func=lambda i: f"{drafts[i]['vendor']} ({drafts[i]['to']})", key="alert_pick")
            d = drafts[idx]
            st.text_input("To", d["to"], disabled=True, key=f"alert_to_{idx}")
            st.text_input("Subject", d["subject"], disabled=True, key=f"alert_sub_{idx}")
            st.text_area("Body", d["body"], height=320, key=f"alert_body_{idx}")
            sep = "\n\n" + "=" * 70 + "\n\n"
            all_text = sep.join(f"To: {x['to']}\nSubject: {x['subject']}\n\n{x['body']}" for x in drafts)
            b1, b2 = st.columns(2)
            b1.download_button("Download all drafts (.txt)", all_text.encode("utf-8"), "loss_vendor_alert_drafts.txt", "text/plain", key="dl_alerts")
            if "@" in d["to"] and not d["to"].startswith("["):
                b2.markdown(f"[Open this draft in your email app](mailto:{d['to']}?subject={urllib.parse.quote(d['subject'])}&body={urllib.parse.quote(d['body'])})")

    # ---- data quality ----------------------------------------------------------
    with card("Data Quality", "Instant checks on vendor_summary. The raw-table scan reads a sample, never the full 12.8M rows."):
        q = summary_quality(ctx.version)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Null cells (vendor_summary)", f"{q['nulls']:,}")
        c2.metric("Duplicate vendors", f"{q['dupes']:,}")
        c3.metric("Invalid values (negative sales)", f"{q['invalid']:,}")
        c4.metric("Vendor rows", f"{q['rows']:,}")

        recon = pd.DataFrame(
            {
                "Check": ["Total Sales", "Total Purchases", "Total Profit", "Profit Margin", "Total Vendors"],
                "Dashboard": [money(kp["sales"]), money(kp["purchases"]), money(kp["profit"]), pct(kp["margin"]), f"{kp['vendors']}"],
                "Verified": [money(EXPECTED["sales"]), money(EXPECTED["purchases"]), money(EXPECTED["profit"]), pct(EXPECTED["margin"]), f"{EXPECTED['vendors']}"],
                "Status": [
                    "Match" if abs(kp["sales"] - EXPECTED["sales"]) < 1 else "Differs",
                    "Match" if abs(kp["purchases"] - EXPECTED["purchases"]) < 1 else "Differs",
                    "Match" if abs(kp["profit"] - EXPECTED["profit"]) < 1 else "Differs",
                    "Match" if abs(kp["margin"] - EXPECTED["margin"]) < 0.01 else "Differs",
                    "Match" if kp["vendors"] == EXPECTED["vendors"] else "Differs",
                ],
            }
        )
        st.markdown("**Reconciliation with verified totals**")
        show_df(recon)

        if st.button("Run raw-table quality scan (sampled, 200K rows per table)", key="btn_dq"):
            engine = get_engine()
            try:
                with st.spinner("Scanning samples..."):
                    st.session_state.dq_result = get_data_quality(engine)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Quality scan failed: {exc}")
            finally:
                engine.dispose()
        dq = st.session_state.get("dq_result")
        if dq is not None and len(dq):
            d1, d2, d3 = st.columns(3)
            d1.metric("Nulls (sampled)", f"{int(dq['Null Cells'].sum()):,}")
            d2.metric("Duplicate rows (sampled)", f"{int(dq['Duplicate Rows'].sum()):,}")
            d3.metric("Invalid dates (sampled)", f"{int(dq['Invalid Dates'].sum()):,}")
            show_df(dq)

    # ---- anomalies -------------------------------------------------------------
    with card("Anomalies", "Margin below 0%, margin above 60%, or sell-through below 20% (filtered vendors)"):
        an = anomalies_frame(fv)
        if an.empty:
            st.success("No anomalies in the current selection.")
        else:
            table = pd.DataFrame({"Vendor": an["vendor_name"], "Sales": an["sales"], "Profit": an["profit"], "Margin %": an["margin"], "Sell-Through %": an["sell_through"], "Reason": an["reason"]}).sort_values("Profit").head(1000)
            st.caption(f"{len(an)} anomalous vendors")
            show_df(styled(table, money_cols=["Sales", "Profit"], pct_cols=["Margin %", "Sell-Through %"]), height=360)

    render_pipeline(has_data=True)


# ----------------------------------------------------------------------------
# New tabs: Dashboard extras, Vendors, Control Tower, Analytics, What-If
# ----------------------------------------------------------------------------
def band_badge(band: str) -> str:
    cls = {"Red": "b-red", "Yellow": "b-yellow", "Green": "b-green"}.get(band, "b-grey")
    return f"<span class='badge {cls}'>{esc(band)}</span>"


def num_or_dash(x, digits: int = 1) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:,.{digits}f}"


def tab_dashboard(ctx) -> None:
    vs, fv = ctx.vs, ctx.fv
    with card("Auto-Insights", "Generated from the live data every time the dashboard loads"):
        dead = 0.0
        pa = ctx.pf_all
        if pa is not None and not pa.empty:
            dead = float(pa[(pa["purchases"] > 0) & (pa["sales"] <= 0)]["purchases"].sum())
        for line in auto_insights(vs, ctx.vm_all, dead):
            st.markdown(f"<div class='ai-bullet'>{line}</div>", unsafe_allow_html=True)

    bands = vs["risk_band"].value_counts()
    r1, r2, r3, r4 = st.columns(4)
    r1.markdown(f"<div class='insight'><b>Vendor Risk</b><br>{band_badge('Red')} <b>{int(bands.get('Red', 0))}</b> high risk</div>", unsafe_allow_html=True)
    r2.markdown(f"<div class='insight'><b>&nbsp;</b><br>{band_badge('Yellow')} <b>{int(bands.get('Yellow', 0))}</b> watch list</div>", unsafe_allow_html=True)
    r3.markdown(f"<div class='insight'><b>&nbsp;</b><br>{band_badge('Green')} <b>{int(bands.get('Green', 0))}</b> healthy</div>", unsafe_allow_html=True)
    r4.markdown(f"<div class='insight'><b>Loss-making vendors</b><br><b>{int((vs['profit'] < 0).sum())}</b> of {len(vs)}</div>", unsafe_allow_html=True)
    st.write("")

    if fv.empty:
        return empty_message()
    c1, c2 = st.columns([2, 3])
    with c1:
        with card("Where each sales dollar goes", "Purchases + gross profit = sales (filtered vendors)"):
            t = ctx.ftot
            fig = go.Figure(go.Pie(labels=["Purchases (cost)", "Gross profit"], values=[max(t["purchases"], 0.0), max(t["profit"], 0.0)],
                                   hole=0.62, marker=dict(colors=[AMBER, GREEN]), textinfo="label+percent",
                                   hovertemplate="%{label}: $%{value:,.0f}<extra></extra>"))
            fig.add_annotation(text=f"Sales<br><b>{money_short(t['sales'])}</b>", showarrow=False, font=dict(size=14))
            fig.update_layout(showlegend=False)
            show_plot(fig, 340)
            if t["profit"] < 0:
                st.caption("Filtered profit is negative, so only the cost slice is drawn.")
    with c2:
        with card("Vendor performance bubble chart", "x = sales (log scale), y = margin % (capped at +/-100 for display), size = purchases, colour = risk band"):
            d = fv[fv["sales"] > 0].copy()
            if d.empty:
                st.info("No vendors with sales in the current selection.")
            else:
                d["margin_plot"] = d["margin"].clip(-100, 100)
                d["bubble"] = d["purchases"].clip(lower=1)
                fig = px.scatter(d, x="sales", y="margin_plot", size="bubble", color="risk_band", color_discrete_map=BAND_COLORS,
                                 hover_name="vendor_name", size_max=48, log_x=True,
                                 hover_data={"sales": ":$,.0f", "profit": ":$,.0f", "margin": ":.1f", "margin_plot": False, "bubble": False})
                fig.add_hline(y=0, line_dash="dot", line_color="#8E9AAF")
                fig.update_yaxes(title="Margin % (capped)")
                fig.update_xaxes(title="Sales ($, log)")
                show_plot(fig, 340)
    tab_overview(ctx)


def render_risk_table(df: pd.DataFrame, height: int = 420) -> None:
    t = pd.DataFrame({
        "Vendor": df["vendor_name"], "Risk Score": df["risk_score"], "Band": df["risk_band"],
        "Margin risk": df["rk_margin"], "Delay risk": df["rk_delay"], "Unsold (return proxy)": df["rk_unsold"],
        "Consistency risk": df["rk_consistency"], "Margin %": df["margin"], "Sales": df["sales"], "Profit": df["profit"],
    }).head(1000)
    s = styled(t, money_cols=["Sales", "Profit"], pct_cols=["Margin %"], dec_cols=["Risk Score", "Margin risk", "Delay risk", "Unsold (return proxy)", "Consistency risk"])
    show_df(color_cells(s, "Band", BAND_COLORS), height=height)


def tab_risk(ctx) -> None:
    fv = ctx.fv
    if fv.empty:
        return empty_message()
    with card("Vendor Risk Score (0-100)", "Green below 35 | Yellow 35 to 60 | Red 60 and above. Higher = riskier."):
        st.markdown(
            "**Score = Margin risk x 35% + Delay risk x 25% + Unsold share x 20% + Sales consistency x 20%.** "
            "Margin risk is 0 at a 40% margin and 100 at 0% or below. Delay risk comes from the delay score (Control Tower). "
            "Unsold share = 100 - sell-through %, used as the **return-rate proxy** because the database has no returns table. "
            "Consistency risk = coefficient of variation of monthly sales (needs 3+ months). "
            "A component that cannot be measured is dropped and the other weights are re-scaled."
        )
        missing = []
        if fv["rk_delay"].notna().sum() == 0:
            missing.append("delay (build delay analytics in Control Tower)")
        if fv["rk_consistency"].notna().sum() == 0:
            missing.append("consistency (needs 3+ months of monthly data)")
        if missing:
            st.info("Not yet included in the score: " + "; ".join(missing) + ".")
    pick = st.radio("Show", ["All", "Red", "Yellow", "Green"], horizontal=True, key="risk_pick")
    view = fv if pick == "All" else fv[fv["risk_band"] == pick]
    view = view.sort_values("risk_score", ascending=False)
    c1, c2 = st.columns([3, 2])
    with c1:
        with card(f"Risk table ({len(view):,} vendors)"):
            render_risk_table(view, 440)
    with c2:
        with card("Top 15 riskiest vendors"):
            top = view.head(15)
            fig = go.Figure(go.Bar(x=top["risk_score"], y=top["vendor_name"].str.slice(0, 28), orientation="h",
                                   marker_color=[BAND_COLORS.get(b, BLUE) for b in top["risk_band"]], text=top["risk_score"].round(0), textposition="auto"))
            fig.update_yaxes(autorange="reversed")
            fig.update_xaxes(range=[0, 100])
            show_plot(fig, 440)


def tab_loss(ctx) -> None:
    loss = loss_frame(ctx.fv)
    if loss.empty:
        st.success("No loss-making vendors in the current selection.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("Loss-making vendors", f"{len(loss):,}")
    c2.metric("Combined loss", money_short(loss["profit"].sum()))
    c3.metric("Worst vendor", str(loss.iloc[0]["vendor_name"])[:26], money_short(loss.iloc[0]["profit"]))
    st.caption("Margin is computed safely: vendors with zero sales show 'n/a (no sales)' instead of a divide-by-zero, and extreme values such as -476,000% "
               "are labelled 'below -1,000%' in the table and capped at -100% in the chart.")
    a, b = st.columns([3, 2])
    with a:
        with card("Loss-making vendors"):
            t = pd.DataFrame({"Vendor": loss["vendor_name"], "Sales": loss["sales"], "Purchases": loss["purchases"], "Loss": loss["profit"],
                              "Margin": loss["margin_label"], "Risk Band": loss["risk_band"]}).head(1000)
            s = styled(t, money_cols=["Sales", "Purchases", "Loss"])
            show_df(color_cells(s, "Risk Band", BAND_COLORS), height=420)
    with b:
        with card("Largest losses"):
            top = loss.head(15)
            show_plot(hbar(top["vendor_name"].str.slice(0, 28), top["profit"], "", RED), 420)


def tab_vendors(ctx) -> None:
    sub = st.tabs(["Scorecard", "Risk Scores", "Loss-Making", "Deep-Dive"])
    with sub[0]:
        tab_scorecard(ctx)
    with sub[1]:
        tab_risk(ctx)
    with sub[2]:
        tab_loss(ctx)
    with sub[3]:
        tab_deepdive(ctx)


def tab_catalog(ctx) -> None:
    sub = st.tabs(["Product & Brand", "Store & Category"])
    with sub[0]:
        tab_products(ctx)
    with sub[1]:
        tab_store_category(ctx)


# ---- Control tower -------------------------------------------------------------
def tab_delay(ctx) -> None:
    if not table_exists("purchases"):
        st.info("The purchases table is missing. Upload purchases.csv in the Data Pipeline (Reports tab) first.")
        return
    if ctx.dm.empty:
        st.info("Delay prediction needs lead times (receiving date minus PO date) per vendor and month. "
                "Build them once from the purchases table (a single SQL scan, usually under a minute).")
        if st.button("Build delay analytics", key="btn_build_delay"):
            with st.spinner("Scanning the purchases table..."):
                ok, msg = build_delay_table()
            if ok:
                st.cache_data.clear()
                st.session_state.last_refresh = datetime.now()
                st.rerun()
            else:
                st.error(msg)
        return
    d = ctx.fv[ctx.fv["delay_score"].notna()]
    if d.empty:
        return st.info("No delay data for the current selection.")
    sla = ctx.sla
    high = d[d["delay_risk"] == "High"].sort_values("delay_score", ascending=False)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Vendors analysed", f"{len(d):,}")
    m2.metric("Median lead time", f"{d['avg_lead'].median():.1f} days")
    m3.metric("High delay risk", f"{len(high):,}")
    m4.metric("Late threshold", f"{sla} days", help="Change it in the sidebar under Supply-chain settings")

    with card("High Delay Risk Vendors", "Prediction = weighted mean of the last 3 months + linear trend of monthly lead time. "
                                          "Score = 50% late share + 30% predicted lead vs portfolio median + 20% volatility."):
        if high.empty:
            st.success("No vendor is currently flagged High delay risk.")
        else:
            t = pd.DataFrame({
                "Vendor": high["vendor_name"], "Orders": high["orders"], "Avg Lead (days)": high["avg_lead"],
                "Predicted Next Month (days)": high["pred_lead"], "Trend (days/month)": high["trend"],
                f"Late % (> {sla}d)": high["late_pct"], "Delay Score": high["delay_score"], "Delay Risk": high["delay_risk"],
            })
            s = styled(t, int_cols=["Orders"], dec_cols=["Avg Lead (days)", "Predicted Next Month (days)", "Trend (days/month)", "Delay Score"], pct_cols=[f"Late % (> {sla}d)"])
            show_df(color_cells(s, "Delay Risk", DELAY_COLORS), height=360)
            st.download_button("Download delay table (CSV)", t.round(2).to_csv(index=False).encode("utf-8"), "delay_risk_vendors.csv", "text/csv", key="dl_delay")

    c1, c2 = st.columns(2)
    with c1:
        with card("Top 15 delay scores"):
            top = d.sort_values("delay_score", ascending=False).head(15)
            fig = go.Figure(go.Bar(x=top["delay_score"], y=top["vendor_name"].str.slice(0, 28), orientation="h",
                                   marker_color=[DELAY_COLORS.get(r, BLUE) for r in top["delay_risk"]], text=top["delay_score"].round(0), textposition="auto"))
            fig.update_yaxes(autorange="reversed")
            show_plot(fig, 380)
    with c2:
        with card("Portfolio lead time by month (days)"):
            dm = ctx.dm
            tot = dm["n"].groupby(dm["month"]).sum()
            wl = (dm["avg_lead"] * dm["n"]).groupby(dm["month"]).sum() / tot
            fig = go.Figure(go.Scatter(x=wl.index.tolist(), y=wl.values, mode="lines+markers", line=dict(color=BLUE, width=3)))
            fig.add_hline(y=sla, line_dash="dash", line_color=RED, annotation_text=f"{sla}-day threshold")
            fig.update_xaxes(type="category")
            show_plot(fig, 380)

    with card("Single-vendor lead-time trend and prediction"):
        names = d.sort_values("orders", ascending=False)
        labels = {int(r.vendor_no): f"{r.vendor_name} (#{r.vendor_no})" for r in names.itertuples()}
        vid = st.selectbox("Vendor", list(labels.keys()), format_func=lambda i: labels[i], key="delay_vendor")
        g = ctx.dm[ctx.dm["vendor_no"] == vid].sort_values("month")
        row = d[d["vendor_no"] == vid].iloc[0]
        fig = go.Figure(go.Scatter(x=g["month"], y=g["avg_lead"], mode="lines+markers", name="Actual avg lead", line=dict(color=BLUE, width=3)))
        try:
            nxt = str(pd.Period(g["month"].iloc[-1], freq="M") + 1)
            fig.add_trace(go.Scatter(x=[g["month"].iloc[-1], nxt], y=[g["avg_lead"].iloc[-1], row["pred_lead"]], mode="lines+markers",
                                     name="Prediction", line=dict(color=RED, width=3, dash="dash")))
        except Exception:  # noqa: BLE001
            pass
        fig.update_xaxes(type="category")
        show_plot(fig, 320)
        st.caption(f"Predicted next-month lead time: {row['pred_lead']:.1f} days | average {row['avg_lead']:.1f} days | trend {row['trend']:+.2f} days per month | {row['late_pct']:.0f}% of orders later than {sla} days.")


def tab_inventory(ctx) -> None:
    if ctx.vp.empty:
        return aggregates_message()
    if ctx.fv.empty:
        return empty_message()
    st.caption("The database has no stock-on-hand column, so stock is ESTIMATED as units purchased minus units sold (opening inventory is unknown). "
               "Cover days = estimated stock / average daily units sold. Treat alerts as a review list, not an exact count.")
    c1, c2, c3 = st.columns(3)
    low_days = c1.slider("Low stock if cover below (days)", 1, 60, 15, key="inv_low")
    over_days = c2.slider("Overstock if cover above (days)", 60, 720, 180, key="inv_over")
    min_units = c3.number_input("Ignore products with fewer units sold than", min_value=0, value=10, step=5, key="inv_min")
    inv = inventory_frame(ctx.pf, ctx.n_months, low_days, over_days, min_units)
    if inv.empty:
        return st.info("No product data for the current selection.")
    low = inv[inv["status"] == "Low stock"].sort_values("sales", ascending=False)
    over = inv[inv["status"] == "Overstock"].sort_values("stock_value", ascending=False)
    catalog_idle = 0
    if not ctx.products.empty and "brand" in ctx.products.columns:
        catalog_idle = int(len(set(ctx.products["brand"].astype(str)) - set(ctx.pf["brand"].astype(str))))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Low stock products", f"{len(low):,}")
    m2.metric("Overstock products", f"{len(over):,}")
    m3.metric("Capital tied up in overstock", money_short(over["stock_value"].sum()))
    m4.metric("Catalog brands with no activity", f"{catalog_idle:,}" if len(ctx.products) else "n/a", help="Brands listed in products.csv with no purchases or sales")

    def show_status(frame: pd.DataFrame, colour: str) -> None:
        if frame.empty:
            return st.success("Nothing to flag.")
        view = frame.head(500).copy()
        view["cover_days"] = view["cover_days"].replace(np.inf, np.nan)
        t = pd.DataFrame({"Brand": view["brand"], "Product": view["description"], "Units Sold": view["sales_qty"], "Units Purchased": view["purchase_qty"],
                          "Est. Stock": view["stock"], "Cover (days)": view["cover_days"], "Stock Value": view["stock_value"]})
        show_df(styled(t, money_cols=["Stock Value"], int_cols=["Units Sold", "Units Purchased", "Est. Stock"], dec_cols=["Cover (days)"]), height=360)

    a, b = st.columns(2)
    with a:
        with card("Low stock - reorder candidates", "Selling fast, little estimated stock left"):
            show_status(low, AMBER)
    with b:
        with card("Overstock - slow or no movement", "Large estimated stock relative to sales"):
            show_status(over, RED)
    with card("Stock status mix"):
        mix = inv["status"].value_counts().reset_index()
        mix.columns = ["Status", "Products"]
        fig = px.pie(mix, names="Status", values="Products", hole=0.55, color="Status",
                     color_discrete_map={"OK": GREEN, "Low stock": AMBER, "Overstock": RED})
        show_plot(fig, 300)


def tab_pareto(ctx) -> None:
    if ctx.fv.empty:
        return empty_message()
    basis = st.radio("Pareto basis", ["Purchase value", "Sales"], horizontal=True, key="pareto_basis")
    value = "purchases" if basis == "Purchase value" else "sales"
    d = pareto_frame(ctx.fv, value)
    if d.empty:
        return st.info("No positive values to analyse.")
    nv = int(d["vital"].sum())
    st.markdown(f"<div class='insight'><b>{nv}</b> of <b>{len(d)}</b> vendors (<b>{nv / len(d) * 100:.0f}%</b>) drive 80% of {basis.lower()}. "
                f"Top vendor: <b>{esc(d.iloc[0]['vendor_name'])}</b> ({d.iloc[0]['share']:.1f}% share).</div>", unsafe_allow_html=True)
    st.write("")
    c1, c2 = st.columns([3, 2])
    with c1:
        with card(f"Pareto chart - {basis}", "Dark bars = the vital few that make up 80%"):
            fig = go.Figure()
            fig.add_trace(go.Bar(x=d["rank"], y=d[value], marker_color=[BLUE if v else "#B8C4D9" for v in d["vital"]], name=basis,
                                 hovertext=d["vendor_name"], hovertemplate="%{hovertext}<br>$%{y:,.0f}<extra></extra>"))
            fig.add_trace(go.Scatter(x=d["rank"], y=d["cum_share"], yaxis="y2", mode="lines", name="Cumulative %", line=dict(color=RED, width=3)))
            fig.add_shape(type="line", xref="paper", x0=0, x1=1, yref="y2", y0=80, y1=80, line=dict(color=AMBER, dash="dash"))
            fig.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 100], ticksuffix="%", showgrid=False))
            fig.update_xaxes(title="Vendor rank")
            fig.update_yaxes(tickprefix="$", separatethousands=True)
            show_plot(fig, 400)
    with c2:
        with card("The vital few"):
            vital = d[d["vital"]]
            t = pd.DataFrame({"Rank": vital["rank"], "Vendor": vital["vendor_name"], basis: vital[value], "Share %": vital["share"], "Cumulative %": vital["cum_share"]})
            show_df(styled(t, money_cols=[basis], pct_cols=["Share %", "Cumulative %"]), height=400)


def tab_control_tower(ctx) -> None:
    sub = st.tabs(["Delay Prediction", "Inventory Alerts", "Pareto 80/20"])
    with sub[0]:
        tab_delay(ctx)
    with sub[1]:
        tab_inventory(ctx)
    with sub[2]:
        tab_pareto(ctx)


# ---- Analytics ---------------------------------------------------------------------
def render_comparison(ctx, kp: str = "cmp") -> None:
    vs = ctx.vs
    with card("Side-by-Side Vendor Comparison", "Select any two vendors"):
        ordered = vs.sort_values("sales", ascending=False)
        labels = {int(r.vendor_no): f"{r.vendor_name} (#{r.vendor_no})" for r in ordered.itertuples()}
        ids = list(labels.keys())
        c1, c2 = st.columns(2)
        a = c1.selectbox("Vendor A", ids, index=0, format_func=lambda i: labels[i], key=f"{kp}_a")
        b = c2.selectbox("Vendor B", ids, index=min(1, len(ids) - 1), format_func=lambda i: labels[i], key=f"{kp}_b")
        ra = vs[vs["vendor_no"] == a].iloc[0]
        rb = vs[vs["vendor_no"] == b].iloc[0]

        def col(r) -> list:
            return [money(r["sales"]), money(r["purchases"]), money(r["profit"]), pct(r["margin"]), pct(r["sell_through"]), f"{r['score']:.1f}", r["grade"], r["risk"],
                    f"{r['risk_score']:.0f} ({r['risk_band']})", num_or_dash(r["avg_lead"]), str(r["delay_risk"])]

        compare = pd.DataFrame({
            "Metric": ["Sales", "Purchases", "Profit", "Margin %", "Sell-Through %", "Score", "Grade", "Risk", "Risk Score (0-100)", "Avg Lead Days", "Delay Risk"],
            ra["vendor_name"] + " (A)": col(ra),
            rb["vendor_name"] + " (B)": col(rb),
        })
        show_df(compare)
        g1, g2, g3 = st.columns(3)
        fig1 = go.Figure()
        for name, row, colr in ((ra["vendor_name"], ra, BLUE), (rb["vendor_name"], rb, AMBER)):
            fig1.add_trace(go.Bar(name=name, x=["Sales", "Purchases", "Profit"], y=[row["sales"] / 1e6, row["purchases"] / 1e6, row["profit"] / 1e6], marker_color=colr))
        fig1.update_layout(barmode="group", title=dict(text="Financials ($M)", font=dict(size=13)))
        fig2 = go.Figure()
        for name, row, colr in ((ra["vendor_name"], ra, BLUE), (rb["vendor_name"], rb, AMBER)):
            fig2.add_trace(go.Bar(name=name, x=["Margin %", "Sell-Through %", "Score"], y=[row["margin"], row["sell_through"], row["score"]], marker_color=colr))
        fig2.update_layout(barmode="group", title=dict(text="Performance indicators", font=dict(size=13)))
        fig3 = go.Figure()
        for name, row, colr in ((ra["vendor_name"], ra, BLUE), (rb["vendor_name"], rb, AMBER)):
            fig3.add_trace(go.Bar(name=name, x=["Margin", "Delay", "Unsold", "Consistency"],
                                  y=[row["rk_margin"], row["rk_delay"], row["rk_unsold"], row["rk_consistency"]], marker_color=colr))
        fig3.update_layout(barmode="group", title=dict(text="Risk components (0-100)", font=dict(size=13)))
        with g1:
            show_plot(fig1, 320)
        with g2:
            show_plot(fig2, 320)
        with g3:
            show_plot(fig3, 320)


def tab_trend(ctx) -> None:
    if ctx.vm.empty:
        return aggregates_message()
    if ctx.fv.empty:
        return empty_message()
    monthly = monthly_frame(ctx.vm, ctx.fv["vendor_no"])
    if monthly.empty:
        return st.info("No monthly data in the selected date range.")
    with card("Month-wise Purchase vs Sales vs Profit", f"{ctx.months_text} | filtered vendors"):
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["purchases"], name="Purchases", mode="lines+markers", line=dict(color=AMBER, width=3)))
        fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["sales"], name="Sales", mode="lines+markers", line=dict(color=BLUE, width=3)))
        fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["profit"], name="Profit", mode="lines+markers", line=dict(color=GREEN, width=3)))
        fig.update_xaxes(type="category")
        fig.update_yaxes(tickprefix="$", separatethousands=True)
        show_plot(fig, 380)
    with card("Monthly numbers"):
        t = pd.DataFrame({"Month": monthly["month"], "Purchases": monthly["purchases"], "Sales": monthly["sales"], "Profit": monthly["profit"]})
        t["Margin %"] = np.where(t["Sales"] > 0, t["Profit"] / t["Sales"].where(t["Sales"] > 0, 1) * 100, 0.0)
        show_df(styled(t, money_cols=["Purchases", "Sales", "Profit"], pct_cols=["Margin %"]))


def tab_profit_drop(ctx) -> None:
    if ctx.vm_all.empty:
        return aggregates_message()
    months = sorted(ctx.vm_all["month"].astype(str).unique())
    if len(months) < 2:
        return st.info("Anomaly detection compares each month with the previous one, so it needs at least 2 months of data.")
    c1, c2, c3 = st.columns(3)
    month = c1.selectbox("Month to check", months[1:], index=len(months) - 2, key="drop_month")
    threshold = c2.slider("Flag if profit dropped by more than (%)", 10, 90, 30, key="drop_thr")
    min_prev = c3.number_input("Ignore vendors with previous profit below ($)", min_value=0.0, value=1000.0, step=500.0, key="drop_min")
    drops, prev, cur = profit_drop_frame(ctx.vm_all, ctx.vs, month, float(threshold), float(min_prev))
    st.markdown(f"<div class='insight'>Comparing <b>{esc(cur)}</b> with <b>{esc(prev)}</b>: <b>{len(drops)}</b> vendors lost more than <b>{threshold}%</b> of their profit.</div>", unsafe_allow_html=True)
    st.write("")
    if drops.empty:
        return st.success("No anomalies for this month and threshold.")
    a, b = st.columns([3, 2])
    with a:
        with card("Vendors with a sudden profit drop"):
            t = pd.DataFrame({"Vendor": drops["vendor_name"], f"Profit {prev}": drops["prev_profit"], f"Profit {cur}": drops["profit"], "Drop %": drops["drop_pct"]})
            show_df(styled(t, money_cols=[f"Profit {prev}", f"Profit {cur}"], pct_cols=["Drop %"]), height=400)
    with b:
        with card("Largest drops"):
            top = drops.head(15)
            show_plot(hbar(top["vendor_name"].str.slice(0, 28), top["drop_pct"].clip(upper=300), "Drop % (capped at 300)", RED, as_money=False), 400)


def tab_forecast(ctx) -> None:
    if ctx.vm_all.empty:
        return aggregates_message()
    if ctx.fv.empty:
        return empty_message()
    c1, c2 = st.columns(2)
    method = c1.selectbox("Method", ["Blend (linear + moving average)", "Linear trend", "3-month moving average"], key="fc_method")
    drop_last = c2.checkbox("Exclude the latest month (it may be incomplete)", value=False, key="fc_droplast")
    monthly = monthly_frame(ctx.vm_all, ctx.fv["vendor_no"])
    if drop_last and len(monthly) > 1:
        monthly = monthly.iloc[:-1]
    if len(monthly) < 2:
        return st.info("A forecast needs at least 2 months of history.")
    try:
        fc = forecast_sales(monthly, method, 3)
    except Exception:  # noqa: BLE001
        return st.error("Month labels could not be read as dates (expected YYYY-MM), so the forecast cannot be drawn.")
    if len(monthly) < 4:
        st.warning(f"Only {len(monthly)} months of history, so this forecast is indicative only.")
    with card("Next 3 months sales forecast", f"{method} | filtered vendors | shaded band = +/- 1 standard deviation of the trend residuals"):
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=monthly["month"], y=monthly["sales"], name="Actual sales", mode="lines+markers", line=dict(color=BLUE, width=3)))
        xs = [monthly["month"].iloc[-1]] + fc["months"]
        ys = [monthly["sales"].iloc[-1]] + list(fc["pred"])
        fig.add_trace(go.Scatter(x=fc["months"] + fc["months"][::-1], y=list(fc["upper"]) + list(fc["lower"])[::-1], fill="toself",
                                 fillcolor="rgba(230,159,0,0.18)", line=dict(width=0), name="Range", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=xs, y=ys, name="Forecast", mode="lines+markers", line=dict(color=AMBER, width=3, dash="dash")))
        fig.update_xaxes(type="category")
        fig.update_yaxes(tickprefix="$", separatethousands=True)
        show_plot(fig, 380)
    t = pd.DataFrame({"Month": fc["months"], "Forecast Sales": fc["pred"], "Low": fc["lower"], "High": fc["upper"]})
    show_df(styled(t, money_cols=["Forecast Sales", "Low", "High"]))
    st.caption(f"Trend: {money_short(fc['slope'])} per month on {fc['n']} months of history.")


def tab_analytics(ctx) -> None:
    sub = st.tabs(["Monthly Trend", "Vendor Comparison", "Profit-Drop Anomalies", "Forecast"])
    with sub[0]:
        tab_trend(ctx)
    with sub[1]:
        render_comparison(ctx, "cmp2")
    with sub[2]:
        tab_profit_drop(ctx)
    with sub[3]:
        tab_forecast(ctx)


# ---- What-if ----------------------------------------------------------------------------
def tab_whatif(ctx) -> None:
    fv, vs = ctx.fv, ctx.vs
    with card("What-If Simulator", "Live recalculation. Volume change scales sales AND purchases. "
                                   "New profit = sales x (1 + price) x (1 + volume) - purchases x (1 + cost) x (1 + volume)"):
        scope_choice = st.radio("Apply to", ["Filtered vendors", "All vendors"], horizontal=True, key="wi_scope")
        base = fv if scope_choice == "Filtered vendors" else vs
        c1, c2, c3 = st.columns(3)
        cost = c1.slider("Purchase cost change (%)", -30.0, 30.0, -5.0, 0.5, key="wi_cost2", help="-5 means purchase cost falls by 5%")
        price = c2.slider("Selling price change (%)", -30.0, 30.0, 0.0, 0.5, key="wi_price2")
        vol = c3.slider("Sales volume change (%)", -50.0, 50.0, 0.0, 1.0, key="wi_vol")
        if base.empty:
            return st.info("No vendors in scope.")
        sales, purchases = float(base["sales"].sum()), float(base["purchases"].sum())
        current = sales - purchases
        new_sales = sales * (1 + price / 100) * (1 + vol / 100)
        new_purch = purchases * (1 + cost / 100) * (1 + vol / 100)
        projected = new_sales - new_purch
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Current Profit", money_short(current))
        m2.metric("New Projected Profit", money_short(projected), delta=money_short(projected - current))
        m3.metric("Current Margin", pct(current / sales * 100 if sales else 0))
        m4.metric("Projected Margin", pct(projected / new_sales * 100 if new_sales else 0))
        st.markdown(f"<div class='insight'>If purchase cost changes by <b>{cost:+.1f}%</b>, selling price by <b>{price:+.1f}%</b> and volume by <b>{vol:+.0f}%</b>, "
                    f"profit becomes <b>{money(projected)}</b> ({money(projected - current)} vs today).</div>", unsafe_allow_html=True)
        be = (sales * (1 + price / 100) / purchases - 1) * 100 if purchases else None
        if be is not None:
            st.caption(f"Break-even: profit reaches zero if purchase cost rises by {be:+.1f}% (at the selected price change).")
        fig = go.Figure(go.Bar(x=["Current Profit", "Projected Profit"], y=[current, projected], marker_color=[BLUE, GREEN if projected >= current else RED],
                               text=[money_short(current), money_short(projected)], textposition="auto"))
        fig.update_yaxes(tickprefix="$", separatethousands=True)
        show_plot(fig, 300)

    with card("Vendor-level impact", "Which vendors gain or flip from loss to profit under this scenario"):
        d = base.copy()
        d["new_profit"] = d["sales"] * (1 + price / 100) * (1 + vol / 100) - d["purchases"] * (1 + cost / 100) * (1 + vol / 100)
        d["change"] = d["new_profit"] - d["profit"]
        flipped = int(((d["profit"] < 0) & (d["new_profit"] >= 0)).sum())
        turned = int(((d["profit"] >= 0) & (d["new_profit"] < 0)).sum())
        a, b = st.columns(2)
        a.metric("Loss-making vendors turning profitable", f"{flipped:,}")
        b.metric("Profitable vendors turning loss-making", f"{turned:,}")
        top = d.reindex(d["change"].abs().sort_values(ascending=False).index).head(15)
        t = pd.DataFrame({"Vendor": top["vendor_name"], "Profit Today": top["profit"], "Projected Profit": top["new_profit"], "Change": top["change"]})
        show_df(styled(t, money_cols=["Profit Today", "Projected Profit", "Change"]), height=380)


# ----------------------------------------------------------------------------
# App body
# ----------------------------------------------------------------------------
st.markdown(CSS, unsafe_allow_html=True)
st.session_state.setdefault("last_refresh", datetime.now())
for _k, _v in FILTER_DEFAULTS.items():
    st.session_state.setdefault(_k, copy.deepcopy(_v))

VERSION = data_version()
vs = load_vendor_summary(VERSION)

# ---- No data yet ------------------------------------------------------------------
if vs.empty:
    st.markdown(
        "<div class='vpi-header'><div class='vpi-logo'>VPI</div><div><div class='vpi-title'>Vendor Performance Intelligence</div>"
        "<div class='vpi-sub'>Executive Dashboard</div></div></div>",
        unsafe_allow_html=True,
    )
    st.warning("The vendor_summary table was not found in the database.")
    db_diagnostics()
    render_pipeline_report()
    if table_exists("purchases") and table_exists("sales"):
        st.info("purchases and sales exist. Build the summary once (two SQL GROUP BY scans).")
        if st.button("Build vendor_summary now", key="btn_build_summary"):
            engine = get_engine()
            prog = st.progress(0.0)
            try:
                create_vendor_summary(engine, lambda d, t, m: prog.progress(d / max(t, 1), text=m))
                st.cache_data.clear()
                done = True
            except Exception as exc:  # noqa: BLE001
                done = False
                st.error(f"Failed: {exc}")
            finally:
                engine.dispose()
            if done:
                st.rerun()
    render_pipeline(has_data=False)
    st.stop()

# ---- Load the other small tables -------------------------------------------------------
vsc = load_agg("agg_vendor_store_class", VERSION)
vm = load_agg("agg_vendor_month", VERSION)
vp = load_agg("agg_vendor_product", VERSION)
vendors_df = load_vendors(VERSION)
products_df = load_products(VERSION)
status = load_status(VERSION)
counts = load_row_counts(VERSION)
aggs_ready = not vsc.empty and not vm.empty and not vp.empty

# ---- Supply-chain enrichment: delay prediction + 0-100 risk score ---------------------------
dm = load_delay_monthly(VERSION)
SLA = int(st.session_state.get("set_sla", 10))
if SLA not in SLA_OPTIONS:
    SLA = 10
n_months = int(vm["month"].nunique()) if not vm.empty else 0
vs = enrich_vendors(vs, vm, dm, SLA, n_months)
month_options = sorted(vm["month"].astype(str).unique().tolist()) if not vm.empty else []
if len(month_options) >= 2:
    _cur = st.session_state.get("f_months")
    if (not _cur) or len(_cur) != 2 or _cur[0] not in month_options or _cur[1] not in month_options:
        st.session_state.f_months = (month_options[0], month_options[-1])
else:
    st.session_state.f_months = None

# ---- Sidebar ----------------------------------------------------------------------------
store_options = sorted(vsc["store"].unique().tolist(), key=lambda s: (0, int(float(s))) if s.replace(".0", "").isdigit() else (1, s)) if not vsc.empty else []
class_options = sorted(vsc["classification"].unique().tolist()) if not vsc.empty else []
st.session_state.f_store = [x for x in st.session_state.f_store if x in store_options]
st.session_state.f_class = [x for x in st.session_state.f_class if x in class_options]

alerts = alerts_frame(vs)
with st.sidebar:
    st.markdown("<div class='sb-brand'><div class='sb-logo'>VPI</div><div><div class='sb-brand-text'>Vendor Performance</div><div class='sb-brand-sub'>Intelligence Platform</div></div></div>", unsafe_allow_html=True)
    st.markdown(f"<div class='sb-title'>Alerts<span class='sb-badge'>{len(alerts)}</span></div>", unsafe_allow_html=True)
    if alerts.empty:
        st.caption("No active alerts.")
    else:
        html = ""
        for r in alerts.head(8).itertuples():
            cls = "sb-alert crit" if r.critical else "sb-alert"
            html += f"<div class='{cls}'><div class='sb-alert-name'>{esc(str(r.vendor_name)[:30])}</div><div class='sb-alert-why'>{esc(r.why)}</div></div>"
        st.markdown(html, unsafe_allow_html=True)
        if len(alerts) > 8:
            st.caption(f"+ {len(alerts) - 8} more. See Anomalies in the Reports tab.")
        st.caption("Alert rules: margin < 0, sell-through < 20%, or loss > $10K.")

    st.markdown("<div class='sb-title'>Ask Vendor AI</div>", unsafe_allow_html=True)
    with st.form("sb_ai_form", clear_on_submit=True):
        sb_q = st.text_input("Ask in English or Hinglish", placeholder="Top 5 loss-making vendors kaun hain?", key="sb_ai_input")
        sb_go = st.form_submit_button("Ask")
    sb_ai_slot = st.container()

    st.markdown("<div class='sb-title'>Filters</div>", unsafe_allow_html=True)
    st.text_input("Vendor search", key="f_search", placeholder="Name or vendor number")
    st.selectbox("Profitability", PROFIT_OPTIONS, key="f_profit")
    st.slider("Margin range (%)", -100.0, 100.0, step=1.0, key="f_margin")
    st.number_input("Minimum sales ($)", min_value=0.0, step=10000.0, format="%.0f", key="f_minsales")
    st.multiselect("Store", store_options, key="f_store", disabled=not store_options, format_func=label_store, help="Needs the analytics tables" if not store_options else None)
    st.multiselect("Classification", class_options, key="f_class", disabled=not class_options, format_func=label_class, help="Needs the analytics tables" if not class_options else None)
    if len(month_options) >= 2:
        st.select_slider("Date range (month)", options=month_options, key="f_months", help="Applies to monthly trend, forecast, anomaly and deep-dive charts. KPI cards stay global.")
    st.button("Clear all filters", on_click=clear_filters, key="btn_clear")

    st.markdown("<div class='sb-title'>Supply-chain settings</div>", unsafe_allow_html=True)
    st.selectbox("Late delivery threshold (days)", SLA_OPTIONS, index=SLA_OPTIONS.index(SLA), key="set_sla", help="An order is late if receiving date minus PO date is above this.")

    st.markdown("<div class='sb-title'>Database</div>", unsafe_allow_html=True)
    st.caption(f"{DB_PATH.name} - {db_file_size() / 1e9:.2f} GB")

# ---- Filters -> views -----------------------------------------------------------------------
filters = {
    "search": st.session_state.f_search,
    "profit": st.session_state.f_profit,
    "margin": tuple(st.session_state.f_margin),
    "minsales": float(st.session_state.f_minsales or 0),
    "stores": list(st.session_state.f_store),
    "classes": list(st.session_state.f_class),
}
fv, scope = apply_filters(vs, vsc, filters)
kpi = totals(vs)  # GLOBAL - never filtered
ftot = totals(fv)
vp_f = vp[vp["vendor_no"].isin(fv["vendor_no"])] if not vp.empty else vp
months_sel = st.session_state.get("f_months")
if months_sel and not vm.empty:
    m_lo, m_hi = months_sel[0], months_sel[1]
    vm_view = vm[(vm["month"].astype(str) >= m_lo) & (vm["month"].astype(str) <= m_hi)]
    full_range = (m_lo == month_options[0] and m_hi == month_options[-1])
    months_text = "all months" if full_range else f"{m_lo} to {m_hi}"
else:
    vm_view, full_range, months_text = vm, True, "all months"
active = []
if filters["search"].strip():
    active.append(f"search '{filters['search'].strip()}'")
if filters["profit"] != "All vendors":
    active.append(filters["profit"].lower())
if filters["margin"] != FILTER_DEFAULTS["f_margin"]:
    active.append(f"margin {filters['margin'][0]:g}% to {filters['margin'][1]:g}%")
if filters["minsales"] > 0:
    active.append(f"min sales {money(filters['minsales'])}")
if filters["stores"]:
    active.append(f"{len(filters['stores'])} store(s)")
if filters["classes"]:
    active.append(f"{len(filters['classes'])} classification(s)")
if not full_range:
    active.append(f"months {months_text}")
filter_text = ", ".join(active) if active else "none"

summary_rows = [
    ["Total Sales", money(kpi["sales"]), money(ftot["sales"])],
    ["Total Purchases", money(kpi["purchases"]), money(ftot["purchases"])],
    ["Total Profit", money(kpi["profit"]), money(ftot["profit"])],
    ["Profit Margin", pct(kpi["margin"]), pct(ftot["margin"])],
    ["Vendors", f"{kpi['vendors']}", f"{ftot['vendors']}"],
    ["Active filters", "-", filter_text],
]

pf_all = load_product_totals(VERSION) if not vp.empty else pd.DataFrame()
ctx = SimpleNamespace(
    vs=vs, fv=fv, scope=scope, vm=vm_view, vm_all=vm, vp=vp, vsc=vsc, pf=product_frame(vp_f), pf_all=pf_all, vendors=vendors_df, products=products_df,
    kpi=kpi, ftot=ftot, summary_rows=summary_rows, filter_text=filter_text, version=VERSION,
    dm=dm, sla=SLA, n_months=n_months, months_text=months_text,
)

# ---- Sidebar "Ask Vendor AI" (needs ctx, so the answer is drawn into the slot created above) ----
if sb_go and sb_q.strip():
    _q = sb_q.strip()
    _ans = safe_agent_answer(_q, ctx)
    st.session_state.sb_last = {"q": _q, **_ans}
    st.session_state.setdefault("chat", [])
    st.session_state.chat.append({"role": "user", "text": _q})
    st.session_state.chat.append({"role": "assistant", **_ans})
    st.session_state.chat = st.session_state.chat[-30:]
with sb_ai_slot:
    _last = st.session_state.get("sb_last")
    if _last:
        _body = esc(_last["text"]).replace("\n", "<br>")
        st.markdown(f"<div class='sb-answer'><b>Q:</b> {esc(_last['q'])}<br><br>{_body}</div>", unsafe_allow_html=True)
        if _last.get("table") is not None:
            show_df(_last["table"].head(5))
        if _last.get("sql"):
            with st.expander("SQL used"):
                st.code(str(_last["sql"]), language="sql")
        st.caption(f"{_last.get('mode', '')} | full charts in the AI Agent tab")

# ---- Header + top bar -------------------------------------------------------------------------
refreshed = st.session_state.last_refresh
st.markdown(
    "<div class='vpi-header'><div class='vpi-logo'>VPI</div><div>"
    "<div class='vpi-title'>Vendor Performance Intelligence</div>"
    "<div class='vpi-sub'>Executive Dashboard | Sales, procurement and profitability analytics</div></div>"
    f"<div class='vpi-meta'>Last refreshed<br><b>{refreshed:%d %b %Y, %H:%M:%S}</b></div></div>",
    unsafe_allow_html=True,
)
render_pipeline_report()
tb1, tb2, tb3, tb4 = st.columns([4, 1.2, 1.4, 1.6])
tb1.caption(f"Database updated {datetime.fromtimestamp(VERSION):%d %b %Y %H:%M}" if VERSION else "")
tb2.button("Refresh data", on_click=refresh_data, key="btn_refresh")
tb3.download_button("Export CSV", make_csv(fv), "vendors_filtered.csv", "text/csv", key="dl_top_csv")
tb4.download_button("Export Excel", make_excel(fv, summary_rows), "vendor_report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_top_xlsx")

if not aggs_ready:
    st.warning(
        "Monthly, store, classification and product analytics need the one-time analytics tables. "
        "This runs SQL GROUP BY queries on the large tables once (a few minutes) and is then instant. "
        "Alternative from a terminal: python -m src.database aggregates"
    )
    if status.get("has_sales") and status.get("has_purchases"):
        if st.button("Build analytics tables", key="btn_build_aggs"):
            run_aggregate_build()
elif status.get("stale"):
    st.warning("Source tables changed after the analytics tables were built. Rebuild to refresh monthly, store and product views.")
    if st.button("Rebuild analytics tables", key="btn_rebuild_aggs"):
        run_aggregate_build()

# ---- KPI cards (GLOBAL) --------------------------------------------------------------------------
KPI_ICONS = {
    "sales": '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    "purchases": '<circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/>',
    "profit": '<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
    "margin": '<line x1="19" y1="5" x2="5" y2="19"/><circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>',
}


def kpi_card(label: str, value: str, delta_html: str, color: str, icon: str = "") -> str:
    svg = f"<div class='kpi-icon' style='background:{color}'><svg viewBox='0 0 24 24'>{KPI_ICONS[icon]}</svg></div>" if icon in KPI_ICONS else ""
    return (
        f"<div class='kpi' style='border-left-color:{color}'><div class='kpi-head'><div class='kpi-label'>{label}</div>{svg}</div>"
        f"<div class='kpi-value'>{value}</div><div class='kpi-delta'>{delta_html}</div></div>"
    )


k1, k2, k3, k4 = st.columns(4)
k1.markdown(kpi_card("Total Sales", money(kpi["sales"]), f"Filtered: <b>{money(ftot['sales'])}</b>", BLUE, "sales"), unsafe_allow_html=True)
k2.markdown(kpi_card("Total Purchases", money(kpi["purchases"]), f"Filtered: <b>{money(ftot['purchases'])}</b>", AMBER, "purchases"), unsafe_allow_html=True)
k3.markdown(kpi_card("Gross Profit", money(kpi["profit"]), f"Filtered: <b>{money(ftot['profit'])}</b>", GREEN, "profit"), unsafe_allow_html=True)
k4.markdown(kpi_card("Profit Margin", pct(kpi["margin"]), f"Filtered: <b>{pct(ftot['margin'])}</b> | {kpi['vendors']} vendors total", TEAL, "margin"), unsafe_allow_html=True)

st.markdown(
    f"<div class='fstrip'><span class='pill'>Showing {ftot['vendors']} of {kpi['vendors']} vendors</span>"
    f"<span>Filtered Sales: <b>{money(ftot['sales'])}</b></span>"
    f"<span>Filtered Profit: <b>{money(ftot['profit'])}</b></span>"
    f"<span>Filtered Margin: <b>{pct(ftot['margin'])}</b></span>"
    f"<span>Active filters: <b>{esc(filter_text)}</b></span></div>",
    unsafe_allow_html=True,
)

# ---- Tabs -----------------------------------------------------------------------------------------------
tabs = st.tabs(["Dashboard", "Vendors", "Products & Stores", "Control Tower", "Analytics", "AI Agent", "Reports", "What-If"])
with tabs[0]:
    tab_dashboard(ctx)
with tabs[1]:
    tab_vendors(ctx)
with tabs[2]:
    tab_catalog(ctx)
with tabs[3]:
    tab_control_tower(ctx)
with tabs[4]:
    tab_analytics(ctx)
with tabs[5]:
    tab_ai(ctx)
with tabs[6]:
    tab_reports(ctx)
with tabs[7]:
    tab_whatif(ctx)

# ---- Footer -------------------------------------------------------------------------------------------------
st.markdown(
    f"<div class='vpi-footer'><span>Vendors: <b>{kpi['vendors']:,}</b></span>"
    f"<span>Sales rows: <b>{counts['sales']:,}</b></span>"
    f"<span>Purchase rows: <b>{counts['purchases']:,}</b></span>"
    f"<span>Built with <b>Streamlit</b></span></div>",
    unsafe_allow_html=True,
)