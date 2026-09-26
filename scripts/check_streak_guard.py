#!/usr/bin/env python
"""Read-only acceptance check for Issue #64: the EPS beat/miss streak.

The detail panel used to render "不及预期 3季" for ASML FY2026 Q2 while the very
same row's 较预期 was already "—（预期与实际币种不同）", because
``phase3.build_decision_metrics`` decided beat/miss with an unconditional
``actual > estimate``.  This script reproduces the issue's production scan over
the same universe window the calendar uses (``today-180d ~ today+30d``) and
reports the two numbers its acceptance criterion asks for:

* how many visible disclosed rows the row-level rule marks **non-comparable**;
* how many rows still produce a directional "连续 N季" — the subset that is
  non-comparable must be **0** after the fix.

Every statement it issues is a ``SELECT``; it never writes, and it needs no
credentials of its own (it runs wherever the app can reach the database):

    docker exec fincal python scripts/check_streak_guard.py
    python scripts/check_streak_guard.py --days 180 --forward 30 --json

Exit code 0 means the guard holds, 1 means at least one non-comparable row still
rendered a directional streak.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, fiscal  # noqa: E402
from app.earnings import fetch_earnings_from_db  # noqa: E402
from app.phase3 import build_decision_metrics  # noqa: E402
from app.universe import popular_stocks  # noqa: E402

#: The decision path's history projection — it must stay identical to the query
#: in ``app/routers/api.py`` (``test_the_acceptance_check_reads_what_the_endpoint_reads``
#: locks the two together), because a missing attribution column silently turns
#: the rule into ``currency_unknown``.
HISTORY_COLUMNS = (
    "id,fiscal_year,fiscal_quarter,report_date,eps_actual,revenue_actual,"
    "eps_estimate,estimate_currency,estimate_basis,estimate_source,"
    "actual_currency,actual_basis,actual_source"
)

#: Reasons that mean "this quarter could not be judged", i.e. it must not count.
_COMPARISON_CODES = (
    fiscal.COMPARISON_CURRENCY_UNKNOWN,
    fiscal.COMPARISON_CURRENCY_MISMATCH,
    fiscal.COMPARISON_BASIS_MISMATCH,
    fiscal.COMPARISON_BASIS_UNVERIFIED,
)


def symbol_history(history: dict, symbol: str, market: str) -> list:
    """The symbol's whole history, read once, with the decision projection."""
    key = (symbol, market)
    if key not in history:
        with db.db_cursor() as cur:
            cur.execute(
                f"SELECT {HISTORY_COLUMNS} FROM earnings"
                " WHERE symbol=%s AND market=%s ORDER BY report_date",
                (symbol, market),
            )
            history[key] = [dict(row) for row in cur.fetchall()]
    return history[key]


def scan(days: int = 180, forward: int = 30) -> dict:
    """Count the production rows and how many of them answer with a streak."""
    end = date.today() + timedelta(days=forward)
    start = date.today() - timedelta(days=days)
    universe_us, universe_hk = popular_stocks()
    symbols = list(dict.fromkeys(universe_us + universe_hk))
    rows = fetch_earnings_from_db(symbols=symbols, markets=["US", "HK"], start=start, end=end)

    report = {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "symbols": len(symbols)},
        "visible_rows": len(rows),
        "rows_with_eps_pair": 0,
        "rows_marked_non_comparable": 0,
        "directional_rows": 0,
        "non_comparable_rows_with_direction": 0,
        "directional_rows_ending_at_a_non_comparable_quarter": 0,
        "reason_counts": {},
        "examples": [],
    }
    history: dict = {}
    for row in rows:
        if row.get("eps_actual") is None or row.get("eps_estimate") is None:
            continue
        report["rows_with_eps_pair"] += 1
        reason = fiscal.comparison_unavailable_reason(row)
        if reason:
            report["rows_marked_non_comparable"] += 1
            report["reason_counts"][reason] = report["reason_counts"].get(reason, 0) + 1
        streak = build_decision_metrics(
            symbol_history(history, row["symbol"], row["market"]), row["id"]
        )["beat_miss_streak"]
        directional = streak["kind"] in ("beat", "miss") and bool(streak["count"])
        if not directional:
            continue
        report["directional_rows"] += 1
        if reason:
            # The defect: a row the panel itself calls non-comparable still
            # answers with a direction and a count.
            report["non_comparable_rows_with_direction"] += 1
            if len(report["examples"]) < 10:
                report["examples"].append({
                    "symbol": row["symbol"], "market": row["market"],
                    "fiscal_year": row.get("fiscal_year"), "fiscal_quarter": row.get("fiscal_quarter"),
                    "reason": reason, "streak": streak,
                })
        if streak.get("break_reason") in _COMPARISON_CODES:
            report["directional_rows_ending_at_a_non_comparable_quarter"] += 1
    report["guard_holds"] = report["non_comparable_rows_with_direction"] == 0
    return report


def _format(report: dict) -> str:
    lines = [
        f"window           : {report['window']['start']} ~ {report['window']['end']} "
        f"({report['window']['symbols']} symbols)",
        f"visible rows     : {report['visible_rows']}",
        f"  with EPS pair  : {report['rows_with_eps_pair']}",
        f"  non-comparable : {report['rows_marked_non_comparable']} {report['reason_counts']}",
        f"directional rows : {report['directional_rows']}",
        f"  non-comparable rows still showing a direction : "
        f"{report['non_comparable_rows_with_direction']} (must be 0)",
        f"  runs that ended at a non-comparable quarter   : "
        f"{report['directional_rows_ending_at_a_non_comparable_quarter']}",
        "guard : " + ("holds" if report["guard_holds"] else "VIOLATED"),
    ]
    for example in report["examples"]:
        lines.append(f"  e.g. {example['symbol']} FY{example['fiscal_year']} "
                     f"Q{example['fiscal_quarter']} ({example['reason']}): {example['streak']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Issue #64 acceptance check: the EPS beat/miss streak guard")
    parser.add_argument("--days", type=int, default=180, help="window lookback (default 180)")
    parser.add_argument("--forward", type=int, default=30, help="window lookahead (default 30)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    report = scan(days=args.days, forward=args.forward)
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2) if args.json else _format(report))
    return 0 if report["guard_holds"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
