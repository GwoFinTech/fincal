"""Failure-safe audit API for earnings sync scripts.

Supports heartbeat, stale recovery, idempotency keys, checkpoint/progress,
and timeout reaping — issues #3 and #5.
"""
from __future__ import annotations

import hashlib
import logging
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from . import db

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 3600  # 1 hour


class SyncCancelledError(Exception):
    """Raised by sync scripts when the run was cancelled by an admin.

    Lets long-running scripts stop at the next checkpoint instead of burning
    external API quota and then overwriting the terminal 'cancelled' state
    (Issue #28).
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Idempotency key helpers ────────────────────────────────────────

def make_idempotency_key(*parts: str) -> str:
    """Build a stable, deterministic idempotency key from parts."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Start / finish ─────────────────────────────────────────────────

def start_run(
    stage: str,
    source: str,
    *,
    symbol_count: int = 0,
    idempotency_key: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> int | None:
    """Create a new sync run. Returns the run ID, or None if a duplicate
    idempotency key already has a running/success entry.

    When ``idempotency_key`` is set, a new attempt is created only if no
    existing running entry exists for the same key.
    """
    now = _utcnow()
    try:
        with db.db_cursor() as cur:
            if idempotency_key:
                cur.execute(
                    "SELECT id, status FROM sync_runs WHERE idempotency_key=%s AND status='running'",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing:
                    logger.warning(
                        "idempotent skip: key=%s existing_run=%s status=%s",
                        idempotency_key, existing["id"], existing["status"],
                    )
                    return None
                # Count previous attempts
                cur.execute(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM sync_runs WHERE idempotency_key=%s",
                    (idempotency_key,),
                )
                attempt = cur.fetchone()["next"]
            else:
                attempt = 1

            cur.execute(
                """INSERT INTO sync_runs
                   (stage, status, source, symbol_count, heartbeat_at, attempt,
                    idempotency_key, timeout_seconds)
                   VALUES (%s, 'running', %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (stage, source, symbol_count, now, attempt, idempotency_key, timeout_seconds),
            )
            run_id = cur.fetchone()["id"]
            logger.info("sync run started: id=%d stage=%s attempt=%d key=%s",
                         run_id, stage, attempt, idempotency_key)
            return run_id
    except psycopg2.errors.UniqueViolation:
        # A concurrent attempt for the same key inserted a *running* row
        # between our running-check and our INSERT. The partial unique index
        # (status='running', Issue #38) rejects the duplicate. Treat it as
        # "already running" and skip rather than surfacing a bare 500. Terminal
        # rows for the same key no longer conflict, so scheduled re-runs (the
        # original #38 failure) create a fresh attempt instead of crashing.
        logger.warning("idempotent skip (concurrent running): key=%s", idempotency_key)
        return None


def finish_run(
    run_id: int,
    *,
    status: str,
    record_count: int = 0,
    details: dict | None = None,
    error_code: str | None = None,
) -> bool:
    """Transition a running sync run to a terminal state.

    Only succeeds while the run is still ``'running'`` — a run that was
    cancelled (or otherwise already transitioned) by an admin is never
    overwritten back to ``success``/``failed`` (Issue #28).

    Returns ``True`` if the row transitioned, ``False`` if the run was already
    in a terminal state and was left untouched.
    """
    with db.db_cursor() as cur:
        cur.execute(
            """UPDATE sync_runs
               SET status=%s, record_count=%s, details=%s, error_code=%s,
                   finished_at=%s, heartbeat_at=%s
               WHERE id=%s AND status='running'""",
            (status, record_count, db.psycopg2.extras.Json(details or {}),
             error_code, _utcnow(), _utcnow(), run_id),
        )
        return cur.rowcount > 0


# ── Cancellation awareness (Issue #28) ──────────────────────────────

def is_cancelled(run_id: int) -> bool:
    """Return ``True`` when the run is no longer in a 'running' state.

    Sync scripts poll this at checkpoints so an admin ``cancel`` takes effect at
    the next checkpoint instead of after the whole job has run.
    """
    with db.db_cursor() as cur:
        cur.execute("SELECT status FROM sync_runs WHERE id=%s", (run_id,))
        row = cur.fetchone()
        if row is None:
            return True
        return row["status"] != "running"


def check_cancelled(run_id: int) -> None:
    """Raise :class:`SyncCancelledError` if the run is no longer running."""
    if is_cancelled(run_id):
        raise SyncCancelledError(f"sync run {run_id} cancelled")


# ── Heartbeat ──────────────────────────────────────────────────────

def heartbeat(run_id: int, *, phase: str | None = None,
              current: int | None = None, total: int | None = None) -> None:
    """Update heartbeat timestamp and optional progress fields."""
    with db.db_cursor() as cur:
        sets = ["heartbeat_at=%s"]
        params: list = [_utcnow()]
        if phase is not None:
            sets.append("phase=%s")
            params.append(phase)
        if current is not None:
            sets.append("current=%s")
            params.append(current)
        if total is not None:
            sets.append("total=%s")
            params.append(total)
        params.append(run_id)
        cur.execute(f"UPDATE sync_runs SET {', '.join(sets)} WHERE id=%s", params)


# ── Checkpoint (for resume) ────────────────────────────────────────

def save_checkpoint(run_id: int, *, phase: str, current: int, total: int) -> None:
    """Persist progress so interrupted runs can resume from this point."""
    heartbeat(run_id, phase=phase, current=current, total=total)


def get_checkpoint(run_id: int) -> dict | None:
    """Return the last checkpoint for a run, or None."""
    with db.db_cursor() as cur:
        cur.execute(
            "SELECT phase, current, total FROM sync_runs WHERE id=%s",
            (run_id,),
        )
        row = cur.fetchone()
        if row and row["current"] is not None:
            return dict(row)
        return None


# ── Stale run recovery (Issue #3) ──────────────────────────────────

def recover_stale_runs(timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> int:
    """Mark stale 'running' tasks as 'interrupted'.

    Called at application startup. A run is stale if:
    - status = 'running'
    - heartbeat_at is older than timeout_seconds (or NULL)
    - started_at is older than timeout_seconds

    Returns the number of runs marked as interrupted.
    """
    cutoff = _utcnow() - timedelta(seconds=timeout_seconds)
    with db.db_cursor() as cur:
        cur.execute(
            """UPDATE sync_runs
               SET status = 'interrupted',
                   finished_at = NOW(),
                   error_code = 'interrupted_stale_on_startup',
                   details = details || '{"recovered_by": "startup"}'::jsonb
               WHERE status = 'running'
                 AND (heartbeat_at IS NULL AND started_at < %s
                      OR heartbeat_at < %s)""",
            (cutoff, cutoff),
        )
        count = cur.rowcount
        if count:
            logger.info("recovered %d stale sync runs on startup", count)
        return count


def reap_timeout_runs(timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> int:
    """Background reaper: mark timed-out running tasks.

    Should be called periodically (e.g. every 5 minutes).
    Returns the number of runs reaped.
    """
    cutoff = _utcnow() - timedelta(seconds=timeout_seconds)
    with db.db_cursor() as cur:
        cur.execute(
            """UPDATE sync_runs
               SET status = 'interrupted',
                   finished_at = NOW(),
                   error_code = 'timeout_reaper',
                   details = details || '{"recovered_by": "reaper"}'::jsonb
               WHERE status = 'running'
                 AND heartbeat_at < %s""",
            (cutoff,),
        )
        count = cur.rowcount
        if count:
            logger.info("reaper interrupted %d timed-out sync runs", count)
        return count


# ── Advisory lock (Issue #8 / #31) ─────────────────────────────────

@contextmanager
def advisory_lock(lock_key: int, *, timeout: float = 0) -> Iterator[bool]:
    """Acquire a PostgreSQL advisory lock, held for the whole context.

    The lock is acquired and released on the *same* dedicated (non-pooled)
    connection. PostgreSQL advisory locks are session-scoped, so a release must
    run on the session that acquired it. The old pool-based helper used
    ``db_cursor`` for both halves, which could return a different connection for
    each call — ``pg_advisory_unlock`` then landed on a session that never held
    the lock and silently failed, leaking the lock (Issue #31).

    ``timeout=0`` is non-blocking (``pg_try_advisory_lock``). ``timeout>0``
    waits up to ``timeout`` seconds (``SET lock_timeout`` + ``pg_advisory_lock``).

    Yields ``True`` when the lock was acquired, ``False`` otherwise. The lock is
    released automatically when the context exits, including on abnormal exit.
    """
    with db.db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if timeout > 0:
                cur.execute("SET lock_timeout = %s", (int(timeout * 1000),))
                try:
                    cur.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
                    acquired = True
                except Exception:
                    acquired = False
            else:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
                row = cur.fetchone()
                acquired = bool(row and row.get("pg_try_advisory_lock"))
        try:
            yield acquired
        finally:
            if acquired:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


# Lock keys for different sync stages (must be stable, unique per resource)
LOCK_LONGBRIDGE_EARNINGS = 1001
LOCK_FUTU_EARNINGS = 1002
LOCK_CONSENSUS = 1003
LOCK_STOCK_NAMES = 1004
LOCK_PREDICTION = 1005

def request_cancel(run_id: int) -> bool:
    """Mark a running task as cancelled. Returns True if transitioned."""
    with db.db_cursor() as cur:
        cur.execute(
            """UPDATE sync_runs
               SET status = 'cancelled', finished_at = NOW(),
                   error_code = 'cancelled_by_admin'
               WHERE id = %s AND status = 'running'""",
            (run_id,),
        )
        return cur.rowcount > 0
