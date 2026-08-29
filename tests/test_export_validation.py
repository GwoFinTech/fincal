"""Tests for /api/export parameter validation (Issue #22) and auth (Issue #36).

Covers:
- Invalid date format → 422 Validation Error
- Valid date format → 200 with correct Content-Type
- No identity header → 401 for /api/export, /api/search, /api/popular (Issue #36)
- Valid identity header → 200
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
        # Return a user row so ensure_user() succeeds in positive tests.
        return {
            "id": 1,
            "portal_user_id": 1,
            "email": "t@t.com",
            "name": "T",
            "ical_token": "tok-abc",
        }

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeConn:
    def cursor(self, **kw):
        return _FakeCursor()

    def __enter__(self):
        # db.db_cursor() is a contextmanager that yields a cursor;
        # mimic that so `with db.db_cursor() as cur` binds cur to a cursor.
        return _FakeCursor()

    def __exit__(self, *a):
        pass


def _client():
    """TestClient with get_current_user overridden (authenticated)."""
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "admin",
    }
    return TestClient(app, raise_server_exceptions=False)


def _unauth_client():
    """TestClient with no dependency override and no X-User-* headers."""
    app.dependency_overrides = {}
    return TestClient(app, raise_server_exceptions=False)


def _patched_client():
    """Return a TestClient with mocked db and auth."""
    with patch.object(db, "db_cursor", lambda: _FakeConn()):
        client = _client()
        yield client
    app.dependency_overrides = {}


def _patched_unauth_client():
    """Return a TestClient with mocked db, no auth override, no headers."""
    with patch.object(db, "db_cursor", lambda: _FakeConn()):
        client = _unauth_client()
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


# ── Issue #36: unauthenticated → 401, authenticated → 200 ────────

def test_export_without_auth_returns_401():
    client = next(_patched_unauth_client())
    resp = client.get("/api/export?start=2000-01-01&end=2099-12-31&format=json")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


def test_search_without_auth_returns_401():
    client = _unauth_client()
    resp = client.get("/api/search?q=tech")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


def test_popular_without_auth_returns_401():
    client = _unauth_client()
    resp = client.get("/api/popular")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


def test_export_with_auth_header_returns_200():
    """Real X-User-Id header (no dependency override) -> authenticated 200."""
    with patch.object(db, "db_cursor", lambda: _FakeConn()):
        app.dependency_overrides = {}
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/export?start=2026-08-01&end=2026-08-20&format=csv",
            headers={"X-User-Id": "1", "X-User-Email": "t@t.com", "X-User-Name": "T"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert "text/csv" in resp.headers.get("content-type", "")
    app.dependency_overrides = {}


def test_search_with_auth_header_returns_200():
    with patch.object(db, "db_cursor", lambda: _FakeConn()):
        app.dependency_overrides = {}
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/search?q=tech", headers={"X-User-Id": "1"})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    app.dependency_overrides = {}


def test_popular_with_auth_header_returns_200():
    app.dependency_overrides = {}
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/popular", headers={"X-User-Id": "1"})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    app.dependency_overrides = {}


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
