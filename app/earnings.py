"""Earnings data fetching from Futu OpenD + Longbridge fallback."""
import logging
from datetime import date, timedelta
from . import config

logger = logging.getLogger(__name__)


def _get_popular_stocks():
    """Read popular stocks from configured watchlist source, fallback to hardcoded."""
    try:
        from .watchlist import get_source
        syms = get_source().get_symbols_by_market()
        if syms["US"] or syms["HK"]:
            return syms["US"], syms["HK"]
    except Exception as e:
        logger.warning(f"Failed to load watchlist: {e}")
    # Fallback
    return (
        ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"],
        ["0700.HK", "9988.HK", "1810.HK"],
    )

POPULAR_STOCKS_US, POPULAR_STOCKS_HK = _get_popular_stocks()


def fetch_earnings_from_db(
    symbols: list[str] | None = None,
    markets: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict]:
    """Fetch earnings from our database. symbols are bare (no .US/.HK suffix).
    If markets is provided, filter by market. Otherwise return all."""
    from . import db

    if start is None:
        start = date.today() - timedelta(days=7)
    if end is None:
        end = date.today() + timedelta(days=90)

    with db.db_cursor() as cur:
        conditions = ["report_date BETWEEN %s AND %s"]
        params: list = [start, end]

        if symbols:
            placeholders = ",".join(["%s"] * len(symbols))
            conditions.append(f"symbol IN ({placeholders})")
            params.extend(symbols)

        if markets:
            placeholders = ",".join(["%s"] * len(markets))
            conditions.append(f"market IN ({placeholders})")
            params.extend(markets)

        where = " AND ".join(conditions)
        cur.execute(
            f"SELECT * FROM earnings WHERE {where} ORDER BY report_date, market, symbol",
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def seed_earnings_if_empty():
    """Seed DB with earnings data if empty."""
    from . import db

    with db.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) as cnt FROM earnings")
        count = cur.fetchone()["cnt"]
        if count > 0:
            return

    logger.info("Earnings table empty, seeding demo data...")
    _seed_demo_data()


def _seed_demo_data():
    """Insert demo earnings data for testing."""
    from . import db

    demo = [
        ("AAPL", "US", "Apple Inc.", "2026-07-30", "Q", 2026, 3, None, None, None, None, "after"),
        ("MSFT", "US", "Microsoft Corp.", "2026-07-22", "Q", 2026, 4, None, None, None, None, "after"),
        ("GOOGL", "US", "Alphabet Inc.", "2026-07-28", "Q", 2026, 2, None, None, None, None, "after"),
        ("AMZN", "US", "Amazon.com Inc.", "2026-08-04", "Q", 2026, 2, None, None, None, None, "after"),
        ("NVDA", "US", "NVIDIA Corp.", "2026-08-26", "Q", 2026, 2, None, None, None, None, "after"),
        ("META", "US", "Meta Platforms", "2026-07-30", "Q", 2026, 2, None, None, None, None, "after"),
        ("TSLA", "US", "Tesla Inc.", "2026-07-23", "Q", 2026, 2, None, None, None, None, "after"),
        ("NFLX", "US", "Netflix Inc.", "2026-07-17", "Q", 2026, 2, None, None, None, None, "after"),
        ("AMD", "US", "AMD Inc.", "2026-07-29", "Q", 2026, 2, None, None, None, None, "after"),
        ("INTC", "US", "Intel Corp.", "2026-07-24", "Q", 2026, 2, None, None, None, None, "after"),
        ("JPM", "US", "JPMorgan Chase", "2026-07-15", "Q", 2026, 2, None, None, None, None, "before"),
        ("V", "US", "Visa Inc.", "2026-07-23", "Q", 2026, 3, None, None, None, None, "after"),
        ("JNJ", "US", "Johnson & Johnson", "2026-07-16", "Q", 2026, 2, None, None, None, None, "before"),
        ("WMT", "US", "Walmart Inc.", "2026-08-14", "Q", 2026, 2, None, None, None, None, "before"),
        ("PG", "US", "Procter & Gamble", "2026-07-31", "Q", 2026, 4, None, None, None, None, "before"),
        # June dates for immediate visibility
        ("AAPL", "US", "Apple Inc.", "2026-06-10", "Q", 2026, 2, 1.45, None, 95000, None, "after"),
        ("NVDA", "US", "NVIDIA Corp.", "2026-06-11", "Q", 2026, 1, 0.85, None, 43000, None, "after"),
        ("MSFT", "US", "Microsoft Corp.", "2026-06-18", "Q", 2026, 4, 2.95, None, 64000, None, "after"),
        ("GOOGL", "US", "Alphabet Inc.", "2026-06-20", "Q", 2026, 2, 1.89, None, 74000, None, "after"),
        # HK Stocks
        ("0700.HK", "HK", "Tencent", "2026-08-15", "Q", 2026, 2, None, None, None, None, None),
        ("9988.HK", "HK", "Alibaba", "2026-08-20", "Q", 2026, 1, None, None, None, None, None),
        ("0005.HK", "HK", "HSBC Holdings", "2026-08-05", "Q", 2026, 2, None, None, None, None, None),
        ("1810.HK", "HK", "Xiaomi Corp", "2026-08-25", "Q", 2026, 2, None, None, None, None, None),
        ("0700.HK", "HK", "Tencent", "2026-06-16", "Q", 2026, 1, None, None, None, None, None),
        ("9988.HK", "HK", "Alibaba", "2026-06-12", "Q", 2026, 4, None, None, None, None, None),
    ]

    with db.db_cursor() as cur:
        for row in demo:
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, market, report_date, report_type)
                DO UPDATE SET company_name=EXCLUDED.company_name, eps_estimate=EXCLUDED.eps_estimate,
                   before_after=EXCLUDED.before_after, updated_at=NOW()
                """,
                row,
            )
    logger.info(f"Seeded {len(demo)} demo earnings records")
