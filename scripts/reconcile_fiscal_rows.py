#!/usr/bin/env python3
"""Merge confirmed duplicate rows that share one fiscal period (Issue #50).

``earnings`` is keyed by ``(symbol, market, report_date, report_type)``, so every
time a provider moved an announcement day — or Longbridge and Futu disagreed
about it — a *new* row appeared for a fiscal period that already had one.  In
production that left 439 confirmed duplicate groups (913 rows), one period
rendering twice with contradictory actuals, blocking the fiscal-identity unique
index (``app/db.py::ensure_fiscal_identity_index``).

What this script does
---------------------
For every confirmed duplicate group it keeps the row the read paths already show
(``app.fiscal.authority_key`` — confirmed first, then rows carrying actuals,
newest report date, newest ``updated_at``, highest id) and removes the others
**after** re-pointing their estimate snapshots to the survivor, so no history is
lost (``ON DELETE CASCADE`` would otherwise silently drop it).

What it deliberately does *not* do
----------------------------------
It performs no value arbitration: when two rows of one period carry different
actuals (UUUU ``-0.13`` vs ``27.898188``, MARA ``-4.52`` vs ``1.272849``), the
survivor keeps its own values and every dropped row (with its values and its
snapshot count) is copied verbatim into ``earnings_fiscal_reconcile_backup`` for
review.  Deciding which of two conflicting values is the truth — and the
magnitude sanity guard that goes with it — is a product decision tracked in
Issue #50 / #45, not something a maintenance script may guess.

Usage
-----
    python scripts/reconcile_fiscal_rows.py                 # dry-run (read-only)
    python scripts/reconcile_fiscal_rows.py --json          # machine-readable plan
    python scripts/reconcile_fiscal_rows.py --symbol UUUU   # scope
    python scripts/reconcile_fiscal_rows.py --apply         # merge (destructive)

``--apply`` is opt-in because it deletes rows; it writes the backup table first
and, at the end, builds the fiscal-identity unique index if the data now allows
it.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import fiscal  # noqa: E402
from app.db import db_cursor  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BACKUP_TABLE = "earnings_fiscal_reconcile_backup"

VALUE_FIELDS = ("eps_actual", "revenue_actual", "eps_estimate", "revenue_estimate")

_COLUMNS = """
    e.id, e.symbol, e.market, e.fiscal_year, e.fiscal_quarter, e.report_date,
    e.report_type, e.is_predicted, e.date_source, e.date_status,
    e.actual_source, e.estimate_source, e.eps_estimate, e.eps_actual,
    e.revenue_estimate, e.revenue_actual, e.updated_at, e.company_name,
    (SELECT count(*) FROM earnings_estimate_snapshots s WHERE s.earning_id = e.id) AS snapshot_count
"""

_DUPLICATE_GROUPS = """
    SELECT symbol, market, fiscal_year, fiscal_quarter
    FROM earnings
    WHERE is_predicted = FALSE AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL
    {symbol_filter}
    GROUP BY symbol, market, fiscal_year, fiscal_quarter
    HAVING count(*) > 1
"""


@dataclass
class GroupPlan:
    """The reconcile decision for one fiscal period."""

    key: tuple
    survivor: dict
    dropped: list[dict] = field(default_factory=list)
    snapshots_to_move: int = 0
    snapshot_collision: list[str] = field(default_factory=list)
    value_conflicts: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if not self.dropped:
            return ""
        return (
            "kept row {sid} ({sdate}, {ssource}) over {dids} ({ddates}, {dsources}) by authority order "
            "confirmed>actuals>report_date>updated_at>id".format(
                sid=self.survivor["id"],
                sdate=_date_str(self.survivor.get("report_date")),
                ssource=self.survivor.get("date_source") or "unknown",
                dids=",".join(str(row["id"]) for row in self.dropped),
                ddates=",".join(_date_str(row.get("report_date")) for row in self.dropped),
                dsources=",".join(row.get("date_source") or "unknown" for row in self.dropped),
            )
        )

    def as_dict(self) -> dict:
        return {
            "symbol": self.key[0],
            "market": self.key[1],
            "fiscal_year": self.key[2],
            "fiscal_quarter": self.key[3],
            "survivor_id": self.survivor["id"],
            "survivor_report_date": _date_str(self.survivor.get("report_date")),
            "dropped_ids": [row["id"] for row in self.dropped],
            "snapshots_to_move": self.snapshots_to_move,
            "snapshot_collisions": self.snapshot_collision,
            "value_conflicts": self.value_conflicts,
            "reason": self.reason,
        }


def _date_str(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _text(value) -> str:
    return "NULL" if value is None else str(value)


def group_rows(rows: list[dict]) -> dict[tuple, list[dict]]:
    """Bucket rows by fiscal identity, ignoring rows without one."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = fiscal.fiscal_key(row)
        if key is None:
            continue
        groups.setdefault(key, []).append(row)
    return groups


def plan_group(key: tuple, rows: list[dict], snapshots_by_id: dict[int, list[dict]]) -> GroupPlan:
    """Choose the survivor of one fiscal period and describe the consequence.

    Pure: everything the report and the ``--apply`` path need comes from the
    arguments, so the decision is unit-testable without a database.
    """
    survivor = min(rows, key=fiscal.authority_key)
    plan = GroupPlan(key=key, survivor=survivor)
    for row in rows:
        if row["id"] == survivor["id"]:
            continue
        plan.dropped.append(row)
        plan.snapshots_to_move += len(snapshots_by_id.get(row["id"], []))
    # Snapshots are unique per (earning_id, source, captured_at): re-pointing one
    # onto the survivor may collide with a snapshot the survivor already has.
    taken = {
        (snap.get("source"), snap.get("captured_at"))
        for snap in snapshots_by_id.get(survivor["id"], [])
    }
    for row in plan.dropped:
        for snap in snapshots_by_id.get(row["id"], []):
            marker = (snap.get("source"), snap.get("captured_at"))
            if marker in taken:
                plan.snapshot_collision.append(f"earning={row['id']} {marker[0]}@{marker[1]}")
    for row in plan.dropped:
        for field_name in VALUE_FIELDS:
            dropped_value, kept_value = row.get(field_name), survivor.get(field_name)
            if dropped_value is not None and dropped_value != kept_value:
                plan.value_conflicts.append(
                    f"{field_name}: dropped id={row['id']} {_text(dropped_value)} "
                    f"vs kept id={survivor['id']} {_text(kept_value)}"
                )
    return plan


def load_plans(cur, symbol: str | None = None) -> list[GroupPlan]:
    """Read every confirmed duplicate group and plan its reconciliation."""
    from psycopg2.extras import execute_values

    symbol_filter = "AND symbol = %s" if symbol else ""
    cur.execute(_DUPLICATE_GROUPS.format(symbol_filter=symbol_filter), (symbol,) if symbol else ())
    keys = [tuple(row[k] for k in ("symbol", "market", "fiscal_year", "fiscal_quarter"))
            for row in cur.fetchall()]
    if not keys:
        return []

    execute_values(
        cur,
        f"""SELECT {_COLUMNS} FROM earnings e
        WHERE e.is_predicted = FALSE AND e.fiscal_year IS NOT NULL AND e.fiscal_quarter IS NOT NULL
          AND (e.symbol, e.market, e.fiscal_year, e.fiscal_quarter) IN (VALUES %s)
        ORDER BY e.symbol, e.market, e.fiscal_year, e.fiscal_quarter, e.report_date""",
        [list(k) for k in keys],
        page_size=500,
    )
    rows = [dict(row) for row in cur.fetchall()]
    earning_ids = [row["id"] for row in rows]
    snapshots_by_id: dict[int, list[dict]] = {}
    if earning_ids:
        cur.execute(
            "SELECT earning_id, source, captured_at FROM earnings_estimate_snapshots WHERE earning_id = ANY(%s)",
            (earning_ids,),
        )
        for snap in cur.fetchall():
            snapshots_by_id.setdefault(snap["earning_id"], []).append(dict(snap))

    return [
        plan_group(key, group, snapshots_by_id)
        for key, group in sorted(group_rows(rows).items())
        if len(group) > 1
    ]


def count_duplicate_groups(cur) -> int:
    cur.execute(_DUPLICATE_GROUPS.format(symbol_filter=""))
    return len(cur.fetchall())


def count_snapshots(cur) -> int:
    cur.execute("SELECT count(*) AS total FROM earnings_estimate_snapshots")
    row = cur.fetchone()
    return row["total"] if row else 0


def count_orphan_snapshots(cur) -> int:
    cur.execute(
        "SELECT count(*) AS total FROM earnings_estimate_snapshots s"
        " LEFT JOIN earnings e ON e.id = s.earning_id WHERE e.id IS NULL"
    )
    row = cur.fetchone()
    return row["total"] if row else 0


def apply_plan(cur, plan: GroupPlan) -> None:
    """Back up, re-point snapshots, then delete the non-survivor rows of a group."""
    dropped_ids = [row["id"] for row in plan.dropped]
    if not dropped_ids:
        return
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            earning_id INTEGER NOT NULL,
            group_key TEXT NOT NULL,
            survivor_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            row_data JSONB NOT NULL,
            reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""
    )
    cur.execute(
        f"""INSERT INTO {BACKUP_TABLE} (earning_id, group_key, survivor_id, reason, row_data)
        SELECT e.id, %s, %s, %s, to_jsonb(e) FROM earnings e WHERE e.id = ANY(%s)""",
        (f"{plan.key[0]}.{plan.key[1]}:FY{plan.key[2]}Q{plan.key[3]}", plan.survivor["id"],
         plan.reason, dropped_ids),
    )
    # Re-point before deleting: earnings_estimate_snapshots cascades on delete.
    cur.execute(
        "UPDATE earnings_estimate_snapshots SET earning_id = %s WHERE earning_id = ANY(%s)",
        (plan.survivor["id"], dropped_ids),
    )
    cur.execute(
        "DELETE FROM earnings WHERE id = ANY(%s) AND is_predicted = FALSE AND id <> %s",
        (dropped_ids, plan.survivor["id"]),
    )


def render(plans: list[GroupPlan], duplicates: int, snapshots: int, orphans: int,
           limit: int = 0) -> str:
    """Print the whole plan's totals, with an optional tail limit on decisions."""
    lines = [
        f"confirmed duplicate fiscal periods : {duplicates}",
        f"rows in those periods             : {sum(1 + len(p.dropped) for p in plans)}",
        f"rows to delete                    : {sum(len(p.dropped) for p in plans)}",
        f"snapshots to re-point             : {sum(p.snapshots_to_move for p in plans)}",
        f"snapshot collisions (would block) : {sum(len(p.snapshot_collision) for p in plans)}",
        f"groups with conflicting actuals   : {sum(1 for p in plans if p.value_conflicts)}",
        f"earnings_estimate_snapshots total : {snapshots}",
        f"orphan snapshots                  : {orphans}",
        "",
        "per-period decisions (survivor keeps its own values; dropped rows are backed up):",
    ]
    for plan in (plans[:limit] if limit else plans):
        lines.append(
            f"  {plan.key[0]}.{plan.key[1]} FY{plan.key[2]} Q{plan.key[3]}: {plan.reason}"
            f" | snapshots→{plan.snapshots_to_move}"
            + (" | SNAPSHOT COLLISION" if plan.snapshot_collision else "")
            + (" | VALUE CONFLICT: " + "; ".join(plan.value_conflicts) if plan.value_conflicts else "")
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge confirmed duplicate fiscal-period rows (Issue #50)")
    parser.add_argument("--apply", action="store_true",
                       help="delete non-surviving rows (backs up first, re-points snapshots)")
    parser.add_argument("--symbol", default=None, help="only this symbol")
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    parser.add_argument("--limit", type=int, default=0, help="print at most N decisions (0 = all)")
    args = parser.parse_args()

    with db_cursor() as cur:
        duplicates = count_duplicate_groups(cur)
        plans = load_plans(cur, args.symbol)
        snapshots = count_snapshots(cur)
        orphans = count_orphan_snapshots(cur)
        blocking = [plan for plan in plans if plan.snapshot_collision]

        if args.json:
            print(json.dumps({
                "duplicate_periods": duplicates,
                "rows_to_delete": sum(len(p.dropped) for p in plans),
                "snapshots_to_move": sum(p.snapshots_to_move for p in plans),
                "snapshots_total": snapshots,
                "orphan_snapshots": orphans,
                "plans": [p.as_dict() for p in plans],
            }, ensure_ascii=False, indent=2))
        else:
            print(render(plans, duplicates, snapshots, orphans, limit=args.limit))
            if args.limit and len(plans) > args.limit:
                print(f"  … {len(plans) - args.limit} more decision(s) omitted (--limit)")

        if not args.apply:
            print("\ndry-run (read-only): re-run with --apply to merge, once the value "
                  "arbitration in Issue #50 is decided.")
            return 0

        if blocking:
            print(f"\nrefusing to apply: {len(blocking)} group(s) would collide on "
                  f"earnings_estimate_snapshots(earning_id, source, captured_at); "
                  f"resolve those first.")
            return 2

        applied_rows = 0
        for plan in plans:
            apply_plan(cur, plan)
            applied_rows += len(plan.dropped)

    # Separate transaction: report the result from committed state.
    from app.db import ensure_fiscal_identity_index
    with db_cursor() as cur:
        remaining = count_duplicate_groups(cur)
        after_snapshots = count_snapshots(cur)
        after_orphans = count_orphan_snapshots(cur)
    index_ready = ensure_fiscal_identity_index()

    print(f"\napplied: deleted {applied_rows} duplicate row(s) from {len(plans)} period(s)")
    print(f"duplicate periods remaining: {remaining} (was {duplicates})")
    print(f"earnings_estimate_snapshots: {after_snapshots} (was {snapshots})")
    print(f"orphan snapshots: {after_orphans} (was {orphans})")
    print(f"fiscal identity unique index ready: {index_ready}")
    if after_snapshots < snapshots:
        print("WARNING: snapshot rows were lost — inspect "
              f"{BACKUP_TABLE} and earnings_estimate_snapshots")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
