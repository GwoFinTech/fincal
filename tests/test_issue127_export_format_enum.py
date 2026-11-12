"""Issue #127: `/api/export` must reject an unknown `format`, not serve CSV.

The parameter was a bare `str` and the body had a single `if format == "json"`
branch, so `format=jsoon`, `format=JSON` and `format=xlsx` all produced a **CSV**
with HTTP 200. A typo in a script therefore looked like success and produced the
wrong file; nothing surfaced the mistake.

Declaring the enum makes FastAPI answer 422 and name the allowed values, and the
Content-Type now follows the branch that actually ran.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402
from app import db, universe  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.watchlist.base import WatchlistSource  # noqa: E402


class _FakeCursor:
    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return {"id": 1, "portal_user_id": 1, "email": "t@t.com", "name": "T", "ical_token": "tok-abc"}

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


class _FakeSource(WatchlistSource):
    def fetch_symbols(self):
        return ["AAPL.US", "0700.HK"]


@pytest.fixture(autouse=True)
def _offline_boundaries(monkeypatch):
    """Keep the test off the real database (same boundary as Issue #98)."""
    monkeypatch.setattr(db, "db_cursor", lambda: _FakeConn())
    monkeypatch.setattr(
        universe, "_universe",
        universe.SymbolUniverse(source_factory=lambda: _FakeSource()),
    )


@pytest.fixture
def client():
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "admin",
    }
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides = {}


def _detail_text(resp) -> str:
    return json.dumps(resp.json(), ensure_ascii=False)


@pytest.mark.parametrize("bad", ["jsoon", "JSON", "xlsx", "", "CSV"])
def test_an_unknown_format_is_rejected_instead_of_served_as_csv(client, bad):
    resp = client.get(f"/api/export?start=2026-08-01&end=2026-08-20&format={bad}")
    assert resp.status_code == 422, (
        f"format={bad!r} returned {resp.status_code} "
        f"({resp.headers.get('content-type')}) — a typo must not download a CSV"
    )
    detail = _detail_text(resp)
    assert "csv" in detail and "json" in detail, (
        f"the 422 must name the allowed values, got: {detail}"
    )


def test_the_declared_values_still_work(client):
    csv_resp = client.get("/api/export?start=2026-08-01&end=2026-08-20&format=csv")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]

    json_resp = client.get("/api/export?start=2026-08-01&end=2026-08-20&format=json")
    assert json_resp.status_code == 200
    assert "application/json" in json_resp.headers["content-type"]


def test_omitting_the_parameter_keeps_the_documented_default(client):
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-20")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]


def test_the_content_type_matches_the_branch_that_ran(client):
    """CSV is text and needs a charset; JSON must not be labelled as CSV."""
    csv_resp = client.get("/api/export?start=2026-08-01&end=2026-08-20&format=csv")
    assert csv_resp.headers["content-type"].startswith("text/csv"), csv_resp.headers["content-type"]
    assert "charset=utf-8" in csv_resp.headers["content-type"], (
        "CSV carries 中文 company names; the charset must be explicit"
    )

    json_resp = client.get("/api/export?start=2026-08-01&end=2026-08-20&format=json")
    assert json_resp.headers["content-type"].startswith("application/json"), json_resp.headers["content-type"]


def test_the_openapi_schema_advertises_the_enum():
    schema = app.openapi()
    params = {
        p["name"]: p
        for p in schema["paths"]["/api/export"]["get"].get("parameters", [])
    }
    format_param = params["format"]["schema"]
    assert format_param.get("type") == "string"
    assert format_param.get("enum") == ["csv", "json"], (
        f"clients read the contract from here, got {format_param}"
    )
