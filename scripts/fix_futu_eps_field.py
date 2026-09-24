#!/usr/bin/env python3
"""One-time retraction of the wrong Futu ``eps_actual`` values (Issue #62).

Before the field fix in ``scripts/sync_futu.py`` the actuals stage read "EPS"
from a field id of the *key-metrics* statement (``statement_type=4``), where that
id is 流动比率 (current ratio).  Every row whose ``eps_actual`` came from Futu is
therefore a liquidity ratio, not an EPS — production measured 312 rows over 76
symbols, e.g. OKLO 59.932626 and TSLA 1.940946 rendered as "actual EPS", against
OpenD's 基本每股收益 of -0.19 and 0.34 for the same fiscal quarters.

Re-reading the same field cannot repair those values, so the script:

1. **snapshots** every affected row (``eps_actual``, estimate, provenance) into
   ``earnings_eps_field_fix_backup``, so the change is auditable and reversible;
2. **retracts** them — ``eps_actual = NULL`` plus
   ``actual_source = 'futu_invalid_field'`` (``app.provenance.RETRACTED_ACTUAL_SOURCE``).
   The row no longer presents a number as its actual EPS while keeping a
   machine-readable reason.  ``revenue_actual``, ``date_status`` and
   ``actual_as_of`` are untouched: the revenue field was correct (acceptance 4);
3. **refills** through the corrected pipeline: ``sync_futu.sync_actuals`` runs for
   exactly the affected symbols under its own ``sync_runs`` row and the Futu
   advisory lock, so the real 基本每股收益 is written under the normal source
   precedence and ``actual_source`` returns to ``futu`` wherever OpenD answers.
   Symbols OpenD no longer answers for stay retracted — and stay *replaceable*,
   which is why ``futu_invalid_field`` is part of
   ``app.provenance.REPLACEABLE_ACTUAL_SOURCES``.

Only ``--apply`` writes.  ``--verify`` is a read-only production check that
compares every Futu-sourced ``eps_actual`` with the provider's own
基本每股收益 (acceptance 1): exit ``0`` when every row matches, ``1`` on a real
difference, ``2`` when the check could not cover every row (OpenD down, or the
provider refused a symbol) — a quota rejection is reported as unverified, never
as a data difference.

Usage::

    python scripts/fix_futu_eps_field.py                    # dry run (read-only)
    python scripts/fix_futu_eps_field.py --verify            # read-only OpenD check
    python scripts/fix_futu_eps_field.py --apply             # retract + refill
    python scripts/fix_futu_eps_field.py --apply --no-refill # retract only
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from app.db import db_cursor, init_db  # noqa: E402
from app.provenance import RETRACTED_ACTUAL_SOURCE  # noqa: E402
from app.sync_audit import (  # noqa: E402
    LOCK_FUTU_EARNINGS,
    advisory_lock,
    finish_run,
    start_run,
)
import sync_futu  # noqa: E402

#: Rows poisoned by the wrong field id: a Futu-written actual EPS.
AFFECTED_PREDICATE = "actual_source = 'futu' AND eps_actual IS NOT NULL"

BACKUP_TABLE = "earnings_eps_field_fix_backup"

#: Audit identity of the refill run.  Deliberately *not* the declared ``futu``
#: stage: this run covers only the retracted symbols, so it must not refresh the
#: weekly stage's freshness window.
REFILL_STAGE = "futu_eps_field_fix"
REFILL_KEY = "futu:eps-field-fix"

#: Floating-point slack for the DB ↔ provider comparison (numeric column).
EPS_TOLERANCE = 1e-6

_COLUMNS = ("id, symbol, market, fiscal_year, fiscal_quarter, report_date, "
            "eps_actual, eps_estimate, actual_source, actual_as_of")


def load_affected() -> list[dict]:
    """Rows whose ``eps_actual`` was written by Futu.

    Before the fix these are the poisoned rows; after it, this is the set that
    ``--verify`` checks against the provider.
    """
    with db_cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM earnings WHERE {AFFECTED_PREDICATE}"
            " ORDER BY symbol, fiscal_year, fiscal_quarter"
        )
        return [dict(row) for row in cur.fetchall()]


def affected_symbols(rows: list[dict]) -> list[str]:
    """Watchlist spelling of the symbols to refill (``AAPL`` → ``AAPL.US``)."""
    codes = {
        f"{row['symbol']}.US" if row.get("market") == "US" else row["symbol"]
        for row in rows
    }
    return sorted(codes)


def retract(ids: list[int]) -> tuple[int, int]:
    """Snapshot then void ``ids`` in one transaction. Returns (backed up, voided)."""
    with db_cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {BACKUP_TABLE}")
        cur.execute(
            f"CREATE TABLE {BACKUP_TABLE} AS SELECT {_COLUMNS} FROM earnings"
            " WHERE id = ANY(%s)",
            (ids,),
        )
        cur.execute(f"SELECT COUNT(*) AS n FROM {BACKUP_TABLE}")
        backed_up = cur.fetchone()["n"]
        cur.execute(
            f"""UPDATE earnings SET eps_actual = NULL, actual_source = %s,
                                    updated_at = NOW()
                WHERE id = ANY(%s) AND {AFFECTED_PREDICATE}""",
            (RETRACTED_ACTUAL_SOURCE, ids),
        )
        voided = cur.rowcount
    return backed_up, voided


def statement_eps_map(payload: dict) -> dict[tuple[int, int], float]:
    """Read 基本每股收益 per fiscal period from one OpenD statement response.

    Uses the same field ids and label check as the sync, so a fixture that would
    be refused there is not treated as a reference value here either.
    """
    resolved: dict[tuple[int, int], float] = {}
    for report in (payload or {}).get("report_list", []):
        quarter = sync_futu.F10_TO_QUARTER.get(report.get("financial_type"))
        year = report.get("fiscal_year")
        if not quarter or not year:
            continue
        field = sync_futu.read_statement_field(report, sync_futu.FUTU_EPS_FIELD_IDS)
        if field.value is None:
            continue
        resolved[(int(year), int(quarter))] = field.value
    return resolved


def compare_rows(rows: list[dict],
                 provider: dict[tuple[str, str], dict],
                 unreadable: tuple[tuple[str, str], ...] = ()) -> tuple[list[dict], list[dict], list[dict]]:
    """Split ``rows`` into (matched, mismatched, unverified) against the provider.

    ``provider`` maps ``(symbol, market)`` to the fiscal-period map returned by
    :func:`statement_eps_map`.  A symbol listed in ``unreadable`` (OpenD refused
    or errored) is *unverified*, never a mismatch — a quota rejection must not be
    presented as a data difference.  A period the provider did report but without
    an EPS figure is a mismatch: the stored value cannot be confirmed.
    """
    matched: list[dict] = []
    mismatched: list[dict] = []
    unverified: list[dict] = []
    for row in rows:
        key = (row["symbol"], row["market"])
        if key in unreadable:
            unverified.append({**row, "reason": "provider_unavailable_for_symbol"})
            continue
        periods = provider.get(key, {})
        try:
            period = (int(row["fiscal_year"]), int(row["fiscal_quarter"]))
        except (TypeError, ValueError):
            mismatched.append({**row, "reason": "period_unknown"})
            continue
        expected = periods.get(period)
        if expected is None:
            mismatched.append({**row, "reason": "provider_has_no_eps_for_period"})
        elif abs(float(row["eps_actual"]) - float(expected)) <= EPS_TOLERANCE:
            matched.append({**row, "provider_eps": expected})
        else:
            mismatched.append({**row, "provider_eps": expected, "reason": "value_differs"})
    return matched, mismatched, unverified


def fetch_provider_eps(symbols: list[str]) -> tuple[dict[tuple[str, str], dict], list[tuple[str, str]]]:
    """OpenD income statements for ``symbols`` → ``eps_actual`` reference values.

    Returns ``(provider, unreadable)`` where ``unreadable`` lists the symbols
    OpenD refused or errored for.  Read-only, and paced/retried through the same
    :func:`sync_futu.futu_call` helper the sync uses, so a quota rejection is
    reported as unverified instead of being read as a missing EPS figure.
    """
    if not symbols:
        return {}, []
    ctx = sync_futu.create_futu_context()
    if ctx is None:
        print("  OpenD unavailable — the provider comparison cannot be made")
        return {}, []
    limiter = sync_futu.get_rate_limiter()
    stats = sync_futu.FutuStageStats()
    provider: dict[tuple[str, str], dict] = {}
    unreadable: list[tuple[str, str]] = []
    try:
        for source_symbol in symbols:
            symbol, market = sync_futu.canonical_earnings_symbol(source_symbol)
            futu_code = sync_futu.to_futu_code(source_symbol)
            outcome, data = sync_futu.futu_call(
                futu_code, "VerifyEPS",
                lambda: ctx.get_financials_statements(
                    futu_code, statement_type=sync_futu.FUTU_STATEMENT_INCOME,
                    financial_type=9, num=4,
                ),
                limiter, stats,
                timeout_seconds=sync_futu.config.FUTU_ACTUALS_TIMEOUT_SECONDS,
            )
            if outcome != sync_futu.OUTCOME_OK:
                unreadable.append((symbol, market))
                continue
            provider[(symbol, market)] = statement_eps_map(data)
    finally:
        ctx.close()
    return provider, unreadable


def refill(symbols: list[str]):
    """Re-read the corrected field for ``symbols`` through the real sync stage."""
    if not symbols:
        print("  nothing to refill")
        return None
    ctx = sync_futu.create_futu_context()
    if ctx is None:
        print("  OpenD unavailable — refill skipped; retracted rows stay replaceable")
        return None
    run_id = start_run(REFILL_STAGE, "futu", symbol_count=len(symbols),
                       idempotency_key=REFILL_KEY)
    if run_id is None:
        ctx.close()
        print("  a refill run is already in progress — skipped")
        return None
    try:
        stats = sync_futu.sync_actuals(ctx, run_id, symbols)
    except Exception:
        finish_run(run_id, status="failed", error_code="eps_field_refill_failed")
        raise
    finally:
        ctx.close()
    status, error_code = sync_futu.futu_audit_outcome(stats)
    finish_run(
        run_id, status=status, record_count=stats.total, error_code=error_code,
        details={
            "issue": 62,
            "symbols_requested": len(symbols),
            "actual_symbols": stats.total,
            "actual_failed_symbols": stats.failed_symbols,
            "unsupported_symbols": stats.unsupported_symbols,
            "rate_limited_symbols": stats.rate_limited_symbols,
        },
    )
    print(f"  refill run {run_id}: status={status} symbols={stats.total} "
          f"failed={stats.failed_symbols} unsupported={stats.unsupported_symbols}")
    return stats


def state() -> dict:
    """Current retraction/refill coverage of the EPS actual column."""
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT
                    count(*) FILTER (WHERE {AFFECTED_PREDICATE}) AS futu_eps_rows,
                    count(*) FILTER (WHERE actual_source = %s) AS retracted_rows,
                    count(*) FILTER (WHERE actual_source = 'futu'
                                       AND revenue_actual IS NOT NULL) AS futu_revenue_rows,
                    count(*) FILTER (WHERE date_status = 'reported') AS reported_rows
                FROM earnings""",
            (RETRACTED_ACTUAL_SOURCE,),
        )
        return dict(cur.fetchone())


def verify(rows: list[dict]) -> int:
    """Read-only comparison of the DB's Futu EPS against OpenD (acceptance 1).

    Exit codes: ``0`` every row equals the provider's 基本每股收益, ``1`` at least
    one row differs, ``2`` the comparison could not cover every row (OpenD
    unavailable or refused some symbols) — a partial read is never a pass.
    """
    provider, unreadable = fetch_provider_eps(affected_symbols(rows))
    if not provider and not unreadable:
        print("VERIFY: no provider values — result unknown")
        return 2
    matched, mismatched, unverified = compare_rows(rows, provider, tuple(unreadable))
    print(f"VERIFY: {len(matched)} row(s) match 基本每股收益, "
          f"{len(mismatched)} mismatch(es), {len(unverified)} unverified")
    for row in mismatched[:10]:
        print("  MISMATCH", row["symbol"], row["market"], row["fiscal_year"],
              row["fiscal_quarter"], "db=", row["eps_actual"],
              "provider=", row.get("provider_eps"), row["reason"])
    if len(mismatched) > 10:
        print(f"  … {len(mismatched) - 10} more mismatch(es) not shown")
    for symbol, market in unreadable:
        print("  UNVERIFIED (provider refused):", symbol, market)
    if mismatched:
        return 1
    return 0 if not unverified else 2


def main() -> int:
    args = sys.argv[1:]
    apply_requested = "--apply" in args
    do_refill = "--no-refill" not in args

    init_db()
    rows = load_affected()
    symbols = affected_symbols(rows)
    print(f"rows with a Futu-written eps_actual: {len(rows)} "
          f"({len(symbols)} symbol(s)); retracted marker: {RETRACTED_ACTUAL_SOURCE}")

    if "--verify" in args:
        return verify(rows)

    print("current state:", state())
    if not apply_requested:
        print("\nDRY-RUN: no changes made. Re-run with --apply to retract and refill.")
        for row in rows[:5]:
            print("  sample:", row["symbol"], row["market"], row["fiscal_year"],
                  row["fiscal_quarter"], "eps_actual=", row["eps_actual"],
                  "eps_estimate=", row["eps_estimate"])
        return 0

    with advisory_lock(LOCK_FUTU_EARNINGS) as acquired:
        if not acquired:
            print("ABORT: another Futu sync holds the lock")
            return 1

        backed_up, voided = retract([row["id"] for row in rows])
        print(f"retracted {voided} row(s); snapshot backed up: {backed_up} "
              f"(table {BACKUP_TABLE})")

        if do_refill:
            refill(symbols)
        else:
            print("refill skipped (--no-refill)")

    print("state after:", state())
    print("next: python scripts/fix_futu_eps_field.py --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
