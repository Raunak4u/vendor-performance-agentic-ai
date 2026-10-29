"""
Vendor Performance Intelligence - Executive Dashboard
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
import re
import shutil
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
    return "-" if x is None or pd.isna(x) else f"{x:.{digits}f}%"


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
    if include_rank:
        out.insert(0, "Rank", range(1, len(out) + 1))
    return out.reset_index(drop=True)


def render_vendor_table(df: pd.DataFrame, include_rank: bool = False, height: int = 420) -> None:
    table = vendor_table(df.head(1000), include_rank)
    s = styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %", "Sell-Through %"], dec_cols=["Score"])
    s = color_cells(s, "Grade", GRADE_COLORS)
    s = color_cells(s, "Risk", RISK_COLORS)
    show_df(s, height=height)


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
def make_pdf(summary_rows: list, top: pd.DataFrame, bottom: pd.DataFrame, filter_text: str) -> bytes:
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
    lower = Table([[vendor_block(top.head(8), "Top 8 vendors by profit"), vendor_block(bottom.head(8), "Bottom 8 vendors by profit")]], colWidths=[125 * mm, 125 * mm])
    lower.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    story = [
        Paragraph("Vendor Performance Intelligence - One-Page Executive Summary", title),
        Paragraph(f"Generated {datetime.now():%d %b %Y %H:%M} | Filters: {filter_text}", small),
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


def execute_pipeline(chosen: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bar = st.progress(0.0)
    status = st.empty()
    order = [t for t in ("vendors", "products", "purchases", "sales") if t in chosen]
    n = len(order)
    ok = False
    engine = get_engine()
    try:
        for i, table in enumerate(order):
            upload = chosen[table]
            tmp = DATA_DIR / f"_upload_{table}.csv"
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
            finally:
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
        st.caption(
            "For very large files (sales.csv) you can also place them in the data folder and run "
            "`python -m src.database all` from the terminal after loading."
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
    "Top 5 products of DIAGEO",
    "Which vendors have margin below 10%?",
    "Show dead stock",
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


def product_frame(vp: pd.DataFrame) -> pd.DataFrame:
    if vp.empty:
        return pd.DataFrame()
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
        rows["profit"] = rows["sales"] - rows["purchases"]
        rows = rows.sort_values("sales", ascending=False).head(n)
        table = pd.DataFrame({"Product": rows["description"], "Brand": rows["brand"], "Sales": rows["sales"], "Purchases": rows["purchases"], "Profit": rows["profit"]})
        fig = hbar(rows["description"].str.slice(0, 32), rows["sales"], f"Top {len(rows)} products by sales - {vendor['vendor_name']}")
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

    with card("Side-by-Side Vendor Comparison"):
        ordered = vs.sort_values("sales", ascending=False)
        labels = {int(r.vendor_no): f"{r.vendor_name} (#{r.vendor_no})" for r in ordered.itertuples()}
        ids = list(labels.keys())
        c1, c2 = st.columns(2)
        a = c1.selectbox("Vendor A", ids, index=0, format_func=lambda i: labels[i], key="cmp_a")
        b = c2.selectbox("Vendor B", ids, index=min(1, len(ids) - 1), format_func=lambda i: labels[i], key="cmp_b")
        ra = vs[vs["vendor_no"] == a].iloc[0]
        rb = vs[vs["vendor_no"] == b].iloc[0]
        compare = pd.DataFrame(
            {
                "Metric": ["Sales", "Purchases", "Profit", "Margin %", "Sell-Through %", "Score", "Grade", "Risk"],
                ra["vendor_name"] + " (A)": [money(ra["sales"]), money(ra["purchases"]), money(ra["profit"]), pct(ra["margin"]), pct(ra["sell_through"]), f"{ra['score']:.1f}", ra["grade"], ra["risk"]],
                rb["vendor_name"] + " (B)": [money(rb["sales"]), money(rb["purchases"]), money(rb["profit"]), pct(rb["margin"]), pct(rb["sell_through"]), f"{rb['score']:.1f}", rb["grade"], rb["risk"]],
            }
        )
        show_df(compare)
        g1, g2 = st.columns(2)
        fig1 = go.Figure()
        for name, row, colr in ((ra["vendor_name"], ra, BLUE), (rb["vendor_name"], rb, AMBER)):
            fig1.add_trace(go.Bar(name=name, x=["Sales", "Purchases", "Profit"], y=[row["sales"] / 1e6, row["purchases"] / 1e6, row["profit"] / 1e6], marker_color=colr))
        fig1.update_layout(barmode="group", title=dict(text="Financials ($M)", font=dict(size=13)))
        fig2 = go.Figure()
        for name, row, colr in ((ra["vendor_name"], ra, BLUE), (rb["vendor_name"], rb, AMBER)):
            fig2.add_trace(go.Bar(name=name, x=["Margin %", "Sell-Through %", "Score"], y=[row["margin"], row["sell_through"], row["score"]], marker_color=colr))
        fig2.update_layout(barmode="group", title=dict(text="Performance indicators", font=dict(size=13)))
        with g1:
            show_plot(fig1, 340)
        with g2:
            show_plot(fig2, 340)


def tab_deepdive(ctx) -> None:
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
            rows["profit"] = rows["sales"] - rows["purchases"]
            rows["margin"] = np.where(rows["sales"] > 0, rows["profit"] / rows["sales"].where(rows["sales"] > 0, 1) * 100, 0.0)
            rows = rows.sort_values("sales", ascending=False).head(10)
            if rows.empty:
                st.info("No product data for this vendor.")
            else:
                table = pd.DataFrame({"Product": rows["description"], "Brand": rows["brand"], "Sales": rows["sales"], "Purchases": rows["purchases"], "Profit": rows["profit"], "Margin %": rows["margin"]})
                a, b = st.columns([3, 2])
                with a:
                    show_df(styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %"]))
                with b:
                    show_plot(hbar(rows["description"].str.slice(0, 28), rows["sales"], "", BLUE), 330)

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
        table = pd.DataFrame({"Brand": view["brand"], "Product": view["description"], "Sales": view["sales"], "Purchases": view["purchases"], "Profit": view["profit"], "Margin %": view["margin"], "Units Sold": view["sales_qty"]})
        show_df(styled(table, money_cols=["Sales", "Purchases", "Profit"], pct_cols=["Margin %"], int_cols=["Units Sold"]), height=380)

    c1, c2 = st.columns(2)
    with c1:
        with card("Top 20 Products by Sales"):
            top = pf.nlargest(20, "sales")
            show_plot(hbar(top["description"].str.slice(0, 30), top["sales"], "", BLUE), 560)
    with c2:
        with card("Top 20 Products by Profit"):
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
    st.markdown("<div class='card-sub'>Rule-based analyst - no external API. Answers always use the full portfolio (all vendors), regardless of sidebar filters.</div>", unsafe_allow_html=True)
    st.session_state.setdefault("chat", [])

    pending = st.session_state.pop("pending_q", None)
    if pending:
        st.session_state.chat.append({"role": "user", "text": pending})
        st.session_state.chat.append({"role": "assistant", **ai_answer(pending, ctx)})

    cols = st.columns(3)
    for i, suggestion in enumerate(SUGGESTIONS):
        cols[i % 3].button(suggestion, key=f"sg_{i}", on_click=queue_question, args=(suggestion,))

    if not st.session_state.chat:
        st.info("Ask a question below or click a suggestion.")
    for msg in st.session_state.chat:
        with st.chat_message(msg["role"]):
            st.write(msg["text"])
            if msg.get("table") is not None:
                table = msg["table"]
                money_cols = [c for c in table.columns if c in ("Sales", "Purchases", "Profit", "Purchased $")]
                pct_cols = [c for c in table.columns if c.endswith("%")]
                int_cols = [c for c in table.columns if c in ("Units Purchased",)]
                show_df(styled(table, money_cols=money_cols, pct_cols=pct_cols, int_cols=int_cols))
            if msg.get("fig") is not None:
                show_plot(msg["fig"], 340)

    with st.form("ai_form", clear_on_submit=True):
        prompt = st.text_input("Ask a question", placeholder="e.g. Top 5 products of DIAGEO", key="ai_input")
        asked = st.form_submit_button("Ask")
    if asked and prompt.strip():
        st.session_state.chat.append({"role": "user", "text": prompt.strip()})
        st.session_state.chat.append({"role": "assistant", **ai_answer(prompt.strip(), ctx)})
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
            pdf = make_pdf(ctx.summary_rows, fv.nlargest(8, "profit")[["vendor_name", "sales", "profit", "margin"]], fv.nsmallest(8, "profit")[["vendor_name", "sales", "profit", "margin"]], ctx.filter_text)
            c3.download_button("Download One-Page PDF", pdf, "vendor_summary.pdf", "application/pdf", key="dl_pdf")

    # ---- what-if ---------------------------------------------------------------
    with card("What-If Simulator", "Assumes unchanged volumes: new profit = sales x (1 + price increase) - purchases x (1 - cost reduction)"):
        scope_choice = st.radio("Apply to", ["Filtered vendors", "All vendors"], horizontal=True, key="wi_scope")
        base = fv if scope_choice == "Filtered vendors" else vs
        c1, c2 = st.columns(2)
        price_up = c1.slider("Price increase (%)", 0.0, 30.0, 0.0, 0.5, key="wi_price")
        cost_down = c2.slider("Cost reduction (%)", 0.0, 30.0, 0.0, 0.5, key="wi_cost")
        if base.empty:
            st.info("No vendors in scope.")
        else:
            sales, purchases = base["sales"].sum(), base["purchases"].sum()
            current = sales - purchases
            new_sales = sales * (1 + price_up / 100)
            new_purchases = purchases * (1 - cost_down / 100)
            projected = new_sales - new_purchases
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Current Profit", money_short(current))
            m2.metric("New Projected Profit", money_short(projected), delta=money_short(projected - current))
            m3.metric("Current Margin", pct(current / sales * 100 if sales else 0))
            m4.metric("Projected Margin", pct(projected / new_sales * 100 if new_sales else 0))
            fig = go.Figure(go.Bar(x=["Current Profit", "Projected Profit"], y=[current, projected], marker_color=[BLUE, GREEN], text=[money_short(current), money_short(projected)], textposition="auto"))
            fig.update_yaxes(tickprefix="$", separatethousands=True)
            show_plot(fig, 300)

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

    st.markdown("<div class='sb-title'>Filters</div>", unsafe_allow_html=True)
    st.text_input("Vendor search", key="f_search", placeholder="Name or vendor number")
    st.selectbox("Profitability", PROFIT_OPTIONS, key="f_profit")
    st.slider("Margin range (%)", -100.0, 100.0, step=1.0, key="f_margin")
    st.number_input("Minimum sales ($)", min_value=0.0, step=10000.0, format="%.0f", key="f_minsales")
    st.multiselect("Store", store_options, key="f_store", disabled=not store_options, format_func=label_store, help="Needs the analytics tables" if not store_options else None)
    st.multiselect("Classification", class_options, key="f_class", disabled=not class_options, format_func=label_class, help="Needs the analytics tables" if not class_options else None)
    st.button("Clear all filters", on_click=clear_filters, key="btn_clear")

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
filter_text = ", ".join(active) if active else "none"

summary_rows = [
    ["Total Sales", money(kpi["sales"]), money(ftot["sales"])],
    ["Total Purchases", money(kpi["purchases"]), money(ftot["purchases"])],
    ["Total Profit", money(kpi["profit"]), money(ftot["profit"])],
    ["Profit Margin", pct(kpi["margin"]), pct(ftot["margin"])],
    ["Vendors", f"{kpi['vendors']}", f"{ftot['vendors']}"],
    ["Active filters", "-", filter_text],
]

ctx = SimpleNamespace(
    vs=vs, fv=fv, scope=scope, vm=vm, vp=vp, vsc=vsc, pf=product_frame(vp_f), vendors=vendors_df, products=products_df,
    kpi=kpi, ftot=ftot, summary_rows=summary_rows, filter_text=filter_text, version=VERSION,
)

# ---- Header + top bar -------------------------------------------------------------------------
refreshed = st.session_state.last_refresh
st.markdown(
    "<div class='vpi-header'><div class='vpi-logo'>VPI</div><div>"
    "<div class='vpi-title'>Vendor Performance Intelligence</div>"
    "<div class='vpi-sub'>Executive Dashboard | Sales, procurement and profitability analytics</div></div>"
    f"<div class='vpi-meta'>Last refreshed<br><b>{refreshed:%d %b %Y, %H:%M:%S}</b></div></div>",
    unsafe_allow_html=True,
)
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
def kpi_card(label: str, value: str, delta_html: str, color: str) -> str:
    return (
        f"<div class='kpi' style='border-left-color:{color}'><div class='kpi-label'>{label}</div>"
        f"<div class='kpi-value'>{value}</div><div class='kpi-delta'>{delta_html}</div></div>"
    )


k1, k2, k3, k4 = st.columns(4)
k1.markdown(kpi_card("Total Sales", money(kpi["sales"]), f"Filtered: <b>{money(ftot['sales'])}</b>", BLUE), unsafe_allow_html=True)
k2.markdown(kpi_card("Total Purchases", money(kpi["purchases"]), f"Filtered: <b>{money(ftot['purchases'])}</b>", AMBER), unsafe_allow_html=True)
k3.markdown(kpi_card("Total Profit", money(kpi["profit"]), f"Filtered: <b>{money(ftot['profit'])}</b>", GREEN), unsafe_allow_html=True)
k4.markdown(kpi_card("Profit Margin", pct(kpi["margin"]), f"Filtered: <b>{pct(ftot['margin'])}</b> | {kpi['vendors']} vendors total", TEAL), unsafe_allow_html=True)

st.markdown(
    f"<div class='fstrip'><span class='pill'>Showing {ftot['vendors']} of {kpi['vendors']} vendors</span>"
    f"<span>Filtered Sales: <b>{money(ftot['sales'])}</b></span>"
    f"<span>Filtered Profit: <b>{money(ftot['profit'])}</b></span>"
    f"<span>Filtered Margin: <b>{pct(ftot['margin'])}</b></span>"
    f"<span>Active filters: <b>{esc(filter_text)}</b></span></div>",
    unsafe_allow_html=True,
)

# ---- Tabs -----------------------------------------------------------------------------------------------
tabs = st.tabs(["Overview", "Vendor Scorecard", "Vendor Deep-Dive", "Product & Brand", "Store & Category", "AI Analyst", "Reports, What-If & Data Quality"])
with tabs[0]:
    tab_overview(ctx)
with tabs[1]:
    tab_scorecard(ctx)
with tabs[2]:
    tab_deepdive(ctx)
with tabs[3]:
    tab_products(ctx)
with tabs[4]:
    tab_store_category(ctx)
with tabs[5]:
    tab_ai(ctx)
with tabs[6]:
    tab_reports(ctx)

# ---- Footer -------------------------------------------------------------------------------------------------
st.markdown(
    f"<div class='vpi-footer'><span>Vendors: <b>{kpi['vendors']:,}</b></span>"
    f"<span>Sales rows: <b>{counts['sales']:,}</b></span>"
    f"<span>Purchase rows: <b>{counts['purchases']:,}</b></span>"
    f"<span>Built with <b>Streamlit</b></span></div>",
    unsafe_allow_html=True,
)
