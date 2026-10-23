"""Issue #98: the default suite must never open a real database connection.

These cases pin the guard itself. They exist because the failure mode being
fixed was *silent*: a test patched one module's ``db_cursor``, the code under
test wrote through another import of it, and the test passed only where the
production database was readable — so "green" was a property of the host.

A deliberate connection attempt must therefore fail: (1) loudly, with the
caller named, and (2) even when the production code wraps its I/O in a broad
``except Exception`` fallback (``app.universe`` does exactly that, which is how
three of the affected tests kept a green result offline).

Each probe below intentionally trips the guard, so it clears the attempt ledger
after asserting — the attempt *is* the assertion, not a leak.
"""
import psycopg2
import pytest

from app import db


def _expect_refusal(request, attempt) -> str:
    with pytest.raises(pytest.fail.Exception) as excinfo:
        attempt()
    request.node._unexpected_db_attempts.clear()
    return str(excinfo.value)


def test_psycopg2_connect_is_refused(request):
    message = _expect_refusal(
        request, lambda: psycopg2.connect(host="127.0.0.1", port=1, connect_timeout=1)
    )
    assert "unexpected database access" in message
    assert "psycopg2.connect" in message


def test_connection_pool_acquire_is_refused(request):
    """``app.db._get_pool().getconn()`` — the pooled read/write path."""
    message = _expect_refusal(request, lambda: db._get_pool().getconn())
    assert "_get_pool" in message


def test_db_cursor_is_refused(request):
    def attempt():
        with db.db_cursor():
            pass

    assert "unexpected database access" in _expect_refusal(request, attempt)


def test_db_connection_is_refused(request):
    """``db_connection`` connects outside the pool (Issue #31 advisory locks)."""

    def attempt():
        with db.db_connection():
            pass

    _expect_refusal(request, attempt)


def test_a_broad_except_fallback_cannot_swallow_the_refusal(request):
    """A production ``except Exception`` must not turn the attempt green again.

    ``SymbolUniverse._reload`` and ``WatchlistSource.get_symbols_with_status``
    both fall back on *any* exception. If the guard raised a plain
    ``RuntimeError``, those paths would keep a stale/fallback universe and the
    test would pass while reading production — the guard fails with
    ``pytest.fail`` (a ``BaseException``) precisely so it cannot be caught there.
    """
    fell_back = False

    def attempt():
        nonlocal fell_back
        try:
            psycopg2.connect(host="127.0.0.1", port=1, connect_timeout=1)
        except Exception:  # noqa: BLE001 - the pattern the guard must survive
            fell_back = True

    _expect_refusal(request, attempt)
    assert fell_back is False, "the refusal was swallowed by an except Exception handler"


def test_the_pool_singleton_is_reset_between_tests():
    """``app.db._pool`` is process-wide; a leaked pool would serve DB handles."""
    assert db._pool is None
