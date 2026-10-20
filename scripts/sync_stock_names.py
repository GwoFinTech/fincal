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

# How many unresolved English names the audit details may carry: enough to act
# on, small enough that a bad upstream cannot bloat the row (Issue #55).
ENGLISH_MISSING_SAMPLE = 20


def missing_name_targets() -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            """SELECT DISTINCT symbol, market FROM earnings
               WHERE (company_name IS NULL OR company_name = '')
               ORDER BY market, symbol"""
        )
        return cur.fetchall()


def name_scope() -> list[dict]:
    """Symbols whose English name this stage is responsible for (Issue #55).

    The whole ``earnings`` universe is thousands of symbols and most of them are
    never displayed, so the scope is what the product actually renders: the
    default universe, every user's watchlist, and every symbol already cached in
    ``stock_names``. Bounded, and still covering the 100% of the calendar.
    """
    pairs: set[tuple[str, str]] = set()
    try:
        from app.universe import popular_stocks

        us, hk = popular_stocks()
        pairs |= {(symbol, "US") for symbol in us}
        pairs |= {(symbol, "HK") for symbol in hk}
    except Exception as exc:  # noqa: BLE001 - the cache tables alone still work
        logger.warning("universe unavailable while selecting name targets: %s", exc)
    with db_cursor() as cur:
        cur.execute("SELECT symbol, market FROM stock_names")
        pairs |= {(r["symbol"], r["market"]) for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol, market FROM watchlist")
        pairs |= {(r["symbol"], r["market"]) for r in cur.fetchall()}
    return [{"symbol": s, "market": m} for s, m in sorted(pairs, key=lambda p: (p[1], p[0]))]


def english_name_targets() -> list[dict]:
    """Scope symbols whose English name is still missing (Issue #55)."""
    scope = name_scope()
    if not scope:
        return []
    with db_cursor() as cur:
        cur.execute("SELECT symbol, market, company_name_en FROM stock_names")
        cached = {(r["symbol"], r["market"]): (r["company_name_en"] or "")
                  for r in cur.fetchall()}
        cur.execute("""SELECT DISTINCT symbol, market FROM earnings
                       WHERE company_name_en IS NOT NULL AND company_name_en <> ''""")
        filled = {(r["symbol"], r["market"]) for r in cur.fetchall()}
    return [t for t in scope
            if not (cached.get((t["symbol"], t["market"])) or "")
            and (t["symbol"], t["market"]) not in filled]


def cache_name(symbol: str, market: str, name: str, source: str,
               name_en: str = "") -> None:
    """Store both names, each into its own column and only when it is new.

    Issue #55: the display name and the English name come from different
    providers, so an empty result on one side must never clear the other. Issue
    #4 still applies to the display name: existing values are not overwritten.
    """
    name = (name or "").strip()
    name_en = (name_en or "").strip()
    if not name and not name_en:
        return
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO stock_names (symbol, market, company_name, company_name_en, source, fetched_at)
               VALUES (%s, %s, %s, %s, %s, NOW())
               ON CONFLICT (symbol, market) DO UPDATE SET
                 company_name = CASE
                     WHEN COALESCE(stock_names.company_name, '') = '' AND EXCLUDED.company_name <> ''
                     THEN EXCLUDED.company_name ELSE stock_names.company_name END,
                 company_name_en = CASE
                     WHEN COALESCE(stock_names.company_name_en, '') = '' AND EXCLUDED.company_name_en <> ''
                     THEN EXCLUDED.company_name_en ELSE stock_names.company_name_en END,
                 source = CASE WHEN EXCLUDED.company_name <> '' THEN EXCLUDED.source ELSE stock_names.source END,
                 fetched_at = NOW()""",
            (symbol, market, name, name_en, source),
        )
        # Issue #4: only overwrite empty names, never clobber existing ones
        cur.execute(
            """UPDATE earnings SET company_name = %s
               WHERE symbol = %s AND market = %s
                 AND (company_name IS NULL OR company_name = '')""",
            (name, symbol, market),
        )
        # Issue #55: same fill-only rule for the English column, so a symbol that
        # only resolved an English name still gets it written.
        cur.execute(
            """UPDATE earnings SET company_name_en = %s
               WHERE symbol = %s AND market = %s
                 AND (company_name_en IS NULL OR company_name_en = '')""",
            (name_en, symbol, market),
        )


def main(run_id: int | None = None) -> tuple[int, list[tuple[str, str]], list[tuple[str, str]]]:
    """Resolve every company name that is still missing.

    Returns ``(filled, unresolved, english_missing)``: ``unresolved`` holds
    ``(symbol, error_code)`` pairs for symbols with no display name at all, and
    ``english_missing`` the scope symbols that got a display name but no Latin
    one (Issue #55) — a documented fallback, not a failed stage.  Callers own the
    verdict: a round that had targets but resolved none of them must not be
    recorded as a success (Issue #67), which is why the failure list leaves this
    function instead of only being logged.

    ``run_id`` (when given) receives a throttled progress heartbeat — each target
    is a network round trip, so the stage can run for minutes against its 900s
    budget and its audit row must age from real progress (Issue #78).
    """
    init_db()
    targets = {(t["symbol"], t["market"]): t for t in missing_name_targets()}
    for t in english_name_targets():
        targets.setdefault((t["symbol"], t["market"]), t)
    targets = [targets[key] for key in sorted(targets, key=lambda k: (k[1], k[0]))]
    logger.info("targets to resolve: %d", len(targets))

    beats = HeartbeatThrottle()
    filled = 0
    failed: list[tuple[str, str]] = []
    english_missing: list[tuple[str, str]] = []
    for i, t in enumerate(targets):
        symbol, market = t["symbol"], t["market"]
        if run_id is not None:
            beats.maybe(run_id, phase="names", current=i, total=len(targets))
        result = resolve_company_name_result(symbol, market)
        if result.ok:
            cache_name(symbol, market, result.name, result.source, result.name_en)
            filled += 1
            logger.info("resolved %s (%s) <- %s: %s%s", symbol, market, result.source,
                        result.name, f" / {result.name_en}" if result.name_en else "")
            if not result.name_en:
                english_missing.append((symbol, "english_name_unavailable"))
        else:
            failed.append((symbol, result.error_code or "unknown"))
            logger.debug("unresolved %s: %s", symbol, result.error_code)

    logger.info("stock name sync complete: %d filled, %d unresolved, %d without english name",
                filled, len(failed), len(english_missing))
    return filled, failed, english_missing


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
        filled, unresolved, english_missing = main(run_id)
        finish_run(
            run_id,
            status="failed" if unresolved else "success",
            record_count=filled,
            details={
                "filled": filled,
                "unresolved": len(unresolved),
                "unresolved_symbols": [symbol for symbol, _ in unresolved],
                "unresolved_errors": sorted({code for _, code in unresolved}),
                # Issue #55: the fallback coverage is visible here, so "how many
                # events still fall back to the Chinese name" is answerable
                # without another SQL exploration.
                "english_missing": len(english_missing),
                "english_missing_symbols": [symbol for symbol, _ in english_missing][:ENGLISH_MISSING_SAMPLE],
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
