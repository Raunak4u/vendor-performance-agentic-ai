from langchain_core.tools import tool
from app.db import get_top_vendors, get_vendor_detail, get_monthly_sales

@tool
def top_vendors(limit: int = 10) -> str:
    """Get top vendors by total sales. Use when user asks who is top vendor, best vendor, highest sales."""
    df = get_top_vendors(limit)
    return df.to_string(index=False)

@tool
def vendor_detail(vendor_name: str) -> str:
    """Get detail of a specific vendor by name. Use when user asks about one vendor like DIAGEO, BACARDI."""
    df = get_vendor_detail(vendor_name)
    if df.empty:
        return f"No data found for vendor: {vendor_name}"
    return df.to_string(index=False)

@tool
def monthly_sales(vendor_name: str = "") -> str:
    """Get monthly sales trend. Use when user asks monthly sales, trend, month-wise."""
    df = get_monthly_sales(vendor_name if vendor_name else None)
    if df.empty:
        return "No monthly data found"
    return df.to_string(index=False)
