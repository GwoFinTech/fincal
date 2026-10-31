"""Issue #108: a legacy stored symbol must not 500 the read paths.

Issue #105 made ``normalize()`` raise ``ValueError("symbol_market_mismatch")``
for a numeric code stored under ``US``.  That contract is right for the write
paths (they answer 400/422), but three read paths called ``normalize()`` on rows
already in the database, and ``DELETE /api/watchlist`` did too — so a single
``('0700','US')`` row (which every write path accepted before #105) turned the
user's calendar, iCal feed and watchlist view into a 500 **and** could not be
removed.

What this pins:

1. ``resolve_stored()`` is the non-throwing read-side entry point: canonical pair
   or ``None``, never ``ValueError``;
2. the three read paths answer 200 with the unservable row skipped (and logged
   once) instead of 500;
3. ``DELETE`` validates like ``POST`` (400 ``invalid_market`` /
   ``symbol_required`` / ``symbol_market_mismatch``) and answers 404
   ``watchlist_entry_not_found`` when nothing was deleted, instead of the old
   unconditional 200;
4. ``init_db()`` converges the numeric US spellings to ``(NNNN.HK, 'HK')``,
   removing a duplicate that already holds the canonical key first.
"""
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import api as api_router  # noqa: E402
from app.routers import ical as ical_router  # noqa: E402
from app.symbol import resolve_stored, resolve_stored_rows  # noqa: E402

USER = {"id": 1, "email": "t@t.com", "name": "T", "role": "user"}

_USER_ROW = {
    "id": 1,
    "portal_user_id": 1,
    "email": "t@t.com",
    "name": "T",
    "ical_token": "tok-abc",
    "symbol": "AAPL",
    "market": "US",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


# ── 1. resolve_stored(): canonical pair or None, never a raise ────────────

@pytest.mark.parametrize("symbol,market,expected", [
    ("AAPL", "US", ("AAPL", "US")),
    ("AAPL.US", "US", ("AAPL", "US")),
    ("US.AAPL", "US", ("AAPL", "US")),
    (" aapl ", "us", ("AAPL", "US")),
    ("BRK.A", "US", ("BRK.A", "US")),
    ("0700.HK", "HK", ("0700.HK", "HK")),
    ("700", "HK", ("0700.HK", "HK")),
    ("82333", "HK", ("82333.HK", "HK")),
])
def test_resolve_stored_returns_the_canonical_pair(symbol, market, expected):
    assert resolve_stored(symbol, market) == expected


@pytest.mark.parametrize("symbol,market", [
    ("0700", "US"),        # the ghost row #105 turned into a 500
    ("700", "US"),
    ("00700", "US"),
    ("159326", "US"),      # Shenzhen fund whose suffix was pasted off
    ("600028.SH", "US"),
    ("000651.SZ", "US"),
    ("US.0700", "US"),     # reachable through market_of(), refused by normalize()
    ("AAPL", "SZ"),        # stored market FinCal cannot serve
    ("", "US"),
])
def test_resolve_stored_answers_none_instead_of_raising(symbol, market):
    assert resolve_stored(symbol, market) is None


def test_resolve_stored_rows_skips_unservable_rows_and_warns_once(caplog):
    rows = [
        {"symbol": "0700", "market": "US"},
        {"symbol": "AAPL", "market": "US"},
        {"symbol": "AAPL.US", "market": "US"},  # same canonical key → once
    ]

    with caplog.at_level(logging.WARNING, logger="app.symbol"):
        pairs = resolve_stored_rows(rows, context="probe")

    assert pairs == [("AAPL", "US")]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "probe" in message and "0700" in message, message


# ── 2. Fakes ─────────────────────────────────────────────────────────────

class _Cursor:
    """Cursor answering the watchlist read/write statements of the API."""

    def __init__(self, rows, *, delete_rowcount=1, executed=None):
        self.rows = [dict(symbol=s, market=m) for s, m in rows]
        self._delete_rowcount = delete_rowcount
        self._executed = executed if executed is not None else []
        self.rowcount = 1

    def execute(self, sql, params=()):
        statement = " ".join(str(sql).split())
        self._executed.append(statement)
        if statement.startswith("DELETE FROM watchlist"):
            self.rowcount = self._delete_rowcount
        else:
            self.rowcount = 1

    def fetchone(self):
        return dict(_USER_ROW)

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kw):
        return self._cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *a):
        return False


def _client(rows=(), *, delete_rowcount=1, executed=None, events=(),
            universe=(["AAPL"], [])):
    """TestClient with the watchlist table stubbed and earnings captured."""
    api_router._earnings_cache.invalidate()
    ical_router._ical_cache.clear()
    ical_router._content_versions.clear()
    cursor = _Cursor(rows, delete_rowcount=delete_rowcount, executed=executed)
    captured = {}

    def _fake_fetch(*, symbols=None, markets=None, start=None, end=None):
        captured["symbols"] = list(symbols or [])
        captured["markets"] = list(markets or [])
        return list(events)

    app.dependency_overrides = {get_current_user: lambda: dict(USER)}
    patchers = [
        patch.object(db, "db_cursor", lambda: _Conn(cursor)),
        patch.object(api_router, "invalidate_calendar_caches"),
        # The default view folds the live universe in; it reads the watchlist
        # source over its own connection, so it is stubbed like the table.
        patch("app.universe.popular_stocks", lambda: universe),
        patch("app.earnings.fetch_earnings_from_db", _fake_fetch),
    ]
    for p in patchers:
        p.start()
    return TestClient(app, raise_server_exceptions=False), captured, patchers


def _stop(patchers):
    for p in reversed(patchers):
        p.stop()
    app.dependency_overrides = {}
    api_router._earnings_cache.invalidate()


# ── 3. DELETE: 400 for unservable input, 404 for a miss, 200 for a hit ────

@pytest.mark.parametrize("query,expected_code", [
    ("symbol=0700&market=US", "symbol_market_mismatch"),
    ("symbol=159326&market=US", "symbol_market_mismatch"),
    ("symbol=600028.SH&market=US", "symbol_market_mismatch"),
    ("symbol=AAPL&market=SZ", "invalid_market"),
    ("symbol=%20&market=US", "symbol_required"),
])
def test_delete_rejects_what_cannot_name_a_stored_row(query, expected_code):
    """These used to be a 500 (``0700``) or a false 200 (``600028.SH``)."""
    executed: list[str] = []
    client, _, patchers = _client([("AAPL", "US")], executed=executed)
    try:
        resp = client.delete(f"/api/watchlist?{query}")
    finally:
        _stop(patchers)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == expected_code
    assert not [s for s in executed if s.startswith("DELETE FROM watchlist")], executed


def test_delete_reports_a_missing_row_as_404_not_a_false_success():
    executed: list[str] = []
    client, _, patchers = _client([("AAPL", "US")], delete_rowcount=0, executed=executed)
    try:
        resp = client.delete("/api/watchlist?symbol=MSFT&market=US")
    finally:
        _stop(patchers)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "watchlist_entry_not_found"
    # The statement still ran — the answer comes from ``rowcount``, not a guess.
    assert [s for s in executed if s.startswith("DELETE FROM watchlist")], executed


def test_delete_removes_an_existing_canonical_row_with_the_normalized_spelling():
    executed: list[str] = []
    client, _, patchers = _client([("AAPL", "US")], executed=executed)
    try:
        resp = client.delete("/api/watchlist?symbol=AAPL.US&market=US")
    finally:
        _stop(patchers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "removed"}


# ── 4. Read paths: 200 with the ghost row skipped ────────────────────────

_GHOST_ROWS = [("0700", "US"), ("AAPL", "US")]


def test_earnings_watchlist_only_skips_the_ghost_row(caplog):
    client, captured, patchers = _client(_GHOST_ROWS)
    try:
        with caplog.at_level(logging.WARNING):
            resp = client.get("/api/earnings?watchlistOnly=true")
    finally:
        _stop(patchers)

    assert resp.status_code == 200, resp.text
    assert captured["symbols"] == ["AAPL"], captured
    assert captured["markets"] == ["US"], captured
    assert any("0700" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING), [r.getMessage() for r in caplog.records]


def test_earnings_default_view_skips_the_ghost_row():
    client, captured, patchers = _client(_GHOST_ROWS)
    try:
        resp = client.get("/api/earnings")
    finally:
        _stop(patchers)

    assert resp.status_code == 200, resp.text
    assert "0700" not in captured["symbols"], captured
    assert "AAPL" in captured["symbols"], captured


def test_earnings_watchlist_only_with_only_ghost_rows_is_an_empty_answer():
    client, captured, patchers = _client([("0700", "US")])
    try:
        resp = client.get("/api/earnings?watchlistOnly=true")
    finally:
        _stop(patchers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    assert "symbols" not in captured, "no query may run for an empty servable set"


def test_ical_watchlist_scope_serves_a_valid_calendar_for_a_ghost_row():
    row_ts = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    events = [{
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 9, 20), "before_after": "after",
        "updated_at": row_ts, "is_predicted": False,
    }]
    client, _, patchers = _client(_GHOST_ROWS, events=events)
    try:
        resp = client.get("/ical/tok-abc?scope=watchlist")
    finally:
        _stop(patchers)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/calendar")
    body = resp.text
    assert body.startswith("BEGIN:VCALENDAR"), body[:200]
    assert body.rstrip().endswith("END:VCALENDAR"), body[-200:]
    assert "AAPL" in body
    assert "0700" not in body, "the skipped row must not reach the feed"


# ── 5. init_db(): the numeric convergence is in place ────────────────────

def test_startup_migration_converges_numeric_us_watchlist_rows():
    sql = " ".join(str(v) for v in db.init_db.__code__.co_consts if isinstance(v, str))

    assert "LPAD((symbol::int)::text, 4, '0') || '.HK'" in sql, \
        "the migration must rewrite a numeric US row to the canonical HK spelling"
    assert "UPPER(market) = 'US' AND symbol ~ '^[0-9]{1,5}$'" in sql, \
        "only 1–5 digit codes are HK; a six-digit fund code must not be guessed"
    assert "duplicate_rank" in sql and "ROW_NUMBER" in sql, \
        "the duplicate holding the canonical key must be removed before the UPDATE"
    assert "^[0-9]{6,}$" in sql, "unservable six-digit codes must be counted for the log"
