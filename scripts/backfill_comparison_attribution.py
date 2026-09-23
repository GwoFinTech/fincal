#!/usr/bin/env python3
"""One-time attribution backfill for Issue #61.

``estimate_currency``/``estimate_basis`` never received a value and the actual side
had no columns at all (``actual_currency``/``actual_basis`` are new), so every row
in the table is unattributed and the read path can no longer claim that an estimate
and an actual are the same unit of money.  This script fills the four attribution
columns for rows that already exist, using only provider-declared values:

* **Longbridge pass** — the calendar event states the currency of the figures it
  carries (``currency``), so every row whose *estimate* came from the calendar
  (``estimate_source='longbridge'`` or unattributed) is labelled with it, and so is
  an actual that Longbridge itself wrote (``actual_source='longbridge'``, same
  event, same currency).
* **Futu pass** — OpenD states the *reporting* currency and accounting standard of
  each statement (``currency_code``/``accounting_standards``: TSM→TWD, BABA/PDD→CNY,
  00700.HK→CNY).  Rows whose actual was written by Futu (``actual_source='futu'``)
  are labelled from that response, paced through the same OpenD limiter the sync
  uses.  Anything OpenD does not declare is stored as the explicit ``unknown``
  marker — this script never assumes a listing's currency.

Rows whose actual has no attributed source at all (``actual_source IS NULL``, the
pre-#39 rows) are deliberately left unattributed: their value could be either
provider's, so no currency can be claimed for them.

Only the four attribution columns change — no numeric value is ever touched, and
only columns that are currently NULL are written.  The script is DATA-MUTATING and
refuses to run without ``--apply``; before writing it snapshots the affected rows
into ``earnings_attribution_backfill_backup`` so the change can be reverted by
restoring the backed-up keys.

Usage (dry-run first):
    python scripts/backfill_comparison_attribution.py
    python scripts/backfill_comparison_attribution.py --apply
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import db_cursor  # noqa: E402
from app.provenance import (  # noqa: E402
    UNKNOWN_ATTRIBUTION, normalize_basis, normalize_currency,
)
from app.symbol import from_lb_counter_id  # noqa: E402
import sync_earnings  # noqa: E402
import sync_futu  # noqa: E402

BACKUP_TABLE = "earnings_attribution_backfill_backup"
DEFAULT_DAYS_BACK = 180
DEFAULT_DAYS_FORWARD = 365


def _period_key(symbol, market, fiscal_year, fiscal_quarter):
    if fiscal_year in (None, "") or fiscal_quarter in (None, ""):
        return None
    try:
        return (symbol, market, int(fiscal_year), int(fiscal_quarter))
    except (TypeError, ValueError):
        return None


def longbridge_currency_map(days_back: int, days_forward: int) -> tuple[dict, dict]:
    """Return ``(by_date, by_period)`` currency maps from the live calendar.

    ``by_date`` is keyed by the provider's own announcement date, ``by_period`` by
    ``(symbol, market, fiscal_year, fiscal_quarter)`` — the persistent identity
    (Issue #50) — so a row that a second provider re-dated is still matched.
    """
    start = (date.today() - timedelta(days=days_back)).isoformat()
    end = (date.today() + timedelta(days=days_forward)).isoformat()
    by_date: dict[tuple, str] = {}
    by_period: dict[tuple, str] = {}
    for market in ("US", "HK"):
        pages = sync_earnings.fetch_calendar(market, start, end)
        for page in pages:
            for info in page.get("infos", []):
                symbol, mkt = from_lb_counter_id(info.get("counter_id", ""))
                if not symbol:
                    continue
                currency = normalize_currency(info.get("currency"))
                report_date = sync_earnings.parse_report_date(info.get("date", ""))
                if report_date:
                    by_date[(symbol, mkt, report_date)] = currency
                report = info.get("ext", {}).get("financial_report", {}) or {}
                try:
                    fiscal_year = int(report.get("fiscal_year"))
                    fiscal_quarter = int(report.get("period"))
                except (TypeError, ValueError):
                    continue
                if 1 <= fiscal_quarter <= 4:
                    by_period[(symbol, mkt, fiscal_year, fiscal_quarter)] = currency
        print(f"  {market}: {len(by_date)} date keys, {len(by_period)} period keys so far")
    return by_date, by_period


def load_rows(days_back: int, days_forward: int) -> list[dict]:
    start = date.today() - timedelta(days=days_back)
    end = date.today() + timedelta(days=days_forward)
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, symbol, market, report_date, fiscal_year, fiscal_quarter,
                      eps_estimate, eps_actual, revenue_estimate, revenue_actual,
                      estimate_source, actual_source,
                      estimate_currency, estimate_basis, actual_currency, actual_basis
               FROM earnings WHERE report_date BETWEEN %s AND %s
               ORDER BY symbol, report_date""",
            (start, end),
        )
        return [dict(r) for r in cur.fetchall()]


def futu_attribution(symbols: list[tuple[str, str]]) -> dict[tuple, tuple[str, str]]:
    """Ask OpenD for each symbol's statement currency/basis (``{(symbol, market): ...}``)."""
    from app import config

    ctx = sync_futu.create_futu_context() if hasattr(sync_futu, "create_futu_context") else None
    if ctx is None:
        print("  OpenD unavailable: Futu-sourced actuals stay unattributed")
        return {}

    limiter = sync_futu.get_rate_limiter()
    resolved: dict[tuple, tuple[str, str]] = {}
    try:
        for symbol, market in symbols:
            futu_code = f"HK.{symbol.replace('.HK', '')}" if market == "HK" else f"US.{symbol}"
            currency = basis = UNKNOWN_ATTRIBUTION
            for statement_type in (4, 1):
                limiter.acquire()
                try:
                    ret, data = ctx.get_financials_statements(
                        futu_code, statement_type=statement_type, financial_type=9, num=4,
                    )
                except Exception as exc:  # provider error must not abort the backfill
                    print(f"  {futu_code}: statement_type={statement_type} failed: {exc}")
                    continue
                if ret != 0 or not data.get("report_list"):
                    print(f"  {futu_code}: statement_type={statement_type} ret={ret} "
                          f"{str(data)[:80]}")
                    continue
                for report in data["report_list"]:
                    key = _period_key(symbol, market, report.get("fiscal_year"),
                                      sync_futu.F10_TO_QUARTER.get(report.get("financial_type")))
                    if key is None:
                        continue
                    period_currency = normalize_currency(report.get("currency_code"))
                    period_basis = normalize_basis(report.get("accounting_standards"))
                    # The EPS statement declares no standard while the income
                    # statement does, so merge both responses per period.
                    current = resolved.get(key, (UNKNOWN_ATTRIBUTION, UNKNOWN_ATTRIBUTION))
                    resolved[key] = (
                        period_currency if period_currency != UNKNOWN_ATTRIBUTION else current[0],
                        period_basis if period_basis != UNKNOWN_ATTRIBUTION else current[1],
                    )
    finally:
        ctx.close()
    print(f"  OpenD resolved {len(resolved)} fiscal periods for {len(symbols)} symbol(s)")
    return resolved


def build_plan(rows: list[dict], by_date: dict, by_period: dict, futu: dict) -> list[dict]:
    """Return the UPDATE plan: only NULL attribution columns, never a value field."""
    plan: list[dict] = []
    for row in rows:
        key = _period_key(row["symbol"], row["market"], row["fiscal_year"], row["fiscal_quarter"])
        date_key = (row["symbol"], row["market"], row["report_date"].isoformat()
                    if hasattr(row["report_date"], "isoformat") else str(row["report_date"]))
        calendar_currency = by_period.get(key) or by_date.get(date_key)
        updates: dict[str, str] = {}

        estimate_source = (row.get("estimate_source") or "").strip().lower()
        has_estimate = row.get("eps_estimate") is not None or row.get("revenue_estimate") is not None
        if (row.get("estimate_currency") is None and has_estimate
                and estimate_source in ("", "longbridge") and calendar_currency):
            updates["estimate_currency"] = calendar_currency
        if row.get("estimate_basis") is None and ("estimate_currency" in updates
                                                 or row.get("estimate_currency")):
            updates["estimate_basis"] = UNKNOWN_ATTRIBUTION

        actual_source = (row.get("actual_source") or "").strip().lower()
        has_actual = row.get("eps_actual") is not None or row.get("revenue_actual") is not None
        if row.get("actual_currency") is None and has_actual:
            if actual_source == "longbridge" and calendar_currency:
                updates["actual_currency"] = calendar_currency
            elif actual_source == "futu":
                provider_currency, provider_basis = futu.get(key, (None, None))
                if provider_currency:
                    updates["actual_currency"] = provider_currency
                    updates["actual_basis"] = provider_basis or UNKNOWN_ATTRIBUTION
        if updates:
            updates["id"] = row["id"]
            plan.append(updates)
    return plan


def _summary(plan: list[dict], rows: list[dict]) -> None:
    reasons: dict[str, int] = {}
    for row in rows:
        key = (row["symbol"], row["market"])
        source = f"{row.get('estimate_source') or 'unattributed'}/" \
                 f"{row.get('actual_source') or 'unattributed'}"
        if key:
            reasons[source] = reasons.get(source, 0) + 1
    print("  rows by (estimate_source/actual_source):",
          dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
    fields: dict[str, int] = {}
    for update in plan:
        for field in update:
            if field != "id":
                fields[field] = fields.get(field, 0) + 1
    print("  planned column writes:", fields)


def apply_plan(plan: list[dict]) -> int:
    """Snapshot the affected rows' attribution, then write the plan."""
    from psycopg2.extras import execute_values

    estimate_updates = [
        (u["id"], u.get("estimate_currency"), u.get("estimate_basis")) for u in plan
        if u.get("estimate_currency") or u.get("estimate_basis")
    ]
    actual_updates = [
        (u["id"], u.get("actual_currency"), u.get("actual_basis")) for u in plan
        if u.get("actual_currency") or u.get("actual_basis")
    ]
    ids = [update["id"] for update in plan]
    with db_cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {BACKUP_TABLE}")
        cur.execute(
            f"""CREATE TABLE {BACKUP_TABLE} AS
                SELECT id, estimate_currency, estimate_basis, actual_currency, actual_basis
                FROM earnings WHERE id = ANY(%s)""",
            (ids,),
        )
        cur.execute(f"SELECT COUNT(*) AS n FROM {BACKUP_TABLE}")
        backed_up = cur.fetchone()["n"]
        written = 0
        if estimate_updates:
            execute_values(
                cur,
                """UPDATE earnings AS e SET
                       estimate_currency = COALESCE(v.currency, e.estimate_currency),
                       estimate_basis = COALESCE(e.estimate_basis, v.basis)
                   FROM (VALUES %s) AS v(id, currency, basis)
                   WHERE e.id = v.id::int""",
                estimate_updates,
                page_size=500,
            )
            written += cur.rowcount
        if actual_updates:
            execute_values(
                cur,
                """UPDATE earnings AS e SET
                       actual_currency = COALESCE(v.currency, e.actual_currency),
                       actual_basis = COALESCE(e.actual_basis, v.basis)
                   FROM (VALUES %s) AS v(id, currency, basis)
                   WHERE e.id = v.id::int""",
                actual_updates,
                page_size=500,
            )
            written += cur.rowcount
    print(f"backfilled {len(plan)} rows ({written} column groups written; "
          f"snapshot backed up: {backed_up})")
    return len(plan)


def verify() -> None:
    with db_cursor() as cur:
        cur.execute(
            """SELECT count(*) AS rows_total,
                      count(estimate_currency) AS estimate_currency,
                      count(actual_currency) AS actual_currency
               FROM earnings"""
        )
        print("  attribution coverage:", dict(cur.fetchone()))


def main() -> int:
    apply_requested = "--apply" in sys.argv
    days_back = DEFAULT_DAYS_BACK
    days_forward = DEFAULT_DAYS_FORWARD
    if "--days-back" in sys.argv:
        days_back = int(sys.argv[sys.argv.index("--days-back") + 1])
    if "--days-forward" in sys.argv:
        days_forward = int(sys.argv[sys.argv.index("--days-forward") + 1])

    print(f"window: today-{days_back}d … today+{days_forward}d")
    print("Longbridge calendar pass (read-only upstream):")
    by_date, by_period = longbridge_currency_map(days_back, days_forward)
    rows = load_rows(days_back, days_forward)
    print(f"  {len(rows)} rows in window")

    futu_symbols = sorted({
        (row["symbol"], row["market"]) for row in rows
        if row.get("actual_currency") is None
        and (row.get("actual_source") or "").strip().lower() == "futu"
        and (row.get("eps_actual") is not None or row.get("revenue_actual") is not None)
    })
    print(f"Futu pass: {len(futu_symbols)} symbol(s) with a Futu-sourced actual")
    futu = futu_attribution(futu_symbols) if futu_symbols else {}

    plan = build_plan(rows, by_date, by_period, futu)
    _summary(plan, rows)

    if not apply_requested:
        print("\nDRY-RUN: no changes made. Re-run with --apply to write the attribution columns.")
        for update in plan[:10]:
            print("  sample:", update)
        return 0

    apply_plan(plan)
    print("\n--- attribution coverage after ---")
    verify()
    print(f"revert: rows can be restored from {BACKUP_TABLE} (attribution columns only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
