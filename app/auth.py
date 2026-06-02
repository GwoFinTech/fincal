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


def ensure_user(portal_user_id: int, email: str, name: str) -> dict:
    """Ensure user exists in fincal DB, create if not. Returns user dict."""
    with db.db_cursor() as cur:
        cur.execute(
            "SELECT * FROM users WHERE portal_user_id = %s",
            (portal_user_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE users SET email=%s, name=%s WHERE portal_user_id=%s RETURNING *",
                (email, name, portal_user_id),
            )
            return dict(cur.fetchone())
        token = secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO users (portal_user_id, email, name, ical_token) VALUES (%s, %s, %s, %s) RETURNING *",
            (portal_user_id, email, name, token),
        )
        return dict(cur.fetchone())
