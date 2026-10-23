"""Regression tests for Issue #77: the admin retry endpoint must not report a
retry that nothing will execute.

``POST /api/admin/sync-runs/{id}/retry`` used to insert a ``status='running'``
row whose idempotency key (``<stage key>:retry:<id>``) no code in the repository
ever read. The endpoint answered ``200 {"status": "retry_created"}`` — a fake
success: no stage ran, the calendar was not refreshed, and the ghost row later
made every further retry of the same run fail with ``retry_already_running``
until the timeout reaper recorded the non-event as ``interrupted``.

A retry is now recorded as a request that is **terminal from the start**
(``status='skipped'``, ``phase='retry_requested'``) and is fulfilled by the
scheduled round that runs that stage (``scripts/cron_sync.sh`` is the only
allowed executor, Issue #57).

Covers:
- the endpoint writes no ``running`` row and answers 202 with the reason code
  ``retry_requires_cron``;
- a second retry of the same run is refused because a request really is pending,
  and is allowed again once that request was fulfilled;
- a stage the pipeline never schedules is refused instead of being promised;
- ``start_run`` fulfils the pending request of its own stage;
- against real PostgreSQL (opt-in, ``FINCAL_TEST_DB=1``): the request row, the
  refusal and the fulfilment round-trip on the production ``sync_runs`` shape.
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

import pytest

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, db, sync_audit  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

ADMIN_SOURCE = (ROOT / "app" / "routers" / "admin.py").read_text()
AUDIT_SOURCE = (ROOT / "app" / "sync_audit.py").read_text()


# ── A database stand-in that records every statement ───────────────────────

class FakeCursor:
    """Serves the handful of statements the retry path issues.

    ``run`` is what ``SELECT ... FROM sync_runs WHERE id=%s`` returns,
    ``pending_request`` what the pending-request lookup returns.
    """

    def __init__(self, *, run=None, pending_request=None, inserted_id=77,
                 consumed=0):
        self.run = run
        self.pending_request = pending_request
        self.inserted_id = inserted_id
        self.consumed = consumed
        self.executed: list[tuple[str, object]] = []
        self.rowcount = 0
        self._one = None

    # assertions helpers
    def statements(self, needle: str) -> list[str]:
        return [sql for sql, _ in self.executed if needle in sql]

    def parameters(self, needle: str) -> list:
        return [params for sql, params in self.executed if needle in sql]

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        self._one = None
        self.rowcount = 0
        if flat.startswith("SELECT") and "FROM sync_runs WHERE id=%s" in flat:
            self._one = self.run
        elif flat.startswith("SELECT") and "status='skipped' AND phase=%s" in flat:
            self._one = self.pending_request
        elif "MAX(attempt)" in flat:
            self._one = {"next": 1}
        elif flat.startswith("INSERT INTO sync_runs"):
            self._one = {"id": self.inserted_id}
        elif flat.startswith("UPDATE sync_runs"):
            self.rowcount = self.consumed

    def fetchone(self):
        return self._one

    def fetchall(self):
        return [] if self._one is None else [self._one]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    """Context manager standing in for ``db.db_cursor()``."""

    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc):
        return False

    def cursor(self, **kwargs):
        return self._cursor


def _patch_db(cursor):
    return patch.object(db, "db_cursor", lambda: _Conn(cursor))


def _refuse_real_database(*args, **kwargs):
    raise AssertionError(
        "this test reached a real database: patch db.db_cursor. A test that does "
        "not is not a test — the fincal DB on localhost is the production one, "
        "and the first version of this file wrote three sync_runs rows into it."
    )


class _NoRealDatabase(TestCase):
    """Every DB access in this module must be faked.

    ``db.db_cursor`` defaults to the *production* fincal database on localhost,
    which is exactly what the sync pipeline writes to. Guarding it turns an
    accidental unpatched call into a failure instead of a production write.
    """

    def setUp(self):
        self._db_guard = patch.object(db, "db_cursor", _refuse_real_database)
        self._db_guard.start()
        self.addCleanup(self._db_guard.stop)


@contextmanager
def _admin_client():
    fastapi_app.dependency_overrides = {
        get_current_user: lambda: {"id": 1, "email": "ops@example.com",
                                   "name": "Ops", "role": "admin"},
    }
    try:
        yield TestClient(fastapi_app, raise_server_exceptions=False)
    finally:
        fastapi_app.dependency_overrides = {}


def _failed_run(stage="futu", source="futu", symbol_count=5):
    return {"stage": stage, "source": source, "symbol_count": symbol_count,
            "status": "failed"}


# ── The endpoint no longer fakes a run ────────────────────────────────────

class TheEndpointRecordsARequestInsteadOfGivingUpATokenTests(_NoRealDatabase):
    """`retry_created` + a `running` row was the whole defect."""

    def test_the_running_row_and_its_orphan_key_are_gone(self):
        """The old shape must not survive anywhere in the endpoint."""
        self.assertNotIn(":retry:", ADMIN_SOURCE,
                         "the executor-less `:retry:<id>` key is back")
        self.assertNotIn("retry_already_running", ADMIN_SOURCE)
        self.assertNotIn('"status": "retry_created"', ADMIN_SOURCE)
        # …and the endpoint must not start a run of its own at all: that is the
        # only way it could create a row nobody executes.
        retry_body = ADMIN_SOURCE.split("def retry_sync_run")[1]
        self.assertNotIn("start_run(", retry_body)

    def test_the_request_is_recorded_terminal_from_the_start(self):
        cursor = FakeCursor(run=_failed_run())
        with _admin_client() as client, _patch_db(cursor):
            resp = client.post("/api/admin/sync-runs/113/retry")

        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "retry_scheduled")
        self.assertEqual(body["original_run_id"], 113)
        self.assertEqual(body["request_id"], 77)
        self.assertEqual(body["stage"], "futu")
        self.assertEqual(body["executed_by"], "scripts/cron_sync.sh")
        self.assertEqual(body["reason_code"], sync_audit.RETRY_REASON_CODE)

        inserts = cursor.statements("INSERT INTO sync_runs")
        self.assertEqual(len(inserts), 1, cursor.executed)
        self.assertIn("'skipped'", inserts[0])
        self.assertNotIn("'running'", inserts[0])
        params = cursor.parameters("INSERT INTO sync_runs")[0]
        self.assertIn(sync_audit.RETRY_REQUEST_PHASE, params)
        self.assertIn(sync_audit.retry_request_key(113), params)
        details = [p for p in params if isinstance(p, db.psycopg2.extras.Json)]
        self.assertTrue(details, params)
        self.assertEqual(details[0].adapted["retry_requested_for"], "113")

    def test_no_running_row_is_written_by_any_statement(self):
        cursor = FakeCursor(run=_failed_run())
        with _admin_client() as client, _patch_db(cursor):
            client.post("/api/admin/sync-runs/113/retry")
        for sql, params in cursor.executed:
            self.assertNotIn("status='running'", sql.replace("%s", ""),
                             f"a running row is being written: {sql} {params}")


# ── The refusal must be about a request that really is pending ─────────────

class SecondRetryTests(_NoRealDatabase):
    def test_a_pending_request_refuses_the_second_retry(self):
        pending = {"id": 12, "details": {"retry_requested_for": "113"}}
        cursor = FakeCursor(run=_failed_run(), pending_request=pending)
        with _patch_db(cursor):
            self.assertIsNone(
                sync_audit.request_retry(113, "futu", "futu"),
                "a pending request must not be silently duplicated",
            )
        self.assertFalse(cursor.statements("INSERT INTO sync_runs"), cursor.executed)

    def test_the_endpoint_reports_the_real_reason(self):
        pending = {"id": 12, "details": {"retry_requested_for": "113"}}
        cursor = FakeCursor(run=_failed_run(), pending_request=pending)
        with _admin_client() as client, _patch_db(cursor):
            resp = client.post("/api/admin/sync-runs/113/retry")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "retry_already_requested")

    def test_a_fulfilled_request_does_not_block_a_new_one(self):
        """Once the stage really re-ran, asking again is legitimate."""
        fulfilled = {"id": 12, "details": {"retry_requested_for": "113",
                                           sync_audit.RETRY_SATISFIED_BY: 120}}
        cursor = FakeCursor(run=_failed_run(), pending_request=fulfilled,
                            inserted_id=131)
        with _patch_db(cursor):
            request_id = sync_audit.request_retry(113, "futu", "futu")
        self.assertEqual(request_id, 131)
        self.assertEqual(len(cursor.statements("INSERT INTO sync_runs")), 1)

    def test_a_refused_stage_is_never_promised_an_execution(self):
        """`futu_eps_field_fix` is not in the scheduled pipeline."""
        cursor = FakeCursor(run=_failed_run(stage="futu_eps_field_fix"))
        with _admin_client() as client, _patch_db(cursor):
            resp = client.post("/api/admin/sync-runs/58/retry")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "retry_stage_not_scheduled")
        self.assertFalse(cursor.statements("INSERT INTO sync_runs"), cursor.executed)


class ExistingGuardTests(_NoRealDatabase):
    def test_an_unknown_run_is_a_404(self):
        cursor = FakeCursor(run=None)
        with _admin_client() as client, _patch_db(cursor):
            resp = client.post("/api/admin/sync-runs/999/retry")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_a_successful_run_is_not_retryable(self):
        cursor = FakeCursor(run={**_failed_run(), "status": "success"})
        with _admin_client() as client, _patch_db(cursor):
            resp = client.post("/api/admin/sync-runs/120/retry")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "run_not_retryable")


# ── Fulfilment: the run that starts afterwards is the one that ran ─────────

class FulfilmentTests(_NoRealDatabase):
    def test_start_run_is_the_call_that_fulfils_a_request(self):
        """The fulfilment hook must stay wired into `start_run`."""
        self.assertIn("consume_retry_requests(stage, run_id)", AUDIT_SOURCE)

    def test_start_run_fulfils_the_requests_of_its_own_stage(self):
        cursor = FakeCursor(run=None, inserted_id=121, consumed=1)
        with _patch_db(cursor):
            run_id = sync_audit.start_run("futu", "futu", symbol_count=5,
                                          idempotency_key="futu:earnings:full",
                                          timeout_seconds=config.stage_timeout("futu"))
        self.assertEqual(run_id, 121)
        updates = cursor.statements("UPDATE sync_runs")
        self.assertEqual(len(updates), 1, cursor.executed)
        self.assertIn("status='skipped'", updates[0])
        self.assertIn("phase=%s", updates[0])
        self.assertIn("stage=%s", updates[0])
        params = cursor.parameters("UPDATE sync_runs")[0]
        self.assertIn("futu", params, "only the stage that ran may be fulfilled")
        self.assertIn(sync_audit.RETRY_REQUEST_PHASE, params)
        payload = [p for p in params if isinstance(p, db.psycopg2.extras.Json)][0]
        self.assertEqual(payload.adapted[sync_audit.RETRY_SATISFIED_BY], 121)

    def test_consume_is_idempotent_by_construction(self):
        """The guard lives in SQL: a fulfilled request is not fulfilled twice."""
        cursor = FakeCursor(consumed=2)
        with _patch_db(cursor):
            self.assertEqual(sync_audit.consume_retry_requests("futu", 121), 2)
        sql = cursor.statements("UPDATE sync_runs")[0]
        self.assertIn("NOT (details ? %s)", sql)

    def test_a_skipped_request_row_is_never_a_running_row(self):
        """The request row must stay out of the `running` partial unique index."""
        cursor = FakeCursor(run=_failed_run())
        with _patch_db(cursor):
            sync_audit.request_retry(113, "futu", "futu")
        sql = cursor.statements("INSERT INTO sync_runs")[0]
        self.assertIn("'skipped'", sql)
        self.assertNotIn("'running'", sql)
        self.assertIn("retry-request:", str(cursor.parameters("INSERT INTO sync_runs")))


# ── opt-in: the same round-trip on real PostgreSQL ────────────────────────

@pytest.mark.integration
@skipUnless(os.environ.get("FINCAL_TEST_DB") == "1",
            "set FINCAL_TEST_DB=1 with a reachable fincal DB (the request row, "
            "the refusal and the fulfilment run against a temp table inside a "
            "rolled-back transaction)")
class RetryRequestRoundTripTests(TestCase):
    """Acceptance 1-3 on the production table shape, inside a transaction."""

    _TEMP_TABLE = """
        CREATE TEMP TABLE sync_runs (
            id SERIAL PRIMARY KEY,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT,
            symbol_count INTEGER NOT NULL DEFAULT 0,
            record_count INTEGER NOT NULL DEFAULT 0,
            attempt INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            timeout_seconds INTEGER NOT NULL DEFAULT 3600,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            heartbeat_at TIMESTAMPTZ,
            phase TEXT,
            current INTEGER,
            total INTEGER,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            finished_at TIMESTAMPTZ,
            error_code TEXT
        )
    """

    def _rows(self, conn, sql, params=None):
        with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def test_a_retry_request_is_terminal_and_fulfilled_by_the_next_round(self):
        with db.db_connection() as conn:
            conn.autocommit = False
            with conn.cursor() as setup:
                setup.execute(self._TEMP_TABLE)

            @contextmanager
            def _transaction_cursor():
                with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
                    yield cur

            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO sync_runs (stage, status, source, "
                                "symbol_count, idempotency_key) "
                                "VALUES ('futu', 'failed', 'futu', 5, 'futu:earnings:full') "
                                "RETURNING id")
                    original = cur.fetchone()[0]

                with patch.object(db, "db_cursor", _transaction_cursor):
                    request_id = sync_audit.request_retry(
                        original, "futu", "futu", symbol_count=5,
                        requested_by="ops@example.com")
                    assert request_id is not None

                    # Acceptance 1: no executor-less `running` row appeared.
                    running = self._rows(conn, "SELECT id FROM sync_runs WHERE status='running'")
                    self.assertEqual(running, [], "the retry created a ghost running row")

                    request_row = self._rows(
                        conn, "SELECT status, phase, idempotency_key, timeout_seconds, "
                              "details, finished_at FROM sync_runs WHERE id=%s",
                        (request_id,))[0]
                    self.assertEqual(request_row["status"], "skipped")
                    self.assertEqual(request_row["phase"], sync_audit.RETRY_REQUEST_PHASE)
                    self.assertEqual(request_row["idempotency_key"],
                                     sync_audit.retry_request_key(original))
                    self.assertEqual(request_row["timeout_seconds"],
                                     config.stage_timeout("futu"))
                    self.assertIsNotNone(request_row["finished_at"],
                                         "a request is terminal, not an open run")
                    self.assertEqual(request_row["details"]["retry_requested_for"],
                                     str(original))

                    # Acceptance 2: the second retry is refused for a real reason.
                    self.assertIsNone(sync_audit.request_retry(original, "futu", "futu"))
                    self.assertEqual(
                        len(self._rows(conn, "SELECT id FROM sync_runs WHERE phase=%s",
                                       (sync_audit.RETRY_REQUEST_PHASE,))), 1)

                    # Acceptance 3: the scheduled round runs the stage, fulfils
                    # the request and leaves a real terminal state.
                    run_id = sync_audit.start_run(
                        "futu", "futu", symbol_count=5,
                        idempotency_key="futu:earnings:full",
                        timeout_seconds=config.stage_timeout("futu"))
                    assert run_id is not None
                    fulfilled = self._rows(
                        conn, "SELECT details FROM sync_runs WHERE id=%s",
                        (request_id,))[0]["details"]
                    self.assertEqual(fulfilled[sync_audit.RETRY_SATISFIED_BY], run_id)

                    self.assertTrue(sync_audit.finish_run(
                        run_id, status="success", record_count=424,
                        details={"date_rows_written": 424}))
                    run_row = self._rows(
                        conn, "SELECT status, record_count FROM sync_runs WHERE id=%s",
                        (run_id,))[0]
                    self.assertEqual(run_row["status"], "success")
                    self.assertEqual(run_row["record_count"], 424)

                    # …and asking again after the fulfilment is legitimate.
                    self.assertIsNotNone(
                        sync_audit.request_retry(original, "futu", "futu"))
                    self.assertEqual(sync_audit.consume_retry_requests("futu", run_id), 1)
                    # Consumed exactly once.
                    self.assertEqual(sync_audit.consume_retry_requests("futu", run_id), 0)

                    # The production table was shadowed by the temp table all along.
                    with conn.cursor() as probe:
                        probe.execute("SELECT count(*) FROM pg_catalog.pg_class "
                                      "WHERE relname='sync_runs' AND relpersistence='t'")
                        self.assertEqual(probe.fetchone()[0], 1)
            finally:
                conn.rollback()
