#!/usr/bin/env python3
"""Fetch earnings calendar dates + actual EPS/revenue from Futu OpenD.
Uses batched DB writes. One shared OpenQuoteContext for all symbols.
"""
import signal
import logging
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app.symbol import from_futu_code, to_futu_code
from app.watchlist import get_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

F10_TO_QUARTER = {1: 1, 2: 2, 3: 3, 4: 4}
PUB_TYPE_MAP = {1: "before", 2: "after", 3: "during"}


def sync_earnings_dates(ctx) -> int:
    """Fetch earnings calendar dates from Futu, single shared context."""
    batch = []
    total = 0
    cutoff = date.today() - timedelta(days=365)
    symbols = get_source().get_futu_symbols()

    for symbol in symbols:
        futu_code = to_futu_code(symbol)
        market = symbol.rsplit(".", 1)[-1]
        try:
            signal.alarm(15)
            ret, data = ctx.get_financials_earnings_price_history(futu_code)
            signal.alarm(0)
            if ret != 0:  # RET_OK = 0
                continue

            df = data.drop_duplicates(subset=["fiscal_year", "financial_type"], keep="first")
            for _, row in df.iterrows():
                fy = int(row["fiscal_year"])
                ft = int(row["financial_type"])
                fq = F10_TO_QUARTER.get(ft)
                if fq is None:
                    continue
                pub_date_str = row.get("pub_trading_day_str", "")
                if not pub_date_str:
                    continue
                report_date = date.fromisoformat(pub_date_str)
                if report_date < cutoff:
                    continue
                pub_type = PUB_TYPE_MAP.get(int(row.get("pub_type", 0)))
                batch.append((symbol, market, "", pub_date_str, "Q", fy, fq, pub_type))
                total += 1
        except Exception as e:
            signal.alarm(0)
            logger.debug(f"Dates failed {futu_code}: {e}")
            continue

    # Batch upsert all earnings dates
    if batch:
        from psycopg2.extras import execute_values
        with db_cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                   fiscal_year, fiscal_quarter, before_after)
                VALUES %s
                ON CONFLICT (symbol, market, report_date, report_type)
                DO UPDATE SET
                    fiscal_year = EXCLUDED.fiscal_year,
                    fiscal_quarter = EXCLUDED.fiscal_quarter,
                    before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                    is_predicted = FALSE,
                    company_name = CASE WHEN earnings.company_name = '' THEN EXCLUDED.company_name ELSE earnings.company_name END,
                    updated_at = NOW()
                """,
                batch,
                page_size=200,
            )
        logger.info(f"Flushed {len(batch)} earnings dates")

    logger.info(f"Futu earnings dates: {total} records")
    return total


def sync_actuals(ctx) -> int:
    """Fetch actual EPS (fid=14020) and revenue (fid=8002) via shared context."""
    total = 0
    symbols = get_source().get_futu_symbols()

    for symbol in symbols:
        futu_code = to_futu_code(symbol)
        market = symbol.rsplit(".", 1)[-1]
        try:
            # MainIndex for EPS (fid=14020)
            signal.alarm(20)
            ret, main_data = ctx.get_financials_statements(
                futu_code, statement_type=4, financial_type=9, num=4
            )
            signal.alarm(0)
            if ret == 0 and main_data.get("report_list"):
                for report in main_data["report_list"]:
                    fy = report.get("fiscal_year")
                    ft = report.get("financial_type")
                    fq = F10_TO_QUARTER.get(ft)
                    if not fy or not fq:
                        continue
                    eps_val = None
                    for item in report.get("item_list", []):
                        if item["field_id"] == 14020 and item.get("data") is not None:
                            try:
                                eps_val = float(item["data"])
                            except (ValueError, TypeError):
                                pass
                            break
                    if eps_val is not None:
                        with db_cursor() as cur:
                            cur.execute(
                                """UPDATE earnings SET eps_actual = %s, updated_at = NOW()
                                WHERE symbol = %s AND market = %s AND fiscal_year = %s
                                AND fiscal_quarter = %s AND (eps_actual IS NULL OR ABS(eps_actual) > 1000)
                                """,
                                (eps_val, symbol, market, fy, fq),
                            )

            # Income Statement for revenue (fid=8002)
            signal.alarm(20)
            ret, income_data = ctx.get_financials_statements(
                futu_code, statement_type=1, financial_type=9, num=4
            )
            signal.alarm(0)
            if ret == 0 and income_data.get("report_list"):
                for report in income_data["report_list"]:
                    fy = report.get("fiscal_year")
                    ft = report.get("financial_type")
                    fq = F10_TO_QUARTER.get(ft)
                    if not fy or not fq:
                        continue
                    rev_val = None
                    for item in report.get("item_list", []):
                        if item["field_id"] == 8002 and item.get("data") is not None:
                            try:
                                rev_val = float(item["data"])
                            except (ValueError, TypeError):
                                pass
                            break
                    if rev_val is not None:
                        with db_cursor() as cur:
                            cur.execute(
                                """UPDATE earnings SET revenue_actual = %s, updated_at = NOW()
                                WHERE symbol = %s AND market = %s AND fiscal_year = %s
                                AND fiscal_quarter = %s AND revenue_actual IS NULL
                                """,
                                (rev_val, symbol, market, fy, fq),
                            )
            total += 1
        except Exception as e:
            signal.alarm(0)
            logger.debug(f"Actuals failed {futu_code}: {e}")
            continue

    logger.info(f"Futu actuals synced: {total} symbols")
    return total


if __name__ == "__main__":
    from app.db import init_db
    init_db()

    try:
        from futu import OpenQuoteContext
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        logger.info("Connected to Futu OpenD (shared context)")
    except Exception as e:
        logger.warning(f"Failed to connect to Futu: {e}")
        sys.exit(0)  # Non-fatal — skip Futu sync

    try:
        sync_earnings_dates(ctx)
        sync_actuals(ctx)
    finally:
        ctx.close()
        logger.info("Futu context closed")
