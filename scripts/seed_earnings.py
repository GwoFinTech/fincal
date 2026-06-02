"""Seed earnings data from Longbridge or Jin10 calendar API."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from app.db import db_cursor


def seed_from_jin10():
    """Try to get earnings calendar data from Jin10 MCP."""
    # We'll use Longbridge as primary source
    pass


def seed_demo_data():
    """Insert demo earnings data for testing."""
    demo_earnings = [
        # US Tech Q2 2026
        ("AAPL", "US", "Apple Inc.", "2026-07-30", "Q", 2026, 3, None, None, None, None, "after"),
        ("MSFT", "US", "Microsoft Corp.", "2026-07-22", "Q", 2026, 4, None, None, None, None, "after"),
        ("GOOGL", "US", "Alphabet Inc.", "2026-07-28", "Q", 2026, 2, None, None, None, None, "after"),
        ("AMZN", "US", "Amazon.com Inc.", "2026-08-04", "Q", 2026, 2, None, None, None, None, "after"),
        ("NVDA", "US", "NVIDIA Corp.", "2026-08-26", "Q", 2026, 2, None, None, None, None, "after"),
        ("META", "US", "Meta Platforms", "2026-07-30", "Q", 2026, 2, None, None, None, None, "after"),
        ("TSLA", "US", "Tesla Inc.", "2026-07-23", "Q", 2026, 2, None, None, None, None, "after"),
        ("NFLX", "US", "Netflix Inc.", "2026-07-17", "Q", 2026, 2, None, None, None, None, "after"),
        ("AMD", "US", "AMD Inc.", "2026-07-29", "Q", 2026, 2, None, None, None, None, "after"),
        ("CRM", "US", "Salesforce Inc.", "2026-08-27", "Q", 2026, 2, None, None, None, None, "after"),
        ("ADBE", "US", "Adobe Inc.", "2026-09-11", "Q", 2026, 3, None, None, None, None, "after"),
        ("INTC", "US", "Intel Corp.", "2026-07-24", "Q", 2026, 2, None, None, None, None, "after"),
        ("PYPL", "US", "PayPal Holdings", "2026-08-05", "Q", 2026, 2, None, None, None, None, "after"),
        ("QCOM", "US", "Qualcomm Inc.", "2026-08-06", "Q", 2026, 3, None, None, None, None, "after"),
        ("ORCL", "US", "Oracle Corp.", "2026-09-09", "Q", 2026, 1, None, None, None, None, "after"),
        ("DIS", "US", "Walt Disney Co.", "2026-08-07", "Q", 2026, 3, None, None, None, None, "before"),
        ("BA", "US", "Boeing Co.", "2026-07-29", "Q", 2026, 2, None, None, None, None, "before"),
        ("NKE", "US", "Nike Inc.", "2026-09-25", "Q", 2026, 1, None, None, None, None, "after"),
        ("SBUX", "US", "Starbucks Corp.", "2026-07-30", "Q", 2026, 3, None, None, None, None, "after"),
        ("JPM", "US", "JPMorgan Chase", "2026-07-15", "Q", 2026, 2, None, None, None, None, "before"),
        ("V", "US", "Visa Inc.", "2026-07-23", "Q", 2026, 3, None, None, None, None, "after"),
        ("MA", "US", "Mastercard Inc.", "2026-07-30", "Q", 2026, 2, None, None, None, None, "before"),
        ("HD", "US", "Home Depot", "2026-08-12", "Q", 2026, 2, None, None, None, None, "before"),
        ("COST", "US", "Costco Wholesale", "2026-09-25", "Q", 2026, 4, None, None, None, None, "after"),
        ("ABBV", "US", "AbbVie Inc.", "2026-07-25", "Q", 2026, 2, None, None, None, None, "before"),

        # US Big Cap upcoming
        ("BRK.B", "US", "Berkshire Hathaway", "2026-08-09", "Q", 2026, 2, None, None, None, None, "before"),
        ("UNH", "US", "UnitedHealth Group", "2026-07-18", "Q", 2026, 2, None, None, None, None, "before"),
        ("XOM", "US", "Exxon Mobil", "2026-08-01", "Q", 2026, 2, None, None, None, None, "before"),
        ("JNJ", "US", "Johnson & Johnson", "2026-07-16", "Q", 2026, 2, None, None, None, None, "before"),
        ("PG", "US", "Procter & Gamble", "2026-07-31", "Q", 2026, 4, None, None, None, None, "before"),

        # HK Stocks - Interim 2026
        ("0700.HK", "HK", "Tencent", "2026-08-15", "Q", 2026, 2, None, None, None, None, None),
        ("9988.HK", "HK", "Alibaba", "2026-08-20", "Q", 2026, 1, None, None, None, None, None),
        ("0005.HK", "HK", "HSBC Holdings", "2026-08-05", "Q", 2026, 2, None, None, None, None, None),
        ("1299.HK", "HK", "AIA Group", "2026-08-22", "Q", 2026, 2, None, None, None, None, None),
        ("0941.HK", "HK", "China Mobile", "2026-08-14", "Q", 2026, 2, None, None, None, None, None),
        ("2318.HK", "HK", "Ping An Insurance", "2026-08-28", "Q", 2026, 2, None, None, None, None, None),
        ("0388.HK", "HK", "HK Exchanges", "2026-08-07", "Q", 2026, 2, None, None, None, None, None),
        ("9999.HK", "HK", "NetEase", "2026-08-18", "Q", 2026, 2, None, None, None, None, None),
        ("1810.HK", "HK", "Xiaomi Corp", "2026-08-25", "Q", 2026, 2, None, None, None, None, None),
        ("2020.HK", "HK", "ANTA Sports", "2026-08-12", "Q", 2026, 2, None, None, None, None, None),
        ("9618.HK", "HK", "JD.com", "2026-08-19", "Q", 2026, 2, None, None, None, None, None),
        ("0883.HK", "HK", "CNOOC", "2026-08-21", "Q", 2026, 2, None, None, None, None, None),
        ("0016.HK", "HK", "Sun Hung Kai", "2026-09-10", "Q", 2026, 2, None, None, None, None, None),
        ("1398.HK", "HK", "ICBC", "2026-08-30", "Q", 2026, 2, None, None, None, None, None),
        ("3988.HK", "HK", "Bank of China", "2026-08-29", "Q", 2026, 2, None, None, None, None, None),

        # Some June 2026 entries for immediate visibility
        ("AAPL", "US", "Apple Inc.", "2026-06-10", "Q", 2026, 2, 1.45, None, 95000, None, "after"),
        ("NVDA", "US", "NVIDIA Corp.", "2026-06-11", "Q", 2026, 1, 0.85, None, 43000, None, "after"),
        ("TSLA", "US", "Tesla Inc.", "2026-06-12", "Q", 2026, 2, 0.62, None, 25000, None, "after"),
        ("MSFT", "US", "Microsoft Corp.", "2026-06-18", "Q", 2026, 4, 2.95, None, 64000, None, "after"),
        ("GOOGL", "US", "Alphabet Inc.", "2026-06-20", "Q", 2026, 2, 1.89, None, 74000, None, "after"),
        ("0700.HK", "HK", "Tencent", "2026-06-16", "Q", 2026, 1, None, None, None, None, None),
    ]

    with db_cursor() as cur:
        for row in demo_earnings:
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type, fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, market, report_date, report_type, fiscal_year, fiscal_quarter)
                DO UPDATE SET company_name=EXCLUDED.company_name, eps_estimate=EXCLUDED.eps_estimate, before_after=EXCLUDED.before_after, updated_at=NOW()
                """,
                row,
            )
    print(f"Seeded {len(demo_earnings)} earnings records")


if __name__ == "__main__":
    from app.db import init_db
    init_db()
    seed_demo_data()
