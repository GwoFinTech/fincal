"""Regression tests for Issue #38: a fixed idempotency key must not crash on
re-run after a terminal (success/failed/cancelled) state.

Root cause: ``sync_runs.idempotency_key`` had an *all-time unique* partial
index, so a scheduled sync reusing the same fixed key (``longbridge:earnings:full``
etc.) raised ``UniqueViolation`` on its second cron run, silently breaking the
whole pipeline.

Fix: the index is now unique only among ``status='running'`` rows, so terminal
rows no longer block a fresh attempt, while two concurrent running attempts are
still rejected. ``start_run`` also converts a residual concurrent conflict into
an "already running" skip (``None``) rather than a bare 500.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import sync_audit
from app.sync_audit import start_run


class _Cursor:
    """Records executed SQL and returns queued fetchone() results per call.

    ``raise_on``/``raise_at`` let a test force a failure on the Nth execute
    (0-based) to model a concurrent unique-index conflict.
    """

    def __init__(self, fetch_results=None, raise_on=None, raise_at=None):
        self._results = list(fetch_results or [])
        self._i = 0
        self.raise_on = raise_on
        self.raise_at = raise_at
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.raise_on is not None and self.raise_at == len(self.executed) - 1:
            raise self.raise_on

    def fetchone(self):
        if self._i >= len(self._results):
            return self._results[-1] if self._results else None
        res = self._results[self._i]
        self._i += 1
        return res

    def close(self):
        self.closed = True


def _patch_db_cursor(cursor):
    """Patch ``db.db_cursor`` with a context manager that does NOT suppress
    exceptions raised inside the ``with`` block (unlike a default MagicMock)."""
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return patch.object(sync_audit.db, "db_cursor", return_value=ctx)


# ── A running row with the same key ⇒ idempotent skip ────────────────

def test_start_run_skips_while_same_key_running():
    cursor = _Cursor(fetch_results=[{"id": 5, "status": "running"}])
    with _patch_db_cursor(cursor):
        rid = start_run("longbridge", "longbridge",
                        idempotency_key="longbridge:earnings:full")
    assert rid is None, "duplicate running key must be skipped (None)"


# ── Terminal state ⇒ a fresh attempt is created (the #38 fix) ──────────

def test_start_run_reruns_after_terminal_state():
    # No running row (fetch #1 -> None); MAX(attempt)=1 so next=2 (fetch #2);
    # INSERT returns id=99 (fetch #3).
    cursor = _Cursor(fetch_results=[None, {"next": 2}, {"id": 99}])
    with _patch_db_cursor(cursor):
        rid = start_run("longbridge", "longbridge",
                        idempotency_key="longbridge:earnings:full")
    assert rid == 99, "terminal state must allow a fresh attempt (new run_id)"
    insert_sql, insert_params = cursor.executed[-1]
    assert insert_sql.strip().startswith("INSERT INTO sync_runs")
    # params: (stage, source, symbol_count, now, attempt, key, timeout_seconds)
    assert insert_params[4] == 2, "attempt must be incremented across attempts"


# ── Concurrent running conflict ⇒ skip, no 500 ────────────────────────

def test_start_run_returns_none_on_concurrent_unique_violation():
    uv = psycopg2.errors.UniqueViolation(
        'duplicate key value violates unique constraint "idx_sync_runs_idempotency_key"'
    )
    # raise on the 3rd execute (the INSERT), after the running/attempt probes.
    cursor = _Cursor(fetch_results=[None, {"next": 2}], raise_on=uv, raise_at=2)
    with _patch_db_cursor(cursor):
        rid = start_run("longbridge", "longbridge",
                        idempotency_key="longbridge:earnings:full")
    assert rid is None, "concurrent unique violation must become a graceful skip (None)"


# ── No idempotency key ⇒ always a fresh attempt at attempt=1 ───────────

def test_start_run_without_key_uses_attempt_one():
    cursor = _Cursor(fetch_results=[{"id": 7}])
    with _patch_db_cursor(cursor):
        rid = start_run("futu", "futu")
    assert rid == 7
    insert_sql, insert_params = cursor.executed[-1]
    assert insert_sql.strip().startswith("INSERT INTO sync_runs")
    assert insert_params[4] == 1, "no key ⇒ attempt defaults to 1"


# ── The index migration is converged on "running-only" uniqueness ─────

def test_migration_index_is_running_scoped():
    """The idempotency index must be unique only among status='running' rows.

    This guards the root-cause DDL in ``app/db.py``: the old all-time unique
    index would break today's otherwise-correct ``start_run`` behaviour, so the
    migration SQL must carry the running-scoped predicate.
    """
    from app import db as db_mod
    src = Path(db_mod.__file__).read_text(encoding="utf-8")
    assert "DROP INDEX IF EXISTS idx_sync_runs_idempotency_key" in src, (
        "must drop the legacy all-time unique index"
    )
    assert "AND status='running'" in src, (
        "index must be unique only among running rows"
    )
