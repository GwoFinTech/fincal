#!/usr/bin/env python3
"""Repair rows created from Longbridge disclosure-only event sequences (Issue #75).

Longbridge ``saf``/``af`` events are not a second fiscal-quarter numbering
scheme.  The default is a read-only plan.  ``--apply`` backs up every affected
row and its estimate snapshots, then merges it into the canonical qf/3q row or
moves it onto the canonical event date when no row exists there.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.symbol import from_lb_counter_id  # noqa: E402
from app.db import db_cursor  # noqa: E402
from sync_earnings import (  # noqa: E402
    _DISCLOSURE_PERIOD_TYPES,
    _raw_fiscal_period,
    build_fiscal_period_index,
    fetch_calendar,
    parse_report_date,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BACKUP_TABLE = "earnings_period_type_reconcile_backup"


@dataclass(frozen=True)
class DisclosureMatch:
    symbol: str
    market: str
    disclosure_date: str
    canonical_date: str
    fiscal_year: int
    fiscal_quarter: int
    period_type: str

    def key(self) -> tuple:
        return (self.symbol, self.market, self.disclosure_date)


def collect_matches(start: str | None = None, end: str | None = None) -> tuple[list[DisclosureMatch], list[dict]]:
    today = date.today()
    start = start or (today - timedelta(days=180)).isoformat()
    end = end or (today + timedelta(days=365)).isoformat()
    matches: dict[tuple, DisclosureMatch] = {}
    canonical_rows: dict[tuple, dict] = {}
    for market in ("US", "HK"):
        pages = fetch_calendar(market, start, end)
        canonical = build_fiscal_period_index(pages)
        for (symbol, mkt), rows in canonical.items():
            for row in rows:
                key = (symbol, mkt, row["fiscal_year"], row["fiscal_quarter"], row["report_date"])
                canonical_rows[key] = {**row, "symbol": symbol, "market": mkt}
        for page in pages:
            for info in page.get("infos", []):
                symbol, mkt = from_lb_counter_id(info.get("counter_id", ""))
                disclosure_date = parse_report_date(info.get("date", ""))
                if not symbol or not disclosure_date:
                    continue
                ext = info.get("ext", {}).get("financial_report", {})
                fiscal_year, fiscal_quarter, period_type = _raw_fiscal_period(
                    ext, disclosure_date, mkt
                )
                if period_type not in _DISCLOSURE_PERIOD_TYPES:
                    continue
                candidates = [
                    row for row in canonical.get((symbol, mkt), [])
                    if fiscal_year is None or row["fiscal_year"] == fiscal_year
                ]
                if not candidates:
                    continue
                event_date = date.fromisoformat(disclosure_date)
                nearest = min(
                    candidates,
                    key=lambda row: abs((date.fromisoformat(row["report_date"]) - event_date).days),
                )
                match = DisclosureMatch(
                    symbol=symbol,
                    market=mkt,
                    disclosure_date=disclosure_date,
                    canonical_date=nearest["report_date"],
                    fiscal_year=nearest["fiscal_year"],
                    fiscal_quarter=nearest["fiscal_quarter"],
                    period_type=period_type,
                )
                matches[match.key()] = match
    return (
        sorted(matches.values(), key=lambda m: (m.market, m.symbol, m.disclosure_date)),
        sorted(canonical_rows.values(), key=lambda row: (row["market"], row["symbol"], row["report_date"])),
    )


def _row(cur, match: DisclosureMatch) -> dict | None:
    cur.execute(
        """SELECT * FROM earnings
           WHERE symbol=%s AND market=%s AND report_date=%s AND report_type='Q'
           LIMIT 1""",
        (match.symbol, match.market, match.disclosure_date),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _target(cur, match: DisclosureMatch, source_id: int) -> dict | None:
    cur.execute(
        """SELECT * FROM earnings
           WHERE symbol=%s AND market=%s AND fiscal_year=%s AND fiscal_quarter=%s
             AND is_predicted=FALSE AND id<>%s
           ORDER BY report_date DESC, id DESC LIMIT 1""",
        (match.symbol, match.market, match.fiscal_year, match.fiscal_quarter, source_id),
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    cur.execute(
        """SELECT * FROM earnings
           WHERE symbol=%s AND market=%s AND report_date=%s AND report_type='Q' AND id<>%s
           LIMIT 1""",
        (match.symbol, match.market, match.canonical_date, source_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _snapshots(cur, earning_id: int) -> list[dict]:
    cur.execute(
        "SELECT source, captured_at, eps_estimate, revenue_estimate, payload "
        "FROM earnings_estimate_snapshots WHERE earning_id=%s ORDER BY captured_at",
        (earning_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def plan(cur, matches: list[DisclosureMatch]) -> list[dict]:
    plans = []
    for match in matches:
        source = _row(cur, match)
        if not source or source["is_predicted"]:
            continue
        target = _target(cur, match, source["id"])
        if target and target["fiscal_year"] not in (None, match.fiscal_year):
            plans.append({"match": match, "source": source, "target": target, "conflict": True})
            continue
        plans.append({"match": match, "source": source, "target": target, "conflict": False})
    return plans


def legacy_mislabel_matches(
    cur, existing_keys: set[tuple], canonical_rows: list[dict]
) -> list[DisclosureMatch]:
    """Find old rows near a canonical qf period with a different quarter label."""
    cur.execute(
        """SELECT id, symbol, market, fiscal_year, fiscal_quarter, report_date
           FROM earnings
          WHERE is_predicted=FALSE AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL"""
    )
    rows = [dict(row) for row in cur.fetchall()]
    by_symbol_year: dict[tuple, list[dict]] = {}
    for row in canonical_rows:
        by_symbol_year.setdefault(
            (row["symbol"], row["market"], row["fiscal_year"]), []
        ).append(row)
    matches = {}
    for bad in rows:
        key = (bad["symbol"], bad["market"], bad["report_date"].isoformat())
        if key in existing_keys:
            continue
        candidates = []
        for canonical in by_symbol_year.get(
            (bad["symbol"], bad["market"], bad["fiscal_year"]), []
        ):
            if bad["fiscal_quarter"] == canonical["fiscal_quarter"]:
                continue
            distance = abs(
                (bad["report_date"] - date.fromisoformat(canonical["report_date"])).days
            )
            if distance <= 14:
                candidates.append((distance, canonical["fiscal_quarter"], canonical))
        if not candidates:
            continue
        _, _, canonical = min(candidates, key=lambda item: (item[0], item[1]))
        matches[key] = DisclosureMatch(
            symbol=bad["symbol"],
            market=bad["market"],
            disclosure_date=bad["report_date"].isoformat(),
            canonical_date=canonical["report_date"],
            fiscal_year=canonical["fiscal_year"],
            fiscal_quarter=canonical["fiscal_quarter"],
            period_type="legacy",
        )
    return sorted(matches.values(), key=lambda m: (m.market, m.symbol, m.disclosure_date))


def render(plans: list[dict]) -> str:
    merge = sum(1 for p in plans if p["target"] and not p["conflict"])
    move = sum(1 for p in plans if not p["target"] and not p["conflict"])
    conflicts = sum(1 for p in plans if p["conflict"])
    lines = [
        f"disclosure rows matched                 : {len(plans)}",
        f"rows to merge into canonical period     : {merge}",
        f"rows to move to canonical date          : {move}",
        f"target conflicts requiring review       : {conflicts}",
        "",
    ]
    for item in plans:
        m = item["match"]
        source = item["source"]
        target = item["target"]
        action = "CONFLICT" if item["conflict"] else (f"MERGE→{target['id']}" if target else "MOVE")
        lines.append(
            f"{m.symbol}.{m.market} {m.period_type} {m.disclosure_date} → "
            f"{m.canonical_date} FY{m.fiscal_year}Q{m.fiscal_quarter} "
            f"source={source['id']} {action}"
        )
    return "\n".join(lines)


def _backup_table(cur) -> None:
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
        id BIGSERIAL PRIMARY KEY,
        source_earning_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        market TEXT NOT NULL,
        disclosure_date DATE NOT NULL,
        canonical_date DATE NOT NULL,
        fiscal_year INTEGER NOT NULL,
        fiscal_quarter INTEGER NOT NULL,
        period_type TEXT NOT NULL,
        row_data JSONB NOT NULL,
        snapshots JSONB NOT NULL,
        reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")


def _source_by_id(cur, source_id: int) -> dict | None:
    cur.execute("SELECT * FROM earnings WHERE id=%s", (source_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def apply_plan(cur, item: dict) -> None:
    """Apply one repair using current rows, not the initial dry-run snapshot.

    Several disclosure events can point at one canonical period.  Earlier repairs
    in the same transaction may therefore remove the row that was initially
    listed as this item's target; re-resolving both sides keeps the operation
    idempotent and avoids stale target foreign keys.
    """
    match: DisclosureMatch = item["match"]
    source = _source_by_id(cur, item["source"]["id"])
    if source is None:
        return
    if source["is_predicted"]:
        return
    target = _target(cur, match, source["id"])
    if target and target["fiscal_year"] not in (None, match.fiscal_year):
        raise RuntimeError(
            f"target conflict during apply: {match.symbol}.{match.market} "
            f"{match.disclosure_date} -> row {target['id']}"
        )
    snapshots = _snapshots(cur, source["id"])
    _backup_table(cur)
    cur.execute(
        f"""INSERT INTO {BACKUP_TABLE}
        (source_earning_id, symbol, market, disclosure_date, canonical_date,
         fiscal_year, fiscal_quarter, period_type, row_data, snapshots)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)""",
        (source["id"], match.symbol, match.market, match.disclosure_date,
         match.canonical_date, match.fiscal_year, match.fiscal_quarter,
         match.period_type, json.dumps(source, default=str), json.dumps(snapshots, default=str)),
    )
    if target:
        cur.execute(
            """UPDATE earnings AS target SET
                company_name=COALESCE(NULLIF(target.company_name,''), NULLIF(source.company_name,'')),
                eps_estimate=COALESCE(target.eps_estimate, source.eps_estimate),
                eps_actual=COALESCE(target.eps_actual, source.eps_actual),
                revenue_estimate=COALESCE(target.revenue_estimate, source.revenue_estimate),
                revenue_actual=COALESCE(target.revenue_actual, source.revenue_actual),
                actual_source=COALESCE(target.actual_source, source.actual_source),
                actual_as_of=COALESCE(target.actual_as_of, source.actual_as_of),
                estimate_source=COALESCE(target.estimate_source, source.estimate_source),
                estimate_as_of=COALESCE(target.estimate_as_of, source.estimate_as_of),
                updated_at=NOW()
              FROM earnings AS source
             WHERE target.id=%s AND source.id=%s""",
            (target["id"], source["id"]),
        )
        cur.execute(
            """INSERT INTO earnings_estimate_snapshots
              (earning_id, source, captured_at, eps_estimate, revenue_estimate, payload)
              SELECT %s, source, captured_at, eps_estimate, revenue_estimate, payload
                FROM earnings_estimate_snapshots WHERE earning_id=%s
              ON CONFLICT (earning_id, source, captured_at) DO NOTHING""",
            (target["id"], source["id"]),
        )
        cur.execute("DELETE FROM earnings_estimate_snapshots WHERE earning_id=%s", (source["id"],))
        cur.execute("DELETE FROM earnings WHERE id=%s", (source["id"],))
    else:
        cur.execute(
            """UPDATE earnings SET report_date=%s, fiscal_year=%s, fiscal_quarter=%s,
                    updated_at=NOW()
               WHERE id=%s""",
            (match.canonical_date, match.fiscal_year, match.fiscal_quarter, source["id"]),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    matches, canonical_rows = collect_matches()
    with db_cursor() as cur:
        existing_keys = {(m.symbol, m.market, m.disclosure_date) for m in matches}
        matches.extend(legacy_mislabel_matches(cur, existing_keys, canonical_rows))
        plans = plan(cur, matches)
        if args.json:
            print(json.dumps({
                "matches": len(plans),
                "merge": sum(1 for p in plans if p["target"] and not p["conflict"]),
                "move": sum(1 for p in plans if not p["target"] and not p["conflict"]),
                "conflicts": sum(1 for p in plans if p["conflict"]),
                "plans": [{
                    "symbol": p["match"].symbol,
                    "market": p["match"].market,
                    "disclosure_date": p["match"].disclosure_date,
                    "canonical_date": p["match"].canonical_date,
                    "fiscal_year": p["match"].fiscal_year,
                    "fiscal_quarter": p["match"].fiscal_quarter,
                    "source_id": p["source"]["id"],
                    "target_id": p["target"]["id"] if p["target"] else None,
                    "conflict": p["conflict"],
                } for p in plans],
            }, default=str, ensure_ascii=False, indent=2))
        else:
            print(render(plans))
        if not args.apply:
            print("\ndry-run (read-only): use --apply after reviewing the plan")
            return 0
        conflicts = [p for p in plans if p["conflict"]]
        if conflicts:
            print(f"refusing to apply: {len(conflicts)} target conflicts require review", file=sys.stderr)
            return 2
        for item in plans:
            apply_plan(cur, item)
    print(f"applied {len(plans)} period-type repairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
