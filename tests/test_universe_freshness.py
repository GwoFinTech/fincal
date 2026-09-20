"""Regression tests for the live default-universe accessor (Issue #58).

The default calendar/export universe (``/api/earnings`` default view,
``/api/export``, ``/api/popular``) used to be read once at import time, so an
upstream watchlist change — or a source that was down during startup — stayed
frozen for the whole process lifetime and only a container restart could pick it
up.  These tests pin the properties the fix has to hold:

1. importing ``app.earnings`` reads nothing;
2. an upstream change lands without a restart (TTL / explicit invalidation);
3. an admin watchlist write is visible on the very next request;
4. a startup-time degradation neither sticks nor hides;
5. the ``/api/popular`` contract is unchanged and TTL-internal reads hit the
   source only once.
"""
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db
from app import universe as universe_mod
from app.auth import get_current_user
from app.main import app
from app.routers import api as api_router
from app.watchlist.base import WatchlistSource, group_symbols_by_market

ADMIN = {"id": 1, "email": "t@t.com", "name": "T", "role": "admin"}

# Superset row: satisfies ensure_user() and the managed-watchlist RETURNING clauses.
_ROW = {
    "id": 1,
    "portal_user_id": 1,
    "email": "t@t.com",
    "name": "T",
    "ical_token": "tok-abc",
    "symbol": "AAPL",
    "market": "US",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "cnt": 0,
}


class _FakeSource(WatchlistSource):
    """Watchlist source whose upstream contents the test controls."""

    def __init__(self, codes=None, fail=False):
        self.codes = list(codes or [])
        self.fail = fail
        self.reads = 0

    def fetch_symbols(self):
        self.reads += 1
        if self.fail:
            raise RuntimeError("tsummt unreachable")
        return list(self.codes)

    @property
    def source_name(self):
        return "fake"


class _FakeCursor:
    rowcount = 1

    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return dict(_ROW)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self, **kw):
        return _FakeCursor()

    def __enter__(self):
        return _FakeCursor()

    def __exit__(self, *a):
        return False


def _install_universe(monkeypatch, source, *, ttl=120.0, error_retry=15.0):
    """Point the process-wide universe at a test source."""
    universe = universe_mod.SymbolUniverse(
        ttl_seconds=ttl, error_retry_seconds=error_retry, source_factory=lambda: source
    )
    monkeypatch.setattr(universe_mod, "_universe", universe)
    return universe


# ── Criterion 1: importing the module must not read the source ──────

def test_importing_earnings_does_not_read_the_watchlist_source():
    child = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        import app.watchlist as watchlist

        reads = []

        def _probe():
            reads.append(1)
            raise AssertionError("watchlist source read at import time")

        watchlist.get_source = _probe
        import app.earnings  # noqa: F401
        import app.universe as universe
        print(len(reads))
        print(universe._universe.status()["error_code"])
    """)
    proc = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, cwd=str(ROOT)
    )
    assert proc.returncode == 0, proc.stderr
    reads, error_code = proc.stdout.split()
    assert reads == "0", "the source was read while importing app.earnings"
    # Nothing loaded yet — and it says so instead of pretending to be live.
    assert error_code == universe_mod.NOT_LOADED


def test_deprecated_constants_still_resolve_through_the_live_accessor(monkeypatch):
    """Out-of-tree importers of POPULAR_STOCKS_* keep working (deprecated shim)."""
    _install_universe(monkeypatch, _FakeSource(["AAPL.US", "0700.HK"]))
    import app.earnings as earnings

    assert earnings.POPULAR_STOCKS_US == ["AAPL"]
    assert earnings.POPULAR_STOCKS_HK == ["0700.HK"]
    assert not hasattr(earnings, "NOT_A_THING")


# ── Criterion 2/6: upstream changes land; TTL bounds the reads ──────

def test_universe_picks_up_source_changes_without_a_restart(monkeypatch):
    source = _FakeSource(["AAPL.US", "0700.HK"])
    universe = _install_universe(monkeypatch, source, ttl=5.0)

    assert universe.get() == (["AAPL"], ["0700.HK"])
    assert source.reads == 1

    source.codes = ["AAPL.US", "NEWSYM.US"]
    # Within the TTL the cached universe is reused: no per-request source read.
    assert universe.get() == (["AAPL"], ["0700.HK"])
    assert source.reads == 1

    # Invalidation (what the admin hook does) makes it visible at once.
    universe.invalidate()
    assert universe.get() == (["AAPL", "NEWSYM"], [])
    assert source.reads == 2


def test_universe_reloads_after_the_ttl_expires(monkeypatch):
    source = _FakeSource(["AAPL.US"])
    universe = _install_universe(monkeypatch, source, ttl=0.05)

    assert universe.get() == (["AAPL"], [])
    source.codes = ["AAPL.US", "XYZ.US"]
    time.sleep(0.1)
    assert universe.get() == (["AAPL", "XYZ"], [])
    assert source.reads == 2


# ── Criterion 4: degradation is bounded, recoverable and visible ────

def test_startup_failure_uses_the_fallback_then_recovers(monkeypatch):
    source = _FakeSource(["AAPL.US"], fail=True)
    universe = _install_universe(monkeypatch, source, ttl=60.0, error_retry=0.05)

    us, hk = universe.get()
    assert us == universe_mod.FALLBACK_US
    assert hk == universe_mod.FALLBACK_HK

    status = universe.status()
    assert status["source"] == "fallback"
    assert status["stale"] is True
    assert status["error_code"], "a degraded universe must not look healthy"
    assert status["last_success_at"] is None

    # The source comes back: no restart needed, just the bounded retry.
    source.fail = False
    source.codes = ["AAPL.US", "MSFT.US"]
    time.sleep(0.1)
    assert universe.get() == (["AAPL", "MSFT"], [])
    assert universe.status()["error_code"] is None
    assert universe.status()["stale"] is False
    assert universe.status()["last_success_at"]


def test_failure_after_success_keeps_the_last_good_universe(monkeypatch):
    source = _FakeSource(["AAPL.US"])
    universe = _install_universe(monkeypatch, source, ttl=0.05, error_retry=5.0)

    assert universe.get() == (["AAPL"], [])
    source.fail = True
    time.sleep(0.1)

    assert universe.get() == (["AAPL"], [])  # last good data, explicitly stale
    assert universe.status()["stale"] is True
    assert universe.status()["error_code"]


# ── Criterion 3: admin watchlist writes are visible immediately ─────

def test_admin_watchlist_write_is_visible_without_a_restart(monkeypatch):
    source = _FakeSource(["AAPL.US"])
    _install_universe(monkeypatch, source, ttl=120.0)
    api_router._earnings_cache.invalidate()

    captured = {}

    def _fake_fetch(*, symbols=None, markets=None, start=None, end=None):
        captured["symbols"] = list(symbols or [])
        return []

    app.dependency_overrides = {get_current_user: lambda: dict(ADMIN)}
    try:
        with patch.object(db, "db_cursor", lambda: _FakeConn()), \
                patch("app.earnings.fetch_earnings_from_db", _fake_fetch):
            client = TestClient(app, raise_server_exceptions=False)

            assert client.get("/api/earnings?watchlistOnly=false").status_code == 200
            assert "AAPL" in captured["symbols"]
            assert "XYZ" not in captured["symbols"]

            # The upstream universe now offers XYZ (managed + tsummt combined).
            source.codes = ["AAPL.US", "XYZ.US"]
            resp = client.post("/api/admin/watchlist", json={"symbol": "XYZ", "market": "US"})
            assert resp.status_code == 201, resp.text

            assert client.get("/api/earnings?watchlistOnly=false").status_code == 200
            assert "XYZ" in captured["symbols"], "admin write was masked by the universe cache"

            export = client.get("/api/export?start=2026-08-01&end=2026-08-31&format=json")
            assert export.status_code == 200
            assert "XYZ" in captured["symbols"], "export must use the same live universe"

            # Removing it must disappear from the calendar too, not linger.
            source.codes = ["AAPL.US"]
            assert client.delete("/api/admin/watchlist/1").status_code == 200
            assert client.get("/api/earnings?watchlistOnly=false").status_code == 200
            assert "XYZ" not in captured["symbols"]
    finally:
        app.dependency_overrides = {}
        api_router._earnings_cache.invalidate()


def test_diagnostics_exposes_the_universe_state(monkeypatch):
    source = _FakeSource(["AAPL.US"], fail=True)
    _install_universe(monkeypatch, source, ttl=60.0, error_retry=5.0)
    universe_mod.popular_stocks()  # a calendar request already loaded it

    app.dependency_overrides = {get_current_user: lambda: dict(ADMIN)}
    try:
        with patch.object(db, "db_cursor", lambda: _FakeConn()), \
                patch("app.freshness.check_freshness", lambda *a, **kw: {"status": "healthy"}):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/admin/diagnostics")
            body = resp.json()
    finally:
        app.dependency_overrides = {}

    assert resp.status_code == 200, resp.text
    universe = body["universe"]
    assert universe["error_code"], "degraded universe must carry an error code"
    assert universe["stale"] is True
    assert universe["source"] == "fallback"
    assert universe["symbol_count"] == len(universe_mod.FALLBACK_US) + len(universe_mod.FALLBACK_HK)
    assert universe["ttl_seconds"] == 60.0


# ── Criterion 5: contracts and symbol bucketing unchanged ──────────

def test_popular_endpoint_contract_is_unchanged(monkeypatch):
    _install_universe(monkeypatch, _FakeSource(["AAPL.US", "700.HK"]))
    app.dependency_overrides = {get_current_user: lambda: dict(ADMIN)}
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/popular")
    finally:
        app.dependency_overrides = {}

    assert resp.status_code == 200
    assert resp.json() == {"US": ["AAPL"], "HK": ["0700.HK"]}


def test_group_symbols_by_market_keeps_canonical_codes():
    assert group_symbols_by_market(["700.HK", "aapl.us", "TSLA"]) == {
        "US": ["AAPL", "TSLA"],
        "HK": ["0700.HK"],
    }


def test_source_market_accessor_still_delegates_to_the_same_mapping():
    source = _FakeSource(["700.HK", "aapl.us"])
    assert source.get_symbols_by_market() == {"US": ["AAPL"], "HK": ["0700.HK"]}
    assert source.get_symbols_by_market(force_refresh=True) == {"US": ["AAPL"], "HK": ["0700.HK"]}
