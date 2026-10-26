"""Issue #101: the database access layer must have a timeout contract.

Production had one shared database layer with **no upper bound** on any wait:

* three ``psycopg2.connect`` call sites (pool, dedicated connection, the
  cross-database ``tsummt.watchlist`` read) with no ``connect_timeout``, so a
  dependency that *drops* packets held the thread for 131 s (libpq waits for the
  kernel's SYN retries to exhaust) while every other request burned its own 30 s;
* no ``statement_timeout`` / ``lock_timeout`` / ``idle_in_transaction_session_timeout``,
  so one locked statement or abandoned transaction owned a pool slot forever;
* ``ThreadedConnectionPool(maxconn=10)`` against a 40-thread limiter, and
  ``psycopg2`` raises ``PoolError`` instead of queueing — the 11th concurrent
  request became a 500 ``internal_error``;
* ``app/universe.py`` ran the cross-DB read on the request thread, so the whole
  calendar/export/popular/``scope=all`` surface waited on the tsummt database;
* the health probe was a plain ``SELECT 1``: it hung instead of reporting
  ``postgresql: not_ready``.

The tests below pin the contract: static coverage of every connect call, the
session settings, queue-then-503 behaviour, the bounded request path, and the
bounded health probe.
"""
from __future__ import annotations

import ast
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import psycopg2
import psycopg2.pool
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

from app import config, db  # noqa: E402
from app import universe as universe_mod  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.main import app  # noqa: E402
from app.watchlist.base import WatchlistSource  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_connect_cooldowns():
    """The connect cool-down is process-wide state; never leak it between tests."""
    db.reset_connection_cooldowns()
    yield
    db.reset_connection_cooldowns()


# ── Criterion 1: every connect call carries the contract ────────────────

def _python_files(*directories: str):
    for directory in directories:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _is_connect_call(func: ast.AST) -> bool:
    """``psycopg2.connect(...)``, ``psycopg2.pool.ThreadedConnectionPool(...)`` …"""
    if isinstance(func, ast.Attribute):
        if func.attr == "connect" and isinstance(func.value, ast.Name):
            return func.value.id == "psycopg2"
        return func.attr in {"ThreadedConnectionPool", "AbstractConnectionPool"}
    if isinstance(func, ast.Name):
        return func.id in {"ThreadedConnectionPool", "AbstractConnectionPool", "connect"}
    return False


def _feeds_connect_kwargs(call: ast.Call) -> bool:
    """True when the call expands ``**connect_kwargs(...)`` (the single factory)."""
    for keyword in call.keywords:
        if keyword.arg is not None:
            continue
        value = keyword.value
        if isinstance(value, ast.Call):
            target = value.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if name == "connect_kwargs":
                return True
    return False


def test_every_connection_call_carries_a_connect_timeout():
    offenders = []
    scanned = 0
    for path in _python_files("app", "scripts"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_connect_call(node.func):
                continue
            scanned += 1
            has_timeout = any(
                keyword.arg == "connect_timeout" for keyword in node.keywords
            )
            if not (has_timeout or _feeds_connect_kwargs(node)):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert scanned >= 2, "the scan found no connection call at all — it stopped working"
    assert not offenders, (
        "connection calls without connect_timeout (use app.db.connect_kwargs): "
        + ", ".join(offenders)
    )


def test_session_timeout_settings_are_emitted_and_non_zero():
    """``options`` must carry non-zero statement/lock/idle budgets for both roles."""
    for role in (db.API_ROLE, db.SYNC_ROLE):
        with mock.patch.dict("os.environ", {"FINCAL_DB_ROLE": role}):
            kwargs = db.connect_kwargs()
        options = kwargs["options"]
        for setting in (
            "statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout",
        ):
            assert f"-c {setting}=" in options, f"{role}: {setting} missing from {options!r}"
            value = int(options.split(f"-c {setting}=")[1].split()[0])
            assert value > 0, f"{role}: {setting} must be non-zero, got {value}"
        assert kwargs["connect_timeout"] >= 2
        assert kwargs["application_name"] == f"fincal-{role}"


def test_request_and_sync_budgets_differ():
    """A request must fail fast; a weekly bulk upsert must not be killed."""
    api = db._budget(db.API_ROLE)
    sync = db._budget(db.SYNC_ROLE)
    assert api["statement_timeout"] < sync["statement_timeout"]
    assert api["connect_timeout"] < sync["connect_timeout"]


def test_role_follows_the_process_entry_point(monkeypatch):
    """A ``scripts/…`` entry point gets the sync budget; the service does not."""
    monkeypatch.delenv("FINCAL_DB_ROLE", raising=False)
    monkeypatch.setitem(__import__("sys").modules["__main__"].__dict__, "__file__", "/x/scripts/sync_futu.py")
    try:
        assert db.resolve_role() == db.SYNC_ROLE
    finally:
        pass
    monkeypatch.setitem(__import__("sys").modules["__main__"].__dict__, "__file__", "/usr/local/bin/uvicorn")
    assert db.resolve_role() == db.API_ROLE
    monkeypatch.setenv("FINCAL_DB_ROLE", "sync")
    assert db.resolve_role() == db.SYNC_ROLE


# ── Criterion 3: the pool queues, and a saturated pool is a 503 ─────────

class _FakeConn:
    """Minimal connection stand-in: ``db_cursor`` commits/rolls back."""

    def cursor(self, **kwargs):
        return mock.MagicMock()

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeInnerPool:
    """Stand-in for ``ThreadedConnectionPool`` (no socket, no database)."""

    def __init__(self, *, fail_with: BaseException | None = None, fail_times: int = 0):
        self._fail_with = fail_with
        self._fail_times = fail_times
        self.inflight = 0
        self.max_inflight = 0
        self.first_attempts = 0
        self._mutex = threading.Lock()
        self.closed = False

    def getconn(self):
        with self._mutex:
            self.first_attempts += 1
            if self._fail_with is not None and self._fail_times > 0:
                self._fail_times -= 1
                raise self._fail_with
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(0.005)  # a real checkout costs something
        return _FakeConn()

    def putconn(self, conn, close: bool = False):
        with self._mutex:
            self.inflight -= 1

    def closeall(self):
        self.closed = True


def _pool(inner, *, maxconn, acquire_timeout):
    return db._WaitedPool(inner, maxconn=maxconn, acquire_timeout=acquire_timeout)


def test_pool_waits_for_a_slot_instead_of_raising_pool_exhausted():
    inner = _FakeInnerPool()
    pool = _pool(inner, maxconn=1, acquire_timeout=2.0)
    held = pool.getconn()
    timing = {}

    def _second():
        started = time.monotonic()
        conn = pool.getconn()
        timing["waited"] = time.monotonic() - started
        timing["conn"] = conn
        pool.putconn(conn)

    worker = threading.Thread(target=_second)
    worker.start()
    time.sleep(0.15)
    assert worker.is_alive(), "the second caller must queue, not raise PoolError"
    pool.putconn(held)
    worker.join(2.0)
    assert timing.get("conn") is not None
    assert timing["waited"] >= 0.1


def test_saturated_pool_degrades_to_503_db_busy_with_retry_after():
    inner = _FakeInnerPool()
    pool = _pool(inner, maxconn=1, acquire_timeout=0.05)
    pool.getconn()
    with pytest.raises(AppError) as excinfo:
        pool.getconn()
    assert excinfo.value.code == "db_busy"
    assert excinfo.value.status_code == 503
    assert excinfo.value.headers["Retry-After"] == "2"


def test_psycopg2_pool_error_is_mapped_and_releases_the_slot():
    inner = _FakeInnerPool(fail_with=psycopg2.pool.PoolError("connection pool exhausted"),
                           fail_times=1)
    pool = _pool(inner, maxconn=1, acquire_timeout=0.5)
    with pytest.raises(AppError) as excinfo:
        pool.getconn()
    assert excinfo.value.code == "db_busy"
    assert excinfo.value.status_code == 503
    # The slot must come back: a failed checkout may not shrink the pool.
    conn = pool.getconn()
    assert conn is not None
    pool.putconn(conn)


def test_connect_failure_is_mapped_to_503_db_unavailable():
    inner = _FakeInnerPool(
        fail_with=psycopg2.OperationalError("could not connect: timeout expired"),
        fail_times=1,
    )
    pool = _pool(inner, maxconn=1, acquire_timeout=0.5)
    with pytest.raises(AppError) as excinfo:
        pool.getconn()
    assert excinfo.value.code == "db_unavailable"
    assert excinfo.value.status_code == 503
    assert excinfo.value.headers["Retry-After"] == "5"


def test_one_outage_costs_one_connect_timeout(monkeypatch):
    """A failed connect is remembered briefly: three reads, one bounded wait."""
    monkeypatch.setattr(config, "DB_CONNECT_FAILURE_COOLDOWN_SECONDS", 60.0)
    inner = _FakeInnerPool(
        fail_with=psycopg2.OperationalError("could not connect: timeout expired"),
        fail_times=10,
    )
    pool = _pool(inner, maxconn=1, acquire_timeout=0.5)

    started = time.monotonic()
    for _ in range(3):
        with pytest.raises(AppError) as excinfo:
            pool.getconn()
        assert excinfo.value.code == "db_unavailable"
    elapsed = time.monotonic() - started

    assert inner.first_attempts == 1, "the cool-down must prevent further connects"
    assert elapsed < 0.2, f"three reads of one outage took {elapsed:.2f}s"


def test_a_successful_connect_clears_the_cool_down(monkeypatch):
    monkeypatch.setattr(config, "DB_CONNECT_FAILURE_COOLDOWN_SECONDS", 60.0)
    inner = _FakeInnerPool(
        fail_with=psycopg2.OperationalError("boom"), fail_times=1
    )
    pool = _pool(inner, maxconn=1, acquire_timeout=0.5)
    with pytest.raises(AppError):
        pool.getconn()
    assert db._in_connect_cooldown(db.API_ROLE) is True

    db.reset_connection_cooldowns()  # an operator / recovery path
    conn = pool.getconn()
    assert conn is not None
    pool.putconn(conn)
    assert db._in_connect_cooldown(db.API_ROLE) is False


def test_forty_concurrent_callers_never_see_a_pool_error():
    """40 threads (the endpoint limiter) over 5 slots: all queue through."""
    inner = _FakeInnerPool()
    pool = _pool(inner, maxconn=5, acquire_timeout=10.0)
    errors = []

    def _work():
        try:
            conn = pool.getconn()
            time.sleep(0.01)
            pool.putconn(conn)
        except BaseException as exc:  # noqa: BLE001 - failures are asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_work) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15.0)

    assert not errors, f"pool errors under concurrency: {errors[:3]}"
    assert inner.max_inflight <= 5, "the pool exceeded its own maxconn"
    assert inner.inflight == 0, "a slot leaked"


def test_db_cursor_maps_a_saturated_pool_to_the_error_contract():
    """``db_cursor`` is where every request path meets the pool."""
    inner = _FakeInnerPool()
    pool = _pool(inner, maxconn=1, acquire_timeout=0.05)
    with mock.patch.object(db, "_get_pool", lambda *a, **kw: pool):
        with db.db_cursor():
            with pytest.raises(AppError) as excinfo:
                with db.db_cursor():
                    pass
    assert excinfo.value.code == "db_busy"


def test_error_middleware_renders_retry_after():
    """The HTTP surface: 503 ``db_busy`` with ``Retry-After``, not a 500."""

    @contextmanager
    def _busy():
        raise AppError("db_busy", "connection pool saturated", 503,
                       headers={"Retry-After": "2"})
        yield  # pragma: no cover

    app.dependency_overrides = {get_current_user: lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "admin"}}
    try:
        with mock.patch.object(db, "db_cursor", _busy):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/earnings")
    finally:
        app.dependency_overrides = {}

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "db_busy"
    assert resp.headers["Retry-After"] == "2"


# ── Criterion 2: the request path is bounded when the source hangs ──────

class _HangingSource(WatchlistSource):
    """Source that never answers within the request budget (black-holed DB)."""

    def __init__(self, delay: float, codes=("AAPL.US",)):
        self.delay = delay
        self.codes = list(codes)
        self.reads = 0
        self.returned = threading.Event()

    def fetch_symbols(self):
        self.reads += 1
        time.sleep(self.delay)
        self.returned.set()
        return list(self.codes)

    @property
    def source_name(self):
        return "hanging"


def test_calendar_universe_never_blocks_a_request_on_the_source():
    source = _HangingSource(delay=1.0, codes=["AAPL.US", "0700.HK"])
    universe = universe_mod.SymbolUniverse(
        ttl_seconds=60.0,
        error_retry_seconds=60.0,
        request_wait_seconds=0.1,
        source_factory=lambda: source,
    )

    started = time.monotonic()
    us, hk = universe.get()
    elapsed = time.monotonic() - started

    assert elapsed < 0.6, f"the request path waited {elapsed:.1f}s on the source"
    assert (us, hk) == (universe_mod.FALLBACK_US, universe_mod.FALLBACK_HK)

    # The read still completes off-thread and lands in the snapshot.
    assert source.returned.wait(5.0), "the background read never finished"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and universe.status()["last_success_at"] is None:
        time.sleep(0.02)
    assert universe.get() == (["AAPL"], ["0700.HK"])
    assert universe.status()["stale"] is False


def test_concurrent_universe_callers_share_one_read():
    source = _HangingSource(delay=0.3)
    universe = universe_mod.SymbolUniverse(
        ttl_seconds=60.0, error_retry_seconds=60.0, request_wait_seconds=0.05,
        source_factory=lambda: source,
    )

    def _read():
        universe.get()

    threads = [threading.Thread(target=_read) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    assert source.reads == 1, "concurrent callers started more than one source read"


# ── Criterion 2: the health probe answers instead of hanging ────────────

class _HangingCursor:
    def execute(self, *args, **kwargs):
        time.sleep(5.0)

    def fetchone(self):
        return {}

    def close(self):
        pass


@contextmanager
def _hanging_db_cursor():
    yield _HangingCursor()


def test_health_reports_not_ready_within_the_probe_budget(monkeypatch):
    from app.routers import admin

    monkeypatch.setattr(config, "DB_HEALTH_PROBE_TIMEOUT_SECONDS", 0.2)
    with mock.patch.object(db, "db_cursor", _hanging_db_cursor), \
            mock.patch("app.freshness.check_freshness", lambda *a, **kw: {"status": "healthy"}), \
            mock.patch("socket.create_connection", mock.MagicMock()), \
            mock.patch("shutil.which", mock.MagicMock(return_value=None)), \
            mock.patch("app.company_name.probe_kurumi",
                       lambda *a, **kw: {"status": "healthy"}):
        started = time.monotonic()
        response = admin.health_check()
        elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"the health probe took {elapsed:.1f}s with a hanging database"
    assert response["checks"]["postgresql"]["status"] == "not_ready"
    assert response["status"] == "not_ready"


def test_ready_reports_not_ready_within_the_probe_budget(monkeypatch):
    from app.routers import admin

    monkeypatch.setattr(config, "DB_HEALTH_PROBE_TIMEOUT_SECONDS", 0.2)
    with mock.patch.object(db, "db_cursor", _hanging_db_cursor):
        started = time.monotonic()
        response = admin.readiness_check()
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert response.status_code == 503


def test_shipped_health_budget_fits_the_issue_contract():
    """Acceptance: ``/api/admin/health`` answers within 2 s."""
    assert 0 < config.DB_HEALTH_PROBE_TIMEOUT_SECONDS <= 2.0
    assert 0 < config.DB_POOL_ACQUIRE_TIMEOUT_SECONDS <= 5.0
    assert 0 < config.UNIVERSE_REQUEST_WAIT_SECONDS <= 5.0


def test_freshness_snapshot_without_a_database_is_the_same_projection():
    """Skipping the read must not change the shape the endpoint reports."""
    from app import freshness

    with mock.patch.object(db, "db_cursor", side_effect=AssertionError("no read allowed")):
        fast = freshness.unavailable_snapshot()
    slow = freshness.health_snapshot(freshness._unavailable(
        freshness.datetime.now(freshness.timezone.utc), freshness.stale_after_hours()
    ))

    assert fast.keys() == slow.keys()
    assert fast["status"] == slow["status"] == freshness.STATUS_UNKNOWN
    assert fast["error_code"] == slow["error_code"] == freshness.ERROR_UNAVAILABLE
    assert fast["stale_stages"] == slow["stale_stages"] == []

