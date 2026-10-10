#!/usr/bin/env python3
"""Resolve and cache company names for FinCal symbols.

Priority: Kurumi API → Longbridge CLI → Futu OpenD. Every resolved name is
persisted to the `stock_names` cache table and propagated to `earnings`.

Issue #4: does NOT overwrite existing names with empty results.
Issue #67: the audited run reports `failed` (with `error_code=
stock_names_unresolved`) when a round had targets but left any unresolved —
this table is written on demand, so the stage entry in `sync_runs`, not
`stock_names.fetched_at`, is what proves the stage is healthy.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.company_name import resolve_company_name_result  # noqa: E402
from app.config import stage_timeout  # noqa: E402
from app.db import db_cursor, init_db  # noqa: E402
from app.sync_audit import (  # noqa: E402
    HeartbeatThrottle, finish_run, heartbeat, start_run,
)

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
        # Issue #4: only overwrite empty names, never clobber existing ones
        cur.execute(
            """UPDATE earnings SET company_name = %s
               WHERE symbol = %s AND market = %s
                 AND (company_name IS NULL OR company_name = '')""",
            (name, symbol, market),
        )


def main(run_id: int | None = None) -> tuple[int, list[tuple[str, str]]]:
    """Resolve every company name that is still missing.

    Returns ``(filled, unresolved)`` where ``unresolved`` holds
    ``(symbol, error_code)`` pairs.  Callers own the verdict: a round that had
    targets but resolved none of them must not be recorded as a success
    (Issue #67), which is why the failure list leaves this function instead of
    only being logged.

    ``run_id`` (when given) receives a throttled progress heartbeat — each target
    is a network round trip, so the stage can run for minutes against its 900s
    budget and its audit row must age from real progress (Issue #78).
    """
    init_db()
    targets = missing_name_targets()
    logger.info("targets without company name: %d", len(targets))

    beats = HeartbeatThrottle()
    filled = 0
    failed: list[tuple[str, str]] = []
    for i, t in enumerate(targets):
        symbol, market = t["symbol"], t["market"]
        if run_id is not None:
            beats.maybe(run_id, phase="names", current=i, total=len(targets))
        result = resolve_company_name_result(symbol, market)
        if result.ok:
            cache_name(symbol, market, result.name, result.source)
            filled += 1
            logger.info("resolved %s (%s) <- %s: %s", symbol, market, result.source, result.name)
        else:
            failed.append((symbol, result.error_code or "unknown"))
            logger.debug("unresolved %s: %s", symbol, result.error_code)

    logger.info("stock name sync complete: %d filled, %d unresolved", filled, len(failed))
    return filled, failed


def run() -> int:
    """Audited entrypoint: sync the names, then record the stage verdict.

    Exit code is non-zero when the round had unresolved targets, so a run that
    resolved nothing is visible as a failed stage instead of a silent success
    (``stock_names`` writes on demand, so its table cannot be used as the
    freshness signal — see ``app/freshness.DERIVED_TABLES``).
    """
    init_db()
    run_id = start_run("stock_names", "kurumi+longbridge+futu",
                       idempotency_key="stock_names:full",
                       timeout_seconds=stage_timeout("stock_names"))
    if run_id is None:
        logger.info("stock name sync already running, skipping")
        return 0

    unresolved: list[tuple[str, str]] = []
    try:
        filled, unresolved = main(run_id)
        finish_run(
            run_id,
            status="failed" if unresolved else "success",
            record_count=filled,
            details={
                "filled": filled,
                "unresolved": len(unresolved),
                "unresolved_symbols": [symbol for symbol, _ in unresolved],
                "unresolved_errors": sorted({code for _, code in unresolved}),
            },
            error_code="stock_names_unresolved" if unresolved else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("stock name sync failed")
        finish_run(run_id, status="failed", error_code="stock_names_sync_failed",
                   details={"error": str(exc)})
        raise
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(run())
