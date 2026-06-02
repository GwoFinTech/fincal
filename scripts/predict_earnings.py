#!/usr/bin/env python3
"""Predict future earnings dates from historical patterns.

Logic:
- For each symbol+market, examine historical earnings grouped by quarter
- Find the latest reported (fiscal_year, fiscal_quarter) pair
- Compute the next expected quarter: (fy, fq) → (fy, fq+1) or (fy+1, 1)
- Use the median month/day from historical same-quarter data for the prediction
- Mark with is_predicted=TRUE; confirmed data from sync overwrites later
"""
import logging
import sys
import os
import calendar
from datetime import date, timedelta
from collections import defaultdict
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

POPULAR_SYMBOLS = {
    "US": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "NFLX",
        "AMD", "INTC", "JPM", "V", "MA", "HD", "COST", "ABBV", "CRM", "PYPL",
        "QCOM", "AVGO", "WMT", "TSM", "ORCL", "ADBE", "SBUX", "NKE", "DIS",
        "BA", "MRVL",
    ],
    "HK": [
        "0700.HK", "9988.HK", "0005.HK", "1299.HK", "0941.HK",
        "2318.HK", "0388.HK", "9999.HK", "1810.HK", "2020.HK",
        "9618.HK", "0883.HK", "0016.HK", "0001.HK", "0002.HK",
        "0003.HK", "0011.HK", "1398.HK", "3988.HK", "2628.HK",
    ],
}

# How many future quarters ahead to predict (max 4 = ~1 year)
MAX_PREDICT_AHEAD = 4
# Don't predict more than this many days into the future
MAX_FUTURE_DAYS = 420


def next_quarter(fy: int, fq: int) -> tuple[int, int]:
    """Given (fiscal_year, fiscal_quarter), return the next quarter."""
    if fq < 4:
        return (fy, fq + 1)
    else:
        return (fy + 1, 1)


def predict_for_symbol(symbol: str, market: str) -> int:
    """Predict next earnings date(s) for a single symbol."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT report_date, fiscal_year, fiscal_quarter, before_after, is_predicted
            FROM earnings
            WHERE symbol = %s AND market = %s AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL
            ORDER BY report_date""",
            (symbol, market),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    # Separate confirmed vs predicted
    confirmed: dict[tuple[int, int], dict] = {}
    predicted: dict[tuple[int, int], dict] = {}

    for row in rows:
        key = (row["fiscal_year"], row["fiscal_quarter"])
        target = predicted if row["is_predicted"] else confirmed
        # Keep the latest entry per quarter
        target[key] = {
            "report_date": row["report_date"],
            "before_after": row["before_after"],
        }

    # Build historical quarter patterns from confirmed data only
    # year_offset = report_year - fiscal_year (captures cross-year reporting like Q4 in Jan)
    quarter_patterns: dict[int, list] = defaultdict(list)
    for (fy, fq), info in confirmed.items():
        rd = info["report_date"]
        quarter_patterns[fq].append({
            "year": rd.year,
            "month": rd.month,
            "day": rd.day,
            "year_offset": rd.year - fy,
            "before_after": info["before_after"],
        })

    # Find the latest confirmed quarter
    if not confirmed:
        return 0

    latest_fy, latest_fq = max(confirmed.keys(), key=lambda k: (k[0], k[1]))

    # Get company name from any existing row
    company_name = ""
    with db_cursor() as cur:
        cur.execute(
            "SELECT company_name FROM earnings WHERE symbol = %s AND market = %s AND company_name != '' LIMIT 1",
            (symbol, market),
        )
        row = cur.fetchone()
        if row:
            company_name = row["company_name"]

    # Predict forward from the latest confirmed quarter
    predictions_made = 0
    cur_fy, cur_fq = latest_fy, latest_fq
    today = date.today()
    max_date = today + timedelta(days=MAX_FUTURE_DAYS)

    for _ in range(MAX_PREDICT_AHEAD):
        next_fy, next_fq = next_quarter(cur_fy, cur_fq)

        # Skip if we already have confirmed data for this quarter
        if (next_fy, next_fq) in confirmed:
            cur_fy, cur_fq = next_fy, next_fq
            continue

        # Need at least 1 historical data point for this quarter pattern
        history = quarter_patterns.get(next_fq, [])
        if len(history) < 1:
            cur_fy, cur_fq = next_fy, next_fq
            continue

        # Compute predicted month, day and year_offset from recent history
        recent = sorted(history, key=lambda h: h["year"])[-4:]
        pred_month = int(statistics.median([h["month"] for h in recent]))
        pred_day = int(statistics.median([h["day"] for h in recent]))
        pred_year_offset = int(statistics.median([h["year_offset"] for h in recent]))

        # Apply year_offset: e.g. Q4 fiscal_year=2025 → report year = 2026
        pred_year = next_fy + pred_year_offset

        # Clamp day for the target month/year
        max_day = calendar.monthrange(pred_year, pred_month)[1]
        pred_day = min(pred_day, max_day)

        try:
            pred_date = date(pred_year, pred_month, pred_day)
        except ValueError:
            cur_fy, cur_fq = next_fy, next_fq
            continue

        # Skip if too far future
        if pred_date > max_date:
            break

        # Determine before_after
        ba_values = [h["before_after"] for h in recent if h["before_after"]]
        pred_ba = statistics.mode(ba_values) if ba_values else None

        # Upsert: if predicted row already exists for this quarter, update it
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, before_after, is_predicted)
                VALUES (%s, %s, %s, %s, 'Q', %s, %s, %s, TRUE)
                ON CONFLICT (symbol, market, report_date, report_type)
                DO UPDATE SET
                    is_predicted = TRUE,
                    before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                    company_name = CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END,
                    updated_at = NOW()
                """,
                (symbol, market, company_name, pred_date.isoformat(), next_fy, next_fq, pred_ba),
            )
        predictions_made += 1

        # Also update our tracking
        predicted[(next_fy, next_fq)] = {"report_date": pred_date, "before_after": pred_ba}

        cur_fy, cur_fq = next_fy, next_fq

    return predictions_made


def mark_confirmed():
    """Clean up predictions when real data arrives."""
    with db_cursor() as cur:
        # 1) Rows that have actuals are no longer predicted
        cur.execute(
            "UPDATE earnings SET is_predicted = FALSE WHERE is_predicted = TRUE AND eps_actual IS NOT NULL"
        )
        n1 = cur.rowcount

        # 2) Delete predicted rows that overlap with a confirmed row
        #    on the same (symbol, market, fiscal_year, fiscal_quarter)
        cur.execute(
            """DELETE FROM earnings WHERE is_predicted = TRUE AND id IN (
                SELECT p.id FROM earnings p
                JOIN earnings c ON p.symbol = c.symbol AND p.market = c.market
                    AND p.fiscal_year = c.fiscal_year AND p.fiscal_quarter = c.fiscal_quarter
                    AND c.is_predicted = FALSE
            )"""
        )
        n2 = cur.rowcount

    if n1 or n2:
        logger.info(f"Confirmed {n1} rows with actuals, removed {n2} stale predictions")


def cleanup_stale_predictions():
    """Remove predicted rows that are older than 60 days (past their report date)."""
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM earnings WHERE is_predicted = TRUE AND report_date < CURRENT_DATE - INTERVAL '60 days'"
        )
        n = cur.rowcount
    if n:
        logger.info(f"Cleaned up {n} stale predictions")


def merge_duplicate_symbols():
    """Merge XXX.US → XXX and 5-digit HK codes (8XXXX) into 4-digit canonical.

    Prevents stale duplicate rows if a sync run introduces non-canonical symbols.
    """
    with db_cursor() as cur:
        # 1) Merge XXX.US → XXX for US market
        cur.execute("SELECT DISTINCT symbol FROM earnings WHERE symbol LIKE '%.US' AND market = 'US'")
        us_dups = [r["symbol"] for r in cur.fetchall()]
        merged = 0
        for us_sym in us_dups:
            bare = us_sym.replace(".US", "")
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after, is_predicted)
                SELECT %s, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after, is_predicted
                FROM earnings WHERE symbol = %s AND market = 'US'
                ON CONFLICT (symbol, market, report_date, report_type) DO UPDATE SET
                    company_name = CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END,
                    eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings.eps_estimate),
                    eps_actual = COALESCE(EXCLUDED.eps_actual, earnings.eps_actual),
                    revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, earnings.revenue_estimate),
                    revenue_actual = COALESCE(EXCLUDED.revenue_actual, earnings.revenue_actual),
                    before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                    fiscal_year = EXCLUDED.fiscal_year,
                    fiscal_quarter = EXCLUDED.fiscal_quarter,
                    is_predicted = EXCLUDED.is_predicted,
                    updated_at = NOW()""",
                (bare, us_sym),
            )
            cur.execute("DELETE FROM earnings WHERE symbol = %s AND market = 'US'", (us_sym,))
            merged += 1
        if merged:
            logger.info(f"Merged {merged} .US duplicate symbols")

        # 2) Merge 5-digit HK codes (8XXXX.HK) → 4-digit (XXXX.HK)
        cur.execute("SELECT DISTINCT symbol FROM earnings WHERE symbol ~ '^\\d{5}\\.HK$'")
        hk5 = [r["symbol"] for r in cur.fetchall()]
        for sym in hk5:
            bare = (sym.split(".")[0].lstrip("8") or "0").zfill(4) + ".HK"
            cur.execute(
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after, is_predicted)
                SELECT %s, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after, is_predicted
                FROM earnings WHERE symbol = %s AND market = 'HK'
                ON CONFLICT (symbol, market, report_date, report_type) DO UPDATE SET
                    company_name = CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END,
                    eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings.eps_estimate),
                    eps_actual = COALESCE(EXCLUDED.eps_actual, earnings.eps_actual),
                    revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, earnings.revenue_estimate),
                    revenue_actual = COALESCE(EXCLUDED.revenue_actual, earnings.revenue_actual),
                    before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                    fiscal_year = EXCLUDED.fiscal_year,
                    fiscal_quarter = EXCLUDED.fiscal_quarter,
                    is_predicted = EXCLUDED.is_predicted,
                    updated_at = NOW()""",
                (bare, sym),
            )
            cur.execute("DELETE FROM earnings WHERE symbol = %s AND market = 'HK'", (sym,))
        if hk5:
            logger.info(f"Merged {len(hk5)} 5-digit HK symbols")


if __name__ == "__main__":
    from app.db import init_db
    init_db()
    merge_duplicate_symbols()
    mark_confirmed()
    cleanup_stale_predictions()

    # Run predictions
    logger.info("Predicting future earnings dates...")
    total = 0
    all_symbols = []
    for mkt, syms in POPULAR_SYMBOLS.items():
        for s in syms:
            all_symbols.append((s, mkt))
    for i, (symbol, market) in enumerate(all_symbols):
        count = predict_for_symbol(symbol, market)
        total += count
        if (i + 1) % 10 == 0:
            logger.info(f"  Processed {i+1}/{len(all_symbols)} symbols, {total} predictions so far")

    logger.info(f"Prediction complete: {total} future earnings dates predicted")
