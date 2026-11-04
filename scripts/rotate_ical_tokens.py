#!/usr/bin/env python3
"""Rotate iCal subscription tokens (Issue #85).

Why
---
``/ical/{token}`` is the feed's only credential: no header, no login, and
``scope=all`` returns the entire earnings universe. ``app/auth.py`` minted the
token once, when the user row was created, and *nothing* in the repo ever
rewrote ``users.ical_token`` — so a token that reached a container log (144
copies of one live token in production, 2026-10-18) stayed valid forever.

What it does
------------
Generates a new ``secrets.token_urlsafe(24)`` for the selected users and writes
it to ``users.ical_token``. The user's existing calendar subscription stops
working and the app shows the new URL at ``GET /api/me`` (``ical_url``) — that
is the only place the new value is published; this script never prints a token,
only an 8-hex-character fingerprint, so running it does not re-leak the
credential it just replaced.

Usage
-----
    python scripts/rotate_ical_tokens.py                      # dry run, all users
    python scripts/rotate_ical_tokens.py --json               # machine-readable plan
    python scripts/rotate_ical_tokens.py --user-id 3 --apply
    python scripts/rotate_ical_tokens.py --email a@b.c --apply

After ``--apply``: the rotation is written to the database and this process's
feed cache is dropped, so a rotation performed *inside* the service takes effect
immediately. A rotation run from the host against a *running* container cannot
reach the server's memory — but it no longer needs to: the service re-checks
every token at most ``ICAL_TOKEN_CACHE_TTL_SECONDS`` (60 s by default) before
serving a cached feed (Issue #112), so the old URL stops answering within that
window. For an immediate cut-off use ``POST /api/admin/ical-tokens/{id}/rotate``,
which rotates inside the serving process.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, ical_tokens  # noqa: E402
from app.db import db_cursor  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate iCal subscription tokens")
    parser.add_argument("--apply", action="store_true",
                        help="write the new tokens (default: dry run)")
    parser.add_argument("--json", action="store_true",
                        help="emit the plan as JSON (human notes go to stderr)")
    parser.add_argument("--user-id", type=int, default=None,
                        help="only this users.id")
    parser.add_argument("--email", default=None,
                        help="only the user with this email")
    args = parser.parse_args(argv)

    # ``--json`` is consumed by other tooling: every human-readable line goes to
    # stderr in that mode so stdout stays one parseable document.
    note = (lambda *a, **k: print(*a, file=sys.stderr, **k)) if args.json else print

    with db_cursor() as cur:
        targets = ical_tokens.select_targets(
            cur, user_id=args.user_id, email=args.email,
        )
        if not targets:
            note("no matching user — nothing to rotate")
            return 1 if (args.user_id is not None or args.email) else 0
        rows = ical_tokens.rotate(cur, targets) if args.apply else ical_tokens.plan(targets)

    # Feed entries and token resolutions this process actually drops. An
    # out-of-process run reports 0 here, which is the truth — the *service*
    # re-validates the token within ICAL_TOKEN_CACHE_TTL_SECONDS regardless.
    dropped = 0
    if args.apply:
        dropped = ical_tokens.invalidate_feed_cache(rows)

    report = {
        "applied": bool(args.apply),
        "users": len(rows),
        "cache_entries_dropped": dropped,
        # Upper bound on how long a revocation made in the database takes to
        # stop being honoured by a *running* service (Issue #112).
        "revocation_effective_within_seconds": config.ICAL_TOKEN_CACHE_TTL_SECONDS,
        "rotations": [
            {
                "user_id": row["user_id"],
                "email": row["email"],
                "old_fingerprint": row["old_fingerprint"],
                "new_fingerprint": row["new_fingerprint"],
            }
            for row in rows
        ],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"mode                  : {'apply' if args.apply else 'dry-run'}")
        print(f"users matched         : {report['users']}")
        for row in report["rotations"]:
            new = row["new_fingerprint"] or "(unchanged)"
            print(f"  id={row['user_id']:<6} {row['email']:<32} "
                  f"{row['old_fingerprint']} -> {new}")
        if args.apply:
            print(f"cache entries dropped : {report['cache_entries_dropped']} (this process)")
            print(f"NOTE: a running service re-checks each token within "
                  f"{report['revocation_effective_within_seconds']:g} s, so the old "
                  f"URL stops answering inside that window without a restart. For an "
                  f"immediate cut-off call POST /api/admin/ical-tokens/<user_id>/rotate.")
        else:
            print("dry run — nothing written; re-run with --apply")

    return 0


if __name__ == "__main__":
    sys.exit(main())
