"""A sync stage must actually hold its advisory lock for the whole stage.

Background: ``advisory_lock`` borrows one dedicated connection for the stage (the
Issues #31 fix, so acquire and release land on the same session) — but the
connection is *not* in autocommit, so the first ``execute`` leaves it "idle in
transaction". Production PostgreSQL runs with
``idle_in_transaction_session_timeout = 15000``, so the server kills that session
15 seconds in and its session-level advisory lock goes with it:

* every stage silently lost its mutual exclusion ~15 s after acquiring it, while
  the process still believed it held the lock (a second run would then be let in —
  only the idempotency key stood in the way);
* the unlock at the end raised ``server closed the connection unexpectedly``
  *after* ``finish_run`` had recorded a success, so a good stage exited non-zero
  (measured on an 11-minute Futu history backfill, 2026-11-12).

Probed against the production server: a non-autocommit connection was dead by
+45 s, an autocommit one still answered at +160 s and released its lock normally.
"""
import sys
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from app.sync_audit import advisory_lock  # noqa: E402


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append(sql)
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")

    def fetchone(self):
        return {"pg_try_advisory_lock": self.conn.try_result}


class _Conn:
    def __init__(self, try_result=True, fail_on=None):
        self.autocommit = False
        self.try_result = try_result
        self.fail_on = fail_on
        self.statements = []

    def cursor(self, **kwargs):
        return _Cursor(self)

    def close(self):
        pass


def _use(conn):
    """Patch ``db.db_connection`` to yield the given fake connection."""
    from app import db

    class _Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    return mock.patch.object(db, "db_connection", lambda *a, **k: _Ctx())


class LockSessionTests(TestCase):
    def test_the_lock_session_is_in_autocommit(self):
        """Idle-in-transaction is what killed the session (and the lock) at 15 s."""
        conn = _Conn()
        with _use(conn):
            with advisory_lock(1002) as acquired:
                self.assertTrue(acquired)
                self.assertTrue(
                    conn.autocommit,
                    "the lock connection must be autocommit, or the server's "
                    "idle_in_transaction_session_timeout drops it mid-stage",
                )

    def test_autocommit_is_set_before_the_lock_is_taken(self):
        """Setting it afterwards would already have opened a transaction."""
        conn = _Conn()
        seen = {}
        original_execute = _Cursor.execute

        def execute(self, sql, params=None):
            seen.setdefault("autocommit_at_first_statement", self.conn.autocommit)
            return original_execute(self, sql, params)

        with _use(conn), mock.patch.object(_Cursor, "execute", execute):
            with advisory_lock(1002):
                pass
        self.assertTrue(seen["autocommit_at_first_statement"])

    def test_a_dead_session_does_not_fail_a_completed_stage(self):
        """The unlock failure after a success must be a warning, not an exception."""
        conn = _Conn(fail_on="pg_advisory_unlock")
        with _use(conn):
            with advisory_lock(1002) as acquired:
                self.assertTrue(acquired)
            # Reaching here without an exception is the assertion: finish_run has
            # already recorded the stage, and its exit code must stay 0.
        self.assertIn("SELECT pg_advisory_unlock(%s)", conn.statements)

    def test_a_busy_lock_is_still_reported_as_not_acquired(self):
        conn = _Conn(try_result=False)
        with _use(conn):
            with advisory_lock(1002) as acquired:
                self.assertFalse(acquired)
        self.assertNotIn("SELECT pg_advisory_unlock(%s)", conn.statements)

    def test_the_production_setting_that_caused_this_is_named(self):
        """Guard the diagnosis: a raised idle-in-transaction timeout re-opens it."""
        source = (ROOT / "app" / "sync_audit.py").read_text(encoding="utf-8")
        self.assertIn("conn.autocommit = True", source)
        self.assertIn("idle_in_transaction_session_timeout", source)
