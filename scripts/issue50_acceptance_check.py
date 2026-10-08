#!/usr/bin/env python3
"""Read-only acceptance check for Issue #50 (run inside the fincal container).

Five checks, each printed with the evidence it is based on:

1. duplicate confirmed fiscal periods == 0;
2. the partial unique index ``idx_earnings_fiscal_identity`` exists;
3. no estimate snapshot was lost and none is orphaned (compared against the
   pre-merge baseline passed as argv[1], a JSON file with ``snapshots``);
4. ``GET /api/earnings`` (real HTTP, layer cache included) returns at most one
   row per fiscal period, and a period that had a release plus a disclosure keeps
   the *release's* figures.  DEA/CPSH are not in the endpoint's default
   popular+watchlist symbol set, so their rows are asserted through the same
   ``app.earnings.fetch_earnings_from_db`` the endpoint calls, together with
   evidence that the merged annual twin is preserved in the reconciliation
   backup table;
5. ``GET /api/ical/<token>`` emits exactly as many events as the API emits
   distinct periods for the same window (the outlets disagreed before the fix).

Usage inside the container (the container publishes no host port, so the probe
must run in it):
    PYTHONPATH=/app python /tmp/issue50_acceptance_check.py /tmp/before.json
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import date

APP_DIR = os.getenv("FINCAL_APP_DIR", "/app")
sys.path.insert(0, APP_DIR)
sys.path.insert(0, os.path.join(APP_DIR, "scripts"))

from app.db import db_cursor  # noqa: E402

BASE = os.getenv("FINCAL_API_BASE", "http://localhost:8000")
HEADERS = {
    "X-User-Id": "9999",
    "X-User-Email": "probe@local",
    "X-User-Name": "probe",
    "X-User-Role": "admin",
}
WINDOW = ("2026-02-01", "2026-03-10")
#: symbol -> (release date, release revenue_estimate, annual revenue the twin held)
RELEASE_FIGURES = {
    "DEA": ("2026-02-21", 87725750.0, 334384600.0),
    "CPSH": ("2026-03-02", 7890000.0, 32280000.0),
}


def api_get(path: str) -> str:
    request = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode()


def api_earnings(start: str, end: str) -> list:
    """``GET /api/earnings`` answers with a bare JSON list of rows."""
    return json.loads(api_get(f"/api/earnings?start={start}&end={end}"))


def check_duplicate_periods() -> bool:
    with db_cursor() as cur:
        cur.execute("""
            SELECT count(*) AS groups FROM (
                SELECT 1 FROM earnings
                WHERE is_predicted = FALSE AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL
                GROUP BY symbol, market, fiscal_year, fiscal_quarter
                HAVING count(*) > 1
            ) duplicated_periods
        """)
        groups = (cur.fetchone() or {}).get("groups", -1)
    print(f"1. duplicate confirmed fiscal periods: {groups}")
    return groups == 0


def check_index() -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_earnings_fiscal_identity'")
        row = cur.fetchone()
    print(f"2. fiscal identity index: {row['indexdef'] if row else 'MISSING'}")
    return row is not None


def check_snapshots(before: dict) -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM earnings_estimate_snapshots")
        total = (cur.fetchone() or {}).get("total", -1)
        cur.execute("""
            SELECT count(*) AS orphans FROM earnings_estimate_snapshots s
            LEFT JOIN earnings e ON e.id = s.earning_id WHERE e.id IS NULL
        """)
        orphans = (cur.fetchone() or {}).get("orphans", -1)
    baseline = before.get("snapshots")
    ok = orphans == 0 and baseline is not None and total >= baseline
    print(f"3. estimate snapshots: {total} (before merge {baseline}), orphans: {orphans}")
    return ok


def check_api_rows() -> bool:
    start, end = WINDOW
    rows = api_earnings(start, end)
    periods = Counter((r["symbol"], r["market"], r.get("fiscal_year"), r.get("fiscal_quarter"))
                      for r in rows if r.get("fiscal_year") and r.get("fiscal_quarter"))
    repeated = {key: count for key, count in periods.items() if count > 1}
    print(f"4a. /api/earnings {start}..{end}: {len(rows)} rows, "
          f"{len(periods)} distinct periods, duplicated: {repeated or 'none'}")

    from app.earnings import fetch_earnings_from_db  # noqa: E402

    # The container image ships the app package only (no scripts/), so the merge
    # tool's constant is repeated here: keep it in sync with
    # scripts/reconcile_fiscal_rows.py::BACKUP_TABLE.
    backup_table = "earnings_fiscal_reconcile_backup"

    period_ok = True
    for symbol, (release_date, release_revenue, annual_revenue) in RELEASE_FIGURES.items():
        with db_cursor() as cur:
            cur.execute(
                "SELECT id, report_date, report_type, revenue_estimate FROM earnings"
                " WHERE symbol = %s AND fiscal_year = 2025 AND fiscal_quarter = 4"
                " AND is_predicted = FALSE ORDER BY report_date",
                (symbol,),
            )
            stored = [dict(row) for row in cur.fetchall()]
            cur.execute(
                f"SELECT count(*) AS kept FROM {backup_table} WHERE group_key = %s",
                (f"{symbol}.US:FY2025Q4",),
            )
            backed_up = (cur.fetchone() or {}).get("kept", 0)
        rendered = fetch_earnings_from_db(symbols=[symbol], markets=["US"], start=start, end=end)
        shown = [r for r in rendered if r["symbol"] == symbol]
        ok = (len(stored) == 1
              and len(shown) == 1
              and str(shown[0]["report_date"]) == release_date
              and float(shown[0]["revenue_estimate"] or 0) == release_revenue)
        period_ok = period_ok and ok and backed_up >= 1
        print(f"4b. {symbol} FY2025 Q4: stored rows {len(stored)} "
              f"{[(r['id'], str(r['report_date']), r['report_type'], float(r['revenue_estimate'] or 0)) for r in stored]}, "
              f"rendered {len(shown)} {[(r['id'], str(r['report_date']), float(r['revenue_estimate'] or 0)) for r in shown]}, "
              f"release figures kept: {ok}, merged annual twin (revenue {annual_revenue}) backed up: {backed_up}")
    return not repeated and period_ok


def check_ical_agrees() -> bool:
    start, end = WINDOW
    rows = api_earnings(start, end)
    api_periods = len({(r["symbol"], r["market"], r.get("fiscal_year"), r.get("fiscal_quarter"))
                       for r in rows})
    with db_cursor() as cur:
        cur.execute("SELECT ical_token FROM users WHERE ical_token IS NOT NULL ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if not row:
        print("5. iCal: no subscription token in the database — cannot compare")
        return False
    text = api_get(f"/api/ical/{row['ical_token']}?start={start}&end={end}")
    events = text.count("BEGIN:VEVENT")
    print(f"5. iCal events: {events}, distinct API periods: {api_periods}")
    return events == api_periods


def main() -> int:
    before = {}
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        before = json.load(open(sys.argv[1]))
    results = [
        check_duplicate_periods(),
        check_index(),
        check_snapshots(before),
        check_api_rows(),
        check_ical_agrees(),
    ]
    print(f"\nchecked at {date.today().isoformat()}: "
          f"{sum(results)}/{len(results)} acceptance checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
