"""Earnings data fetching from Futu OpenD + Longbridge fallback."""
import logging
from datetime import date, timedelta
from . import config

logger = logging.getLogger(__name__)

# The default universe (POPULAR_STOCKS_*) used to be read here, at import time,
# which froze an upstream add/remove — or a source outage during startup — for
# the whole process lifetime (Issue #58).  It now lives in ``app.universe`` as a
# live, TTL-cached accessor; ``POPULAR_STOCKS_US`` / ``POPULAR_STOCKS_HK`` are
# kept as deprecated shims (below) for any out-of-tree importer.


def __getattr__(name: str):
    """Deprecated compatibility shim for the removed module-level constants.

    Prefer :func:`app.universe.popular_stocks`, which reads the universe live
    instead of returning the value captured at import time (Issue #58).
    """
    if name in ("POPULAR_STOCKS_US", "POPULAR_STOCKS_HK"):
        from .universe import popular_stocks

        us, hk = popular_stocks()
        return us if name == "POPULAR_STOCKS_US" else hk
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        # Same horizon as the iCal feed and the watchlist view (Issue #59): a
        # shorter fallback here truncated the very predictions the rest of the
        # app shows for callers that omit the window.
        end = date.today() + timedelta(days=config.CALENDAR_FORWARD_DAYS)

    with db.db_cursor() as cur:
        conditions = ["e.report_date BETWEEN %s AND %s"]
        params: list = [start, end]

        if symbols:
            placeholders = ",".join(["%s"] * len(symbols))
            conditions.append(f"e.symbol IN ({placeholders})")
            params.extend(symbols)

        if markets:
            placeholders = ",".join(["%s"] * len(markets))
            conditions.append(f"e.market IN ({placeholders})")
            params.extend(markets)

        where = " AND ".join(conditions)
        cur.execute(
            f"""SELECT e.*, c.currency AS consensus_currency, c.eps_gaap AS consensus_eps_gaap,
                   c.eps_adjusted AS consensus_eps_adjusted, c.revenue AS consensus_revenue,
                   c.ebit AS consensus_ebit, c.net_income AS consensus_net_income,
                   c.normalized_net_income AS consensus_normalized_net_income, c.fetched_at AS consensus_fetched_at
            FROM earnings e LEFT JOIN earnings_consensus c ON c.symbol=e.symbol AND c.market=e.market
              AND c.fiscal_year=e.fiscal_year AND c.fiscal_quarter=e.fiscal_quarter AND c.source='longbridge'
            WHERE {where} ORDER BY e.report_date, e.market, e.symbol""",
            params,
        )
        rows = [dict(row) for row in cur.fetchall()]

    # One fiscal period is one event: collapsing here (the single read entry point
    # of the API, the CSV/JSON export and the iCal feed) keeps the three outlets
    # in agreement instead of letting each of them pick its own row (Issue #50).
    from . import fiscal
    rows = fiscal.collapse_fiscal_duplicates(rows)
    # Issue #61: the same single entry point decides whether each row's estimate
    # and actual may be subtracted, so the calendar, the exports and the iCal feed
    # cannot disagree about comparability either. The reason is a code, never
    # prose — the UI owns the wording.
    for row in rows:
        row["comparison_unavailable_reason"] = fiscal.comparison_unavailable_reason(row)
    return rows


def seed_earnings_if_empty():
    """Seed DB with earnings data if empty — best effort, never fatal (Issue #74).

    The demo rows exist so a brand-new installation shows something instead of an
    empty calendar.  They are *not* the app's data (that comes from the sync
    pipeline), so a rejected seed must not take the process down: on a fresh
    database ``init_db()`` has already created ``idx_earnings_fiscal_identity``
    before this runs, and any seeded row that violates the fiscal-period
    invariant would otherwise abort start-up — under
    ``restart: unless-stopped`` that is a restart loop with no self-healing
    path (every subsequent start hits the same index and the same rows).

    Failures are therefore logged at warning level (visible in the container
    logs) and start-up continues; the transaction is rolled back by
    ``db_cursor`` so the database is left untouched.
    """
    from . import db

    try:
        with db.db_cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM earnings")
            row = cur.fetchone() or {}
            if (row.get("cnt") or 0) > 0:
                return

        logger.info("Earnings table empty, seeding demo data...")
        _seed_demo_data()
    except Exception as exc:  # noqa: BLE001 - demo data must not block start-up
        logger.warning(
            "demo seed skipped: %s: %s — the earnings table stays empty until "
            "the sync pipeline populates it (Issue #74)",
            type(exc).__name__, exc,
        )


#: Demo rows: ``(symbol, market, company_name, report_date, report_type,
#: fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate,
#: revenue_actual, before_after)``.
#:
#: Every confirmed demo row must satisfy the fiscal-period invariant that
#: ``app.db.ensure_fiscal_identity_index()`` enforces — one confirmed row per
#: ``(symbol, market, fiscal_year, fiscal_quarter)`` — because on a fresh
#: database the index exists before this data is written (Issue #74).  The two
#: June rows below are dated for immediate visibility, so they carry a *different*
#: fiscal period than the same symbol's July/August row rather than duplicating
#: it.  ``tests/test_fresh_start_seed.py`` fails if that invariant is broken
#: again.
DEMO_EARNINGS_ROWS = [
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
    # FY2026 Q3, not Q4: the July row above already owns (MSFT, US, 2026, 4).
    ("MSFT", "US", "Microsoft Corp.", "2026-06-18", "Q", 2026, 3, 2.95, None, 64000, None, "after"),
    # FY2026 Q1, not Q2: the July row above already owns (GOOGL, US, 2026, 2).
    ("GOOGL", "US", "Alphabet Inc.", "2026-06-20", "Q", 2026, 1, 1.89, None, 74000, None, "after"),
    # HK Stocks
    ("0700.HK", "HK", "Tencent", "2026-08-15", "Q", 2026, 2, None, None, None, None, None),
    ("9988.HK", "HK", "Alibaba", "2026-08-20", "Q", 2026, 1, None, None, None, None, None),
    ("0005.HK", "HK", "HSBC Holdings", "2026-08-05", "Q", 2026, 2, None, None, None, None, None),
    ("1810.HK", "HK", "Xiaomi Corp", "2026-08-25", "Q", 2026, 2, None, None, None, None, None),
    ("0700.HK", "HK", "Tencent", "2026-06-16", "Q", 2026, 1, None, None, None, None, None),
    ("9988.HK", "HK", "Alibaba", "2026-06-12", "Q", 2026, 4, None, None, None, None, None),
]


def _seed_demo_data():
    """Insert demo earnings data for testing (see ``DEMO_EARNINGS_ROWS``)."""
    from . import db

    with db.db_cursor() as cur:
        for row in DEMO_EARNINGS_ROWS:
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
    logger.info(f"Seeded {len(DEMO_EARNINGS_ROWS)} demo earnings records")
