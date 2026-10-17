"""Contract coverage for ``GET /api/admin/sync-runs`` (Issue #84).

``sync_runs.timeout_seconds`` is the per-run budget the timeout reaper obeys
(#33) and #78 gave every stage script a writer for it, so production rows carry
600/900/1500/2400.  This admin endpoint is the only HTTP outlet for that column
and its SELECT omitted it, so ``response_model=list[SyncRun]`` silently answered
the model's own default (3600) for every row — the configured value existed in
the database and nowhere an operator (or the admin panel) looks.

The contract is asserted three ways, mirroring the #73 guard for ``EarningItem``:

* the table DDL (``app/db.py``) declares every field ``SyncRun`` exposes;
* the endpoint's SELECT selects exactly the fields ``SyncRun`` declares;
* the live response carries the row's value, not the default.
"""

import inspect
import re
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db
from app.auth import get_current_user
from app.main import app
from app.routers import admin as admin_module
from app.schemas import SyncRun

_SELECT = re.compile(r"SELECT(.*?)\bFROM\s+sync_runs\b", re.S | re.I)
_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS sync_runs \((.*?)\n\s*\);", re.S)
_COLUMN_DEF = re.compile(
    r"^[ \t]*([a-z_][a-z0-9_]*)\s+(?:BIGSERIAL|SERIAL|TEXT|INTEGER|JSONB|TIMESTAMPTZ|BOOLEAN|NUMERIC|DATE)\b",
    re.M,
)

#: The endpoint's SELECT as it stood before this issue (``timeout_seconds``
#: missing).  Kept verbatim so the guard below is exercised against the real
#: defect instead of a hand-written approximation.
LEGACY_SELECT = """SELECT id, stage, status, source, symbol_count, record_count,
                          details, error_code, started_at, finished_at,
                          heartbeat_at, attempt, idempotency_key,
                          phase, current, total
                   FROM sync_runs ORDER BY started_at DESC LIMIT %s"""


def _columns_in_select(sql: str) -> set[str]:
    """Every column name a ``SELECT ... FROM sync_runs`` statement reads."""
    match = _SELECT.search(sql)
    assert match, f"no 'SELECT ... FROM sync_runs' found in:\n{sql}"
    columns = set()
    for chunk in match.group(1).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        expression = re.split(r"\s+AS\s+", chunk, flags=re.I)[0]
        columns.add(expression.split(".")[-1].strip().strip('"'))
    return columns


def _endpoint_select() -> str:
    """The SELECT the deployed endpoint actually runs."""
    source = inspect.getsource(admin_module.list_sync_runs)
    assert "cur.execute(" in source, "list_sync_runs no longer executes a query"
    return source


def _endpoint_columns() -> set[str]:
    columns = _columns_in_select(_endpoint_select())
    # Guard the guard: a parse that silently found nothing would make the
    # equality assertions below pass for the wrong reason.
    assert {"id", "stage", "status", "timeout_seconds"} <= columns, sorted(columns)
    return columns


def _table_columns() -> set[str]:
    """Columns ``app/db.py`` declares for ``sync_runs`` (base DDL + ALTERs)."""
    source = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    created = _CREATE.search(source)
    assert created, "app/db.py no longer declares sync_runs as expected"
    columns = set(_COLUMN_DEF.findall(created.group(1)))
    # Additive columns are applied with ALTER for existing installations, in the
    # block right after this table's DDL; a new column may live only there.
    after = source[created.end():created.end() + 4000]
    columns |= set(re.findall(r'\(\s*"([a-z_][a-z0-9_]*)"\s*,\s*"', after))
    assert {"id", "stage", "status", "timeout_seconds"} <= columns, sorted(columns)
    return columns


class _RecordingCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_row(run_id: int, stage: str, timeout_seconds: int) -> dict:
    return {
        "id": run_id,
        "stage": stage,
        "status": "success",
        "source": "cron",
        "symbol_count": 133,
        "record_count": 424,
        "details": {"date_rows_written": 424},
        "error_code": None,
        "started_at": "2026-10-12T08:00:00+08:00",
        "finished_at": "2026-10-12T08:07:30+08:00",
        "heartbeat_at": "2026-10-12T08:07:20+08:00",
        "attempt": 1,
        "idempotency_key": f"cron:{stage}:2026-10-12",
        "timeout_seconds": timeout_seconds,
        "phase": "actuals",
        "current": 57,
        "total": 112,
    }


def _admin_client():
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "admin@test.com", "name": "Admin", "role": "admin",
    }
    return TestClient(app, raise_server_exceptions=False)


def test_the_select_and_the_response_model_declare_the_same_fields():
    """Neither side may gain a field the other does not know about."""
    selected = _endpoint_columns()
    declared = set(SyncRun.model_fields)

    assert selected == declared, (
        "list_sync_runs has drifted from SyncRun:\n"
        f"  declared but never selected (answered by the model default): {sorted(declared - selected)}\n"
        f"  selected but never declared (dropped by response_model):       {sorted(selected - declared)}"
    )


def test_the_guard_detects_the_column_this_issue_was_about():
    """The pre-fix SELECT is exactly the drift the contract must report."""
    missing = set(SyncRun.model_fields) - _columns_in_select(LEGACY_SELECT)
    assert missing == {"timeout_seconds"}, sorted(missing)


def test_every_declared_field_exists_as_a_sync_runs_column():
    """A field the table cannot supply is a phantom the API would invent."""
    phantom = set(SyncRun.model_fields) - _table_columns()
    assert not phantom, f"SyncRun declares columns sync_runs does not have: {sorted(phantom)}"


def test_a_missing_column_is_masked_by_the_model_default():
    """Documents the mechanism: FastAPI fills the gap instead of failing."""
    row = _run_row(120, "futu", 1500)
    row.pop("timeout_seconds")
    assert SyncRun.model_validate(row).timeout_seconds == 3600


def test_api_returns_each_runs_own_budget():
    """Acceptance 1: 600/900/1500/2400 come back unchanged, not 3600."""
    rows = [
        _run_row(116, "prediction", 600),
        _run_row(114, "stock_names", 900),
        _run_row(120, "futu", 1500),
        _run_row(115, "consensus", 2400),
    ]
    conn = _RecordingCursor(rows)
    app.dependency_overrides = {}
    try:
        with patch.object(db, "db_cursor", lambda: conn):
            client = _admin_client()
            resp = client.get("/api/admin/sync-runs?limit=6")
    finally:
        app.dependency_overrides = {}

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert [item["timeout_seconds"] for item in body] == [600, 900, 1500, 2400]
    assert [item["id"] for item in body] == [116, 114, 120, 115]
    # No field of the model may be dropped on the way out.
    for item in body:
        assert set(item) == set(SyncRun.model_fields)

    sql = conn.executed[0][0]
    assert "timeout_seconds" in _columns_in_select(sql)
