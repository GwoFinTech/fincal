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
) -> None:
    """Append an admin audit event."""
    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO admin_audit_log (action, actor_id, actor_email, target, details)
               VALUES (%s, %s, %s, %s, %s)""",
            (action, actor_id, actor_email, target, db.psycopg2.extras.Json(details or {})),
        )


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
