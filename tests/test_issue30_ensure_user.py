"""Regression tests for Issue #30: ensure_user read-modify-write race.

Runs against the local PostgreSQL `fincal` database when reachable; skips
otherwise (CI / environments without DB keep the suite green).

Covers the acceptance criteria:
1. Concurrent first requests for the same portal_user_id all succeed and
   produce exactly one row (previously: random UniqueViolation → 500).
2. Repeated calls with unchanged email/name no longer rewrite the row
   (no per-request write amplification).
3. Changed email/name still propagates to the stored row.
"""
import random
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _db_connect():
    from app import config

    import psycopg2

    return psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        connect_timeout=3,
    )


def _db_reachable():
    try:
        conn = _db_connect()
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="local fincal PostgreSQL not available"
)

# Test-only portal_user_id range, far away from real forwardAuth ids.
PORTAL_BASE = 987_654_000


@pytest.fixture()
def isolated_db():
    """Point app.db at a fresh pool with headroom for 16 concurrent threads,
    and guarantee cleanup of test-range users."""
    import psycopg2.pool

    from app import config, db

    pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=24,
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
    )
    old_pool = db._pool
    db._pool = pool

    pid = PORTAL_BASE + random.randint(1, 999_999)

    def cleanup():
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE portal_user_id >= %s", (PORTAL_BASE,))
            conn.commit()
        finally:
            pool.putconn(conn)

    cleanup()
    yield pid
    cleanup()

    db._pool = old_pool
    pool.closeall()


def _fetch_rows(pid):
    conn = _db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE portal_user_id = %s", (pid,))
            assert cur.description is not None
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _xmin(pid):
    conn = _db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT xmin::text::bigint FROM users WHERE portal_user_id = %s", (pid,)
            )
            row = cur.fetchone()
            assert row is not None
            return row[0]
    finally:
        conn.close()


def test_concurrent_first_requests_all_succeed_single_row(isolated_db):
    """Acceptance #1: 16 concurrent first requests → no errors, one row."""
    from app.auth import ensure_user

    pid = isolated_db
    n = 16
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        barrier.wait()
        try:
            results.append(ensure_user(pid, f"issue30-{pid}@test.local", f"u{i}"))
        except Exception as exc:  # noqa: BLE001 - collect everything
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"concurrent ensure_user raised: {errors}"
    assert len(results) == n
    assert all(r["portal_user_id"] == pid for r in results)

    rows = _fetch_rows(pid)
    assert len(rows) == 1, f"expected exactly one user row, got {len(rows)}"


def test_no_write_amplification_when_info_unchanged(isolated_db):
    """Acceptance #2: unchanged email/name must not rewrite the row."""
    from app.auth import ensure_user

    pid = isolated_db
    email = f"issue30-stable-{pid}@test.local"

    first = ensure_user(pid, email, "SameName")
    xmin_before = _xmin(pid)

    second = ensure_user(pid, email, "SameName")
    xmin_after = _xmin(pid)

    # Row was not rewritten: same tuple version, same identity fields.
    assert xmin_after == xmin_before, (
        "ensure_user rewrote an unchanged row (write amplification)"
    )
    assert second["id"] == first["id"]
    assert second["ical_token"] == first["ical_token"]


def test_changed_profile_still_propagates(isolated_db):
    """Changed email/name must still be persisted and returned."""
    from app.auth import ensure_user

    pid = isolated_db
    first = ensure_user(pid, f"a-{pid}@test.local", "Before")
    updated = ensure_user(pid, f"b-{pid}@test.local", "After")

    assert (first["email"], first["name"]) == (f"a-{pid}@test.local", "Before")

    rows = _fetch_rows(pid)
    assert len(rows) == 1
    assert rows[0]["email"] == f"b-{pid}@test.local"
    assert rows[0]["name"] == "After"
    assert updated["name"] == "After"
    # Token is generated for insert only; updates must never rotate it.
    assert rows[0]["ical_token"] == first["ical_token"]
