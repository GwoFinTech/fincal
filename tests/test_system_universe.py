"""The system's own watchlist range is visible to every user, read-only.

Every user's calendar, export and iCal feed is built from one symbol range (the
configured watchlist source), but only ``/api/admin/overview`` could show what
that range *is*. A user could therefore not tell whether a symbol was never
tracked or simply had no upcoming report. ``/api/universe`` answers that for any
signed-in user, and it must stay read-only: the editable list is the admin's
(``/api/admin/watchlist``).

What is pinned here:
* a plain (non-admin) session gets the range, with counts that match the items;
* the range comes from the same ``app.universe`` accessor the calendar reads;
* FinCal's own managed symbols are marked as such (with HK zero-padding applied
  on both sides), the rest as the shared upstream list;
* a degraded source (stale / unreadable managed table / failed name lookup) is
  reported instead of hidden or turned into a 500;
* the page itself offers no mutation control and calls no write endpoint.
"""
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, universe  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import SystemUniverse  # noqa: E402

INDEX_HTML = ROOT / "app" / "static" / "index.html"
APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"

USER = {"id": 7, "email": "user@example.com", "name": "U", "role": "user"}

STATUS = {
    "symbol_count": 3,
    "us_count": 2,
    "hk_count": 1,
    "skipped_count": 1,
    "skipped_symbols": ["600028.SH"],
    "source": "tsummt+managed",
    "stale": False,
    "error_code": None,
    "last_success_at": "2026-10-08T03:00:00+00:00",
    "fetched_at": "2026-10-08T03:00:00+00:00",
    "ttl_seconds": 300.0,
}

DESCRIPTION = {
    "configured": "hybrid",
    "type": "postgresql",
    "location": "db:5432/tsummt",
    "transport": "database",
    "external_dependency": False,
    "local_fallback": True,
}


class _Cursor:
    """Answers each statement from a queue keyed by a substring of the SQL."""

    def __init__(self, rows_by_needle, executed, explode_on=()):
        self._rows_by_needle = rows_by_needle
        self._executed = executed
        self._explode_on = explode_on
        self._rows = []

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        for needle in self._explode_on:
            if needle in sql:
                raise RuntimeError(f"boom: {needle}")
        self._rows = []
        for needle, rows in self._rows_by_needle.items():
            if needle in sql:
                self._rows = rows
                break

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return (list(self._rows) or [None])[0]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *args):
        return False


def _call(rows_by_needle=None, *, us=("AAPL", "NVDA"), hk=("0700.HK",), status=None,
          explode_on=(), role="user"):
    """GET /api/universe with the DB and the universe accessor faked."""
    executed = []
    cursor = _Cursor(rows_by_needle or {}, executed, explode_on)
    overrides = {get_current_user: lambda: {**USER, "role": role}}
    app.dependency_overrides = overrides
    try:
        with patch.object(db, "db_cursor", lambda: _Conn(cursor)), \
             patch.object(universe, "popular_stocks", lambda **kw: (list(us), list(hk))), \
             patch.object(universe, "universe_status", lambda: dict(status or STATUS)), \
             patch("app.watchlist.source_description", lambda: dict(DESCRIPTION)), \
             patch.object(db, "db_cursor", lambda: _Conn(cursor)):
            client = TestClient(app, raise_server_exceptions=False)
            return client.get("/api/universe"), executed
    finally:
        app.dependency_overrides = {}


def test_a_plain_user_gets_the_range_and_the_counts_match_the_items():
    resp, executed = _call({
        "FROM managed_watchlist": [{"symbol": "AAPL", "market": "US"}],
    })

    assert resp.status_code == 200, resp.text
    body = SystemUniverse.model_validate(resp.json()).model_dump()
    assert body["counts"] == {"US": 2, "HK": 1, "total": 3}
    assert [item["symbol"] for item in body["items"]] == ["0700.HK", "AAPL", "NVDA"]
    assert body["source"] == "tsummt+managed"
    assert body["configured"] == "hybrid"
    assert body["transport"] == "database"
    assert body["skipped"] == ["600028.SH"]
    assert body["updated_at"] is not None
    # It reports the universe the calendar reads, not a second copy of it.
    assert any("FROM managed_watchlist" in sql for sql, _ in executed)


def test_managed_symbols_are_marked_and_hk_padding_matches_on_both_sides():
    resp, _ = _call({
        "FROM managed_watchlist": [
            {"symbol": "700.HK", "market": "HK"},   # stored unpadded
            {"symbol": "AAPL.US", "market": "US"},  # stored with a US suffix
        ],
    })

    assert resp.status_code == 200, resp.text
    origins = {item["symbol"]: item["origin"] for item in resp.json()["items"]}
    assert origins == {"0700.HK": "managed", "AAPL": "managed", "NVDA": "shared"}


def test_names_come_from_the_authoritative_row_per_symbol():
    resp, executed = _call({
        "FROM managed_watchlist": [],
        "LEFT JOIN authoritative_name": [
            {"symbol": "AAPL", "market": "US", "company_name": "苹果"},
            {"symbol": "NVDA", "market": "US", "company_name": ""},
        ],
    })

    assert resp.status_code == 200, resp.text
    names = {item["symbol"]: item["company_name"] for item in resp.json()["items"]}
    assert names == {"AAPL": "苹果", "NVDA": "", "0700.HK": ""}
    # One statement, both markets, no per-symbol round trip.
    name_calls = [c for c in executed if "LEFT JOIN authoritative_name" in c[0]]
    assert len(name_calls) == 1
    assert name_calls[0][1]["symbols"] == ["0700.HK", "AAPL", "NVDA"]
    assert name_calls[0][1]["markets"] == ["HK", "US", "US"]


def test_a_degraded_source_is_reported_not_hidden():
    stale_status = {**STATUS, "stale": True, "error_code": "tsummt_source_fetch_failed",
                    "source": "fallback", "last_success_at": None}
    resp, _ = _call({"FROM managed_watchlist": []}, status=stale_status)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stale"] is True
    assert body["error_code"] == "tsummt_source_fetch_failed"
    assert body["source"] == "fallback"
    assert body["updated_at"] is None
    # The range is still shown — the page explains the degradation beside it.
    assert body["counts"]["total"] == 3


def test_an_unreadable_managed_table_degrades_the_origin_instead_of_failing():
    resp, _ = _call({}, explode_on=("FROM managed_watchlist",))

    assert resp.status_code == 200, resp.text
    assert {item["origin"] for item in resp.json()["items"]} == {None}


def test_a_failed_name_lookup_keeps_the_range():
    resp, _ = _call({"FROM managed_watchlist": []}, explode_on=("LEFT JOIN authoritative_name",))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"]["total"] == 3
    assert {item["company_name"] for item in body["items"]} == {""}


def test_the_view_requires_a_session():
    app.dependency_overrides = {}
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/universe")

    assert resp.status_code == 401, resp.text


# ── The page: reachable by everyone, with no way to change the range ─────

def _universe_page() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index("<!-- System universe:")
    end = html.index("<!-- Calendar and selection workspace -->")
    return html[start:end]


def test_the_page_is_a_top_level_tab_next_to_the_personal_watchlist():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert """@click="appTab='universe'\"""" in html
    assert "系统自选" in _universe_page()
    # Sits beside 自选 in the same nav group, not inside the admin modal.
    nav = html[html.index("<nav"):html.index("</nav>")]
    assert "appTab='universe'" in nav and "appTab='watchlist'" in nav
    admin_modal = html[html.index("<!-- Admin Modal -->"):]
    assert "appTab='universe'" not in admin_modal


def test_the_page_offers_no_mutation_control():
    page = _universe_page()

    for forbidden in ("添加自选", "移除", "removeFromWatchlist", "addToWatchlist",
                      "toggleWatchlist", "v-model", "/api/watchlist"):
        assert forbidden not in page, f"read-only page must not contain {forbidden!r}"


def test_the_composable_only_reads_the_universe_endpoint():
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("function useSystemUniverse")
    end = js.index("function useFormatters")
    block = js[start:end]

    assert "apiFetch('/api/universe')" in block
    assert "method: 'POST'" not in block and "method: 'DELETE'" not in block
    assert "/api/admin/" not in block


def test_the_page_renders_both_markets_and_the_source_metadata():
    page = _universe_page()

    assert "usUniverse" in page and "hkUniverse" in page
    assert "universeSourceLabel" in page and "universeUpdatedLabel" in page
    assert "systemUniverse.stale" in page and "systemUniverse.skipped" in page
    assert "universeOriginLabel" in page


def test_the_page_shows_a_sample_of_the_dropped_codes_not_a_wall():
    assert "universeSkippedLabel" in _universe_page()

    js = APP_JS.read_text(encoding="utf-8")
    block = js[js.index("function useSystemUniverse"):js.index("function useFormatters")]
    assert "skipped.slice(0, 8)" in block


def test_the_page_does_not_leak_infrastructure_details():
    """The API carries the source's host/port for the admin panel; the
    user-facing page must not print it."""
    page = _universe_page()

    assert "systemUniverse.location" not in page
    assert "5432" not in page
