#!/usr/bin/env python3
"""Read-only freshness gate for the FinCal sync pipeline (Issue #53).

Reports when each declared stage (``scripts/sync_all.sh``) last succeeded and
how old the derived tables it feeds are.  Exists so the cron wrapper can fail
loudly when a stage silently stops running — production had ``consensus`` idle
for 44 days and ``stock_names`` for 43 days while every health endpoint stayed
green.

Exit codes:
  0  every stage and derived table is within the freshness threshold
  1  at least one stage/derived table is stale or never ran
  2  freshness could not be determined (database unreadable)

Read-only by construction: the shared check issues ``SELECT`` statements only
and makes no external calls (no Futu / Longbridge / Kurumi probe).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.freshness import (  # noqa: E402
    ERROR_UNAVAILABLE,
    check_freshness,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_sync_freshness")

EXIT_OK = 0
EXIT_STALE = 1
EXIT_UNKNOWN = 2


def report(summary: dict) -> None:
    threshold = summary["threshold_hours"]
    print(f"sync freshness (threshold {threshold:g}h, checked {summary['checked_at']})")
    print(f"{'name':<32} {'kind':<8} {'status':<7} {'age_hours':>10}  last_success_at")
    for entry in summary["entries"]:
        age = "-" if entry["age_hours"] is None else f"{entry['age_hours']:g}"
        print(f"{entry['stage']:<32} {entry['kind']:<8} {entry['status']:<7} "
              f"{age:>10}  {entry['last_success_at'] or '-'}")


def main() -> int:
    summary = check_freshness()
    report(summary)

    if summary["status"] == "unknown" or summary["error_code"] == ERROR_UNAVAILABLE:
        print("ERROR: freshness could not be determined (database unreadable)", file=sys.stderr)
        return EXIT_UNKNOWN

    if summary["stale_stages"] or summary["never_run_stages"]:
        for stage in summary["never_run_stages"]:
            print(f"STALE: stage '{stage}' never recorded a successful run", file=sys.stderr)
        for stage in summary["stale_stages"]:
            entry = next(e for e in summary["entries"]
                         if e["kind"] == "stage" and e["stage"] == stage)
            print(f"STALE: stage '{stage}' last succeeded at {entry['last_success_at']} "
                  f"({entry['age_hours']:g}h ago)", file=sys.stderr)
        return EXIT_STALE

    if summary["stale_data"]:
        names = ", ".join(summary["stale_data"])
        print(f"STALE: derived data behind its threshold: {names}", file=sys.stderr)
        return EXIT_STALE

    print("OK: all sync stages and derived data are fresh")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
