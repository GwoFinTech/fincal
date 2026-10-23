"""Tests for /api/admin/sync-runs/recover (Issue #23).

Covers:
- POST /api/admin/sync-runs/recover returns 200
- Response body matches SyncRunRecoverResult: {"recovered": <int>}
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app
from app.auth import get_current_user
from app import audit, sync_audit


@pytest.fixture(autouse=True)
def _silence_admin_audit(monkeypatch):
    """Keep the admin audit log off the database (Issue #98).

    ``POST /api/admin/sync-runs/recover`` records the action through
    ``app.audit.log_admin_action``. Only ``recover_stale_runs`` was patched, so
    the endpoint reached the audit *write* and answered 500 on any machine
    without the production database — the test was green only because that
    database happened to be reachable.
    """
    monkeypatch.setattr(audit, "log_admin_action", lambda *a, **kw: None)


class _FakeCursor:
    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeConn:
    def cursor(self, **kw):
        return _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _admin_client():
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "admin@test.com", "name": "Admin", "role": "admin",
    }
    return TestClient(app, raise_server_exceptions=False)


def test_recover_stale_runs_returns_200():
    """POST /api/admin/sync-runs/recover should return 200, not 500."""
    with patch.object(sync_audit, "recover_stale_runs", return_value=0):
        client = _admin_client()
        resp = client.post("/api/admin/sync-runs/recover")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_recover_stale_runs_returns_recovered_count():
    """Response body should be {"recovered": <int>}."""
    with patch.object(sync_audit, "recover_stale_runs", return_value=3):
        client = _admin_client()
        resp = client.post("/api/admin/sync-runs/recover")
    body = resp.json()
    assert "recovered" in body, f"Missing 'recovered' key in {body}"
    assert body["recovered"] == 3
    assert isinstance(body["recovered"], int)


def test_recover_stale_runs_zero_count():
    """When nothing is stale, recovered should be 0."""
    with patch.object(sync_audit, "recover_stale_runs", return_value=0):
        client = _admin_client()
        resp = client.post("/api/admin/sync-runs/recover")
    body = resp.json()
    assert body["recovered"] == 0
