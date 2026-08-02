"""Small, failure-safe audit API for standalone earnings sync scripts."""
from datetime import datetime, timezone
from . import db


def start_run(stage: str, source: str, symbol_count: int = 0) -> int:
    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO sync_runs (stage, status, source, symbol_count)
               VALUES (%s, 'running', %s, %s) RETURNING id""",
            (stage, source, symbol_count),
        )
        return cur.fetchone()["id"]


def finish_run(run_id: int, *, status: str, record_count: int = 0,
               details: dict | None = None, error_code: str | None = None) -> None:
    with db.db_cursor() as cur:
        cur.execute(
            """UPDATE sync_runs
               SET status=%s, record_count=%s, details=%s, error_code=%s,
                   finished_at=%s
               WHERE id=%s""",
            (status, record_count, db.psycopg2.extras.Json(details or {}), error_code,
             datetime.now(timezone.utc), run_id),
        )
