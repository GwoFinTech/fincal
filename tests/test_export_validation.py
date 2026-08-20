"""Tests for /api/export parameter validation (Issue #22).

Covers:
- Invalid date format → 422 Validation Error
- Valid date format → 200 with correct Content-Type
- OpenAPI schema reflects date types
"""
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app
from app import db
from app.auth import get_current_user


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


def _client():
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "admin",
    }
    return TestClient(app, raise_server_exceptions=False)


def _patched_client():
    """Return a TestClient with mocked db and auth."""
    with patch.object(db, "db_cursor", lambda: _FakeConn()):
        client = _client()
        yield client
    app.dependency_overrides = {}


# ── Invalid date format returns 422, not 500 ─────────────────────

def test_export_invalid_start_returns_422():
    client = next(_patched_client())
    resp = client.get("/api/export?start=not-a-date&end=2026-08-20")
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


def test_export_invalid_end_returns_422():
    client = next(_patched_client())
    resp = client.get("/api/export?start=2026-08-01&end=bad-date")
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"


def test_export_missing_params_returns_422():
    client = next(_patched_client())
    resp = client.get("/api/export")
    assert resp.status_code == 422


# ── Valid date format returns 200 ─────────────────────────────────

def test_export_valid_csv_returns_200():
    client = next(_patched_client())
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-20&format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")


def test_export_valid_json_returns_200():
    client = next(_patched_client())
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-20&format=json")
    assert resp.status_code == 200


# ── OpenAPI schema reflects date types ────────────────────────────

def test_export_openapi_params_are_date():
    schema = app.openapi()
    export_path = schema["paths"].get("/api/export", {})
    get_op = export_path.get("get", {})
    params = {p["name"]: p for p in get_op.get("parameters", [])}
    assert params["start"]["schema"]["type"] == "string"
    assert params["start"]["schema"]["format"] == "date"
    assert params["end"]["schema"]["type"] == "string"
    assert params["end"]["schema"]["format"] == "date"
