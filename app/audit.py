"""Admin audit logging (Issue #11)."""
from __future__ import annotations

import logging
from . import db

logger = logging.getLogger(__name__)


def log_admin_action(
    action: str,
    *,
    actor_id: str | None = None,
    actor_email: str | None = None,
    target: str | None = None,
    details: dict | None = None,
    cur=None,
) -> None:
    """Append an admin audit event.

    ``cur`` lets a caller write the event inside the transaction that performs
    the change it describes (Issue #123).  The two outcomes that gives are both
    the honest ones: the record cannot be lost because the change committed
    first and a later insert failed, and a broken audit table cannot turn a
    request that already wrote its row into a 500 whose retry is a 409.
    """
    statement = """INSERT INTO admin_audit_log (action, actor_id, actor_email, target, details)
               VALUES (%s, %s, %s, %s, %s)"""
    params = (action, actor_id, actor_email, target, db.psycopg2.extras.Json(details or {}))
    if cur is not None:
        cur.execute(statement, params)
        return
    with db.db_cursor() as own:
        own.execute(statement, params)


def get_audit_log(limit: int = 50) -> list[dict]:
    """Retrieve recent audit events."""
    limit = min(max(limit, 1), 200)
    with db.db_cursor() as cur:
        cur.execute(
            """SELECT id, action, actor_id, actor_email, target, details, created_at
               FROM admin_audit_log ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
