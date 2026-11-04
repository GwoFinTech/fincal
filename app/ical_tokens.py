"""Rotate iCal subscription tokens (Issue #85).

A subscription token is the feed's only credential, it never expired, and until
now the repo had no way to replace one: ``app/auth.py`` minted it when the user
row was first created and nothing else ever wrote ``users.ical_token``. Once a
token reached a log line (``docker logs``, a pasted ops record, a screenshot) the
only remedies were hand-editing the database or deleting the user.

This module holds the rotation itself so the CLI (``scripts/
rotate_ical_tokens.py``) and the admin endpoint
(``POST /api/admin/ical-tokens/{user_id}/rotate``) share one implementation:

* :func:`select_targets` reads the users to rotate (all, one id, one email);
* :func:`plan` produces new tokens without writing, so a dry run can report the
  blast radius — it returns fingerprints only, never a credential;
* :func:`rotate` writes them and returns the same shape;
* :func:`invalidate_feed_cache` drops the cached feed and the cached token
  resolution for the rotated users, and reports how many feed entries it really
  removed in this process (Issue #112 — the service now checks every token
  before serving a cached body, so a rotation no longer needs a restart).

The token entropy stays ``secrets.token_urlsafe(24)``: the same 32-character
base64url shape ``ensure_user`` generates, so nothing downstream (path regexes,
length assumptions, existing client URLs) has to change.
"""
from __future__ import annotations

import secrets
from collections.abc import Iterable, Sequence

from .log_redaction import token_fingerprint

#: Byte count behind ``secrets.token_urlsafe`` — kept identical to
#: ``app/auth.py``'s first-issue path so a rotated token is indistinguishable
#: from an original one (32 url-safe characters).
TOKEN_BYTES = 24


def generate_token() -> str:
    """Return a fresh subscription token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def select_targets(cur, *, user_id: int | None = None,
                   email: str | None = None) -> list[dict]:
    """Read ``(id, email, ical_token)`` for the users to rotate.

    No filter means every user — the ``scope=all`` feed has no per-user limit on
    what a leaked token reads, so a global rotation is a legitimate call.
    """
    sql = "SELECT id, email, ical_token FROM users"
    clauses: list[str] = []
    params: list[object] = []
    if user_id is not None:
        clauses.append("id = %s")
        params.append(user_id)
    if email:
        clauses.append("email = %s")
        params.append(email)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    cur.execute(sql, tuple(params))
    return [dict(row) for row in cur.fetchall()]


def plan(targets: Sequence[dict]) -> list[dict]:
    """Describe what :func:`rotate` would do, without generating anything.

    Fingerprints of the *current* tokens are enough to tell them apart in an
    operator report; the values themselves are never returned.
    """
    return [
        {
            "user_id": row["id"],
            "email": row.get("email") or "",
            "old_fingerprint": token_fingerprint(row.get("ical_token")),
            "new_fingerprint": "",
        }
        for row in targets
    ]


def rotate(cur, targets: Sequence[dict]) -> list[dict]:
    """Replace each target's token in ``users`` and describe the outcome.

    The old token is captured first: it is the key the feed cache holds, and the
    caller needs it to drop that entry (see :func:`invalidate_feed_cache`).
    Rows are updated one by one on the caller's cursor/transaction, so a failure
    leaves the untouched range readable rather than half-written.
    """
    results: list[dict] = []
    for row in targets:
        old_token = row.get("ical_token") or ""
        new_token = generate_token()
        cur.execute(
            "UPDATE users SET ical_token = %s WHERE id = %s",
            (new_token, row["id"]),
        )
        results.append(
            {
                "user_id": row["id"],
                "email": row.get("email") or "",
                "old_token": old_token,
                "new_token": new_token,
                "old_fingerprint": token_fingerprint(old_token),
                "new_fingerprint": token_fingerprint(new_token),
            }
        )
    return results


def invalidate_feed_cache(rotations: Iterable[dict]) -> int:
    """Drop the cached feed and token resolution for each rotated user.

    ``rotations`` are :func:`rotate`'s result rows: each carries ``user_id``
    plus the ``old_token``/``new_token`` involved. Returns how many **feed
    entries were actually removed from this process** (Issue #112) — the old
    implementation counted the tokens it was handed, so an out-of-process
    rotation reported ``2 × users`` dropped while the server's cache was
    untouched.

    A rotation no longer needs a restart to take effect: the service re-reads
    every token at most ``config.ICAL_TOKEN_CACHE_TTL_SECONDS`` (60 s) after it
    was last resolved, and this function makes that cut-off immediate when the
    rotation runs inside the serving process (admin endpoint, tests).
    """
    from .routers.ical import forget_subscription_token, invalidate_ical_cache

    dropped = 0
    for row in rotations:
        dropped += invalidate_ical_cache(row["user_id"])
        forget_subscription_token(row.get("old_token"), row.get("new_token"))
    return dropped
