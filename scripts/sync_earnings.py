#!/usr/bin/env python3
"""Full sync of earnings data from Longbridge finance-calendar into fincal DB.
Covers wide date ranges and uses pagination to get all records.
Uses batch inserts for performance."""
import subprocess
import json
import logging
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app.symbol import from_lb_counter_id, normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 200


def next_calendar_cursor(api_next_date: str, last_report_date: str, current_start: str) -> str | None:
    """Advance pagination even when Longbridge omits ``next_date``.

    The calendar endpoint can return a full page for only one or two days but
    leave ``next_date`` empty.  Stopping there silently drops every later
    earnings release and its consensus estimates.
    """
    candidates: list[str] = []
    try:
        candidate = api_next_date.strip()
        if candidate and date.fromisoformat(candidate) > date.fromisoformat(current_start):
            candidates.append(candidate)
    except ValueError:
        pass
    try:
        last_day = last_report_date.split(" ", 1)[0].replace(".", "-")
        candidates.append((date.fromisoformat(last_day) + timedelta(days=1)).isoformat())
    except (AttributeError, ValueError):
        pass
    return max(candidates) if candidates else None


def fetch_calendar(market: str, start: str, end: str) -> list[dict]:
    """Fetch all earnings calendar pages from Longbridge, paginating via next_date."""
    all_pages = []
    cursor_start = start
    # Empty calendar days must be advanced explicitly: this endpoint does not
    # seek to the next non-empty day when ``start`` itself has no releases.
    max_iterations = 800

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
                raise RuntimeError(f"longbridge_cli_failed:{market}:{i}:{result.stderr[:200]}")
            data = json.loads(result.stdout)
        except Exception as exc:
            logger.error(f"Fetch error iteration {i}: {exc}")
            raise RuntimeError(f"longbridge_fetch_failed:{market}:{i}") from exc

        pages = data.get("list", [])
        if not pages:
            next_cursor = (date.fromisoformat(cursor_start) + timedelta(days=1)).isoformat()
            if next_cursor > end:
                break
            logger.debug("%s no releases on %s; advancing to %s", market, cursor_start, next_cursor)
            cursor_start = next_cursor
            continue
        all_pages.extend(pages)

        next_date = data.get("next_date", "")
        last_page_date = pages[-1].get("date", "")
        next_cursor = next_calendar_cursor(next_date, last_page_date, cursor_start)
        if not next_cursor or next_cursor > end:
            break
        if next_cursor <= cursor_start:
            raise RuntimeError(f"longbridge_pagination_stalled:{market}:{cursor_start}")
        cursor_start = next_cursor

        logger.info(f"  {market} iteration {i}: got {len(pages)} pages, last_date={last_page_date}, next={cursor_start}")
    else:
        raise RuntimeError(f"longbridge_pagination_limit:{market}:{max_iterations}")

    return all_pages


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


def dedupe_batch(rows: list[tuple]) -> list[tuple]:
    """Collapse provider duplicates before a bulk UPSERT.

    Longbridge can repeat one calendar event across adjacent result windows.
    PostgreSQL rejects duplicate conflict keys within one ``execute_values``
    statement; retain the copy carrying the most financial metadata.
    """
    unique: dict[tuple[str, str, str, str], tuple] = {}
    for row in rows:
        key = (row[0], row[1], row[3], row[4])
        existing = unique.get(key)
        score = sum(value not in (None, "") for value in row[2:])
        existing_score = sum(value not in (None, "") for value in existing[2:]) if existing else -1
        if score > existing_score:
            unique[key] = row
    return list(unique.values())


def flush_batch(cur, rows: list[tuple]):
    """Batch upsert using execute_values."""
    rows = dedupe_batch(rows)
    if not rows:
        return
    from psycopg2.extras import execute_values
    execute_values(
        cur,
        """INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
           fiscal_year, fiscal_quarter,
           eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after)
        VALUES %s
        ON CONFLICT (symbol, market, report_date, report_type)
        DO UPDATE SET
            company_name = EXCLUDED.company_name,
            fiscal_year = EXCLUDED.fiscal_year,
            fiscal_quarter = EXCLUDED.fiscal_quarter,
            eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings.eps_estimate),
            eps_actual = COALESCE(EXCLUDED.eps_actual, earnings.eps_actual),
            revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, earnings.revenue_estimate),
            revenue_actual = COALESCE(EXCLUDED.revenue_actual, earnings.revenue_actual),
            before_after = COALESCE(EXCLUDED.before_after, earnings.before_after),
            is_predicted = FALSE,
            updated_at = NOW()
        """,
        rows,
        page_size=BATCH_SIZE,
    )
    keys = [(r[0], r[1], r[3]) for r in rows]
    execute_values(cur, """UPDATE earnings AS e SET
        date_source = 'longbridge', date_status = CASE WHEN e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL THEN 'reported' ELSE 'scheduled' END,
        estimate_source = CASE WHEN e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL THEN 'longbridge' ELSE e.estimate_source END,
        estimate_as_of = CASE WHEN e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL THEN NOW() ELSE e.estimate_as_of END,
        actual_source = CASE WHEN e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL THEN 'longbridge' ELSE e.actual_source END,
        actual_as_of = CASE WHEN e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL THEN NOW() ELSE e.actual_as_of END
        FROM (VALUES %s) AS v(symbol, market, report_date)
        WHERE (e.symbol,e.market,e.report_date)=(v.symbol,v.market,(v.report_date)::date)""", keys)
    execute_values(cur, """INSERT INTO earnings_estimate_snapshots (earning_id, source, eps_estimate, revenue_estimate, payload)
        SELECT e.id, 'longbridge', e.eps_estimate, e.revenue_estimate, '{"endpoint":"finance-calendar"}'::jsonb
        FROM earnings e JOIN (VALUES %s) AS v(symbol,market,report_date) ON (e.symbol,e.market,e.report_date)=(v.symbol,v.market,(v.report_date)::date)
        WHERE e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL""", keys)


def sync_earnings():
    """Full sync with wide date range, batched inserts."""
    today = date.today()
    start = (today - timedelta(days=180)).isoformat()
    end = (today + timedelta(days=365)).isoformat()

    total = 0

    for market in ["US", "HK"]:
        logger.info(f"=== Fetching {market} earnings [{start} → {end}] ===")
        pages = fetch_calendar(market, start, end)
        logger.info(f"  Total pages received: {len(pages)}")

        batch = []
        for page in pages:
            for info in page.get("infos", []):
                symbol, mkt = from_lb_counter_id(info.get("counter_id", ""))
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

                # Try to get fiscal_year from API response
                fiscal_year = None
                fy_from_api = ext.get("fiscal_year") or ext.get("year")
                if fy_from_api:
                    try:
                        fiscal_year = int(fy_from_api)
                    except (ValueError, TypeError):
                        pass

                # Fallback: heuristic from report date
                if not fiscal_year and fiscal_quarter:
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

                batch.append((
                    symbol, mkt, company_name, report_date, "Q",
                    fiscal_year, fiscal_quarter,
                    kv.get("eps_estimate"), kv.get("eps_actual"),
                    kv.get("revenue_estimate"), kv.get("revenue_actual"),
                    date_type,
                ))
                total += 1

                # Flush when batch is full
                if len(batch) >= BATCH_SIZE:
                    with db_cursor() as cur:
                        flush_batch(cur, batch)
                    logger.info(f"  Flushed {len(batch)} records (total: {total})")
                    batch = []

        # Flush remaining
        if batch:
            with db_cursor() as cur:
                flush_batch(cur, batch)
            logger.info(f"  Flushed final {len(batch)} records")

    logger.info(f"=== Sync complete: {total} records processed ===")
    return total


if __name__ == "__main__":
    from app.db import init_db
    from app.sync_audit import start_run, finish_run
    init_db()
    run_id = start_run("longbridge", "longbridge")
    try:
        total = sync_earnings()
    except Exception:
        finish_run(run_id, status="failed", error_code="longbridge_sync_failed")
        raise
    finish_run(run_id, status="success", record_count=total)
