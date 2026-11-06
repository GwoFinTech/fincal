"""Issue #114: the *handler* layer, not one logger, keeps tokens out of logs.

Issue #85 installed ``TokenRedactionFilter`` on ``uvicorn.access`` and proved
the access line is clean. That filter never sees a record emitted by
``app.errors``: logging applies a logger's filters only to records *that*
logger emits, and a propagating record goes straight to the root handler. So the
route-level 500 fallback (``app/errors.py``) wrote ``GET /ical/<token>``
verbatim — and it is the only catch-all on the request path, so every unhandled
error on a feed (a DB blip, a ``generate_ical`` defect as in #108) produced a
live subscription credential in ``docker logs``.

These tests pin the fix at both layers: the root handler (every propagated
record) and the call site (``error_middleware`` redacts the path it logs).
"""
import io
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "Zx9QQQQQQQQQQQQQvwwwwwwwwwwwwwww"  # 32 base64url-ish chars, like token_urlsafe(24)

_APP_ERRORS = "app.errors"


# ── helpers ──────────────────────────────────────────────────────────────────

def _capture_root():
    """Attach a handler to root, install the filter, return (handler, stream).

    The handler is created *before* the install so the install is the thing that
    covers it: that is also how the production process works (``basicConfig`` in
    ``app.main`` runs first, ``install_log_redaction()`` second).
    """
    from app.log_redaction import install_log_redaction

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    install_log_redaction()
    return handler, stream


def _detach(handler):
    logging.getLogger().removeHandler(handler)


@contextmanager
def _no_handler_filters():
    """Temporarily strip the redaction filter from every root handler.

    Simulates a handler the install never reached, so a test can tell the call
    site's own redaction apart from the handler layer's (#114 asks for both).
    """
    from app.log_redaction import TokenRedactionFilter

    removed = []
    for handler in list(logging.getLogger().handlers):
        for existing in list(handler.filters):
            if isinstance(existing, TokenRedactionFilter):
                handler.removeFilter(existing)
                removed.append((handler, existing))
    try:
        yield
    finally:
        for handler, existing in removed:
            handler.addFilter(existing)


def _client():
    """TestClient whose feed resolution raises — the production 500 branch."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import earnings as earnings_mod
    from app.routers import ical as ical_router

    ical_router._ical_cache.clear()
    ical_router._token_cache.clear()
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        earnings_mod, "fetch_earnings_from_db",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not reach the data layer")),
    )
    patcher.setattr(
        ical_router, "_resolve_subscriber",
        lambda token: (_ for _ in ()).throw(RuntimeError("connection reset")),
    )
    return TestClient(app, raise_server_exceptions=False), patcher


# ── 1. the 500 branch on /ical (criterion 1) ─────────────────────────────────

def test_ical_500_redacts_the_token_in_application_logs():
    handler, stream = _capture_root()
    client, patcher = _client()
    try:
        response = client.get(f"/ical/{TOKEN}?lang=zh&scope=all")
    finally:
        patcher.undo()
        _detach(handler)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"

    output = stream.getvalue()
    assert TOKEN not in output, f"the subscription token reached the app log: {output}"
    assert "/ical/[REDACTED]" in output, output
    # The line still says what failed and where — redaction is not deletion.
    assert "unhandled error on GET" in output, output
    assert "RuntimeError" in output and "connection reset" in output, output


def test_ical_500_would_leak_without_the_handler_filter():
    """Reverse check: strip the filter and the *same* request leaks the token.

    Without this, the test above could pass because ``error_middleware`` happens
    to redact; the pair proves the handler layer is doing work the call site does
    not duplicate. It is also the shape of the regression #114 reports.
    """
    handler, stream = _capture_root()
    # ...and let the call site's own redaction be bypassed too, by logging the
    # raw path from a throwaway app logger: this is what any *other* app.* log
    # line (a warning, a metrics failure) would have done before the fix.
    client, patcher = _client()
    try:
        with _no_handler_filters():
            response = client.get(f"/ical/{TOKEN}")
            logging.getLogger("app.routers.ical").error(
                "feed failed for %s", f"/ical/{TOKEN}")
    finally:
        patcher.undo()
        _detach(handler)

    assert response.status_code == 500
    assert TOKEN in stream.getvalue(), "the probe cannot see the leak it is supposed to detect"


# ── 2. any app.* logger, not just the one we remembered (criterion 2) ────────

@pytest.mark.parametrize("name", [
    "app.errors",
    "app.routers.ical",
    "app.sync_audit",
    "app.anything.deeply.nested",
    "thirdparty.library",
])
def test_any_propagated_logger_is_redacted_at_the_handler(name):
    handler, stream = _capture_root()
    try:
        logging.getLogger(name).warning("failed: %s", f"/ical/{TOKEN}?scope=all")
    finally:
        _detach(handler)

    output = stream.getvalue()
    assert TOKEN not in output, output
    assert "/ical/[REDACTED]?scope=all" in output, output


def test_install_covers_every_root_handler_and_does_not_stack():
    from app.log_redaction import TokenRedactionFilter, install_log_redaction

    first = logging.StreamHandler(io.StringIO())
    second = logging.StreamHandler(io.StringIO())
    root = logging.getLogger()
    root.addHandler(first)
    root.addHandler(second)
    try:
        install_log_redaction()
        install_log_redaction()
        install_log_redaction()
        for handler in root.handlers:
            count = sum(isinstance(f, TokenRedactionFilter) for f in handler.filters)
            assert count == 1, f"{handler} carries {count} redaction filters"
    finally:
        root.removeHandler(first)
        root.removeHandler(second)


def test_the_install_is_not_bound_to_one_logger_name():
    """``logger_name`` stays a parameter, but the handler layer is always covered."""
    from app.log_redaction import TokenRedactionFilter, install_log_redaction

    handler = logging.StreamHandler(io.StringIO())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_log_redaction("some.other.access")
        assert any(isinstance(f, TokenRedactionFilter) for f in handler.filters)
        assert any(isinstance(f, TokenRedactionFilter)
                   for f in logging.getLogger("some.other.access").filters)
    finally:
        root.removeHandler(handler)
        logging.getLogger("some.other.access").filters = [
            f for f in logging.getLogger("some.other.access").filters
            if not isinstance(f, TokenRedactionFilter)
        ]


# ── 3. the call site keeps its own guard (criterion 3, belt and braces) ──────

def test_the_500_branch_redacts_the_path_even_without_a_handler_filter():
    handler, stream = _capture_root()
    client, patcher = _client()
    try:
        with _no_handler_filters():
            response = client.get(f"/ical/{TOKEN}")
    finally:
        patcher.undo()
        _detach(handler)

    assert response.status_code == 500
    output = stream.getvalue()
    assert TOKEN not in output, output
    assert "/ical/[REDACTED]" in output, output


# ── 4. the deployment path: plain `uvicorn app.main:app` (criterion 4) ───────

_PROBE = r'''
import logging, sys
import app.main  # noqa: F401  — importing is the deployment behaviour
# No handler added here: the point is what the install did to the handlers that
# already existed (basicConfig's StreamHandler on root).
logging.getLogger("app.errors").error("unhandled error on GET %s", sys.argv[1])
for handler in logging.getLogger().handlers:
    names = [type(f).__name__ for f in handler.filters]
    print("HANDLER", type(handler).__name__, names, file=sys.stdout)
'''


def test_a_fresh_process_installs_the_filter_on_root_handlers():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, f"/ical/{TOKEN}?lang=zh"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert TOKEN not in proc.stderr and TOKEN not in proc.stdout, proc.stderr
    assert "/ical/[REDACTED]" in proc.stderr, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.startswith("HANDLER ")]
    assert lines, proc.stdout
    assert all("TokenRedactionFilter" in line for line in lines), proc.stdout


# ── 5. nothing else changed (criterion 5) ────────────────────────────────────

def test_unknown_token_is_still_a_404_and_the_feed_still_serves():
    from fastapi.testclient import TestClient
    from app.main import app
    from app import db
    from app import earnings as earnings_mod
    from app.routers import ical as ical_router
    from datetime import date

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=()):
            return self

        def fetchall(self):
            return [{"symbol": "AAPL", "market": "US"}]

        def fetchone(self):
            return None

    handler, stream = _capture_root()
    patch = pytest.MonkeyPatch()
    ical_router._ical_cache.clear()
    ical_router._token_cache.clear()
    aapl = {"symbol": "AAPL", "market": "US", "company_name": "Apple",
            "report_date": date(2026, 11, 2), "before_after": "after"}
    patch.setattr(db, "db_cursor", lambda: _Cursor())
    patch.setattr(earnings_mod, "fetch_earnings_from_db", lambda **kw: [dict(aapl)])
    patch.setattr(ical_router, "_resolve_subscriber",
                  lambda token: (7, "t@t.com") if token == TOKEN else None)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get(f"/ical/{TOKEN}").status_code == 200
        assert client.get("/ical/unknown-token-000000").status_code == 404
        feed = client.get(f"/ical/{TOKEN}")
    finally:
        patch.undo()
        _detach(handler)

    assert feed.status_code == 200
    assert "BEGIN:VCALENDAR" in feed.text
    assert TOKEN not in stream.getvalue(), stream.getvalue()
