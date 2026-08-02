#!/usr/bin/env python3
"""Fetch earnings calendar dates + actual EPS/revenue from Futu OpenD.
Uses batched DB writes. One shared OpenQuoteContext for all symbols.
"""
import signal
import logging
import sys
import os
import socket
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app import config
from app.symbol import normalize, to_futu_code
from app.watchlist import get_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

F10_TO_QUARTER = {1: 1, 2: 2, 3: 3, 4: 4}
PUB_TYPE_MAP = {1: "before", 2: "after", 3: "during"}


def create_futu_context():
    """Return a connected OpenD context, or ``None`` when it is unavailable.

    ``OpenQuoteContext`` retries a refused connection indefinitely.  A cheap
    TCP preflight prevents a weekly sync from consuming the scheduler's full
    one-hour allowance when OpenD is down or pointed at the wrong port.
    """
    try:
        with socket.create_connection((config.FUTU_HOST, config.FUTU_PORT), timeout=3):
            pass
    except OSError as exc:
        logger.warning(
            "Futu OpenD unavailable at %s:%s; skipping optional Futu sync: %s",
            config.FUTU_HOST,
            config.FUTU_PORT,
            exc,
        )
        return None

    from futu import OpenQuoteContext
    try:
        ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
        logger.info("Connected to Futu OpenD at %s:%s (shared context)", config.FUTU_HOST, config.FUTU_PORT)
        return ctx
    except Exception as exc:
        logger.warning("Failed to create Futu OpenD context: %s", exc)
        return None


def canonical_earnings_symbol(symbol: str) -> tuple[str, str]:
    """Convert a watchlist symbol to the earnings table's canonical key.

    The shared watchlist keeps US tickers as ``AAPL.US`` while the earnings
    table's established US convention is the bare ticker (``AAPL``). Writing
    the watchlist spelling directly made every Futu run create transient
    ``*.US`` duplicates, which prediction cleanup then had to merge.
    """
    raw = symbol.strip().upper()
    market = raw.rsplit(".", 1)[-1] if "." in raw else "US"
    if market == "HK":
        return normalize(raw, "HK"), market
    if market == "US":
        return raw.removesuffix(".US"), market
    raise ValueError(f"unsupported_market:{market}")


def sync_earnings_dates(ctx) -> int:
    """Fetch earnings calendar dates from Futu, single shared context."""
    batch = []
    total = 0
    cutoff = date.today() - timedelta(days=365)
    symbols = get_source().get_futu_symbols()

    for source_symbol in symbols:
        symbol, market = canonical_earnings_symbol(source_symbol)
        futu_code = to_futu_code(source_symbol)
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

    for source_symbol in symbols:
        symbol, market = canonical_earnings_symbol(source_symbol)
        futu_code = to_futu_code(source_symbol)
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
    from app.sync_audit import start_run, finish_run
    init_db()

    ctx = create_futu_context()
    if ctx is None:
        run_id = start_run("futu", "futu")
        finish_run(run_id, status="skipped", error_code="opend_unavailable")
        sys.exit(0)  # Non-fatal — skip Futu sync

    symbols = get_source().get_futu_symbols()
    run_id = start_run("futu", "futu", symbol_count=len(symbols))
    try:
        date_count = sync_earnings_dates(ctx)
        actual_count = sync_actuals(ctx)
    except Exception:
        finish_run(run_id, status="failed", error_code="futu_sync_failed")
        raise
    else:
        finish_run(run_id, status="success", record_count=date_count, details={"actual_symbols": actual_count})
    finally:
        ctx.close()
        logger.info("Futu context closed")
