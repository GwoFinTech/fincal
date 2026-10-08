#!/usr/bin/env python3
"""Safely repair legacy fiscal labels without deleting earnings rows (Issue #75).

The Longbridge parser is now sequence-aware, but old rows keep their original
non-null fiscal identity. This tool only changes ``fiscal_year`` and
``fiscal_quarter`` after a read-only plan has been reviewed. It never deletes or
merges rows; duplicate identities are reported for the separate Issue #50
reconciliation workflow.

Usage::

    DB_HOST=localhost python scripts/reconcile_fiscal_labels.py --json
    DB_HOST=localhost python scripts/reconcile_fiscal_labels.py --apply --json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor  # noqa: E402
from sync_earnings import (  # noqa: E402
    _VALID_PERIOD_TYPES,
    _raw_fiscal_period,
    fetch_calendar,
    from_lb_counter_id,
    parse_report_date,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BACKUP_TABLE = "earnings_fiscal_label_reconcile_backup"
RELEASE_MATCH_DAYS = 14


@dataclass(frozen=True)
class Release:
    symbol: str
    market: str
    fiscal_year: int
    fiscal_quarter: int
    report_date: str


@dataclass(frozen=True)
class Proposal:
    source_id: int
    symbol: str
    market: str
    report_date: str
    before_fiscal_year: int
    before_fiscal_quarter: int
    after_fiscal_year: int
    after_fiscal_quarter: int
    reason: str
    target_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict:
        result = asdict(self)
        result["target_ids"] = list(self.target_ids)
        return result


def _iso(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _distance(left: str, right: str) -> int:
    return abs((date.fromisoformat(left) - date.fromisoformat(right)).days)


def release_events(pages: Iterable[dict]) -> list[Release]:
    """Extract only canonical qf/3q events from Longbridge pages."""
    result: dict[tuple, Release] = {}
    for page in pages:
        for info in page.get("infos", []):
            symbol, market = from_lb_counter_id(info.get("counter_id", ""))
            report_date = parse_report_date(info.get("date", ""))
            if not symbol or not market or not report_date:
                continue
            ext = info.get("ext", {}).get("financial_report", {})
            fiscal_year, fiscal_quarter, period_type = _raw_fiscal_period(
                ext, report_date, market
            )
            if period_type not in _VALID_PERIOD_TYPES:
                continue
            if fiscal_year is None or fiscal_quarter is None:
                continue
            item = Release(symbol, market, int(fiscal_year), int(fiscal_quarter), report_date)
            result[(symbol, market, item.fiscal_year, item.fiscal_quarter, report_date)] = item
    return sorted(result.values(), key=lambda item: (item.market, item.symbol, item.report_date))


def _nearest_release(row: dict, releases: list[Release]) -> tuple[Release, int] | None:
    candidates = [
        item for item in releases
        if item.symbol == row["symbol"]
        and item.market == row["market"]
        and _distance(_iso(row["report_date"]), item.report_date) <= RELEASE_MATCH_DAYS
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda item: (_distance(_iso(row["report_date"]), item.report_date), item.report_date))
    return best, _distance(_iso(row["report_date"]), best.report_date)


def _bracketed_q2(row: dict, releases: list[Release]) -> tuple[int, int] | None:
    """Infer an old Q4 label as Q2 when Q1 is before and Q3 is after it."""
    if int(row["fiscal_quarter"]) != 4:
        return None
    same = [
        item for item in releases
        if item.symbol == row["symbol"] and item.market == row["market"]
        and item.fiscal_year == int(row["fiscal_year"])
    ]
    q1 = [item for item in same if item.fiscal_quarter == 1 and item.report_date < _iso(row["report_date"])]
    q3 = [item for item in same if item.fiscal_quarter == 3 and item.report_date > _iso(row["report_date"])]
    if q1 and q3:
        return int(row["fiscal_year"]), 2
    return None


def inconsistent_group_keys(rows: list[dict]) -> set[tuple[str, str, int]]:
    groups: dict[tuple[str, str, int], list[dict]] = {}
    for row in rows:
        if row.get("is_predicted") or row.get("fiscal_year") is None or row.get("fiscal_quarter") is None:
            continue
        key = (row["symbol"], row["market"], int(row["fiscal_year"]))
        groups.setdefault(key, []).append(row)
    affected = set()
    for key, group in groups.items():
        for left in group:
            for right in group:
                if int(left["fiscal_quarter"]) > int(right["fiscal_quarter"]):
                    if _iso(left["report_date"]) < _iso(right["report_date"]):
                        affected.add(key)
                        break
            if key in affected:
                break
    return affected


def build_proposals(rows: list[dict], releases: list[Release]) -> list[Proposal]:
    """Build deterministic label-only changes for affected symbol/year groups."""
    affected_groups = inconsistent_group_keys(rows)
    proposals: list[Proposal] = []
    for row in rows:
        if row.get("is_predicted") or row.get("date_source") != "longbridge":
            continue
        before_year = row.get("fiscal_year")
        before_quarter = row.get("fiscal_quarter")
        if before_year is None or before_quarter is None:
            continue
        if (row["symbol"], row["market"], int(before_year)) not in affected_groups:
            continue
        after: tuple[int, int] | None = None
        reason = ""
        nearest = _nearest_release(row, releases)
        if nearest:
            release, distance = nearest
            if (int(before_year), int(before_quarter)) != (release.fiscal_year, release.fiscal_quarter):
                after = (release.fiscal_year, release.fiscal_quarter)
                reason = f"qf/3q release {release.report_date} is {distance}d from row date"
        if after is None:
            bracketed = _bracketed_q2(row, releases)
            if bracketed and (int(before_year), int(before_quarter)) != bracketed:
                after = bracketed
                reason = "legacy Q4 lies between the same-year Q1 and Q3 release dates"
        if after is None:
            continue
        proposals.append(Proposal(
            source_id=int(row["id"]),
            symbol=row["symbol"],
            market=row["market"],
            report_date=_iso(row["report_date"]),
            before_fiscal_year=int(before_year),
            before_fiscal_quarter=int(before_quarter),
            after_fiscal_year=after[0],
            after_fiscal_quarter=after[1],
            reason=reason,
        ))
    return sorted(proposals, key=lambda item: (item.market, item.symbol, item.report_date, item.source_id))


def load_rows(cur) -> list[dict]:
    cur.execute(
        """SELECT id, symbol, market, fiscal_year, fiscal_quarter, report_date,
                  date_source, is_predicted, eps_actual, revenue_actual,
                  eps_estimate, revenue_estimate, actual_source, estimate_source
             FROM earnings
            WHERE is_predicted=FALSE
              AND fiscal_year IS NOT NULL
              AND fiscal_quarter IS NOT NULL
            ORDER BY market, symbol, report_date, id"""
    )
    return [dict(row) for row in cur.fetchall()]


def attach_targets(cur, proposals: list[Proposal]) -> list[Proposal]:
    result = []
    for proposal in proposals:
        cur.execute(
            """SELECT id FROM earnings
                WHERE symbol=%s AND market=%s AND fiscal_year=%s AND fiscal_quarter=%s
                  AND is_predicted=FALSE AND id<>%s
                ORDER BY id""",
            (proposal.symbol, proposal.market, proposal.after_fiscal_year,
             proposal.after_fiscal_quarter, proposal.source_id),
        )
        target_ids = tuple(int(row["id"]) for row in cur.fetchall())
        result.append(Proposal(**{**asdict(proposal), "target_ids": target_ids}))
    return result


def invariant_count(cur) -> int:
    cur.execute(
        """SELECT count(*) AS total FROM earnings e
            WHERE e.is_predicted=FALSE
              AND EXISTS (
                SELECT 1 FROM earnings p
                 WHERE p.symbol=e.symbol AND p.market=e.market
                   AND p.fiscal_year=e.fiscal_year
                   AND p.is_predicted=FALSE
                   AND p.fiscal_quarter < e.fiscal_quarter
                   AND p.report_date > e.report_date
              )"""
    )
    return int(cur.fetchone()["total"])


def apply_proposals(cur, proposals: list[Proposal]) -> tuple[int, int]:
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            earning_id INTEGER NOT NULL,
            before_data JSONB NOT NULL,
            after_fiscal_year INTEGER NOT NULL,
            after_fiscal_quarter INTEGER NOT NULL,
            reason TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""
    )
    applied = 0
    skipped = 0
    for proposal in proposals:
        cur.execute("SELECT to_jsonb(e) AS row_data FROM earnings e WHERE e.id=%s FOR UPDATE", (proposal.source_id,))
        current = cur.fetchone()
        if not current:
            skipped += 1
            continue
        cur.execute(
            f"""INSERT INTO {BACKUP_TABLE}
                (earning_id, before_data, after_fiscal_year, after_fiscal_quarter, reason)
                VALUES (%s,%s::jsonb,%s,%s,%s)""",
            (proposal.source_id, json.dumps(current["row_data"], default=str), proposal.after_fiscal_year,
             proposal.after_fiscal_quarter, proposal.reason),
        )
        cur.execute(
            """UPDATE earnings
                  SET fiscal_year=%s, fiscal_quarter=%s, updated_at=NOW()
                WHERE id=%s""",
            (proposal.after_fiscal_year, proposal.after_fiscal_quarter, proposal.source_id),
        )
        applied += cur.rowcount
    return applied, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    pages = []
    for market in ("US", "HK"):
        pages.extend(fetch_calendar(market, "2025-01-01", "2027-12-31"))
    releases = release_events(pages)
    with db_cursor() as cur:
        rows = load_rows(cur)
        proposals = attach_targets(cur, build_proposals(rows, releases))
        before_inconsistent = invariant_count(cur)
        report = {
            "release_events": len(releases),
            "proposals": len(proposals),
            "target_conflicts": sum(bool(item.target_ids) for item in proposals),
            "before_inconsistent": before_inconsistent,
            "rows": [item.as_dict() for item in proposals],
        }
        if not args.apply:
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False, indent=2))
            return 0
        applied, skipped = apply_proposals(cur, proposals)
    with db_cursor() as cur:
        after_inconsistent = invariant_count(cur)
    report.update({"applied": applied, "skipped": skipped, "after_inconsistent": after_inconsistent})
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False, indent=2))
    return 0 if skipped == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
