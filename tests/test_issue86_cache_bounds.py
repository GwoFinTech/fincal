"""Bounds for the `/api/earnings` response cache (Issue #86).

The cache used to be an unbounded dict and nothing ever deleted an entry:
`default_ttl`/`stale_ttl` only decided whether a payload had to be refreshed,
not whether it was still kept. Because the key is built from caller-chosen
`start`/`end` dates, an authenticated caller could pin one payload per window
forever (measured: 1.96 MiB for a ten-year window), and `invalidate()` — called
on every watchlist write — left the ticket/bookkeeping dicts untouched.

These tests pin the three properties the issue asks for:

* a capacity bound with LRU eviction, and eviction that lets the key be fetched
  again (so the bound is not achieved by returning wrong data);
* `invalidate()` clearing all four dicts, without ever deleting the
  singleflight `Event` of an in-flight producer (Issue #34);
* a window bound: inverted or over-wide `start`/`end` answer 422
  `invalid_window` instead of being cached.
"""
import re
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PY = ROOT / "app" / "config.py"
API_ROUTER = ROOT / "app" / "routers" / "api.py"


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── capacity + LRU ─────────────────────────────────────────────────────────

def test_capacity_evicts_the_least_recently_used_key_and_refetches_it():
    """Acceptance #1: after max+N keys, len(entries) <= max, oldest gone, and a
    request for the evicted key goes back to the fetcher."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=120.0, max_entries=3)
    calls: dict[str, int] = {}

    def fetch(key):
        def _f():
            calls[key] = calls.get(key, 0) + 1
            return {"key": key, "n": calls[key]}
        return _f

    for i in range(5):
        key = f"earnings:2026-0{i + 1}-01:2026-12-31:False:1"
        data, _ = cache.get_or_refresh(key, fetch(key))
        assert data["key"] == key

    assert len(cache._entries) <= 3, f"cache grew past its bound: {list(cache._entries)}"
    first = "earnings:2026-01-01:2026-12-31:False:1"
    assert first not in cache._entries, "the oldest key must be the one evicted"
    assert cache._tickets.get(first) is None, "an evicted key must not keep its ticket"
    assert cache._applied.get(first) is None, "an evicted key must not keep its applied marker"

    # Re-requesting the evicted key must not be answered by the cache.
    before = calls[first]
    data, _ = cache.get_or_refresh(first, fetch(first))
    assert calls[first] == before + 1, "an evicted key must be fetched again"
    assert data["n"] == before + 1, "and the caller must get the new payload"


def test_a_key_that_is_still_being_served_survives_eviction():
    """LRU, not FIFO: touching a key moves it out of the eviction order."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=120.0, max_entries=3)
    for key in ("k1", "k2", "k3"):
        cache.get_or_refresh(key, lambda key=key: {"key": key})

    # Use k1 — it is the oldest insertion but the most recent use.
    cache.get_or_refresh("k1", lambda: {"key": "k1"}, ttl=120.0)

    cache.get_or_refresh("k4", lambda: {"key": "k4"})

    assert "k1" in cache._entries, "a key served since the others must not be evicted"
    assert "k2" not in cache._entries, "the least recently used key goes instead"
    assert len(cache._entries) == 3


def test_eviction_never_drops_a_key_with_a_live_producer():
    """Eviction must skip a key whose fetch owns the singleflight slot, or it
    would delete the Event that other callers are waiting on."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=0.05, stale_ttl=60.0, max_entries=2)
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        release.wait(timeout=3.0)
        return {"refreshed": True}

    cache.put("hot", {"init": True})
    time.sleep(0.1)  # expired -> the next read starts a background refresh
    cache.get_or_refresh("hot", slow_fetch, ttl=0.05)
    assert started.wait(timeout=2.0), "background refresh never started"
    assert "hot" in cache._locks, "precondition: the refresh owns the slot"

    # Fill past the bound while the refresh is in flight.
    cache.put("k2", {"v": 2})
    cache.put("k3", {"v": 3})

    assert "hot" in cache._entries, "an in-flight key must not be evicted"
    assert "hot" in cache._locks, "its singleflight Event must survive too"

    release.set()
    assert _wait_for(lambda: "hot" not in cache._locks), "the refresh never finished"
    assert cache.get("hot").data == {"refreshed": True}


# ── invalidate() ───────────────────────────────────────────────────────────

def test_invalidate_clears_entries_locks_tickets_and_applied():
    """Acceptance #2: all four dicts are empty afterwards."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=120.0, max_entries=512)
    for i in range(5):
        key = f"earnings:2026-0{i + 1}-01:2026-12-31:False:1"
        cache.get_or_refresh(key, lambda key=key: {"key": key})

    assert cache._entries and cache._tickets and cache._applied

    cache.invalidate()

    assert cache._entries == {}, "entries survived invalidate()"
    assert cache._locks == {}, "locks survived invalidate()"
    assert cache._tickets == {}, "tickets survived invalidate() — the leak in Issue #86"
    assert cache._applied == {}, "applied markers survived invalidate()"


def test_invalidate_of_one_key_leaves_the_others_intact():
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=120.0)
    for key in ("k1", "k2"):
        cache.get_or_refresh(key, lambda key=key: {"key": key})

    cache.invalidate("k1")

    assert "k1" not in cache._entries
    assert "k1" not in cache._tickets
    assert "k1" not in cache._applied
    assert "k2" in cache._entries, "a scoped invalidate must not clear other keys"
    assert cache._tickets.get("k2"), "nor their bookkeeping"


def test_invalidate_keeps_the_singleflight_slot_of_an_in_flight_fetch():
    """A producer mid-fetch keeps serving the callers waiting on it."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=120.0, waiter_timeout=3.0, max_entries=8)
    calls = []
    release = threading.Event()

    def slow_fetch():
        calls.append(1)
        release.wait(timeout=3.0)
        return {"payload": "fresh"}

    results = []
    producer = threading.Thread(
        target=lambda: results.append(cache.get_or_refresh("k", slow_fetch, ttl=120.0))
    )
    producer.start()
    assert _wait_for(lambda: "k" in cache._locks), "producer never took the slot"

    cache.invalidate()  # e.g. a watchlist write landing mid-fetch

    joiner_result = {}

    def joiner():
        joiner_result["value"] = cache.get_or_refresh("k", slow_fetch, ttl=120.0)

    thread = threading.Thread(target=joiner)
    thread.start()
    time.sleep(0.1)
    release.set()
    producer.join(timeout=5.0)
    thread.join(timeout=5.0)

    assert len(calls) == 1, f"singleflight broke across invalidate(): {len(calls)} fetches"
    assert results and results[0][0] == {"payload": "fresh"}
    assert joiner_result["value"][0] == {"payload": "fresh"}, "the joiner must not refetch"
    assert cache.get("k").data == {"payload": "fresh"}


# ── window validation (endpoint level) ─────────────────────────────────────

def _client_with_auth():
    from fastapi.testclient import TestClient

    from app.auth import get_current_user
    from app.main import app

    app.dependency_overrides = {
        get_current_user: lambda: {
            "id": 4242, "email": "t@t.com", "name": "T", "role": "user",
        },
    }
    return TestClient(app, raise_server_exceptions=False), app


def test_decade_long_window_is_rejected_with_a_code_not_cached():
    """Acceptance #3: start=2020, end=2030 → 422 invalid_window."""
    from app.routers import api as api_router

    client, app = _client_with_auth()
    api_router._earnings_cache.invalidate()
    try:
        with patch.object(api_router, "ensure_user", lambda *a, **k: {"id": 4242}):
            response = client.get("/api/earnings?start=2020-01-01&end=2030-01-01")
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_window", response.text
    assert api_router._earnings_cache._entries == {}, "a rejected window must not be cached"


def test_inverted_window_is_rejected():
    from app.routers import api as api_router

    client, app = _client_with_auth()
    try:
        with patch.object(api_router, "ensure_user", lambda *a, **k: {"id": 4242}):
            response = client.get("/api/earnings?start=2026-05-01&end=2026-04-01")
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_window", response.text


def test_the_shipped_view_windows_are_still_accepted():
    """The bound must not break the windows the product itself requests: the
    calendar view (previous month .. next month) and the watchlist view
    (today .. today + CALENDAR_FORWARD_DAYS)."""
    from app import config
    from app import db
    from app import earnings as earnings_mod
    from app.routers import api as api_router

    class _FakeCursor:
        def execute(self, *args, **kwargs):
            pass

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _FakeConn:
        def __enter__(self):
            return _FakeCursor()

        def __exit__(self, *args):
            return False

    client, app = _client_with_auth()
    api_router._earnings_cache.invalidate()
    today = date.today()
    calendar_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    calendar_end = (today.replace(day=1) + timedelta(days=62)).replace(day=1) - timedelta(days=1)
    try:
        with patch.object(api_router, "ensure_user", lambda *a, **k: {"id": 4242}), \
             patch.object(db, "db_cursor", lambda: _FakeConn()), \
             patch.object(earnings_mod, "fetch_earnings_from_db", lambda **kwargs: []), \
             patch("app.universe.popular_stocks", lambda: (["AAPL"], [])):
            calendar_view = client.get(
                f"/api/earnings?start={calendar_start}&end={calendar_end}"
            )
            watchlist_view = client.get(
                f"/api/earnings?start={today}&end={today + timedelta(days=config.CALENDAR_FORWARD_DAYS)}"
            )
    finally:
        app.dependency_overrides = {}

    assert calendar_view.status_code == 200, calendar_view.text
    assert watchlist_view.status_code == 200, watchlist_view.text


def test_the_shipped_limits_cover_every_view_and_bound_the_cache():
    """Defaults are part of the contract: a documented cap and a window bound
    that still admits the default and watchlist views."""
    from app import config

    source = CONFIG_PY.read_text(encoding="utf-8")
    match = re.search(r'EARNINGS_CACHE_MAX_ENTRIES\s*=\s*int\(\s*os\.getenv\(\s*"EARNINGS_CACHE_MAX_ENTRIES"\s*,\s*"(\d+)"', source)
    assert match, "EARNINGS_CACHE_MAX_ENTRIES default not found in app/config.py"
    cap = int(match.group(1))
    # The cap is unusable if it is smaller than one user's working set (~14
    # windows: 12 browsed months + the watchlist view + the default view), and
    # pointless if it is so large that the retained bodies dwarf the process.
    # A payload is ~1.05 MiB, so 128 entries ≈ 134 MiB, and 6 users x 14 = 84.
    assert cap >= 64, f"cap {cap} would evict the months a single user just browsed"
    assert cap * 1.05 <= 256, (
        f"cap {cap} allows ~{cap * 1.05:.0f} MiB of cached bodies — too much for this service"
    )

    assert config.EARNINGS_MAX_WINDOW_DAYS >= config.CALENDAR_FORWARD_DAYS, (
        "the window bound must admit the watchlist view (today..today+CALENDAR_FORWARD_DAYS)"
    )
    assert config.EARNINGS_MAX_WINDOW_DAYS >= config.CALENDAR_FORWARD_DAYS + 7, (
        "the window bound must admit the default view (today-7d..today+CALENDAR_FORWARD_DAYS)"
    )


def test_the_router_uses_the_configured_bound():
    """The cap is injected from config, not hardcoded at the call site."""
    source = API_ROUTER.read_text(encoding="utf-8")
    assert "max_entries=config.EARNINGS_CACHE_MAX_ENTRIES" in source
    assert "_validate_window(start, end)" in source
