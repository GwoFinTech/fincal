from fastapi import Request, HTTPException
from urllib.parse import unquote
from . import config, db
import secrets


def get_current_user(request: Request) -> dict:
    """Extract user from forwardAuth headers. Raises 401 if not authenticated."""
    user_id = request.headers.get(config.HEADER_USER_ID)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "id": int(user_id),
        "email": request.headers.get(config.HEADER_USER_EMAIL, ""),
        "name": unquote(request.headers.get(config.HEADER_USER_NAME, "")),
        "role": request.headers.get(config.HEADER_USER_ROLE, "user"),
    }


def require_admin(user: dict) -> dict:
    """Require the role injected by kazusa-home-portal forwardAuth."""
    if user.get("role", "").strip().lower() != "admin":
        raise HTTPException(status_code=403, detail="admin_required")
    return user


def ensure_user(portal_user_id: int, email: str, name: str) -> dict:
    """Ensure user exists in fincal DB, create if not. Returns user dict.

    Single upsert instead of SELECT-then-INSERT so concurrent first requests
    from the same portal user cannot race into UniqueViolation (Issue #30).
    The conditional DO UPDATE keeps unchanged rows from being rewritten
    (no per-request write amplification). When the row already matches,
    the DO UPDATE WHERE clause skips it and RETURNING yields nothing, so
    fall back to a plain SELECT inside the same transaction.
    """
    token = secrets.token_urlsafe(24)
    with db.db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (portal_user_id, email, name, ical_token)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (portal_user_id) DO UPDATE
               SET email = EXCLUDED.email,
                   name  = EXCLUDED.name
             WHERE users.email IS DISTINCT FROM EXCLUDED.email
                OR users.name  IS DISTINCT FROM EXCLUDED.name
            RETURNING *
            """,
            (portal_user_id, email, name, token),
        )
        row = cur.fetchone()
        if row is None:
            # Existing row already had identical email/name: DO UPDATE was skipped.
            # The conflicting row is committed by definition of conflict detection,
            # so the read below always finds it within this transaction.
            cur.execute(
                "SELECT * FROM users WHERE portal_user_id = %s",
                (portal_user_id,),
            )
            row = cur.fetchone()
        if row is None:
            # Users rows are never deleted by the application; reaching here
            # means the conflicting row vanished mid-request. Fail loudly
            # instead of returning None to callers.
            raise RuntimeError(
                f"ensure_user: user row for portal_user_id={portal_user_id} vanished during upsert"
            )
        return dict(row)
