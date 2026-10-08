#!/usr/bin/env python3
"""Backfill ``earnings.report_type`` from Longbridge event sequences (Issue #50).

Why
---
Longbridge publishes two calendar events for one fiscal period of many issuers
and both declare ``period='4'``: the ``qf``/``3q`` **release** (quarterly
figures) and the ``af``/``saf`` **disclosure** (full-year / half-year totals).
Parity is invisible in the database — every legacy row says ``report_type='Q'``
— so the fiscal-period reconciliation cannot tell a period's release from its
annual report, and ``app.fiscal.authority_key`` would keep promoting the later
dated annual row (production: DEA ``qf`` 2026-02-21 revenue 87.7M against ``af``
2026-02-23 revenue 334M, both ``FY2025 Q4``).

What it does
------------
Fetches the Longbridge calendar for the window that covers the rows and matches
each confirmed row to its provider event by ``(symbol, market, report_date)``,
then stores the event's kind as the row's ``report_type``:

======================  ==========================================
``qf`` / ``3q``         ``Q``  quarterly release
``saf``                 ``H``  half-year report disclosure
``af``                  ``A``  annual report disclosure
======================  ==========================================

Rows with no matching provider event are left exactly as they are (a Futu-only
row, a row outside the calendar window, or a row whose event the provider no
longer lists).  Nothing is deleted and no fiscal label changes.

Usage
-----
    python scripts/backfill_report_types.py                 # dry-run (read-only)
    python scripts/backfill_report_types.py --json           # machine-readable plan
    python scripts/backfill_report_types.py --apply          # write (backs up first)
    python scripts/backfill_report_types.py --start 2026-01-01 --end 2026-04-30

``--apply`` writes an append-only backup row per change to
``earnings_report_type_backfill_backup`` before the ``UPDATE``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import fiscal  # noqa: E402
from app.db import db_cursor  # noqa: E402
from app.symbol import from_lb_counter_id  # noqa: E402
from sync_earnings import fetch_calendar, parse_report_date  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BACKUP_TABLE = "earnings_report_type_backfill_backup"
MARKETS = ("US", "HK")


def provider_report_types(start: str, end: str, markets=MARKETS) -> dict[tuple[str, str, str], str]:
    """``(symbol, market, ISO date)`` → ``report_type`` for every provider event.

    A date can carry both a release and a disclosure; the release wins, matching
    the rule the reconciliation applies to a fiscal period.
    """
    mapping: dict[tuple[str, str, str], str] = {}
    for market in markets:
        logger.info("fetching Longbridge %s calendar %s → %s", market, start, end)
        pages = fetch_calendar(market, start, end)
        logger.info("  %s pages: %d", market, len(pages))
        for page in pages:
            for info in page.get("infos", []) or []:
                symbol, mkt = from_lb_counter_id(info.get("counter_id", ""))
                if not symbol:
                    continue
                report_date = parse_report_date(info.get("date", ""))
                if not report_date:
                    continue
                ext = (info.get("ext") or {}).get("financial_report") or {}
                report_type = fiscal.report_type_for_period_type(ext.get("period_type"))
                key = (symbol, mkt, report_date)
                previous = mapping.get(key)
                if previous is not None and fiscal.is_disclosure(report_type) and not fiscal.is_disclosure(previous):
                    continue  # a release already claimed this date
                mapping[key] = report_type
    return mapping


def load_rows(cur, start: str | None, end: str | None) -> list[dict]:
    """Confirmed rows carrying a fiscal identity — the only ones with an owner."""
    clauses = ["is_predicted = FALSE", "fiscal_year IS NOT NULL", "fiscal_quarter IS NOT NULL"]
    params: list = []
    if start:
        clauses.append("report_date >= %s")
        params.append(start)
    if end:
        clauses.append("report_date <= %s")
        params.append(end)
    cur.execute(
        "SELECT id, symbol, market, report_date, report_type FROM earnings"
        " WHERE " + " AND ".join(clauses) + " ORDER BY symbol, market, report_date",
        tuple(params),
    )
    return [dict(row) for row in cur.fetchall()]


def plan_changes(rows: list[dict], mapping: dict[tuple[str, str, str], str]) -> tuple[list[dict], Counter]:
    """Rows whose stored ``report_type`` disagrees with the provider event."""
    changes: list[dict] = []
    summary: Counter = Counter()
    for row in rows:
        key = (row["symbol"], row["market"], row["report_date"].isoformat())
        target = mapping.get(key)
        if target is None:
            summary["unmatched"] += 1
            continue
        stored = (row["report_type"] or fiscal.DEFAULT_REPORT_TYPE).strip().upper()
        summary[f"matched:{stored}->{target}"] += 1
        if stored == target:
            continue
        changes.append({
            "earning_id": row["id"],
            "symbol": row["symbol"],
            "market": row["market"],
            "report_date": key[2],
            "before": stored,
            "after": target,
        })
    return changes, summary


def apply_changes(cur, changes: list[dict]) -> None:
    """Back up every change, then write it."""
    if not changes:
        return
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            earning_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            report_date DATE NOT NULL,
            before_report_type TEXT NOT NULL,
            after_report_type TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""
    )
    from psycopg2.extras import execute_values

    execute_values(
        cur,
        f"INSERT INTO {BACKUP_TABLE} (earning_id, symbol, market, report_date,"
        " before_report_type, after_report_type) VALUES %s",
        [(c["earning_id"], c["symbol"], c["market"], c["report_date"], c["before"], c["after"])
         for c in changes],
        page_size=500,
    )
    execute_values(
        cur,
        "UPDATE earnings AS e SET report_type = v.report_type, updated_at = NOW()"
        " FROM (VALUES %s) AS v(id, report_type)"
        " WHERE e.id = v.id::int",
        [(c["earning_id"], c["after"]) for c in changes],
        page_size=500,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill earnings.report_type from Longbridge events")
    parser.add_argument("--apply", action="store_true", help="write the changes (backs up first)")
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    parser.add_argument("--start", default=None, help="only rows on/after this date")
    parser.add_argument("--end", default=None, help="only rows on/before this date")
    parser.add_argument("--window-days", type=int, default=30,
                        help="pad the provider fetch window around the row range (default 30)")
    args = parser.parse_args()

    # ``--json`` is consumed by other tooling: every human-readable line goes to
    # stderr in that mode so stdout stays one parseable document.
    note = (lambda *a, **k: print(*a, file=sys.stderr, **k)) if args.json else print

    with db_cursor() as cur:
        rows = load_rows(cur, args.start, args.end)
    if not rows:
        note("no confirmed rows carry a fiscal identity — nothing to backfill")
        return 0

    dates = sorted(row["report_date"] for row in rows)
    fetch_start = (dates[0] - timedelta(days=args.window_days)).isoformat()
    fetch_end = (dates[-1] + timedelta(days=args.window_days)).isoformat()
    mapping = provider_report_types(fetch_start, fetch_end)
    changes, summary = plan_changes(rows, mapping)

    if args.json:
        print(json.dumps({
            "rows_considered": len(rows),
            "provider_events": len(mapping),
            "changes": len(changes),
            "summary": dict(summary),
            "plan": changes,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"rows considered        : {len(rows)}")
        print(f"provider events fetched: {len(mapping)}")
        print(f"rows to change         : {len(changes)}")
        for key in sorted(summary):
            print(f"  {key}: {summary[key]}")
        for change in changes[:20]:
            print(f"  {change['symbol']}.{change['market']} {change['report_date']}"
                  f" (row {change['earning_id']}): {change['before']} → {change['after']}")
        if len(changes) > 20:
            print(f"  … {len(changes) - 20} more")

    if not args.apply:
        note("\ndry-run (read-only): re-run with --apply to write the changes.")
        return 0

    with db_cursor() as cur:
        apply_changes(cur, changes)
        cur.execute(f"SELECT count(*) AS total FROM {BACKUP_TABLE}")
        backed_up = (cur.fetchone() or {}).get("total", 0)
    note(f"\napplied: {len(changes)} row(s) updated, backup rows: {backed_up}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
