"""Issue #123: managing the global watchlist must leave a trail.

``app/routers/admin.py`` wrote ``admin_audit_log`` rows for the sync operations
(cancel / recover / retry) and for nothing else: the three write paths that
decide what the calendar, the export and the iCal feed *contain* —
``add_managed_watchlist`` / ``update_managed_watchlist`` /
``delete_managed_watchlist`` — wrote no record at all.  A wrong symbol added or a
row removed therefore could not be attributed afterwards, while a cancelled sync
could.

The record is written through the *same* cursor as the change it describes, so
the two commit together: the previous shape (change commits, audit insert opens
its own transaction) produces a 500 on a failed audit insert for a request whose
write already landed, and its retry then hits 409.
"""
import json
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db as app_db  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402
from app.schemas import ManagedWatchlistInput  # noqa: E402

ACTOR = {"id": 42, "email": "admin@example.com", "role": "admin"}

#: ``managed_watchlist`` rows the fake statements return, per statement kind.
BEFORE_ROW = {"id": 7, "symbol": "AAPL", "market": "US"}
AFTER_ROW = {"id": 7, "symbol": "MSFT", "market": "US",
             "created_at": None, "updated_at": None}
NEW_ROW = {"id": 9, "symbol": "0700.HK", "market": "HK",
           "created_at": None, "updated_at": None}


class _FakeCursor:
    """Replays canned ``fetchone()`` results and records every statement."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    # ── assertions helpers ──────────────────────────────────────────────
    def audit_inserts(self):
        return [entry for entry in self.executed if entry[0].startswith("INSERT INTO admin_audit_log")]

    def statements_mentioning(self, fragment):
        return [entry for entry in self.executed if fragment in entry[0]]


def _run(cursor, call):
    """Invoke a route function with a fake pooled cursor bound to ``cursor``."""
    with patch.object(app_db, "db_cursor", return_value=cursor):
        return call()


def _details_of(entry) -> dict:
    """The ``details`` payload as the DB would store it (``psycopg2.extras.Json``)."""
    payload = entry[1][4]
    adapted = payload.adapted
    return dict(adapted) if isinstance(adapted, dict) else json.loads(adapted)


class AddManagedWatchlistAuditTests(TestCase):
    def test_adding_a_symbol_is_recorded_with_actor_and_target(self):
        cursor = _FakeCursor([NEW_ROW])
        row = _run(cursor, lambda: admin_router.add_managed_watchlist(
            ManagedWatchlistInput(symbol="0700", market="HK"), ACTOR))

        self.assertEqual(row["symbol"], "0700.HK")
        inserts = cursor.audit_inserts()
        self.assertEqual(len(inserts), 1)
        action, actor_id, actor_email, target, _ = inserts[0][1]
        self.assertEqual(action, "add_managed_watchlist")
        self.assertEqual(actor_id, "42")
        self.assertEqual(actor_email, "admin@example.com")
        self.assertEqual(target, "managed_watchlist:9")
        self.assertEqual(_details_of(inserts[0]), {"symbol": "0700.HK", "market": "HK"})

    def test_the_audit_row_rides_the_same_transaction_as_the_change(self):
        cursor = _FakeCursor([NEW_ROW])
        _run(cursor, lambda: admin_router.add_managed_watchlist(
            ManagedWatchlistInput(symbol="0700", market="HK"), ACTOR))
        # One cursor → one commit; nothing is written on a second connection.
        kinds = [entry[0].split()[0] for entry in cursor.executed]
        self.assertEqual(kinds, ["INSERT", "INSERT"])


class UpdateManagedWatchlistAuditTests(TestCase):
    def test_the_previous_identity_is_part_of_the_record(self):
        # SELECT before, SELECT duplicate check (none), UPDATE ... RETURNING
        cursor = _FakeCursor([BEFORE_ROW, None, AFTER_ROW])
        row = _run(cursor, lambda: admin_router.update_managed_watchlist(
            7, ManagedWatchlistInput(symbol="MSFT", market="US"), ACTOR))

        self.assertEqual(row["symbol"], "MSFT")
        inserts = cursor.audit_inserts()
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], "update_managed_watchlist")
        self.assertEqual(_details_of(inserts[0]),
                         {"before": {"symbol": "AAPL", "market": "US"},
                          "after": {"symbol": "MSFT", "market": "US"}})

    def test_a_rejected_update_writes_no_record(self):
        from app.errors import NotFoundError
        cursor = _FakeCursor([None])
        with self.assertRaises(NotFoundError):
            _run(cursor, lambda: admin_router.update_managed_watchlist(
                7, ManagedWatchlistInput(symbol="MSFT", market="US"), ACTOR))
        self.assertEqual(cursor.audit_inserts(), [])


class DeleteManagedWatchlistAuditTests(TestCase):
    def test_the_deleted_row_is_snapshotted_before_it_is_gone(self):
        cursor = _FakeCursor([BEFORE_ROW])
        _run(cursor, lambda: admin_router.delete_managed_watchlist(7, ACTOR))

        inserts = cursor.audit_inserts()
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], "delete_managed_watchlist")
        self.assertEqual(inserts[0][1][3], "managed_watchlist:7")
        self.assertEqual(_details_of(inserts[0]),
                         {"deleted": {"symbol": "AAPL", "market": "US"}})
        # The snapshot comes from the DELETE itself, not a read that a concurrent
        # writer could have invalidated.
        deletes = cursor.statements_mentioning("DELETE FROM managed_watchlist")
        self.assertEqual(len(deletes), 1)
        self.assertIn("RETURNING", deletes[0][0])

    def test_deleting_a_missing_row_is_not_recorded(self):
        from app.errors import NotFoundError
        cursor = _FakeCursor([None])
        with self.assertRaises(NotFoundError):
            _run(cursor, lambda: admin_router.delete_managed_watchlist(7, ACTOR))
        self.assertEqual(cursor.audit_inserts(), [])


class AuditWriterTests(TestCase):
    def test_an_explicit_cursor_skips_the_private_transaction(self):
        """``log_admin_action(cur=...)`` must not open its own connection."""
        from app.audit import log_admin_action
        cursor = _FakeCursor([])
        with patch.object(app_db, "db_cursor",
                          side_effect=AssertionError("must not open its own cursor")):
            log_admin_action("x", target="t", cur=cursor)
        self.assertEqual(len(cursor.audit_inserts()), 1)

    def test_details_never_carry_a_token_like_field(self):
        cursor = _FakeCursor([NEW_ROW])
        _run(cursor, lambda: admin_router.add_managed_watchlist(
            ManagedWatchlistInput(symbol="0700", market="HK"), ACTOR))
        serialized = json.dumps(_details_of(cursor.audit_inserts()[0]))
        for forbidden in ("token", "password", "secret"):
            self.assertNotIn(forbidden, serialized.lower())
