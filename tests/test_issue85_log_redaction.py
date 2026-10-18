"""Issue #85: a subscription token must never reach a log line.

``/ical/{token}`` is the feed's only credential and it was written to
``docker logs`` on every poll (production: 144 copies of one live token in six
hours), logged again by the feed's timeout warning, and carried in provider URLs
whose query string holds ``WATCHLIST_HTTP_URL``'s token. The repo had already
declared "no token in a metric label" and shipped an unused
``_sanitize_log_url()``, so these tests cover the missing exits *and* the parts
that must not change: the feed still answers 200, ``/api/me`` still publishes the
same URL, and rotation replaces the token without ever printing it.

Each claim is checked against the real component where possible: a real
``uvicorn.access`` record formatted by uvicorn's own ``AccessFormatter`` (whose
``formatMessage`` unpacks ``record.args`` as a 5-tuple — a filter that only
rewrites ``getMessage()`` would make every access line raise), the real endpoint
served by ``TestClient``, and the real rotation CLI.
"""
import importlib.util
import logging
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]

TOKEN = "AbCdEfGhIjKlMnOpQrStUvWx"          # 24 chars, base64url alphabet
OTHER_TOKEN = "ZyXwVuTsRqPoNmLkJiHgFeDc"     # token_urlsafe(24)-shaped
QUERY_SECRET = "s3cr3t-query-credential"     # stands in for a URL-path token


def _load_rotate_cli():
    spec = importlib.util.spec_from_file_location(
        "rotate_ical_tokens_issue85", ROOT / "scripts" / "rotate_ical_tokens.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── fake Postgres: the users table the feed and the CLI actually read ──────────

class _FakeCursor:
    """Dispatches on the handful of statements this feature runs."""

    def __init__(self, store):
        self._store = store
        self._rows: list[dict] = []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        params = tuple(params)
        if flat.startswith("SELECT id, email, ical_token FROM users"):
            rows = list(self._store.users)
            if "WHERE id = %s" in flat:
                rows = [row for row in rows if row["id"] == params[0]]
            if "WHERE email = %s" in flat:
                rows = [row for row in rows if row["email"] == params[0]]
            self._rows = sorted(rows, key=lambda row: row["id"])
        elif flat.startswith("UPDATE users SET ical_token = %s WHERE id = %s"):
            new_token, user_id = params
            for row in self._store.users:
                if row["id"] == user_id:
                    row["ical_token"] = new_token
            self._rows = []
        elif flat.startswith("SELECT id, email, name FROM users WHERE ical_token"):
            self._rows = [row for row in self._store.users if row["ical_token"] == params[0]]
        elif flat.startswith("SELECT symbol, market FROM watchlist"):
            self._rows = [row for row in self._store.watchlist if row["user_id"] == params[0]]
        elif "FROM earnings" in flat:
            self._rows = []
        else:  # pragma: no cover - a new statement means the test needs updating
            raise AssertionError(f"unexpected SQL in test fake: {flat}")
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, store):
        self._cursor = _FakeCursor(store)

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc):
        return False


class FakeStore:
    """In-memory ``users`` + ``watchlist``."""

    def __init__(self):
        self.users = [{
            "id": 7,
            "portal_user_id": 1,
            "email": "t@t.com",
            "name": "T",
            "ical_token": TOKEN,
        }]
        self.watchlist = [{"user_id": 7, "symbol": "AAPL", "market": "US"}]

    def connection(self):
        return _FakeConn(self)


def _client(store):
    """TestClient wired to the fake store; earnings come back empty."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import db
    from app import earnings as earnings_mod
    from app.routers import ical as ical_router
    from app.routers import api as api_router

    ical_router._ical_cache.clear()
    ical_router._content_versions.clear()
    app.dependency_overrides = {}

    aapl = {
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 11, 2), "before_after": "after",
    }
    patchers = [
        patch.object(db, "db_cursor", lambda: store.connection()),
        patch.object(earnings_mod, "fetch_earnings_from_db", lambda **kw: [dict(aapl)]),
        # ensure_user is an upsert (Issue #30's own test); here the row already
        # exists, so /api/me only needs it to read the user back.
        patch.object(api_router, "ensure_user",
                     lambda portal_user_id, email, name: dict(store.users[0])),
    ]
    for patcher in patchers:
        patcher.start()
    return TestClient(app, raise_server_exceptions=False), patchers


# ── 1. the access log (criterion 1) ───────────────────────────────────────────

_ACCESS_PROBE = r'''
import logging, sys
import app.main  # noqa: F401  — the deployment path: plain `uvicorn app.main:app`
from uvicorn.logging import AccessFormatter

stream = logging.StreamHandler(sys.stdout)
stream.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
access = logging.getLogger("uvicorn.access")
access.addHandler(stream)
access.setLevel(logging.INFO)
access.propagate = False
# uvicorn's own call shape: a 5-tuple of positional args.
access.info('%s - "%s %s HTTP/%s" %d', "172.19.0.3:39076", "GET",
            sys.argv[1], "1.1", 200)
'''


def _emit_access_line_in_fresh_process(full_path):
    """Format one access line in a *fresh* interpreter that imported ``app.main``.

    A subprocess instead of an in-process logger on purpose: the claim is that
    importing the application installs the filter, and logging's global logger
    registry survives between tests — an in-process probe would keep passing off
    a filter some earlier test installed. Any failure to format (the filter
    breaking uvicorn's 5-tuple) surfaces as a missing line.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", _ACCESS_PROBE, full_path],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_access_log_does_not_carry_the_token():
    line = _emit_access_line_in_fresh_process(f"/ical/{TOKEN}?lang=zh&scope=all")

    assert line, "the access line did not format — the filter broke record.args"
    assert TOKEN not in line, f"token reached the access log: {line}"
    assert "/ical/[REDACTED]" in line, line
    # Diagnostics survive the rewrite: who, and with what outcome.
    assert "172.19.0.3:39076" in line and "200" in line, line
    # The query was never a credential here, and it stays readable.
    assert "lang=zh" in line and "scope=all" in line, line


def test_access_log_would_leak_without_the_filter():
    """Reverse check: the *same* args leak when no filter runs.

    Runs the identical record through uvicorn's formatter without the filter, so
    the redaction observed in the subprocess probe can only come from the filter
    installed by ``import app.main``.
    """
    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("172.19.0.3:39076", "GET", f"/ical/{TOKEN}?lang=zh", "1.1", 200), None,
    )
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)

    assert TOKEN in line, "the probe cannot see the leak it is supposed to detect"


def test_access_log_filter_keeps_the_five_tuple_intact():
    from app.log_redaction import TokenRedactionFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        (f"1.2.3.4:1", "GET", f"/ical/{TOKEN}?lang=zh", "1.1", 200), None,
    )
    TokenRedactionFilter().filter(record)

    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert record.args[2] == "/ical/[REDACTED]?lang=zh"
    assert record.args[4] == 200 and record.args[0] == "1.2.3.4:1"


def _filtered_path(full_path):
    """Apply the filter to one access-style record and return its path arg."""
    from app.log_redaction import TokenRedactionFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:1", "GET", full_path, "1.1", 200), None,
    )
    TokenRedactionFilter().filter(record)
    return record.args[2]


def test_access_log_leaves_other_paths_alone():
    for path in ("/api/me", "/assets/app.js", "/ical/tok-abc", f"/api/earnings/{TOKEN}"):
        # ``/ical/tok-abc`` is shorter than any real token (32 chars) and
        # ``/api/earnings/...`` is a different route: neither is a credential
        # location, and rewriting them would corrupt ordinary debugging.
        assert _filtered_path(path) == path, f"{path} was rewritten"


def test_access_log_redacts_absolute_uri_form():
    path = _filtered_path(f"https://fincal.example/ical/{TOKEN}?scope=watchlist")
    assert TOKEN not in path and "/ical/[REDACTED]" in path, path


def test_install_log_redaction_is_idempotent():
    from app.log_redaction import TokenRedactionFilter, install_log_redaction

    install_log_redaction()
    install_log_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, TokenRedactionFilter)]
    assert len(filters) == 1, "stacked filters mean double work on every request"


def test_main_installs_the_filter_on_import():
    """The deployment imports ``app.main`` under plain ``uvicorn`` — no flag."""
    import app.main  # noqa: F401  (importing is the behaviour under test)
    from app.log_redaction import TokenRedactionFilter

    assert any(isinstance(f, TokenRedactionFilter)
               for f in logging.getLogger("uvicorn.access").filters)


# ── 2. the feed's own warning (criterion 2) ───────────────────────────────────

def test_timeout_warning_logs_a_fingerprint_not_the_token(caplog):
    from app.log_redaction import token_fingerprint

    store = FakeStore()
    client, patchers = _client(store)
    try:
        with patch("app.routers.ical._ical_flight.do", side_effect=TimeoutError):
            with caplog.at_level(logging.WARNING):
                response = client.get(f"/ical/{TOKEN}")
    finally:
        for patcher in patchers:
            patcher.stop()

    assert response.status_code == 503
    assert TOKEN not in caplog.text, caplog.text
    assert token_fingerprint(TOKEN) in caplog.text, caplog.text


# ── 3. functionality is unchanged (criterion 3) ──────────────────────────────

def test_feed_and_api_me_still_use_the_database_token():
    store = FakeStore()
    client, patchers = _client(store)
    try:
        feed = client.get(f"/ical/{TOKEN}")
        me = client.get("/api/me", headers={
            "X-User-Id": "1", "X-User-Email": "t@t.com", "X-User-Name": "T",
        })
    finally:
        for patcher in patchers:
            patcher.stop()

    assert feed.status_code == 200
    assert "BEGIN:VCALENDAR" in feed.text
    assert me.status_code == 200
    assert me.json()["ical_token"] == TOKEN
    assert me.json()["ical_url"].endswith(f"/ical/{TOKEN}")


# ── 4. provider / watchlist URLs (the same class of exit) ─────────────────────

def test_provider_error_message_hides_a_query_credential():
    from app.provider_client import classify_error

    exc = urllib.error.HTTPError(
        f"https://api.example/v1/watch?token={QUERY_SECRET}", 429, "Too Many Requests", {}, None,
    )
    error = classify_error(exc, "watchlist_http")

    assert QUERY_SECRET not in str(error), str(error)
    assert "https://api.example/v1/watch?[REDACTED]" in str(error)


def test_watchlist_source_logs_the_endpoint_not_its_query(caplog):
    from app import config
    from app.watchlist.http_source import HttpWatchlistSource

    url = f"https://api.example/v1/watch?token={QUERY_SECRET}"
    with patch.object(config, "WATCHLIST_HTTP_URL", url), \
            patch("app.watchlist.http_source.http_get_json", lambda _url, cfg=None: {"symbols": ["AAPL"]}):
        with caplog.at_level(logging.INFO, logger="app.watchlist.http_source"):
            assert HttpWatchlistSource().fetch_symbols() == ["AAPL"]

    assert QUERY_SECRET not in caplog.text, caplog.text
    assert "https://api.example/v1/watch?[REDACTED]" in caplog.text


# ── 5. rotation (criterion 4) ────────────────────────────────────────────────

def test_rotate_dry_run_reports_but_writes_nothing():
    rotate = _load_rotate_cli()
    store = FakeStore()

    with patch.object(rotate, "db_cursor", lambda: store.connection()):
        code = rotate.main([])

    assert code == 0
    assert store.users[0]["ical_token"] == TOKEN, "a dry run must not write"


def test_rotate_apply_swaps_the_token_and_kills_the_cached_feed(capsys):
    from app.routers import ical as ical_router
    from app.log_redaction import token_fingerprint

    rotate = _load_rotate_cli()
    store = FakeStore()
    client, patchers = _client(store)
    try:
        # Warm the feed cache for the leaked token, as production polling does.
        assert client.get(f"/ical/{TOKEN}").status_code == 200
        assert any(key[0] == TOKEN for key in ical_router._ical_cache), "cache not warmed"

        with patch.object(rotate, "db_cursor", lambda: store.connection()):
            assert rotate.main(["--apply"]) == 0

        new_token = store.users[0]["ical_token"]
        assert new_token != TOKEN and len(new_token) >= 16

        # The cached body for the old key is gone in this process...
        assert not any(key[0] == TOKEN for key in ical_router._ical_cache)
        # ...so the old URL stops resolving and the new one serves the feed.
        assert client.get(f"/ical/{TOKEN}").status_code == 404
        assert client.get(f"/ical/{new_token}").status_code == 200
    finally:
        for patcher in patchers:
            patcher.stop()

    output = capsys.readouterr().out
    # The CLI reports fingerprints only: it must not re-leak what it replaced.
    assert TOKEN not in output and new_token not in output, output
    assert token_fingerprint(TOKEN) in output
    assert "docker restart" in output, "an out-of-process cache is not invalidated here"


def test_rotate_can_target_one_user():
    rotate = _load_rotate_cli()
    store = FakeStore()
    store.users.append({"id": 8, "portal_user_id": 2, "email": "u@u.com",
                        "name": "U", "ical_token": OTHER_TOKEN})

    with patch.object(rotate, "db_cursor", lambda: store.connection()):
        assert rotate.main(["--apply", "--user-id", "8"]) == 0

    assert store.users[0]["ical_token"] == TOKEN, "an unrelated user was rotated"
    assert store.users[1]["ical_token"] != OTHER_TOKEN


def test_rotate_unknown_user_is_reported_without_writing():
    rotate = _load_rotate_cli()
    store = FakeStore()

    with patch.object(rotate, "db_cursor", lambda: store.connection()):
        assert rotate.main(["--apply", "--email", "nobody@example.com"]) == 1

    assert store.users[0]["ical_token"] == TOKEN
