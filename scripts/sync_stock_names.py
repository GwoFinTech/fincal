#!/usr/bin/env python3
"""Resolve and cache company names for FinCal symbols.

Priority: Kurumi API → Longbridge CLI → Futu OpenD. Every resolved name is
persisted to the `stock_names` cache table and propagated to `earnings`.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company_name import resolve_company_name  # noqa: E402
from app.db import db_cursor, init_db  # noqa: E402
from app.sync_audit import finish_run, start_run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sync_stock_names")


def missing_name_targets() -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            """SELECT DISTINCT symbol, market FROM earnings
               WHERE (company_name IS NULL OR company_name = '')
               ORDER BY market, symbol"""
        )
        return cur.fetchall()


def cache_name(symbol: str, market: str, name: str, source: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO stock_names (symbol, market, company_name, source, fetched_at)
               VALUES (%s, %s, %s, %s, NOW())
               ON CONFLICT (symbol, market) DO UPDATE SET
                 company_name = EXCLUDED.company_name,
                 source = EXCLUDED.source,
                 fetched_at = NOW()""",
            (symbol, market, name, source),
        )
        cur.execute(
            """UPDATE earnings SET company_name = %s
               WHERE symbol = %s AND market = %s
                 AND (company_name IS NULL OR company_name = '')""",
            (name, symbol, market),
        )


def main() -> int:
    init_db()
    targets = missing_name_targets()
    logger.info("targets without company name: %d", len(targets))

    filled = 0
    failed = []
    for t in targets:
        symbol, market = t["symbol"], t["market"]
        name, source = resolve_company_name(symbol, market)
        if not name:
            failed.append(symbol)
            continue
        cache_name(symbol, market, name, source)
        filled += 1
        logger.info("resolved %s (%s) <- %s: %s", symbol, market, source, name)

    logger.info("stock name sync complete: %d filled, %d unresolved", filled, len(failed))
    return filled


if __name__ == "__main__":
    init_db()
    run_id = start_run("stock_names", "kurumi+longbridge+futu")
    try:
        count = main()
        finish_run(run_id, "success", record_count=count, details={"filled": count})
    except Exception as exc:  # noqa: BLE001
        logger.exception("stock name sync failed")
        finish_run(run_id, "failed", error_code="stock_names_sync_failed",
                   details={"error": str(exc)})
        raise
