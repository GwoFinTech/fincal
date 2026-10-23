"""Suite-wide isolation: the default run never touches a real database (Issue #98).

The suite is the only regression defence for the #37–#97 fixes, but it used to
depend on the production PostgreSQL being reachable. 39 tests opened real
connections through paths a test's own ``patch.object(<module>, "db_cursor")``
cannot reach — the audit heartbeat writes through ``app.sync_audit``, the default
symbol universe reads ``tsummt.watchlist`` with its own ``psycopg2.connect``,
``POST /api/admin/sync-runs/recover`` writes the admin audit log, and some
scripts bound ``db_cursor`` at import time.  On a machine without that database
those 39 were simply red, so ``pytest`` could never be a CI / pre-deploy gate,
and the green result was a property of the host rather than of the code.

What this module does:

* An autouse guard replaces ``psycopg2.connect``, ``psycopg2.pool``'s pool and
  ``app.db._get_pool`` with a function that fails the test with
  ``unexpected database access`` and names the caller.  The guard fails via
  ``pytest.fail`` (a ``BaseException``), so a production ``except Exception``
  fallback cannot swallow the attempt and turn it back into a green test that
  secretly read production data.
* A test that is *about* the real database opts in with
  ``@pytest.mark.integration``; the guard steps aside for it, and
  :func:`pytest_collection_modifyitems` keeps it out of the default run unless
  ``--run-integration`` is passed.
* ``app.db._pool`` is a process-wide singleton: it is reset before and after
  every test (and a stale pool closed), so connections cannot leak across tests.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import psycopg2
import psycopg2.pool
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


def _caller() -> str:
    """Best-effort ``file:line in function`` of the test that tripped the guard."""
    for frame in inspect.stack()[2:]:
        filename = frame.filename.replace("\\", "/")
        if "/tests/" in filename:
            return f"tests/{filename.rsplit('tests/', 1)[-1]}:{frame.lineno} in {frame.function}"
    return "unknown caller"


def _fail(what: str):
    """Fail the current test loudly (``pytest.fail`` is a BaseException)."""
    pytest.fail(
        f"unexpected database access via {what} ({_caller()}) — "
        "fake the I/O boundary instead; mark the test "
        "@pytest.mark.integration only if it is really about the database",
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def isolated_database(request, monkeypatch):
    """Refuse real PostgreSQL I/O unless the test is marked ``integration``."""
    if request.node.get_closest_marker("integration") is not None:
        yield
        return

    # Attempts are recorded as well as raised: a stage runs some provider calls
    # in a worker thread (the OpenD watchdog), and an exception raised there
    # would only be printed by ``threading`` and leave the test green. The
    # makereport hook below turns a recorded attempt into a failure of the test.
    attempts: list[str] = []
    request.node._unexpected_db_attempts = attempts

    def refuse(what: str):
        def _guard(*_args, **_kwargs):
            attempts.append(f"{what} ({_caller()})")
            _fail(what)

        return _guard

    previous_pool = db._pool
    db._pool = None
    if previous_pool is not None:
        try:
            previous_pool.closeall()
        except Exception:  # pragma: no cover - a stale pool may already be closed
            pass
    monkeypatch.setattr(psycopg2, "connect", refuse("psycopg2.connect"), raising=False)
    monkeypatch.setattr(
        psycopg2.pool, "ThreadedConnectionPool",
        refuse("psycopg2.pool.ThreadedConnectionPool"), raising=False,
    )
    monkeypatch.setattr(db, "_get_pool", refuse("app.db._get_pool"), raising=False)
    try:
        yield
    finally:
        # Never restore ``previous_pool``: it was closed above, and a closed pool
        # handed to the next test would fail with a psycopg2 internal error
        # instead of the guard's own message.
        pool = db._pool
        db._pool = None
        if pool is not None:
            try:
                pool.closeall()
            except Exception:  # pragma: no cover - teardown must not mask a failure
                pass
        report = getattr(request.node, "rep_call", None)
        if attempts and report is not None and report.passed:
            pytest.fail(
                "unexpected database access (raised off the test thread, so the "
                f"test itself stayed green): {'; '.join(attempts)}",
                pytrace=False,
            )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Record each phase's report on the item (used by the isolation teardown).

    A connection attempt made on a worker thread cannot fail the test body (the
    exception stays inside the thread), so a *passing* call phase with recorded
    attempts is turned into a failure here — otherwise the suite would report
    "passed" for a test that read the production database.
    """
    outcome = yield
    report = outcome.get_result()
    attempts = getattr(item, "_unexpected_db_attempts", None)
    if report.when == "call" and report.passed and attempts:
        report.outcome = "failed"
        report.longrepr = (
            "UnexpectedDatabaseAccess: database access was attempted on a thread "
            f"the test does not observe: {'; '.join(attempts)}"
        )
    setattr(item, f"rep_{report.when}", report)


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration", action="store_true", default=False,
        help="run tests marked 'integration' against the real PostgreSQL database",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(
        reason="integration test: run with --run-integration (needs a reachable PostgreSQL)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
