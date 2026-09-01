#!/usr/bin/env python3
"""One-time backfill for Issue #39: fix Futu-sourced earnings rows that were
written before ``sync_futu.py`` recorded provenance, so reported events were
mislabelled ``unknown``/``scheduled``.

Conservative criterion (see issue): a row is attributable to Futu when it
*has actuals* (eps_actual or revenue_actual) AND *every source field is empty*
(date_source='unknown', actual_source IS NULL, estimate_source IS NULL). This
cannot catch Longbridge rows (they set actual_source) or seed demo rows (they
carry no actuals), so the predicate is safe.

The backfill is DATA-MUTATING. It does NOT run unless ``--apply`` is passed;
without it, the script only estimates and prints a sample. Before any UPDATE it
snapshots the affected rows into ``earnings_provenance_backfill_backup`` so the
change is reversible.

Usage (dry-run first):
    python scripts/backfill_futu_provenance.py
    python scripts/backfill_futu_provenance.py --apply
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor  # noqa: E402

# Conservative predicate: has actuals + every source field empty → Futu.
PREDICATE = """date_source = 'unknown' AND date_status = 'scheduled'
   AND (eps_actual IS NOT NULL OR revenue_actual IS NOT NULL)
   AND actual_source IS NULL AND estimate_source IS NULL"""


def count_rows() -> int:
    with db_cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM earnings WHERE {PREDICATE}"
        )
        return cur.fetchone()["n"]


def sample(limit: int = 10) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT symbol, market, fiscal_year, fiscal_quarter,
                       date_source, date_status
                FROM earnings WHERE {PREDICATE} ORDER BY symbol LIMIT %s""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def apply() -> int:
    """Snapshot affected rows, then update them. Returns the number updated."""
    with db_cursor() as cur:
        # 1. Snapshot old values for reversibility.
        cur.execute("DROP TABLE IF EXISTS earnings_provenance_backfill_backup")
        cur.execute(
            f"""CREATE TABLE earnings_provenance_backfill_backup AS
                SELECT symbol, market, report_date, report_type,
                       date_source, date_status, actual_source
                FROM earnings WHERE {PREDICATE}"""
        )
        cur.execute("SELECT COUNT(*) AS n FROM earnings_provenance_backfill_backup")
        backed_up = cur.fetchone()["n"]

        # 2. Apply the fix.
        cur.execute(
            f"""UPDATE earnings
                SET date_source = 'futu', date_status = 'reported',
                    actual_source = 'futu'
                WHERE {PREDICATE}"""
        )
        updated = cur.rowcount
    print(f"backfilled {updated} rows (snapshot backed up: {backed_up})")
    return updated


def verify() -> None:
    """Print post-fix distribution so the change is externally verifiable."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT date_source, date_status, COUNT(*) AS n
               FROM earnings GROUP BY 1, 2 ORDER BY 3 DESC"""
        )
        for r in cur.fetchall():
            print("  ", dict(r))
        cur.execute(
            """SELECT COUNT(*) AS n FROM earnings
               WHERE date_source = 'unknown' AND date_status = 'scheduled'
                 AND (eps_actual IS NOT NULL OR revenue_actual IS NOT NULL)"""
        )
        print("  remaining mislabelled (unknown+scheduled with actuals):",
              dict(cur.fetchone())["n"])


def main() -> int:
    apply_requested = "--apply" in sys.argv
    print(f"target predicate: has actuals AND all source fields empty ({PREDICATE})")
    before = count_rows()
    print(f"rows matching predicate (before): {before}")

    if not apply_requested:
        print("\nDRY-RUN: no changes made. Re-run with --apply to backfill.")
        for r in sample():
            print("  sample:", r)
        return 0

    apply()
    print("\n--- post-fix distribution ---")
    verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
