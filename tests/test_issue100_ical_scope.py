"""Issue #100: the iCal ``scope=all`` feed follows the system universe.

The subscription had two symbol universes. ``scope=watchlist`` and
``/api/earnings?watchlistOnly=true`` agreed, but ``scope=all`` ran
``SELECT DISTINCT symbol, market FROM earnings`` and in production delivered
2,544 symbols / 3,177 events — 86% of them for symbols the calendar, the export
and the read-only 「系统自选范围」 page could not show, and 6.7x the size the
#99/#86 capacity work had assumed.

These tests pin the four properties the issue asks for:

* ``scope=all`` is generated from ``app.universe.popular_stocks()`` (the one
  accessor the other outlets use), filtered by ``markets``;
* the earnings-table symbol scan is gone from that path;
* a universe change (admin watchlist write) invalidates the *feed* cache, so
  the next request already reflects it instead of waiting out the 3600 s TTL;
* the feed cache is bounded in bytes of feed text, not in slots.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ICAL_ROUTER = ROOT / "app" / "routers" / "ical.py"

USER = {"id": 1, "email": "t@t.com", "name": "T", "ical_token": "tok-abc"}


def _event(symbol: str, market: str) -> dict:
    return {
        "symbol": symbol,
        "market": market,
        "company_name": symbol,
        "report_date": date(2026, 11, 5),
        "before_after": "after",
        "updated_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
    }


class _RecordingCursor:
    """Fake cursor that serves the two feed lookups and records every statement."""

    def __init__(self, watchlist: list[dict] | None = None):
        self._watchlist = watchlist or []
        self.statements: list[str] = []
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        self._last_sql = " ".join(sql.split())

    def fetchone(self):
        if "FROM users" in self._last_sql:
            return USER
        return None

    def fetchall(self):
        if "FROM watchlist" in self._last_sql:
            return self._watchlist
        return []


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *args):
        return False


def _client(watchlist=None, universe=None):
    """TestClient for /ical/{token} with the DB and universe boundaries faked.

    ``universe`` is a ``{"US": [...], "HK": [...]}`` map served by a patched
    ``app.universe.popular_stocks``; ``fetch_earnings_from_db`` is patched to
    return one event per requested symbol, so the assertion can read the symbol
    set the feed asked for.
    """
    from fastapi.testclient import TestClient

    from app import db
    from app import earnings as earnings_mod
    from app.main import app
    from app.routers import ical as ical_router

    ical_router._ical_cache.clear()
    ical_router._content_versions.clear()
    app.dependency_overrides = {}

    cursor = _RecordingCursor(watchlist)
    universe = {"US": ["AAPL", "MSFT"], "HK": ["0700.HK"]} if universe is None else universe
    calls: list[dict] = []

    def fake_fetch(symbols=None, markets=None, start=None, end=None):
        calls.append({"symbols": list(symbols or []), "markets": list(markets or [])})
        rows = []
        for symbol in symbols or []:
            market = "HK" if symbol.endswith(".HK") else "US"
            if markets and market not in markets:
                continue
            rows.append(_event(symbol, market))
        return rows

    patchers = [
        patch.object(db, "db_cursor", lambda: _FakeConn(cursor)),
        patch.object(earnings_mod, "fetch_earnings_from_db", fake_fetch),
        patch("app.universe.popular_stocks", lambda **kw: (list(universe["US"]), list(universe["HK"]))),
    ]
    for p in patchers:
        p.start()
    client = TestClient(app, raise_server_exceptions=False)
    return client, calls, cursor, patchers


def _stop(patchers):
    from app.main import app

    for p in patchers:
        p.stop()
    app.dependency_overrides = {}


# ── acceptance #1/#2: the feed's symbol source ─────────────────────────────


def test_scope_all_feeds_exactly_the_system_universe():
    """`scope=all` asks for the universe's symbols, not the earnings table."""
    client, calls, cursor, patchers = _client()
    try:
        response = client.get("/ical/tok-abc?scope=all")
    finally:
        _stop(patchers)

    assert response.status_code == 200, response.text
    assert calls, "the feed never fetched earnings"
    assert calls[0]["symbols"] == ["AAPL", "MSFT", "0700.HK"], (
        f"scope=all used {calls[0]['symbols']} — expected the system universe"
    )
    assert calls[0]["markets"] == ["US", "HK"]


def test_scope_all_honours_the_markets_filter():
    """The universe is narrowed by ``markets`` instead of by the SQL market list."""
    client, calls, _cursor, patchers = _client()
    try:
        response = client.get("/ical/tok-abc?scope=all&markets=HK")
    finally:
        _stop(patchers)

    assert response.status_code == 200, response.text
    assert calls[0]["symbols"] == ["0700.HK"]
    assert calls[0]["markets"] == ["HK"]


def test_scope_all_no_longer_reads_the_earnings_table():
    """The ``DISTINCT symbol, market FROM earnings`` scan is gone (9,932 args)."""
    source = ICAL_ROUTER.read_text(encoding="utf-8")
    assert "DISTINCT symbol, market FROM earnings" not in source, (
        "scope=all must read app.universe.popular_stocks(), not the earnings table"
    )
    assert "popular_stocks_by_market" in source

    client, _calls, cursor, patchers = _client()
    try:
        client.get("/ical/tok-abc?scope=all")
    finally:
        _stop(patchers)
    assert not any("FROM earnings" in sql for sql in cursor.statements), cursor.statements


def test_scope_watchlist_is_unchanged():
    """The personal scope still reads the caller's own watchlist only."""
    client, calls, _cursor, patchers = _client(watchlist=[{"symbol": "AAPL", "market": "US"}])
    try:
        response = client.get("/ical/tok-abc")
    finally:
        _stop(patchers)

    assert response.status_code == 200, response.text
    assert calls[0]["symbols"] == ["AAPL"]
    assert calls[0]["markets"] == ["US", "HK"]


def test_an_empty_universe_serves_an_empty_calendar():
    """No symbols means an empty VCALENDAR, never an unfiltered read (Issue #25)."""
    client, calls, _cursor, patchers = _client(universe={"US": [], "HK": []})
    try:
        response = client.get("/ical/tok-abc?scope=all")
    finally:
        _stop(patchers)

    assert response.status_code == 200, response.text
    assert not calls, "an empty universe must not reach the database"
    assert "BEGIN:VCALENDAR" in response.text
    assert "BEGIN:VEVENT" not in response.text


# ── acceptance #4: a universe change is visible on the next request ────────


def test_universe_change_invalidates_every_cached_feed():
    """A symbol added to the universe lands in the feed of the *next* request."""
    client, calls, _cursor, patchers = _client()
    try:
        first = client.get("/ical/tok-abc?scope=all")
        assert first.status_code == 200
        assert "TSLA" not in calls[0]["symbols"]

        # The admin adds TSLA: the universe accessor now reports it, and the
        # mutation path calls invalidate_symbol_universe().
        with patch(
            "app.universe.popular_stocks",
            lambda **kw: (["AAPL", "MSFT", "TSLA"], ["0700.HK"]),
        ), patch("app.watchlist.invalidate_cache", lambda: None):
            from app.universe import invalidate_symbol_universe

            invalidate_symbol_universe()
            second = client.get("/ical/tok-abc?scope=all")
    finally:
        _stop(patchers)

    assert second.status_code == 200, second.text
    assert calls[-1]["symbols"] == ["AAPL", "MSFT", "TSLA", "0700.HK"], (
        "the cached feed outlived the universe change"
    )
    assert "TSLA" in second.text


def test_invalidate_symbol_universe_clears_the_feed_cache():
    from app.routers import ical as ical_router
    from app.universe import invalidate_symbol_universe

    ical_router._ical_cache.clear()
    ical_router._store_feed(("tok-abc", "zh", "all", 1, "all"),
                            ical_router._CachedFeed("X" * 100, 'W/"e"', "Mon, 01 Aug 2026 08:00:00 GMT"))
    ical_router._store_feed(("tok-xyz", "zh", "watchlist", 1, "all"),
                            ical_router._CachedFeed("Y" * 100, 'W/"f"', "Mon, 01 Aug 2026 08:00:00 GMT"))
    assert len(ical_router._ical_cache) == 2

    with patch("app.watchlist.invalidate_cache", lambda: None):
        invalidate_symbol_universe()

    assert len(ical_router._ical_cache) == 0, (
        "an admin universe change must drop every cached feed, not just the user's"
    )


@pytest.mark.parametrize("path", ["/api/admin/watchlist"])
def test_admin_watchlist_write_drops_the_feeds(path):
    """The three admin write handlers all go through invalidate_symbol_universe."""
    from app.main import app
    from app.routers import ical as ical_router

    app.dependency_overrides = {}
    ical_router._ical_cache.clear()
    ical_router._store_feed(("tok-abc", "zh", "all", 1, "all"),
                            ical_router._CachedFeed("X" * 100, 'W/"e"', "Mon, 01 Aug 2026 08:00:00 GMT"))

    class _AdminCursor:
        rowcount = 1

        def execute(self, sql, params=None):
            self._sql = " ".join(sql.split())

        def fetchone(self):
            return {"id": 7, "symbol": "TSLA", "market": "US",
                    "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1)}

    class _AdminConn:
        def __enter__(self):
            return _AdminCursor()

        def __exit__(self, *args):
            return False

    from app import db

    with patch.object(db, "db_cursor", lambda: _AdminConn()), \
         patch("app.watchlist.invalidate_cache", lambda: None), \
         patch("app.routers.api.invalidate_calendar_caches", lambda: None), \
         patch("app.routers.admin.require_admin", lambda user: {**user, "role": "admin"}):
        from fastapi.testclient import TestClient

        from app.auth import get_current_user

        app.dependency_overrides = {
            get_current_user: lambda: {"id": 4242, "email": "a@b.c", "name": "A", "role": "admin"},
        }
        try:
            response = TestClient(app, raise_server_exceptions=False).post(
                path, json={"symbol": "TSLA", "market": "US"}
            )
        finally:
            app.dependency_overrides = {}

    assert response.status_code in (200, 201), response.text
    assert len(ical_router._ical_cache) == 0, (
        "an admin watchlist write must invalidate the feeds it just changed"
    )


# ── acceptance #5: capacity is measured in bytes ───────────────────────────


def test_feed_entry_size_is_its_utf8_bytes():
    """``getsizeof`` must count the payload — not one slot per entry."""
    from app.routers import ical as ical_router

    feed = ical_router._CachedFeed("é" * 500, 'W/"e"', "Mon, 01 Aug 2026 08:00:00 GMT")
    assert feed.nbytes == len(("é" * 500).encode("utf-8")) == 1000
    assert ical_router._feed_size(feed) == feed.nbytes


def test_the_shipped_cache_budget_is_a_byte_budget():
    """The live cache's ``maxsize`` is bytes, and the shipped numbers add up.

    Worst single key after Issue #100 is the universe-scoped feed (~190 KiB);
    one subscriber can legitimately hold 12 keys (lang 2 × scope 2 × predicted 2
    × markets 3). The budget has to cover that working set and still cap memory.
    """
    from app.routers import ical as ical_router

    budget = ical_router.FEED_CACHE_MAX_BYTES
    assert ical_router._ical_cache.maxsize == budget, (
        "maxsize is a byte budget; a slot count is what Issue #100 removed"
    )
    assert ical_router._ical_cache.getsizeof is ical_router._feed_size

    single_key_cap = 200 * 1024          # measured universe-scoped feed, +margin
    per_subscriber = 12 * single_key_cap  # lang × scope × predicted × markets
    subscribers = 6                       # production user count at the time
    assert per_subscriber * subscribers <= budget, "the budget would evict live keys"
    assert single_key_cap < budget
    assert budget >= 4 * 1024 * 1024, f"{budget} B is too small for a working set"
    assert budget <= 64 * 1024 * 1024, f"{budget} B of resident feeds is too much"


def test_byte_budget_evicts_least_recently_used_feeds():
    """Filling past the byte budget keeps ``currsize`` under it, not just a count."""
    from app.routers import ical as ical_router

    cache = ical_router._make_feed_cache(1000)
    for i in range(6):
        feed = ical_router._CachedFeed(f"{i}" * 400, f'W/"{i}"', "Mon, 01 Aug 2026 08:00:00 GMT")
        ical_router._store_feed((f"tok-{i}", "zh", "all", 1, "all"), feed, cache=cache)

    assert cache.currsize <= cache.maxsize, f"{cache.currsize} B exceeds {cache.maxsize} B"
    assert len(cache) == 2, f"400 B feeds in a 1000 B budget: expected 2 kept, got {len(cache)}"


def test_a_feed_larger_than_the_budget_is_served_but_not_cached():
    """An oversized feed must not turn the response into a 500."""
    from app.routers import ical as ical_router

    cache = ical_router._make_feed_cache(100)
    feed = ical_router._CachedFeed("Z" * 500, 'W/"big"', "Mon, 01 Aug 2026 08:00:00 GMT")

    ical_router._store_feed(("tok", "zh", "all", 1, "all"), feed, cache=cache)

    assert len(cache) == 0
    assert cache.currsize == 0


def test_the_slot_ceiling_bounds_key_bookkeeping():
    """The byte budget alone would allow a flood of tiny empty feeds."""
    from app.routers import ical as ical_router

    cache = ical_router._make_feed_cache(10 * 1024 * 1024)
    with patch.object(ical_router, "FEED_CACHE_MAX_ENTRIES", 3):
        for i in range(6):
            feed = ical_router._CachedFeed("", f'W/"{i}"', "Mon, 01 Aug 2026 08:00:00 GMT")
            ical_router._store_feed((f"tok-{i}", "zh", "all", 1, "all"), feed, cache=cache)

    assert len(cache) == 3
