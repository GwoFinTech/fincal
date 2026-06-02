#!/usr/bin/env python3
"""Full sync of earnings data from Longbridge finance-calendar into fincal DB.
Covers wide date ranges and uses pagination to get all records."""
import subprocess
import json
import logging
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_calendar(market: str, start: str, end: str) -> list[dict]:
    """Fetch all earnings calendar pages from Longbridge, paginating via next_date."""
    all_pages = []
    cursor_start = start
    max_iterations = 50
    
    for i in range(max_iterations):
        cmd = [
            "longbridge", "finance-calendar", "report",
            "--market", market,
            "--start", cursor_start,
            "--end", end,
            "--count", "300",
            "--format", "json",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                logger.warning(f"Longbridge CLI error (iteration {i}): {result.stderr[:200]}")
                break
            data = json.loads(result.stdout)
        except Exception as e:
            logger.error(f"Fetch error iteration {i}: {e}")
            break

        pages = data.get("list", [])
        if not pages:
            break
        all_pages.extend(pages)
        
        next_date = data.get("next_date", "")
        if not next_date or next_date >= end:
            break
        cursor_start = next_date
        
        # Log progress
        last_page_date = pages[-1].get("date", "?")
        logger.info(f"  {market} iteration {i}: got {len(pages)} pages, last_date={last_page_date}, next={next_date}")

    return all_pages


def parse_symbol(counter_id: str) -> tuple[str, str]:
    parts = counter_id.split("/")
    if len(parts) != 3:
        return ("", "")
    market = parts[1]
    code = parts[2]
    if market == "HK":
        # Normalize HK codes to 4-digit with leading zero (e.g. 700 → 0700.HK)
        code = code.zfill(4) + ".HK"
    return (code, market)


def parse_date_type(date_type: str) -> str | None:
    for k, v in {"盘前": "before", "盘后": "after", "盘中": "during",
                 "Before Open": "before", "After Close": "after"}.items():
        if k in (date_type or ""):
            return v
    return None


def extract_kv(data_kv: list[dict]) -> dict:
    result = {}
    for kv in data_kv:
        t = kv.get("type", "")
        raw = kv.get("value_raw")
        val = None
        if raw is not None and raw != "" and raw != "0.000000":
            try:
                val = float(raw)
            except (ValueError, TypeError):
                pass
        if t == "estimate_eps":
            result["eps_estimate"] = val
        elif t == "actual_eps":
            result["eps_actual"] = val
        elif t == "estimate_revenue":
            result["revenue_estimate"] = val
        elif t == "actual_revenue":
            result["revenue_actual"] = val
    return result


def parse_report_date(date_str: str) -> str | None:
    try:
        return date_str.split(" ")[0].replace(".", "-")
    except Exception:
        return None


def sync_earnings():
    """Full sync with wide date range."""
    today = date.today()
    # Cover from 6 months ago to 12 months forward for full coverage
    start = (today - timedelta(days=180)).isoformat()
    end = (today + timedelta(days=365)).isoformat()

    total = 0

    for market in ["US", "HK"]:
        logger.info(f"=== Fetching {market} earnings [{start} → {end}] ===")
        pages = fetch_calendar(market, start, end)
        logger.info(f"  Total pages received: {len(pages)}")

        for page in pages:
            for info in page.get("infos", []):
                symbol, mkt = parse_symbol(info.get("counter_id", ""))
                if not symbol:
                    continue

                report_date = parse_report_date(info.get("date", ""))
                if not report_date:
                    continue

                company_name = info.get("counter_name", "")
                date_type = parse_date_type(info.get("date_type", ""))
                kv = extract_kv(info.get("data_kv", []))

                ext = info.get("ext", {}).get("financial_report", {})
                fiscal_quarter = None
                try:
                    fq = int(ext.get("period", "0") or "0")
                    if 1 <= fq <= 4:
                        fiscal_quarter = fq
                except (ValueError, TypeError):
                    pass

                fiscal_year = None
                if fiscal_quarter:
                    rd_month = int(report_date[5:7])
                    if mkt == "US":
                        if fiscal_quarter <= 2:
                            fiscal_year = int(report_date[:4])
                        else:
                            fiscal_year = int(report_date[:4]) - 1 if rd_month <= 6 else int(report_date[:4])
                    else:
                        if fiscal_quarter in (1, 2):
                            fiscal_year = int(report_date[:4])
                        else:
                            fiscal_year = int(report_date[:4]) - 1 if rd_month <= 3 else int(report_date[:4])

                with db_cursor() as cur:
                    cur.execute(
                        """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
                           fiscal_year, fiscal_quarter,
                           eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, market, report_date, report_type, fiscal_year, fiscal_quarter)
                        DO UPDATE SET
                            company_name = EXCLUDED.company_name,
                            eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings.eps_estimate),
                            eps_actual = COALESCE(EXCLUDED.eps_actual, earnings.eps_actual),
                            revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, earnings.revenue_estimate),
                            revenue_actual = COALESCE(EXCLUDED.revenue_actual, earnings.revenue_actual),
                            before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
                            updated_at = NOW()
                        """,
                        (
                            symbol, mkt, company_name, report_date, "Q",
                            fiscal_year, fiscal_quarter,
                            kv.get("eps_estimate"), kv.get("eps_actual"),
                            kv.get("revenue_estimate"), kv.get("revenue_actual"),
                            date_type,
                        ),
                    )
                total += 1

    logger.info(f"=== Sync complete: {total} records processed ===")


if __name__ == "__main__":
    from app.db import init_db
    init_db()
    sync_earnings()
