#!/usr/bin/env python3
"""Read-only acceptance check for Issue #50 (production, run after the merge).

Five checks, each printed with the evidence it is based on:

1. duplicate confirmed fiscal periods == 0;
2. the partial unique index ``idx_earnings_fiscal_identity`` exists;
3. no estimate snapshot was lost and none is orphaned;
4. ``GET /api/earnings`` returns one row per fiscal period, and the row for a
   period that had a release + a disclosure carries the *release's* figures
   (DEA FY2025 Q4 must show revenue 87,725,750 and not the 334,384,600 annual
   total);
5. ``GET /api/ical`` emits exactly as many events as the API emits rows for the
   same window (the three outlets disagreed before the fix).

Usage (from a checkout with DB access, e.g. /opt/fincal):
    DB_HOST=localhost python scripts/issue50_acceptance_check.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor  # noqa: E402

BASE = os.getenv("FINCAL_API_BASE", "http://localhost:8000")
HEADERS = {
    "X-User-Id": "9999",
    "X-User-Email": "probe@local",
    "X-User-Name": "probe",
    "X-User-Role": "admin",
}


def api_get(path: str):
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
    ok = orphans == 0 and total >= before.get("snapshots", 0)
    print(f"3. estimate snapshots: {total} (before merge {before.get('snapshots')}), orphans: {orphans}")
    return ok


def check_api_rows() -> bool:
    start, end = "2026-02-01", "2026-03-10"
    rows = api_earnings(start, end)
    periods = Counter((r["symbol"], r["market"], r.get("fiscal_year"), r.get("fiscal_quarter"))
                      for r in rows if r.get("fiscal_year") and r.get("fiscal_quarter"))
    repeated = {key: count for key, count in periods.items() if count > 1}
    print(f"4. /api/earnings {start}..{end}: {len(rows)} rows, duplicated periods: {repeated or 'none'}")
    dea = [r for r in rows if r["symbol"] == "DEA" and r.get("fiscal_quarter") == 4]
    cpsh = [r for r in rows if r["symbol"] == "CPSH" and r.get("fiscal_quarter") == 4]
    for label, subset in (("DEA FY2025 Q4", dea), ("CPSH FY2025 Q4", cpsh)):
        for row in subset:
            print(f"   {label}: id={row.get('id')} date={row.get('report_date')} "
                  f"type={row.get('report_type')} rev_est={row.get('revenue_estimate')} "
                  f"rev_act={row.get('revenue_actual')}")
    dea_ok = len(dea) == 1 and float(dea[0].get("revenue_estimate") or 0) < 100_000_000
    cpsh_ok = len(cpsh) == 1 and float(cpsh[0].get("revenue_estimate") or 0) < 20_000_000
    return not repeated and dea_ok and cpsh_ok


def check_ical_agrees() -> bool:
    start, end = "2026-02-01", "2026-03-10"
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
    before_path = sys.argv[1] if len(sys.argv) > 1 else None
    before = json.load(open(before_path)) if before_path and os.path.exists(before_path) else {}
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
