"""The calendar forward horizon is one value, shared by every read outlet.

Issue #59: the iCal feed stopped at a hardcoded ``today + 120d`` while the
predictor places dates up to ``scripts/predict_earnings.py::MAX_FUTURE_DAYS``
ahead (4 quarters, ~1 year) and the app UI already read the full horizon. 80% of
the predictions that were in the database *and* visible in the app were
therefore silently missing from the subscription — and unlike a calendar page,
a subscriber has no second path to that event: whatever the window leaves out is
never delivered.

The invariant pinned here is the shipped default, not the environment of the
test runner: a deployment that narrows ``CALENDAR_FORWARD_DAYS`` recreates the
gap on purpose.

Outlets covered: ``/ical/{token}`` (see also tests/test_ical_subscription.py)
and the default window of ``/api/earnings`` / ``fetch_earnings_from_db``; the
watchlist view reads ``WATCHLIST_NEXT_WINDOW_DAYS``, pinned to the same horizon
by tests/test_frontend_structure.py.
"""
import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from app import config

REPO_ROOT = Path(__file__).resolve().parents[1]
PREDICT_EARNINGS = REPO_ROOT / "scripts" / "predict_earnings.py"
CONFIG_PY = REPO_ROOT / "app" / "config.py"
ICAL_ROUTER = REPO_ROOT / "app" / "routers" / "ical.py"
EARNINGS_PY = REPO_ROOT / "app" / "earnings.py"
API_ROUTER = REPO_ROOT / "app" / "routers" / "api.py"

LOOKBACK_DAYS = 7


def _prediction_horizon_days() -> int:
    """The furthest ahead the predictor may place a report date."""
    match = re.search(
        r"^MAX_FUTURE_DAYS\s*=\s*(\d+)\s*$",
        PREDICT_EARNINGS.read_text(encoding="utf-8"),
        re.M,
    )
    assert match, "MAX_FUTURE_DAYS not found in scripts/predict_earnings.py"
    return int(match.group(1))


def _configured_default_days() -> int:
    """The default compiled into app/config.py (ignores any env override)."""
    match = re.search(
        r'CALENDAR_FORWARD_DAYS\s*=\s*int\(\s*os\.getenv\(\s*"CALENDAR_FORWARD_DAYS"\s*,\s*"(\d+)"',
        CONFIG_PY.read_text(encoding="utf-8"),
    )
    assert match, "CALENDAR_FORWARD_DAYS default not found in app/config.py"
    return int(match.group(1))


def test_shipped_default_window_covers_the_whole_prediction_horizon():
    horizon = _prediction_horizon_days()
    default_days = _configured_default_days()
    assert default_days >= horizon, (
        f"default calendar window ({default_days}d) is shorter than the prediction "
        f"horizon ({horizon}d): predicted events beyond it can never reach a subscriber"
    )
    assert config.CALENDAR_FORWARD_DAYS >= horizon, (
        "this environment narrows CALENDAR_FORWARD_DAYS below the prediction horizon"
    )


def test_no_read_outlet_keeps_its_own_forward_window_literal():
    """Each outlet reads the shared constant — no ``days=90`` / ``days=120`` copy
    may reappear next to it, which is how the three windows drifted apart."""
    ical_src = ICAL_ROUTER.read_text(encoding="utf-8")
    assert "days=120" not in ical_src, "hardcoded 120-day feed window came back"
    assert "config.CALENDAR_FORWARD_DAYS" in ical_src, (
        "the iCal router must take its forward window from app.config"
    )

    for path in (EARNINGS_PY, API_ROUTER):
        src = path.read_text(encoding="utf-8")
        assert "days=90" not in src, f"hardcoded 90-day window came back in {path.name}"
        assert "config.CALENDAR_FORWARD_DAYS" in src, (
            f"{path.name} must take its default forward window from app.config"
        )


def test_ical_endpoint_requests_the_shared_forward_window():
    """/ical/{token} asks the reader for today..today+horizon (behavioural)."""
    from fastapi.testclient import TestClient

    from app import db
    from app import earnings as earnings_mod
    from app.main import app
    from app.routers import ical as ical_router

    captured: dict = {}

    class _FakeCursor:
        def execute(self, *args, **kwargs):
            pass

        def fetchone(self):
            return {"id": 1, "email": "t@t.com", "name": "T"}

        def fetchall(self):
            return [{"symbol": "0700", "market": "HK"}]

    class _FakeConn:
        def __enter__(self):
            return _FakeCursor()

        def __exit__(self, *args):
            return False

    def _capture(**kwargs):
        captured.update(kwargs)
        return []

    ical_router._ical_cache.clear()
    ical_router._content_versions.clear()
    app.dependency_overrides = {}
    patchers = [
        patch.object(db, "db_cursor", lambda: _FakeConn()),
        patch.object(earnings_mod, "fetch_earnings_from_db", _capture),
    ]
    for p in patchers:
        p.start()
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/ical/tok-abc")
    finally:
        for p in patchers:
            p.stop()

    assert response.status_code == 200
    today = date.today()
    assert captured["end"] == today + timedelta(days=config.CALENDAR_FORWARD_DAYS)
    assert captured["start"] == today - timedelta(days=LOOKBACK_DAYS)


def test_fetch_earnings_default_window_is_the_shared_horizon():
    """A caller that omits the window must not get a shorter calendar than the
    feed/watchlist show (the API default used to be ``today + 90d``)."""
    from app import db
    from app import earnings as earnings_mod

    captured: dict = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            captured["params"] = params

        def fetchall(self):
            return []

    with patch.object(db, "db_cursor", lambda: _FakeCursor()):
        assert earnings_mod.fetch_earnings_from_db() == []

    start, end = captured["params"][0], captured["params"][1]
    today = date.today()
    assert end == today + timedelta(days=config.CALENDAR_FORWARD_DAYS)
    assert start == today - timedelta(days=LOOKBACK_DAYS)
