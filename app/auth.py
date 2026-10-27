"""Identity extraction from forwardAuth headers.

The whole authentication model of the service is "the proxy told us who the
caller is": ``X-User-Id`` / ``X-User-Email`` / ``X-User-Name`` / ``X-User-Role``
are assertions, and ``role == admin`` is what ``require_admin`` believes.

That is only sound while nothing else can *write* those headers. Production runs
this container on the shared ``traefik_traefik-net`` bridge next to a dozen other
containers, all of which can address ``http://fincal:8000`` directly and send any
header they like (Issue #102). Two things therefore happen here:

* When ``AUTH_TRUST_SECRET`` is configured, a request must also carry the proxy's
  shared secret (``AUTH_TRUST_HEADER``, injected by the Traefik ``headers``
  middleware in ``docker-compose.yml``) before any ``X-User-*`` header is
  believed. Empty secret = unchanged behaviour (single-user / trusted-network
  deployments).
* The id is *validated*, not merely parsed: a non-numeric or non-positive
  ``X-User-Id`` answers ``401 invalid_identity`` instead of falling out of
  ``int()`` as a 500 ``internal_error`` with a stack trace — a malformed header
  is a client error and must not read like a code defect in the logs.
"""
import hmac
import secrets
from urllib.parse import unquote

from fastapi import HTTPException, Request

from . import config, db
from .errors import AppError

#: ``users.portal_user_id`` is ``INTEGER`` (int4); see ``_parse_user_id``.
_MAX_PORTAL_USER_ID = 2_147_483_647


def _require_trusted_proxy(request: Request) -> None:
    """Reject the request unless it carries the proxy's shared secret.

    ``hmac.compare_digest`` on bytes: constant time (the value is a credential),
    and it cannot raise ``TypeError`` on a non-ASCII header the way the ``str``
    form does — an unparseable header must be a 401, never a 500.
    """
    secret = config.AUTH_TRUST_SECRET
    if not secret:
        return
    presented = request.headers.get(config.AUTH_TRUST_HEADER, "") or ""
    if not hmac.compare_digest(
        presented.encode("utf-8", "surrogateescape"),
        secret.encode("utf-8", "surrogateescape"),
    ):
        raise AppError(
            "untrusted_proxy",
            "request did not come from the trusted proxy",
            401,
        )


def _parse_user_id(raw: str) -> int:
    """Return a positive integer user id, or raise 401 ``invalid_identity``.

    Bounded to what ``users.portal_user_id`` can hold (``INTEGER``, i.e. int4):
    an id outside that range cannot name a user, and letting it through would
    turn a forged header into a database error — a 500 again, one layer down.
    """
    try:
        user_id = int(str(raw).strip())
    except (TypeError, ValueError):
        raise AppError(
            "invalid_identity",
            "user id must be a positive integer",
            401,
        ) from None
    if user_id <= 0 or user_id > _MAX_PORTAL_USER_ID:
        raise AppError(
            "invalid_identity",
            "user id must be a positive integer",
            401,
        )
    return user_id


def get_current_user(request: Request) -> dict:
    """Extract user from forwardAuth headers. Raises 401 if not authenticated."""
    _require_trusted_proxy(request)
    user_id = request.headers.get(config.HEADER_USER_ID)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "id": _parse_user_id(user_id),
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
