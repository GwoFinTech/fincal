"""Regression coverage for response-model shapes found in production."""
import re
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app
from app import db
from app.auth import get_current_user
from app.schemas import EarningItem


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


# ── Issue #73: EarningItem must declare every field the read path returns ──
#
# ``/api/earnings`` is the only JSON calendar outlet, and FastAPI drops any row
# field the response model does not declare — silently.  Both exports and the
# read SQL keep carrying such a field, so the same row is visible in
# ``/api/export`` and invisible here (this is how ``estimate_as_of``,
# ``actual_as_of`` and ``consensus_normalized_net_income`` disappeared, and how
# the phantom ``created_at`` survived).  The contract is therefore asserted
# three ways below: the source of truth (``app/db.py`` + the read SQL), the
# model, and the live response.

# Fields the read path derives instead of selecting — every other field of
# ``EarningItem`` must come from a column or a ``consensus_*`` alias.
COMPUTED_READ_PATH_FIELDS = {"comparison_unavailable_reason"}

_EARNINGS_ALTER_LOOP = re.compile(
    r"for column, definition in \((.*?)\n\s*\):\s*\n\s*cur\.execute\(f\"ALTER TABLE earnings ADD COLUMN IF NOT EXISTS",
    re.S,
)
_EARNINGS_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS earnings \((.*?)\n\s*\);", re.S)
_COLUMN_DEF = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s+(?:SERIAL|BIGSERIAL|TEXT|INTEGER|NUMERIC|BOOLEAN|DATE|TIMESTAMPTZ)\b",
    re.M,
)
_ALTER_COLUMN = re.compile(
    r'\(\s*"([a-z_][a-z0-9_]*)"\s*,\s*"(?:TEXT|TIMESTAMPTZ|NUMERIC|INTEGER|BOOLEAN|DATE)\s?[^"]*"',
)
_CONSENSUS_ALIAS = re.compile(r"c\.([a-z_][a-z0-9_]*)\s+AS\s+([a-z_][a-z0-9_]*)")


def _earnings_columns() -> set[str]:
    """The ``earnings`` columns exactly as ``app/db.py`` declares them."""
    source = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    created = _EARNINGS_CREATE.search(source)
    assert created, "app/db.py no longer declares the earnings table as expected"
    columns = set(_COLUMN_DEF.findall(created.group(1)))

    altered = _EARNINGS_ALTER_LOOP.search(source)
    assert altered, "app/db.py no longer lists the additive earnings columns as expected"
    columns |= set(_ALTER_COLUMN.findall(altered.group(1)))

    # Guard the guards: a parse that silently returns nothing would make the
    # equality below pass for the wrong reason.
    assert {"id", "symbol", "company_name", "estimate_as_of", "actual_as_of"} <= columns, sorted(columns)
    return columns


def _read_path_fields() -> set[str]:
    """Every field ``fetch_earnings_from_db`` puts on a row."""
    source = (ROOT / "app" / "earnings.py").read_text(encoding="utf-8")
    assert "SELECT e.*" in source, "the read path no longer selects e.* — update this contract test"
    aliases = {alias for _, alias in _CONSENSUS_ALIAS.findall(source)}
    assert len(aliases) >= 8, sorted(aliases)
    return _earnings_columns() | aliases | COMPUTED_READ_PATH_FIELDS


def _full_row() -> dict:
    """One row shaped exactly like ``fetch_earnings_from_db`` output."""
    return {
        "id": 42, "symbol": "AAPL", "market": "US", "company_name": "Apple Inc.",
        "report_date": "2026-07-30", "report_type": "Q",
        "fiscal_year": 2026, "fiscal_quarter": 3, "before_after": "after",
        "eps_estimate": 1.89, "eps_actual": 2.03,
        "revenue_estimate": 90000.0, "revenue_actual": 95000.0,
        "is_predicted": False,
        "date_source": "futu", "date_status": "reported",
        "estimate_source": "longbridge", "estimate_as_of": "2026-07-20T08:03:13+08:00",
        "actual_source": "futu", "actual_as_of": "2026-07-31T02:43:02+08:00",
        "estimate_currency": "USD", "estimate_basis": "consensus",
        "actual_currency": "USD", "actual_basis": "gaap",
        "comparison_unavailable_reason": None,
        "consensus_eps_gaap": 1.90, "consensus_eps_adjusted": 1.92,
        "consensus_revenue": 91000.0, "consensus_ebit": 30000.0,
        "consensus_net_income": 25000.0, "consensus_normalized_net_income": 24500.0,
        "consensus_currency": "USD", "consensus_fetched_at": "2026-07-19T00:00:00+00:00",
        "updated_at": "2026-07-31T02:43:02+08:00",
    }


def test_earning_item_mirrors_the_read_path_field_set():
    """Read path and response model agree exactly — in both directions."""
    read_path = _read_path_fields()
    model_fields = set(EarningItem.model_fields)

    assert read_path == model_fields, (
        "EarningItem has drifted from the read path:\n"
        f"  dropped by the response model : {sorted(read_path - model_fields)}\n"
        f"  declared but never supplied   : {sorted(model_fields - read_path)}"
    )
    # The three fields this issue is about, named so the regression cannot be
    # re-introduced by a rename either.
    assert {"estimate_as_of", "actual_as_of", "consensus_normalized_net_income"} <= model_fields
    # ``earnings`` has no ``created_at`` column: the model used to advertise it
    # and answered ``null`` forever (Issue #73).
    assert "created_at" not in model_fields


def test_earning_item_keeps_every_read_path_field():
    """Nothing a row carries may be dropped on the way out of the model."""
    row = _full_row()
    assert set(row) == _read_path_fields()

    item = EarningItem.model_validate(row)
    dumped = item.model_dump()
    assert set(dumped) == set(row)
    # Values, not just keys: the timestamps must survive serialisation intact.
    assert item.estimate_as_of.isoformat() == "2026-07-20T08:03:13+08:00"
    assert item.actual_as_of.isoformat() == "2026-07-31T02:43:02+08:00"
    assert item.consensus_normalized_net_income == 24500.0


def test_api_earnings_returns_the_provenance_timestamps():
    """/api/earnings (the `response_model` path) exposes all three fields."""
    from app.routers import api as api_module

    api_module._earnings_cache.invalidate()
    row = _full_row()
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "user",
    }
    try:
        with patch.object(db, "db_cursor", lambda: _FakeConn()), \
             patch("app.routers.api.ensure_user", return_value={"id": 1, "portal_user_id": 1, "email": "t@t.com", "name": "T", "ical_token": "tok"}), \
             patch("app.universe.popular_stocks", return_value=([], [])), \
             patch("app.earnings.fetch_earnings_from_db", return_value=[row]):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/earnings?start=2026-07-01&end=2026-08-31")
    finally:
        app.dependency_overrides = {}
        api_module._earnings_cache.invalidate()

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert len(body) == 1
    assert set(body[0]) == set(EarningItem.model_fields)
    assert body[0]["estimate_as_of"].startswith("2026-07-20T08:03:13")
    assert body[0]["actual_as_of"].startswith("2026-07-31T02:43:02")
    assert body[0]["consensus_normalized_net_income"] == 24500.0
    assert "created_at" not in body[0]
