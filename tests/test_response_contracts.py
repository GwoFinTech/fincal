"""Regression coverage for response-model shapes found in production."""
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app
from app import db
from app.auth import get_current_user


def test_user_response_accepts_integer_portal_user_id():
    from app.schemas import UserResponse

    user = UserResponse.model_validate({
        "id": 42,
        "portal_user_id": 7,
        "email": "u@example.com",
        "name": "User",
        "role": "user",
        "is_admin": False,
        "ical_token": "token",
        "ical_url": "https://example.test/ical/token",
    })
    assert user.portal_user_id == 7


def test_decision_response_accepts_revision_summary_object():
    from app.schemas import DecisionResponse

    result = DecisionResponse.model_validate({
        "status": "available",
        "revision_trend": {
            "status": "available",
            "sample_count": 1,
            "eps": {"direction": "flat"},
        },
    })
    assert result.revision_trend is not None
    assert result.revision_trend["status"] == "available"


# ── Issue #24: /api/earnings/{id}/decision returns 404 for missing id ──

class _FakeCursor:
    """Cursor that returns None for any fetchone (no earning found)."""

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
        return _FakeCursor()

    def __exit__(self, *a):
        pass


def test_api_earning_decision_returns_404_for_missing_id():
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "user",
    }
    with patch.object(db, "db_cursor", lambda: _FakeConn()), \
         patch("app.routers.api.ensure_user", return_value={"id": 1, "portal_user_id": 1, "email": "t@t.com", "name": "T", "ical_token": "tok"}):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/earnings/999999/decision")
    app.dependency_overrides = {}
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["error"]["code"] == "earning_not_found"
