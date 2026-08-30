"""Regression tests for Issue #28: admin cancel is honoured by sync scripts
and ``finish_run`` never overwrites a terminal (cancelled) state.

Covers:
- ``finish_run`` only transitions a run still in 'running' state
- ``is_cancelled`` / ``check_cancelled`` status polling
- ``sync_earnings`` stops at a checkpoint when the run is cancelled
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import sync_audit
from app.sync_audit import SyncCancelledError

SPEC = importlib.util.spec_from_file_location(
    "sync_earnings", ROOT / "scripts" / "sync_earnings.py"
)
sync_earnings = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_earnings)


class _FakeCursor:
    """Records executed SQL and returns a configurable status/rowcount."""

    def __init__(self, rowcount=0, status=None):
        self.rowcount = rowcount
        self.status = status
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if self.status is None:
            return None
        return {"status": self.status}

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _patch_db_cursor(cursor):
    """Patch ``db.db_cursor`` used by sync_audit with a fake context manager."""
    ctx = MagicMock()
    ctx.__enter__.return_value = cursor
    return patch.object(sync_audit.db, "db_cursor", return_value=ctx)


# ── finish_run must not overwrite a terminal state ───────────────────

def test_finish_run_does_not_overwrite_cancelled_run():
    """A cancelled run must not be flipped back to success/failed."""
    cursor = _FakeCursor(rowcount=0, status=None)
    with _patch_db_cursor(cursor):
        transitioned = sync_audit.finish_run(
            99, status="success", record_count=5, error_code=None
        )
    # rowcount == 0 -> the row was NOT running so nothing was written.
    assert transitioned is False, "cancelled run was overwritten"
    sql = cursor.executed[0][0]
    assert "AND status='running'" in sql, "finish_run must guard on status='running'"
    # It must NOT actually set status to success on a terminal run.
    assert "status=%s" in sql


def test_finish_run_updates_running_run():
    """A normal running run should still transition (rowcount==1 -> True)."""
    cursor = _FakeCursor(rowcount=1, status=None)
    with _patch_db_cursor(cursor):
        transitioned = sync_audit.finish_run(
            7, status="success", record_count=3, details={"ok": True}
        )
    assert transitioned is True
    assert cursor.executed[0][0].strip().startswith("UPDATE sync_runs")


# ── is_cancelled / check_cancelled ──────────────────────────────────

def test_is_cancelled_true_when_status_cancelled():
    cursor = _FakeCursor(status="cancelled")
    with _patch_db_cursor(cursor):
        assert sync_audit.is_cancelled(10) is True


def test_is_cancelled_false_when_running():
    cursor = _FakeCursor(status="running")
    with _patch_db_cursor(cursor):
        assert sync_audit.is_cancelled(10) is False


def test_is_cancelled_true_when_run_missing():
    cursor = _FakeCursor(status=None)
    with _patch_db_cursor(cursor):
        assert sync_audit.is_cancelled(404) is True


def test_check_cancelled_raises_when_cancelled():
    cursor = _FakeCursor(status="cancelled")
    with _patch_db_cursor(cursor):
        try:
            sync_audit.check_cancelled(10)
        except SyncCancelledError:
            return
    raise AssertionError("check_cancelled did not raise SyncCancelledError")


def test_check_cancelled_passes_when_running():
    cursor = _FakeCursor(status="running")
    with _patch_db_cursor(cursor):
        sync_audit.check_cancelled(10)  # must not raise


# ── sync_earnings honours cancel at checkpoints ─────────────────────

def test_sync_earnings_stops_at_checkpoint_when_cancelled():
    """When check_cancelled raises, the sync aborts (SyncCancelledError)."""
    with patch.object(sync_earnings, "check_cancelled", side_effect=SyncCancelledError("cancelled")):
        try:
            sync_earnings.sync_earnings(42)
        except SyncCancelledError:
            return
    raise AssertionError("sync_earnings did not stop on cancel")


def test_sync_earnings_polls_cancellation_during_run():
    """The script must poll check_cancelled at each market/page checkpoint."""
    calls = []

    def fake_check(run_id):
        calls.append(run_id)

    with patch.object(sync_earnings, "check_cancelled", side_effect=fake_check):
        with patch.object(sync_earnings, "fetch_calendar", return_value=[{"infos": []}]):
            with patch.object(sync_earnings, "db_cursor") as db_cursor_mock:
                db_cursor_mock.return_value.__enter__.return_value = MagicMock()
                total = sync_earnings.sync_earnings(42)

    assert total == 0
    # At least one poll per market (US, HK) plus one per page (4 total here).
    assert len(calls) >= 3, f"expected checkpoint polls, got {len(calls)}"
    assert all(run_id == 42 for run_id in calls), f"unexpected run_id in polls: {calls}"
