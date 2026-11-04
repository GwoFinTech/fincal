"""Issue #112: a revoked iCal token must stop working, in-process and in bounded time.

``/ical/{token}`` is authenticated by the token alone, and the feed body used to
be cached *by token* for an hour: rotating ``users.ical_token`` (the documented
response to a leaked URL) left the old address answering 197 KB / 482 events
from memory until someone restarted the container. The cache hit path never
asked whether the token still existed.

What is pinned here, against the real route and the real rotation code:

1. revocation is checked **before** any cached body is served — a warm feed plus
   a revoked token is a 404 whose body carries no ``BEGIN:VEVENT``, and it stays
   404 on retry;
2. a rotated token resolves immediately, while the *content* cache stays keyed
   by user id, so the new URL reuses the unchanged body (identical ETag and
   Last-Modified, conditional revalidation still answers 304);
3. the credential check is amortised: N polls inside
   ``ICAL_TOKEN_CACHE_TTL_SECONDS`` issue at most one ``users`` lookup, and a
   DB-only revocation is honoured once that window expires;
4. ``cache_entries_dropped`` reports entries really removed, so an
   out-of-process run reports 0 instead of ``2 x users``;
5. the admin endpoint rotates inside the serving process (immediate cut-off, no
   restart) and audits fingerprints only.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]

TOKEN = "FbHcIdJeKfLgMhNiOjPkQlRm"       # token_urlsafe(24)-shaped
OTHER_TOKEN = "GsHtIuJvKwLxMyNzOaPbQcRd"

AAPL = {
    "symbol": "AAPL", "market": "US", "company_name": "Apple",
    "report_date": date(2026, 11, 20), "before_after": "after",
    "updated_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
}

USER_SQL = "SELECT id, email FROM users WHERE ical_token = %s"


def _flat(sql: str) -> str:
    return " ".join(sql.split())


class Store:
    """In-memory ``users``/``watchlist``/``admin_audit_log`` with SQL dispatch.

    Dispatch (not call order) so a test can assert *how many* user lookups a
    sequence of polls performed without depending on their position.
    """

    def __init__(self):
        self.users = [{"id": 7, "email": "t@t.com", "name": "T", "ical_token": TOKEN}]
        self.watchlist = [{"user_id": 7, "symbol": "AAPL", "market": "US"}]
        self.statements: list[str] = []
        self.audit: list[dict] = []

    # -- helpers used by the assertions -----------------------------------
    def queries_matching(self, needle: str) -> int:
        return sum(1 for sql in self.statements if needle in sql)

    def connection(self):
        return _Conn(self)


class _Cursor:
    def __init__(self, store: Store):
        self._store = store
        self._rows: list[dict] = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        flat = _flat(sql)
        params = tuple(params or ())
        self._store.statements.append(flat)
        self.rowcount = 0
        if flat.startswith("SELECT id, email FROM users WHERE ical_token"):
            self._rows = [row for row in self._store.users if row["ical_token"] == params[0]]
        elif flat.startswith("SELECT id, email, ical_token FROM users"):
            rows = list(self._store.users)
            if "WHERE id = %s" in flat:
                rows = [row for row in rows if row["id"] == params[0]]
            self._rows = sorted(rows, key=lambda row: row["id"])
        elif flat.startswith("UPDATE users SET ical_token = %s WHERE id = %s"):
            new_token, user_id = params
            for row in self._store.users:
                if row["id"] == user_id:
                    row["ical_token"] = new_token
            self.rowcount = 1
            self._rows = []
        elif flat.startswith("SELECT symbol, market FROM watchlist"):
            self._rows = [row for row in self._store.watchlist if row["user_id"] == params[0]]
        elif flat.startswith("INSERT INTO admin_audit_log"):
            details = params[4]
            self._store.audit.append({
                "action": params[0],
                "target": params[3],
                "details": getattr(details, "adapted", details),
            })
            self.rowcount = 1
            self._rows = []
        else:  # pragma: no cover - a new statement must extend this fake
            raise AssertionError(f"unexpected SQL in test fake: {flat}")
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, store: Store):
        self._cursor = _Cursor(store)

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc):
        return False


def _client(store: Store):
    from fastapi.testclient import TestClient

    from app import db, earnings as earnings_mod
    from app.main import app
    from app.routers import ical as ical_router

    ical_router._ical_cache.clear()
    ical_router._content_versions.clear()
    ical_router._token_cache.clear()
    app.dependency_overrides = {}
    patchers = [
        patch.object(db, "db_cursor", lambda: store.connection()),
        patch.object(earnings_mod, "fetch_earnings_from_db", lambda **kw: [dict(AAPL)]),
    ]
    for p in patchers:
        p.start()
    return TestClient(app, raise_server_exceptions=False), patchers


def _load_rotate_cli():
    spec = importlib.util.spec_from_file_location(
        "rotate_ical_tokens_issue112", ROOT / "scripts" / "rotate_ical_tokens.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _admin_client(store: Store):
    """TestClient with an admin identity; the route still goes through auth."""
    from app.auth import get_current_user
    from app.main import app

    client, patchers = _client(store)
    app.dependency_overrides[get_current_user] = (
        lambda: {"id": 4242, "email": "admin@example.com", "name": "A", "role": "admin"}
    )
    return client, patchers


# ── acceptance #1: revocation beats a warm cache ─────────────────────────────


def test_a_revoked_token_is_a_404_even_with_a_warm_feed():
    from app import ical_tokens
    from app.routers import ical as ical_router

    store = Store()
    client, patchers = _client(store)
    try:
        warm = client.get(f"/ical/{TOKEN}")
        assert warm.status_code == 200
        assert "BEGIN:VEVENT" in warm.text
        assert any(key[0] == 7 for key in ical_router._ical_cache), "cache not warmed"

        # Revoke exactly as the supported paths do: the row is replaced and the
        # in-process caches are dropped for that user.
        with store.connection() as cur:
            rows = ical_tokens.rotate(cur, ical_tokens.select_targets(cur, user_id=7))
        ical_tokens.invalidate_feed_cache(rows)

        revoked = client.get(f"/ical/{TOKEN}")
        assert revoked.status_code == 404, revoked.text
        assert "BEGIN:VEVENT" not in revoked.text

        # ...and the answer does not become "200 again" on the next poll.
        again = client.get(f"/ical/{TOKEN}")
        assert again.status_code == 404
        assert not any(key[0] == 7 for key in ical_router._ical_cache), (
            "a 404 must not repopulate a feed for a token that does not exist"
        )
    finally:
        for p in patchers:
            p.stop()


def test_a_db_only_revocation_stops_working_once_the_resolution_window_expires(monkeypatch):
    """Writing ``users`` directly is bounded by the resolution TTL, not 3600 s."""
    from app.routers import ical as ical_router

    store = Store()
    client, patchers = _client(store)
    try:
        assert client.get(f"/ical/{TOKEN}").status_code == 200

        # Simulate the elapse of ICAL_TOKEN_CACHE_TTL_SECONDS without sleeping.
        monkeypatch.setattr(ical_router, "_token_cache", ical_router._make_token_cache(ttl=0.01))
        assert client.get(f"/ical/{TOKEN}").status_code == 200  # re-warms the tiny cache
        store.users.clear()  # revocation happened in the database, no in-app call

        import time
        time.sleep(0.02)
        assert client.get(f"/ical/{TOKEN}").status_code == 404
    finally:
        for p in patchers:
            p.stop()


# ── acceptance #2/#3: rotation keeps the content cache and its validators ────


def test_rotation_404s_the_old_token_and_serves_the_new_one_with_the_same_etag():
    from app import ical_tokens

    store = Store()
    client, patchers = _client(store)
    try:
        first = client.get(f"/ical/{TOKEN}")
        assert first.status_code == 200

        with store.connection() as cur:
            rows = ical_tokens.rotate(cur, ical_tokens.select_targets(cur, user_id=7))
        ical_tokens.invalidate_feed_cache(rows)
        new_token = store.users[0]["ical_token"]
        assert new_token != TOKEN

        assert client.get(f"/ical/{TOKEN}").status_code == 404
        second = client.get(f"/ical/{new_token}")
        assert second.status_code == 200
        # Same user, same content: the body cache was not keyed by the
        # credential, so the ETag does not change across the rotation.
        assert second.headers["etag"] == first.headers["etag"]
        assert second.headers["last-modified"] == first.headers["last-modified"]

        # A client that revalidates with the pre-rotation validator still gets
        # an honest 304 from the new URL (Issue #51/#99 behaviour intact).
        not_modified = client.get(
            f"/ical/{new_token}",
            headers={"if-none-match": first.headers["etag"]},
        )
        assert not_modified.status_code == 304
        assert "BEGIN:VEVENT" not in not_modified.text
    finally:
        for p in patchers:
            p.stop()


def test_feed_cache_keys_never_carry_a_token():
    from app.routers import ical as ical_router

    store = Store()
    client, patchers = _client(store)
    try:
        assert client.get(f"/ical/{TOKEN}").status_code == 200
        assert ical_router._ical_cache, "cold path did not cache anything"
        for key in ical_router._ical_cache:
            assert key[0] == 7, "the content cache must be keyed by user id"
            assert TOKEN not in str(key)
    finally:
        for p in patchers:
            p.stop()


# ── acceptance #4: bounded polling cost ─────────────────────────────────────


def test_repeated_polls_use_one_user_lookup_and_one_cached_body():
    from app.routers import ical as ical_router

    store = Store()
    client, patchers = _client(store)
    try:
        bodies = [client.get(f"/ical/{TOKEN}").text for _ in range(6)]
        assert len(set(bodies)) == 1, "polls inside the TTL must return one body"
        assert store.queries_matching("FROM users WHERE ical_token") == 1, (
            "the token resolution must be reused inside ICAL_TOKEN_CACHE_TTL_SECONDS"
        )
        # One entry per user+options, not one per poll.
        assert len(ical_router._ical_cache) == 1
    finally:
        for p in patchers:
            p.stop()


# ── acceptance #5: the CLI reports real cache drops ─────────────────────────


def test_cli_reports_the_entries_it_really_dropped(capsys):
    from app.routers import ical as ical_router

    rotate = _load_rotate_cli()
    store = Store()
    client, patchers = _client(store)
    try:
        assert client.get(f"/ical/{TOKEN}").status_code == 200
        assert any(key[0] == 7 for key in ical_router._ical_cache)

        with patch.object(rotate, "db_cursor", lambda: store.connection()):
            assert rotate.main(["--apply", "--json"]) == 0

        report = json.loads(capsys.readouterr().out)
        assert report["users"] == 1
        assert report["cache_entries_dropped"] == 1, (
            "the count must be the entries removed, not the tokens supplied"
        )
        assert report["revocation_effective_within_seconds"] > 0
    finally:
        for p in patchers:
            p.stop()


def test_cli_run_elsewhere_reports_zero_drops_not_two_per_user(capsys):
    """A host-side run cannot reach the server's memory; say so with 0."""
    from app.routers import ical as ical_router

    rotate = _load_rotate_cli()
    store = Store()
    store.users.append({"id": 8, "email": "u@u.com", "name": "U", "ical_token": OTHER_TOKEN})

    # No request in this process: both caches are empty, like the CLI's own.
    ical_router._ical_cache.clear()
    ical_router._token_cache.clear()
    assert ical_router._ical_cache == {}

    with patch.object(rotate, "db_cursor", lambda: store.connection()):
        assert rotate.main(["--apply", "--json", "--user-id", "8"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["users"] == 1
    assert report["cache_entries_dropped"] == 0, (
        "an out-of-process rotation deletes nothing here; 2 x users is a lie"
    )


# ── acceptance #6: in-app rotation, audited without the credential ──────────


def test_admin_rotation_takes_effect_immediately_and_is_audited():
    from app.routers import ical as ical_router

    store = Store()
    client, patchers = _admin_client(store)
    try:
        assert client.get(f"/ical/{TOKEN}").status_code == 200
        assert any(key[0] == 7 for key in ical_router._ical_cache)

        response = client.post("/api/admin/ical-tokens/7/rotate")
        assert response.status_code == 200, response.text
        body = response.json()
        new_token = store.users[0]["ical_token"]
        assert body["user_id"] == 7
        assert body["ical_url"].endswith(f"/ical/{new_token}")
        assert body["cache_entries_dropped"] == 1

        # No restart, no TTL wait: the very next request is a 404.
        assert client.get(f"/ical/{TOKEN}").status_code == 404
        assert client.get(f"/ical/{new_token}").status_code == 200

        assert store.audit, "the rotation must be audited"
        entry = store.audit[-1]
        assert entry["action"] == "rotate_ical_token"
        assert entry["target"] == "user:7"
        serialized = json.dumps(entry, default=str)
        assert TOKEN not in serialized and new_token not in serialized, (
            "the audit log must carry fingerprints, never a credential"
        )
        assert re.fullmatch(r"[0-9a-f]{8}", entry["details"]["new_fingerprint"])
    finally:
        for p in patchers:
            p.stop()


def test_admin_rotation_of_an_unknown_user_is_a_404():
    store = Store()
    client, patchers = _admin_client(store)
    try:
        response = client.post("/api/admin/ical-tokens/999/rotate")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "user_not_found"
    finally:
        for p in patchers:
            p.stop()


def test_admin_rotation_requires_an_admin():
    from app.auth import get_current_user
    from app.main import app

    store = Store()
    client, patchers = _client(store)
    app.dependency_overrides[get_current_user] = (
        lambda: {"id": 77, "email": "u@example.com", "name": "U", "role": "user"}
    )
    try:
        response = client.post("/api/admin/ical-tokens/7/rotate")
        assert response.status_code == 403
        assert store.users[0]["ical_token"] == TOKEN, "a rejected call must not rotate"
    finally:
        for p in patchers:
            p.stop()


@pytest.mark.parametrize("value", ["0", "-5"])
def test_token_cache_ttl_is_clamped_to_a_positive_window(value):
    """A zero/negative TTL would silently disable the layer — clamp it."""
    import os
    import subprocess
    import sys

    env = dict(os.environ, ICAL_TOKEN_CACHE_TTL_SECONDS=value)
    out = subprocess.run(
        [sys.executable, "-c",
         "from app import config; print(config.ICAL_TOKEN_CACHE_TTL_SECONDS)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert float(out.stdout.strip()) >= 1.0
