"""Regression tests for the advisory lock held on a single DB session (Issue #31).

Background: the old ``advisory_lock``/``advisory_unlock`` helpers acquired and
released the lock via ``db.db_cursor()``, which borrows a connection from the
pool — the release could land on a *different* session than the one that
acquired it. Because PostgreSQL advisory locks are session-scoped, that unlock
silently failed and leaked the lock (e.g. a sync script aborts and the lock is
never released).

The fix: ``advisory_lock`` is now a context manager that borrows a single
dedicated (non-pooled) connection via ``db.db_connection()`` and performs both
the acquire and the release on that same connection.

These tests exercise the real context manager (no ``db_cursor``), asserting:
- the release happens on the same connection as the acquire,
- it is also released on abnormal exit,
- ``db_cursor`` (the pooled path) is never involved,
- a failed acquire does not attempt an unlock.
"""

from unittest.mock import patch

from app import sync_audit
from app.sync_audit import advisory_lock


class _FakeCursor:
    """Records executed SQL; fetchone returns a dict (RealDictCursor parity)."""

    def __init__(self, conn):
        self.conn = conn
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return None

    def fetchone(self):
        return {"pg_try_advisory_lock": self.conn.try_result}


class _FakeConn:
    """One 'session'. Each connection has a distinct identity."""

    def __init__(self, try_result=True):
        self.id = object()
        self.try_result = try_result
        self.cursors = []
        self.closed = False

    def cursor(self, cursor_factory=None):
        cur = _FakeCursor(self)
        self.cursors.append(cur)
        return cur

    def close(self):
        self.closed = True


class _FakeConnCtx:
    """Context manager standing in for ``db.db_connection()``."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()
        return False


def _unlock_cursors(conn):
    return [c for c in conn.cursors if any("pg_advisory_unlock" in s for s, _ in c.executed)]


def _acquire_cursors(conn, keyword):
    return [c for c in conn.cursors if any(keyword in s for s, _ in c.executed)]


def test_advisory_lock_releases_on_same_session_that_acquired():
    conn = _FakeConn(try_result=True)
    with patch.object(sync_audit.db, "db_cursor") as mock_cursor, \
         patch.object(sync_audit.db, "db_connection", return_value=_FakeConnCtx(conn)):
        with advisory_lock(1001) as acquired:
            assert acquired is True
            # lock was taken via the dedicated connection
            assert _acquire_cursors(conn, "pg_try_advisory_lock")
    # the release must be on the very same connection object (session)
    unlocks = _unlock_cursors(conn)
    assert len(unlocks) == 1, "must release the lock exactly once"
    assert unlocks[0].conn is conn, "release must run on the acquiring session"
    assert conn.closed is True, "dedicated connection must be closed"
    # the pooled cursor path must never have been involved
    mock_cursor.assert_not_called()


def test_advisory_lock_releases_on_abnormal_exit():
    conn = _FakeConn(try_result=True)
    with patch.object(sync_audit.db, "db_cursor") as mock_cursor, \
         patch.object(sync_audit.db, "db_connection", return_value=_FakeConnCtx(conn)):
        try:
            with advisory_lock(1001) as acquired:
                assert acquired is True
                raise RuntimeError("sync script died mid-flight")
        except RuntimeError:
            pass
    unlocks = _unlock_cursors(conn)
    assert len(unlocks) == 1, "lock must be released even on abnormal exit"
    assert unlocks[0].conn is conn
    assert conn.closed is True
    mock_cursor.assert_not_called()


def test_advisory_lock_not_acquired_does_not_unlock():
    conn = _FakeConn(try_result=False)  # pg_try_advisory_lock -> False
    with patch.object(sync_audit.db, "db_cursor") as mock_cursor, \
         patch.object(sync_audit.db, "db_connection", return_value=_FakeConnCtx(conn)):
        with advisory_lock(1001) as acquired:
            assert acquired is False
    assert _unlock_cursors(conn) == [], "no unlock needed when the lock was not acquired"
    assert conn.closed is True
    mock_cursor.assert_not_called()


def test_advisory_lock_timeout_waits_then_unlocks_same_session():
    conn = _FakeConn(try_result=True)
    with patch.object(sync_audit.db, "db_cursor") as mock_cursor, \
         patch.object(sync_audit.db, "db_connection", return_value=_FakeConnCtx(conn)):
        with advisory_lock(1001, timeout=5.0) as acquired:
            assert acquired is True
    all_sql = [s for c in conn.cursors for s, _ in c.executed]
    assert any("lock_timeout" in s for s in all_sql)
    assert any("pg_advisory_lock" in s for s in all_sql)
    unlocks = _unlock_cursors(conn)
    assert len(unlocks) == 1
    assert unlocks[0].conn is conn
    assert conn.closed is True


def test_sync_scripts_no_longer_reference_manual_advisory_unlock():
    """Guard the caller migration: the scripts must rely on the context manager
    instead of a manual ``advisory_unlock()`` call that can run on a different
    connection (Issue #31)."""
    from pathlib import Path
    scripts_dir = Path(sync_audit.__file__).resolve().parent.parent / "scripts"
    for name in ("sync_earnings.py", "sync_futu.py"):
        src = (scripts_dir / name).read_text(encoding="utf-8")
        assert "advisory_unlock" not in src, (
            f"{name} must not call advisory_unlock directly; use the "
            "advisory_lock(..) context manager"
        )
        assert "with advisory_lock(" in src, (
            f"{name} must acquire the lock via the advisory_lock context manager"
        )
