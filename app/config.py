"""
config.py - one place for settings. Everything comes from the .env file.
No folder paths are hardcoded anywhere in this project.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")          # real environment variables always win over .env

APP_TITLE = "Vendor Performance Agent - 2024"
CLASS_LABEL = "Classification"               # no "Category" column exists; meaning of 1/2 is unconfirmed

# Business rules (change here, everything follows)
MIN_SALES_FOR_MARGIN = 10_000.0              # ignore tiny vendors when ranking by margin (USD)
LOW_MARGIN_DEFAULT = 5.0                     # "low margin" means below this percent
SLOW_TURNOVER = 0.70                         # units sold / units bought below this = slow moving
HIGH_FREIGHT_FACTOR = 1.5                    # freight % of purchases above 1.5x the average = high
MAX_ROWS = 50_000                            # safety cap on rows returned by one query

# The ONLY tables the app may read (checked by the SQL safety guard)
ALLOWED_TABLES = frozenset({"vendor_sales_summary", "monthly_vendor_sales", "vendor_freight", "meta"})


def resolve_path(value: str) -> Path:
    """Relative paths in .env start at the project folder."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def get_db_path() -> Path:
    """Path of the small summary database (read each time so tests can change it)."""
    return resolve_path(os.getenv("SUMMARY_DB_PATH", "data/vendor.db"))
