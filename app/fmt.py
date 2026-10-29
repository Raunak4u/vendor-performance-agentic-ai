"""fmt.py - small helpers to show numbers nicely. All money is USD."""
import math
import re


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def money(x, decimals: int = 2) -> str:
    """12,345,678 -> $12.35M   |   4,560 -> $4.6K   |   -1,200,000 -> -$1.20M"""
    v = _num(x)
    if v is None:
        return "n/a"
    sign, a = ("-" if v < 0 else ""), abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.{decimals}f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.{decimals}f}M"
    if a >= 1e4:
        return f"{sign}${a / 1e3:.1f}K"
    return f"{sign}${a:,.0f}"


def pct(x, decimals: int = 1) -> str:
    v = _num(x)
    return "n/a" if v is None else f"{v:.{decimals}f}%"


def number(x, decimals: int = 2) -> str:
    v = _num(x)
    return "n/a" if v is None else f"{v:,.{decimals}f}"


def pretty(col: str) -> str:
    """'TotalSalesDollars' -> 'Total Sales Dollars'"""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", str(col)).replace("  ", " ")
