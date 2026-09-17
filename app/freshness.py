"""Read-only freshness validation for sync stages and derived data (Issue #53).

FinCal's pipeline is declared by ``scripts/sync_all.sh`` (reached through the
single cron entrypoint ``scripts/cron_sync.sh``) and runs **weekly**, yet the
only observability was dependency reachability (``/api/admin/health``) plus a
fixed 24-hour run window (``/api/admin/diagnostics``).  A stage could therefore
stop running for weeks — production had ``consensus`` idle since 2026-08-02 and
``stock_names`` since 2026-08-03 — while every interface kept reporting healthy,
and the calendar kept rendering a 6-week-old consensus snapshot with no "stale"
marker.

This module answers one question with **SELECT-only** queries and **no external
calls**: "when did each declared stage last succeed, and how old is the data it
produces?"  The declared stage list is the contract that ``sync_all.sh`` must
cover; ``tests/test_sync_freshness.py`` fails if the two drift apart, so a stage
added to the pipeline can never silently go unmonitored again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import config, db

logger = logging.getLogger(__name__)

# ── Declared pipeline (must match scripts/sync_all.sh) ─────────────────────
# Each entry is a ``sync_runs.stage`` value written by the script on the right.
SYNC_STAGES: tuple[str, ...] = (
    "longbridge",
    "futu",
    "stock_names",
    "consensus",
    "prediction",
)

STAGE_SCRIPTS: dict[str, str] = {
    "longbridge": "scripts/sync_earnings.py",
    "futu": "scripts/sync_futu.py",
    "stock_names": "scripts/sync_stock_names.py",
    "consensus": "scripts/sync_consensus.py",
    "prediction": "scripts/predict_earnings.py",
}

# Derived tables that are rendered to users, with their freshness column.
# Identifiers are module constants (never user input), so interpolating them
# into the UNION query below is safe.
DERIVED_TABLES: tuple[tuple[str, str], ...] = (
    ("earnings_consensus", "fetched_at"),
    ("earnings_forecast_eps", "fetched_at"),
    ("earnings_institution_ratings", "fetched_at"),
    ("stock_names", "fetched_at"),
    ("earnings", "updated_at"),
)

# 8 days = weekly schedule + 1 day of grace (matches the default of
# SYNC_STAGE_STALE_AFTER_HOURS).
DEFAULT_STALE_AFTER_HOURS = 192.0

STATUS_FRESH = "fresh"
STATUS_STALE = "stale"
STATUS_NEVER = "never"
STATUS_UNKNOWN = "unknown"

ERROR_STAGE_STALE = "sync_stage_stale"
ERROR_STAGE_NEVER = "sync_stage_never_run"
ERROR_DATA_STALE = "sync_data_stale"
ERROR_DATA_MISSING = "sync_data_never_fetched"
ERROR_UNAVAILABLE = "sync_freshness_unavailable"

# Keys of the aggregate-only view that is safe on an unauthenticated endpoint
# (stage names, statuses and an error code — no SQL, timestamps or credentials).
AGGREGATE_KEYS = (
    "status",
    "error_code",
    "threshold_hours",
    "stale_stages",
    "never_run_stages",
    "stale_data",
    "checked_at",
)

_STAGE_QUERY = """
SELECT stage, MAX(COALESCE(finished_at, started_at)) AS last_success_at
FROM sync_runs
WHERE status = 'success' AND stage = ANY(%s)
GROUP BY stage
"""


def _derived_query() -> str:
    """One SELECT (UNION ALL) covering every derived table's freshness column."""
    return " UNION ALL ".join(
        f"SELECT '{table}' AS name, MAX({column}) AS last_at FROM {table}"
        for table, column in DERIVED_TABLES
    )


def stale_after_hours() -> float:
    """Configured staleness threshold in hours (invalid/missing → default)."""
    raw = getattr(config, "SYNC_STAGE_STALE_AFTER_HOURS", DEFAULT_STALE_AFTER_HOURS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("invalid SYNC_STAGE_STALE_AFTER_HOURS=%r, using default", raw)
        return DEFAULT_STALE_AFTER_HOURS
    return value if value > 0 else DEFAULT_STALE_AFTER_HOURS


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a timestamp to aware UTC (naive values are read as UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _entry(name: str, kind: str, last_at: datetime | None, now: datetime,
           threshold_hours: float) -> dict:
    last_at = _as_utc(last_at)
    if last_at is None:
        status = STATUS_NEVER
        age_hours = None
        error_code = ERROR_STAGE_NEVER if kind == "stage" else ERROR_DATA_MISSING
    else:
        age_hours = round((now - last_at).total_seconds() / 3600.0, 2)
        status = STATUS_STALE if age_hours > threshold_hours else STATUS_FRESH
        if status == STATUS_STALE:
            error_code = ERROR_STAGE_STALE if kind == "stage" else ERROR_DATA_STALE
        else:
            error_code = None
    return {
        "stage": name,
        "kind": kind,
        "last_success_at": last_at.isoformat() if last_at else None,
        "age_hours": age_hours,
        "status": status,
        "error_code": error_code,
    }


def _unavailable(now: datetime, threshold_hours: float) -> dict:
    return {
        "status": STATUS_UNKNOWN,
        "error_code": ERROR_UNAVAILABLE,
        "threshold_hours": threshold_hours,
        "checked_at": now.isoformat(),
        "stale_stages": [],
        "never_run_stages": [],
        "stale_data": [],
        "entries": [],
    }


def check_freshness(*, now: datetime | None = None,
                    threshold_hours: float | None = None) -> dict:
    """Read-only freshness summary of every declared stage and derived table.

    ``fresh``/``stale``/``never`` are decided from the **most recent success**
    per stage, never from a fixed time window, so a weekly pipeline is still
    judged correctly on the six days with no run in the last 24 hours.

    Returns a dict with ``status`` (``healthy`` / ``degraded`` / ``unknown``),
    ``error_code`` (language-neutral, for the API contract), the threshold, the
    aggregate name lists and per-entry details.  Runs only ``SELECT`` statements
    and never probes Futu / Longbridge / Kurumi.
    """
    now = _as_utc(now) or datetime.now(timezone.utc)
    threshold = stale_after_hours() if threshold_hours is None else float(threshold_hours)

    try:
        with db.db_cursor() as cur:
            cur.execute(_STAGE_QUERY, (list(SYNC_STAGES),))
            stage_rows = {row["stage"]: row["last_success_at"] for row in cur.fetchall()}
            cur.execute(_derived_query())
            derived_rows = {row["name"]: row["last_at"] for row in cur.fetchall()}
    except Exception as exc:  # unreadable DB: report unknown, never crash /health
        logger.warning("sync freshness check unavailable: %s", type(exc).__name__)
        return _unavailable(now, threshold)

    entries = [
        _entry(stage, "stage", stage_rows.get(stage), now, threshold)
        for stage in SYNC_STAGES
    ]
    entries += [
        _entry(table, "derived", derived_rows.get(table), now, threshold)
        for table, _ in DERIVED_TABLES
    ]

    stale_stages = [e["stage"] for e in entries
                    if e["kind"] == "stage" and e["status"] == STATUS_STALE]
    never_run_stages = [e["stage"] for e in entries
                        if e["kind"] == "stage" and e["status"] == STATUS_NEVER]
    stale_data = [e["stage"] for e in entries
                  if e["kind"] == "derived" and e["status"] != STATUS_FRESH]

    if never_run_stages:
        error_code = ERROR_STAGE_NEVER
    elif stale_stages:
        error_code = ERROR_STAGE_STALE
    elif stale_data:
        error_code = ERROR_DATA_STALE
    else:
        error_code = None

    return {
        "status": "healthy" if error_code is None else "degraded",
        "error_code": error_code,
        "threshold_hours": threshold,
        "checked_at": now.isoformat(),
        "stale_stages": stale_stages,
        "never_run_stages": never_run_stages,
        "stale_data": stale_data,
        "entries": entries,
    }


def health_snapshot(summary: dict | None = None) -> dict:
    """Aggregate-only projection for the unauthenticated ``/api/admin/health``.

    Deliberately drops the per-entry timestamps and ages: the endpoint has no
    application-level auth, so it exposes stage names and statuses only.
    """
    summary = check_freshness() if summary is None else summary
    return {key: summary.get(key) for key in AGGREGATE_KEYS}
