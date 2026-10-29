import sqlite3
import pandas as pd
import os

def get_db_path():
    for p in ["data/vendor.db", "vendor.db", "data/vendor_sales.db"]:
        if os.path.exists(p):
            return p
    return "data/vendor.db"

DB_PATH = get_db_path()

def run_query(query, params=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(query, conn, params=params)
        return df
    finally:
        conn.close()

def get_top_vendors(limit=10):
    query = """
    SELECT VendorName,
           SUM(TotalSalesDollars) as TotalSales,
           SUM(GrossProfit) as TotalProfit,
           AVG(ProfitMargin) as AvgMargin,
           SUM(TotalSalesQuantity) as TotalQuantity
    FROM vendor_sales_summary
    GROUP BY VendorName
    ORDER BY TotalSales DESC
    LIMIT ?
    """
    return run_query(query, (limit,))

def get_vendor_detail(vendor_name):
    query = """
    SELECT VendorName,
           SUM(TotalSalesDollars) as TotalSales,
           SUM(GrossProfit) as TotalProfit,
           AVG(ProfitMargin) as AvgMargin,
           SUM(FreightCost) as TotalFreight,
           SUM(TotalPurchaseDollars) as TotalPurchase
    FROM vendor_sales_summary
    WHERE VendorName LIKE ?
    GROUP BY VendorName
    """
    return run_query(query, (f"%{vendor_name}%",))

def get_monthly_sales(vendor_name=None):
    if vendor_name:
        query = """
        SELECT SalesMonth, VendorName, TotalSalesDollars as TotalSales, GrossProfit as TotalProfit
        FROM monthly_vendor_sales
        WHERE VendorName LIKE ?
        ORDER BY SalesMonth
        """
        return run_query(query, (f"%{vendor_name}%",))
    else:
        query = """
        SELECT SalesMonth, SUM(TotalSalesDollars) as TotalSales, SUM(GrossProfit) as TotalProfit
        FROM monthly_vendor_sales
        GROUP BY SalesMonth
        ORDER BY SalesMonth
        """
        return run_query(query)