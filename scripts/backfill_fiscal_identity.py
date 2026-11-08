#!/usr/bin/env python3
"""Give the rows that lost their fiscal identity one back (Issue #113).

``fiscal_year`` used to be derived from the event's calendar month, which is only
correct for a December year-end.  For a March or June year-end company the
derived label was one year early, the order check rejected it, and the row was
written anyway with ``fiscal_year``/``fiscal_quarter`` NULL: 487 rows on 481
symbols in production.  Those rows sit outside the identity contract — no
``idx_earnings_fiscal_identity`` protection, invisible to
``reschedule_confirmed_rows`` and ``collapse_fiscal_duplicates``, and their iCal
UID degrades to the report date, so a reschedule shows up in a subscriber's
calendar as a brand-new event instead of an update.

The derivation is fixed in the sync (``fiscal.fiscal_year_after_closing``: the
event belongs to the year after the last annual report that preceded it).  This
script applies the same rule to the rows already stored, and collapses the
rescheduled duplicates the missing identity allowed.

What it does
------------
1. Labels an unlabelled row whose period is unambiguous: ``H`` is the second
   quarter and ``A`` the fourth (Issue #75's sequence contract), so only the
   fiscal year is missing — and that comes from the symbol's own annual reports.
   A bare ``Q`` row is left alone: its quarter is unrecoverable, and guessing one
   would invent an identity (counted and reported instead).
2. Applies ``fiscal.fiscal_label_consistent`` against every confirmed row of the
   symbol, so a backfilled label can never contradict the dates already stored.
3. Skips a label whose ``(symbol, market, fiscal_year, fiscal_quarter)`` another
   row already holds — the unique index would reject it anyway, and taking the
   period away from a reported row is not this script's call.
4. Merges the rescheduled duplicates the missing identity produced: rows of the
   same ``(symbol, market, report_type)`` within 90 days, none of them carrying an
   actual value, are collapsed onto the latest date.  Estimate snapshots are
   re-pointed before a delete, and every deleted row is copied verbatim to
   ``earnings_fiscal_backfill_backup``.

Usage
-----
    python scripts/backfill_fiscal_identity.py            # dry-run (read-only)
    python scripts/backfill_fiscal_identity.py --json
    python scripts/backfill_fiscal_identity.py --apply    # writes (deletes rows)

``--apply`` is opt-in: it moves fiscal identity and deletes duplicate rows.  Run
the dry-run first, then check the printed sample against the plan.
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

BACKUP_TABLE = "earnings_fiscal_backfill_backup"

#: ``H`` discloses the second quarter and ``A`` the fourth (#75); a ``Q`` row's
#: quarter is not recoverable from the row itself.
_SEQUENCE_QUARTERS = {"H": 2, "A": 4}

#: A reschedule moves an announcement by days, not months.  Production's evidence
#: for Issue #113 was "same report_type, dates ≤60 days apart"; anything wider
#: starts folding consecutive *different* reports together.
#:
#: Only ``H``/``A`` are merged at all: a company publishes one half-year and one
#: annual report per fiscal year, so two of them within two months are the same
#: announcement moved.  ``Q`` rows legitimately repeat every ~90 days, and folding
#: them by date distance deleted 298 rows of consecutive quarters in the first
#: dry-run of this script — a different quarter is not a duplicate.
_RESCHEDULE_WINDOW_DAYS = 60
_MERGE_SEQUENCES = frozenset(_SEQUENCE_QUARTERS)

_ROWS_SQL = """
    SELECT id, symbol, market, company_name, report_date, report_type,
           fiscal_year, fiscal_quarter, is_predicted,
           eps_actual, revenue_actual, date_source
      FROM earnings
     WHERE is_predicted = FALSE
"""


def _confirmed_rows(cur) -> list[dict]:
    cur.execute(_ROWS_SQL)
    return [dict(row) for row in cur.fetchall()]


def _annual_closings(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Annual rows per symbol — the closings a fiscal year is derived from."""
    closings: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["fiscal_year"] is None:
            continue
        if row["fiscal_quarter"] == 4 or row["report_type"] == "A":
            closings[(row["symbol"], row["market"])].append({
                "fiscal_year": row["fiscal_year"],
                "report_date": row["report_date"],
            })
    return dict(closings)


def _held_identities(rows: list[dict]) -> dict[tuple, dict]:
    """Every fiscal identity already owned by a confirmed row."""
    held: dict[tuple, dict] = {}
    for row in rows:
        if row["fiscal_year"] is None or row["fiscal_quarter"] is None:
            continue
        key = (row["symbol"], row["market"], row["fiscal_year"], row["fiscal_quarter"])
        held.setdefault(key, row)
    return held


def plan_labels(rows: list[dict]) -> tuple[list[dict], dict]:
    """Return ``(labels, report)``: the rows that can be labelled, and why others cannot."""
    closings = _annual_closings(rows)
    held = _held_identities(rows)
    by_symbol: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        by_symbol[(row["symbol"], row["market"])].append(row)

    labels: list[dict] = []
    planned: set[tuple] = set()
    report = {"unlabelled": 0, "quarter_unknown": 0, "no_closing": 0,
              "inconsistent": 0, "held_by_other": 0, "labelled": 0}

    for row in rows:
        if row["fiscal_year"] is not None and row["fiscal_quarter"] is not None:
            continue
        report["unlabelled"] += 1
        quarter = _SEQUENCE_QUARTERS.get((row["report_type"] or "").strip().upper())
        if quarter is None:
            # A bare Q row: the quarter cannot be recovered without guessing.
            report["quarter_unknown"] += 1
            continue
        fiscal_year = fiscal.fiscal_year_after_closing(
            closings.get((row["symbol"], row["market"]), ()), row["report_date"])
        if fiscal_year is None:
            report["no_closing"] += 1
            continue
        siblings = [s for s in by_symbol[(row["symbol"], row["market"])] if s["id"] != row["id"]]
        if not fiscal.fiscal_label_consistent(
                row["symbol"], row["market"], fiscal_year, quarter,
                row["report_date"], siblings):
            report["inconsistent"] += 1
            continue
        key = (row["symbol"], row["market"], fiscal_year, quarter)
        if key in held or key in planned:
            report["held_by_other"] += 1
            continue
        planned.add(key)
        report["labelled"] += 1
        labels.append({
            "id": row["id"], "symbol": row["symbol"], "market": row["market"],
            "report_date": str(row["report_date"]), "report_type": row["report_type"],
            "fiscal_year": fiscal_year, "fiscal_quarter": quarter,
            "company_name": row["company_name"],
        })
    return labels, report


def plan_duplicates(rows: list[dict]) -> list[dict]:
    """Groups of rescheduled duplicates: same symbol/type, within the window, no actuals.

    A row that carries an actual value is never dropped — it is what the period
    reported.  The survivor is the latest date, which is the date the provider
    last announced.
    """
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["eps_actual"] is not None or row["revenue_actual"] is not None:
            continue
        sequence = (row["report_type"] or "").strip().upper()
        if sequence not in _MERGE_SEQUENCES:
            continue
        groups[(row["symbol"], row["market"], sequence)].append(row)

    plans: list[dict] = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: r["report_date"])
        cluster: list[dict] = [members[0]]
        for row in members[1:]:
            gap = (row["report_date"] - cluster[-1]["report_date"]).days
            if gap <= _RESCHEDULE_WINDOW_DAYS:
                cluster.append(row)
            else:
                if len(cluster) > 1:
                    plans.append(_merge_plan(key, cluster))
                cluster = [row]
        if len(cluster) > 1:
            plans.append(_merge_plan(key, cluster))
    return plans


def _merge_plan(key: tuple[str, str, str], cluster: list[dict]) -> dict:
    survivor = max(cluster, key=lambda r: r["report_date"])
    dropped = [r for r in cluster if r["id"] != survivor["id"]]
    return {
        "symbol": key[0], "market": key[1], "report_type": key[2],
        "keep": {"id": survivor["id"], "report_date": str(survivor["report_date"]),
                 "fiscal_year": survivor["fiscal_year"],
                 "fiscal_quarter": survivor["fiscal_quarter"]},
        "drop": [{"id": r["id"], "report_date": str(r["report_date"]),
                  "fiscal_year": r["fiscal_year"], "fiscal_quarter": r["fiscal_quarter"]}
                 for r in dropped],
    }


def apply_labels(cur, labels: list[dict]) -> int:
    changed = 0
    for label in labels:
        # Guarded by the same conditions the plan proved: the row is still
        # unlabelled and still the same period, so a concurrent sync cannot be
        # overwritten by a stale plan.
        cur.execute(
            """
            UPDATE earnings
               SET fiscal_year = %s, fiscal_quarter = %s, updated_at = NOW()
             WHERE id = %s
               AND is_predicted = FALSE
               AND (fiscal_year IS NULL OR fiscal_quarter IS NULL)
            """,
            (label["fiscal_year"], label["fiscal_quarter"], label["id"]),
        )
        changed += cur.rowcount
    return changed


def apply_duplicates(cur, plans: list[dict], rows: list[dict]) -> dict:
    """Delete the superseded duplicates after backing them up and re-pointing snapshots."""
    from scripts.reconcile_fiscal_rows import GroupPlan, _repoint_snapshots

    by_id = {row["id"]: row for row in rows}
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
            backed_up_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reason TEXT NOT NULL,
            row_data JSONB NOT NULL
        )
    """)
    dropped_ids = [d["id"] for plan in plans for d in plan["drop"]]
    if not dropped_ids:
        return {"deleted": 0, "snapshots": 0}

    cur.execute(
        f"""
        INSERT INTO {BACKUP_TABLE} (reason, row_data)
        SELECT 'duplicate_reschedule', to_jsonb(e)
          FROM earnings e WHERE e.id = ANY(%s)
        """,
        (dropped_ids,),
    )

    # Snapshots are re-pointed per (survivor, dropped) pair: the helper reasons
    # about one period's collision set at a time.  Prediction rows carry no
    # snapshots today, so this is defensive, not a live path.
    snapshots = 0
    for plan in plans:
        snapshots += len(_repoint_snapshots(cur, GroupPlan(
            key=(plan["symbol"], plan["market"], plan["report_type"]),
            survivor=by_id[plan["keep"]["id"]],
            dropped=[by_id[d["id"]] for d in plan["drop"]],
        )))

    cur.execute("DELETE FROM earnings WHERE id = ANY(%s)", (dropped_ids,))
    return {"deleted": cur.rowcount, "snapshots": snapshots}


def _summary(cur) -> dict:
    cur.execute("""
        SELECT count(*) FILTER (WHERE fiscal_year IS NULL OR fiscal_quarter IS NULL) AS unlabelled,
               count(*) FILTER (WHERE fiscal_year IS NULL OR fiscal_quarter IS NULL
                                  AND report_type = 'Q') AS unlabelled_q,
               count(*) AS confirmed
          FROM earnings WHERE is_predicted = FALSE
    """)
    return dict(cur.fetchone())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="write the labels and delete the duplicate rows")
    parser.add_argument("--json", action="store_true", help="machine-readable plan")
    parser.add_argument("--no-merge", action="store_true",
                        help="only label rows, leave duplicates for a second pass")
    args = parser.parse_args()

    with db_cursor() as cur:
        before = _summary(cur)
        rows = _confirmed_rows(cur)
        labels, report = plan_labels(rows)
        plans = [] if args.no_merge else plan_duplicates(rows)

        payload = {
            "before": before,
            "label_report": report,
            "labels": labels[:20],
            "label_count": len(labels),
            "duplicate_groups": len(plans),
            "duplicate_rows": sum(len(p["drop"]) for p in plans),
            "duplicates": plans[:10],
            "applied": False,
        }

        if args.apply:
            # Merge before labelling: two rows of one rescheduled half-year would
            # both claim FY2027 Q2, and the unique index rejects the second one
            # (verified — the first --apply run hit UniqueViolation on 1723.HK).
            merged = apply_duplicates(cur, plans, rows)
            if merged["deleted"]:
                rows = _confirmed_rows(cur)
                labels, report = plan_labels(rows)
            labelled = apply_labels(cur, labels)
            payload["applied"] = True
            payload["labelled_rows"] = labelled
            payload["merge"] = merged
            payload["after"] = _summary(cur)

        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"before: {before}")
            print(f"unlabelled rows: {report['unlabelled']} "
                  f"(labellable {report['labelled']}, quarter unknown {report['quarter_unknown']}, "
                  f"no annual report {report['no_closing']}, "
                  f"inconsistent {report['inconsistent']}, held by another row {report['held_by_other']})")
            for label in labels[:20]:
                print(f"  label {label['symbol']}.{label['market']} {label['report_date']} "
                      f"{label['report_type']} → FY{label['fiscal_year']} Q{label['fiscal_quarter']}")
            print(f"reschedule duplicates: {len(plans)} group(s), "
                  f"{sum(len(p['drop']) for p in plans)} row(s) to drop")
            for plan in plans[:10]:
                print(f"  merge {plan['symbol']}.{plan['market']} {plan['report_type']}: "
                      f"keep {plan['keep']['report_date']}, drop "
                      f"{[d['report_date'] for d in plan['drop']]}")
            if args.apply:
                print(f"applied: {payload['labelled_rows']} row(s) labelled, "
                      f"{payload['merge']['deleted']} duplicate row(s) deleted, "
                      f"{payload['merge']['snapshots']} snapshot(s) re-pointed")
                print(f"after: {payload['after']}")
            else:
                print("dry-run: nothing written (use --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
