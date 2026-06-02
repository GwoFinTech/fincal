#!/usr/bin/env python3
"""Fetch earnings calendar dates + actual EPS/revenue from Futu OpenD."""
import signal
import logging
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FUTU_SYMBOLS = {
    "US": [
        "US.AAPL", "US.MSFT", "US.GOOGL", "US.AMZN", "US.NVDA", "US.META",
        "US.TSLA", "US.NFLX", "US.AMD", "US.INTC", "US.JPM", "US.V",
        "US.MA", "US.HD", "US.COST", "US.ABBV", "US.CRM", "US.PYPL",
        "US.QCOM", "US.AVGO", "US.WMT", "US.TSM", "US.ORCL", "US.ADBE",
        "US.SBUX", "US.NKE", "US.DIS", "US.BA", "US.MRVL",
    ],
    "HK": [
        "HK.0700", "HK.9988", "HK.0005", "HK.1299", "HK.0941",
        "HK.2318", "HK.0388", "HK.9999", "HK.1810", "HK.2020",
        "HK.9618", "HK.0883", "HK.0016", "HK.0001", "HK.0002",
        "HK.0003", "HK.0011", "HK.1398", "HK.3988", "HK.2628",
    ],
}

F10_TO_QUARTER = {1: 1, 2: 2, 3: 3, 4: 4}
PUB_TYPE_MAP = {1: "before", 2: "after", 3: "during"}


def sync_earnings_dates():
    """Fetch earnings calendar dates from Futu."""
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        logger.warning("futu-api not installed")
        return

    total = 0
    for market, symbols in FUTU_SYMBOLS.items():
        logger.info(f"Futu: fetching {market} earnings dates ({len(symbols)} symbols)...")
        for futu_code in symbols:
            try:
                signal.alarm(15)
                ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
                ret, data = ctx.get_financials_earnings_price_history(futu_code)
                signal.alarm(0)
                if ret != RET_OK:
                    ctx.close()
                    continue

                parts = futu_code.split(".")
                mkt = parts[0]
                code = parts[1]
                db_symbol = f"{code}.HK" if mkt == "HK" else code

                df = data.drop_duplicates(subset=["fiscal_year", "financial_type"], keep="first")
                cutoff = date.today() - timedelta(days=365)

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

                    with db_cursor() as cur:
                        cur.execute(
                            """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                               fiscal_year, fiscal_quarter, before_after)
                            VALUES (%s, %s, '', %s, 'Q', %s, %s, %s)
                            ON CONFLICT (symbol, market, report_date, report_type, fiscal_year, fiscal_quarter)
                            DO UPDATE SET
                                before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                                updated_at = NOW()
                            """,
                            (db_symbol, market, pub_date_str, fy, fq, pub_type),
                        )
                    total += 1
                ctx.close()
            except Exception as e:
                signal.alarm(0)
                logger.debug(f"Failed {futu_code}: {e}")
                continue
    logger.info(f"Futu earnings dates: {total} records")


def sync_actuals():
    """Fetch actual EPS (fid=14020) and revenue (fid=8002) from Futu."""
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        return

    total = 0
    for market, symbols in FUTU_SYMBOLS.items():
        logger.info(f"Futu: fetching {market} actuals ({len(symbols)} symbols)...")
        for futu_code in symbols:
            try:
                signal.alarm(20)
                ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
                parts = futu_code.split(".")
                mkt = parts[0]
                code = parts[1]
                db_symbol = f"{code}.HK" if mkt == "HK" else code

                # MainIndex for EPS (fid=14020)
                ret, main_data = ctx.get_financials_statements(
                    futu_code, statement_type=4, financial_type=9, num=4
                )
                signal.alarm(0)
                if ret == RET_OK and main_data.get("report_list"):
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
                                    (eps_val, db_symbol, market, fy, fq),
                                )

                # Income Statement for revenue (fid=8002)
                signal.alarm(20)
                ret, income_data = ctx.get_financials_statements(
                    futu_code, statement_type=1, financial_type=9, num=4
                )
                signal.alarm(0)
                if ret == RET_OK and income_data.get("report_list"):
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
                                    (rev_val, db_symbol, market, fy, fq),
                                )
                total += 1
                ctx.close()
            except Exception as e:
                signal.alarm(0)
                logger.debug(f"Actuals failed {futu_code}: {e}")
                continue
    logger.info(f"Futu actuals synced: {total} symbols")


if __name__ == "__main__":
    from app.db import init_db
    init_db()
    sync_earnings_dates()
    sync_actuals()
