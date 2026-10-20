#!/usr/bin/env python3
"""Merge same-announcement rows that share one report date (Issue #87).

``earnings`` is keyed by ``(symbol, market, report_date, report_type)``, so the two
events Longbridge emits for one announcement — the quarterly ``release`` (``Q``)
and the half-year/annual ``disclosure`` (``H``/``A``) — are two rows whenever the
second one lands after the first.  The merge passes in ``app.fiscal`` group by
*fiscal identity*, and these pairs need not share one:

* the release still carries a pre-#75 label (``FY2026 Q4``) while the disclosure
  is the corrected ``Q2`` (``0090.HK 2026-08-28``);
* for ADR symbols neither row carries an identity at all (``FJTSY 2026-10-28``).

Production had 85 such groups.  ``app.fiscal.collapse_fiscal_duplicates`` now folds
them on every read path, and ``app.fiscal.settle_same_date_rows`` stops new ones —
but the stored rows are still there, feeding the export, the iCal UIDs and any
future reader that does not collapse.

What this script does
---------------------
For every ``(symbol, market, report_date)`` holding more than one row **of mixed
kind** (at least one release and at least one disclosure):

1. keeps the release row (``app.fiscal.authority_key`` picks among several),
2. fills the survivor's NULL fields from the dropped rows (``COALESCE``
   semantics: the release's own figures always win — the disclosure's half-year or
   annual totals must never overwrite the quarter, see Issue #50's DEA case),
3. re-points the dropped rows' estimate snapshots to the survivor without losing
   any (the ``(earning_id, source, captured_at)`` collisions are nudged by whole
   microseconds, as ``reconcile_fiscal_rows`` does),
4. copies every dropped row verbatim into ``earnings_same_date_reconcile_backup``
   before deleting it,
5. re-checks the invariants: no same-date duplicates left, and no new duplicate
   *fiscal identity* created.

Two same-day rows of the same kind are left alone — a second, genuinely different
announcement must not be folded away.

Usage
-----
    python scripts/reconcile_same_date_rows.py            # dry-run (read-only)
    python scripts/reconcile_same_date_rows.py --json     # machine-readable plan
    python scripts/reconcile_same_date_rows.py --apply    # merge (deletes rows)

``--apply`` is opt-in because it deletes rows; it writes the backup table first and
prints what it removed, so the run is auditable and reversible by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import fiscal  # noqa: E402
from app.db import db_cursor  # noqa: E402

BACKUP_TABLE = "earnings_same_date_reconcile_backup"

#: Fields the survivor keeps when it already has them, and takes from a dropped
#: row only when it does not.  Deliberately no report_date/report_type: the
#: survivor's own identity and label are what the read paths already show.
MERGE_FIELDS = (
    "company_name", "before_after",
    "eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual",
    "estimate_currency", "actual_currency", "estimate_basis", "actual_basis",
)

GROUP_SQL = """
    SELECT symbol, market, report_date, count(*) AS rows
    FROM earnings
    WHERE is_predicted = FALSE
    GROUP BY symbol, market, report_date
    HAVING count(*) > 1
    ORDER BY report_date, symbol
"""

ROW_SQL = """
    SELECT id, symbol, market, report_date, report_type, fiscal_year, fiscal_quarter,
           is_predicted, date_status, company_name, before_after,
           eps_estimate, eps_actual, revenue_estimate, revenue_actual,
           estimate_currency, actual_currency, estimate_basis, actual_basis,
           updated_at
    FROM earnings
    WHERE symbol = %s AND market = %s AND report_date = %s
    ORDER BY id
"""


def load_groups() -> list[dict]:
    """Every same-date group whose rows mix a release with a disclosure."""
    with db_cursor() as cur:
        cur.execute(GROUP_SQL)
        groups = [dict(row) for row in cur.fetchall()]
        plans: list[dict] = []
        for group in groups:
            cur.execute(ROW_SQL, (group["symbol"], group["market"], group["report_date"]))
            rows = [dict(row) for row in cur.fetchall()]
            releases = [row for row in rows if not fiscal.is_disclosure(row["report_type"])]
            disclosures = [row for row in rows if fiscal.is_disclosure(row["report_type"])]
            if not releases or not disclosures:
                continue  # same kind on one date: not provably one announcement
            survivor = min(releases, key=fiscal.authority_key)
            plans.append({
                "symbol": group["symbol"],
                "market": group["market"],
                "report_date": group["report_date"].isoformat(),
                "survivor": survivor,
                "dropped": [row for row in rows if row["id"] != survivor["id"]],
            })
    return plans


def merged_values(survivor: dict, dropped: list[dict]) -> dict:
    """The survivor's values plus whatever only the dropped rows had."""
    merged = {}
    for field in MERGE_FIELDS:
        value = survivor.get(field)
        if value in (None, ""):
            for row in dropped:
                if row.get(field) not in (None, ""):
                    value = row[field]
                    break
        if value != survivor.get(field):
            merged[field] = value
    return merged


def apply_plan(plans: list[dict]) -> dict:
    """Snapshot, merge values, re-point snapshots, delete the dropped rows."""
    from scripts.reconcile_fiscal_rows import GroupPlan, _repoint_snapshots

    summary = {"groups": len(plans), "deleted": 0, "filled": 0,
               "snapshots_moved": 0, "snapshot_shifts": 0}
    with db_cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {BACKUP_TABLE}")
        cur.execute(
            f"""CREATE TABLE {BACKUP_TABLE} AS
                SELECT *, now() AS backed_up_at FROM earnings WHERE false"""
        )
        for plan in plans:
            survivor = plan["survivor"]
            dropped = plan["dropped"]
            drop_ids = [row["id"] for row in dropped]
            if drop_ids:
                cur.execute(
                    f"""INSERT INTO {BACKUP_TABLE}
                        SELECT *, now() FROM earnings WHERE id = ANY(%s)""",
                    (drop_ids,),
                )
            values = merged_values(survivor, dropped)
            if values:
                assignments = ", ".join(f"{field} = %s" for field in values)
                cur.execute(
                    f"UPDATE earnings SET {assignments}, updated_at = NOW() WHERE id = %s",
                    (*values.values(), survivor["id"]),
                )
                summary["filled"] += len(values)
            snapshots = _repoint_snapshots(cur, GroupPlan(
                key=(plan["symbol"], plan["market"], plan["report_date"]),
                survivor=survivor,
                dropped=dropped,
            ))
            summary["snapshot_shifts"] += len(snapshots)
            if drop_ids:
                cur.execute("DELETE FROM earnings WHERE id = ANY(%s)", (drop_ids,))
                summary["deleted"] += len(drop_ids)
            summary["snapshots_moved"] += len(dropped)
    return summary


def fiscal_now(cur):  # pragma: no cover - kept for the backup insert shape
    cur.execute("SELECT now() AS now")
    return cur.fetchone()["now"]


def verify() -> dict:
    """Post-conditions: no same-date duplicates, no new identity duplicates."""
    with db_cursor() as cur:
        cur.execute("""SELECT count(*) AS same_date_groups FROM (
                           SELECT 1 FROM earnings WHERE is_predicted = FALSE
                           GROUP BY symbol, market, report_date HAVING count(*) > 1) x""")
        same_date = cur.fetchone()["same_date_groups"]
        cur.execute("""SELECT count(*) AS identity_groups FROM (
                           SELECT 1 FROM earnings
                           WHERE is_predicted = FALSE AND fiscal_year IS NOT NULL
                             AND fiscal_quarter IS NOT NULL
                           GROUP BY symbol, market, fiscal_year, fiscal_quarter
                           HAVING count(*) > 1) x""")
        identity = cur.fetchone()["identity_groups"]
        cur.execute("SELECT count(*) AS rows FROM earnings")
        total = cur.fetchone()["rows"]
    return {"same_date_groups": same_date, "identity_groups": identity, "earnings_rows": total}


def plan_json(plans: list[dict]) -> str:
    return json.dumps([{
        "symbol": plan["symbol"], "market": plan["market"],
        "report_date": plan["report_date"],
        "survivor": {"id": plan["survivor"]["id"],
                     "report_type": plan["survivor"]["report_type"],
                     "eps_actual": str(plan["survivor"]["eps_actual"]),
                     "revenue_actual": str(plan["survivor"]["revenue_actual"])},
        "dropped": [{"id": row["id"], "report_type": row["report_type"],
                     "eps_actual": str(row["eps_actual"]),
                     "revenue_actual": str(row["revenue_actual"])}
                    for row in plan["dropped"]],
        "fills": sorted(merged_values(plan["survivor"], plan["dropped"])),
    } for plan in plans], indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="merge and delete (destructive)")
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    args = parser.parse_args()

    print("before:", verify(), file=sys.stderr)
    plans = load_groups()
    print(f"same-date groups of mixed kind: {len(plans)}", file=sys.stderr)
    if args.json:
        print(plan_json(plans))
    else:
        for plan in plans[:10]:
            fills = sorted(merged_values(plan["survivor"], plan["dropped"]))
            print(f"  {plan['symbol']}.{plan['market']} {plan['report_date']}: "
                  f"keep {plan['survivor']['id']} ({plan['survivor']['report_type']}), "
                  f"drop {[row['id'] for row in plan['dropped']]} "
                  f"({[row['report_type'] for row in plan['dropped']]}), fill {fills}",
                  file=sys.stderr)

    if not args.apply:
        print("DRY-RUN: no changes made. Re-run with --apply to merge.", file=sys.stderr)
        return 0

    summary = apply_plan(plans)
    print(f"merged {summary['groups']} groups: {summary['deleted']} rows deleted, "
          f"{summary['filled']} field(s) filled, "
          f"{summary['snapshot_shifts']} snapshot timestamp(s) nudged", file=sys.stderr)
    print("after:", verify(), file=sys.stderr)
    print(f"revert: dropped rows are in {BACKUP_TABLE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
