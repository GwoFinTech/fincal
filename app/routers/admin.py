"""Administrator APIs backed by kazusa-home-portal forwardAuth roles."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from .. import config, db
from ..admin_watchlist import normalize_managed_symbol
from ..auth import get_current_user, require_admin
from ..watchlist import get_source
from ..errors import AppError, NotFoundError, ConflictError, ForbiddenError
from ..schemas import (
    ManagedWatchlistItem, ManagedWatchlistInput as ManagedInput, SyncRun, SyncRunCancelResult,
    SyncRunRecoverResult, SyncRunRetryResult, AuditLogEntry, HealthResponse, ReadyResponse,
    DiagnosticsResponse, OverviewResponse,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# Moved to app/schemas.py


def admin_user(user=Depends(get_current_user)):
    return require_admin(user)


def source_description() -> dict:
    configured = config.WATCHLIST_SOURCE.strip().lower()
    return {
        "configured": configured,
        "type": "postgresql" if configured in ("tsummt", "hybrid") else configured,
        "location": f"{config.DB_HOST}:{config.DB_PORT}/{config.TSUMMT_DB}" if configured in ("tsummt", "hybrid") else "fincal.managed_watchlist",
        "transport": "database" if configured in ("tsummt", "hybrid") else "local",
        "external_dependency": configured == "tsummt",
        "local_fallback": configured in ("tsummt", "hybrid", "http"),
    }


@router.get("/overview", response_model=OverviewResponse)
def overview(_: dict = Depends(admin_user)):
    source = get_source()
    result = source.get_symbols_with_status(force_refresh=True)
    with db.db_cursor() as cur:
        cur.execute("SELECT id, symbol, market, created_at, updated_at FROM managed_watchlist ORDER BY market, symbol")
        managed = [dict(row) for row in cur.fetchall()]
    return {
        "source": {
            **source_description(),
            "symbol_count": len(result.symbols),
            "error_code": result.error_code,
            "stale": result.stale,
            "last_success_at": result.last_success_at.isoformat() if result.last_success_at else None,
        },
        "external_symbols": result.symbols,
        "managed_watchlist": managed,
    }


@router.get("/watchlist", response_model=list[ManagedWatchlistItem])
def list_managed_watchlist(_: dict = Depends(admin_user)):
    with db.db_cursor() as cur:
        cur.execute("SELECT id, symbol, market, created_at, updated_at FROM managed_watchlist ORDER BY market, symbol")
        return [dict(row) for row in cur.fetchall()]


@router.post("/watchlist", status_code=201)
def add_managed_watchlist(payload: ManagedInput, _: dict = Depends(admin_user)):
    try:
        symbol, market = normalize_managed_symbol(payload.symbol, payload.market)
    except ValueError as exc:
        raise AppError("invalid_symbol", str(exc), 422)
    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO managed_watchlist (symbol, market) VALUES (%s, %s)
               ON CONFLICT (symbol, market) DO UPDATE SET updated_at=NOW()
               RETURNING id, symbol, market, created_at, updated_at""",
            (symbol, market),
        )
        return dict(cur.fetchone())


@router.put("/watchlist/{watchlist_id}", response_model=ManagedWatchlistItem)
def update_managed_watchlist(watchlist_id: int, payload: ManagedInput, _: dict = Depends(admin_user)):
    try:
        symbol, market = normalize_managed_symbol(payload.symbol, payload.market)
    except ValueError as exc:
        raise AppError("invalid_symbol", str(exc), 422)
    with db.db_cursor() as cur:
        cur.execute("SELECT 1 FROM managed_watchlist WHERE id=%s", (watchlist_id,))
        if not cur.fetchone():
            raise NotFoundError("managed_watchlist")
        cur.execute("SELECT id FROM managed_watchlist WHERE symbol=%s AND market=%s AND id != %s", (symbol, market, watchlist_id))
        if cur.fetchone():
            raise ConflictError("managed_watchlist_duplicate", "symbol already exists")
        cur.execute(
            """UPDATE managed_watchlist SET symbol=%s, market=%s, updated_at=NOW()
               WHERE id=%s RETURNING id, symbol, market, created_at, updated_at""",
            (symbol, market, watchlist_id),
        )
        return dict(cur.fetchone())


@router.delete("/watchlist/{watchlist_id}")
def delete_managed_watchlist(watchlist_id: int, _: dict = Depends(admin_user)):
    with db.db_cursor() as cur:
        cur.execute("DELETE FROM managed_watchlist WHERE id=%s", (watchlist_id,))
        if cur.rowcount != 1:
            raise NotFoundError("managed_watchlist")
    return {"status": "removed"}


@router.get("/sync-runs", response_model=list[SyncRun])
def list_sync_runs(limit: int = 50, _: dict = Depends(admin_user)):
    limit = min(max(limit, 1), 200)
    with db.db_cursor() as cur:
        cur.execute(
            """SELECT id, stage, status, source, symbol_count, record_count,
                      details, error_code, started_at, finished_at,
                      heartbeat_at, attempt, idempotency_key,
                      phase, current, total
               FROM sync_runs ORDER BY started_at DESC LIMIT %s""",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


@router.post("/sync-runs/{run_id}/cancel", response_model=SyncRunCancelResult)
def cancel_sync_run(run_id: int, user=Depends(admin_user)):
    from ..sync_audit import request_cancel
    from ..audit import log_admin_action
    cancelled = request_cancel(run_id)
    if not cancelled:
        raise ConflictError("run_not_cancellable", "run is not in running state")
    log_admin_action("cancel_sync_run", actor_id=str(user.get("id")), actor_email=user.get("email"),
                     target=f"sync_run:{run_id}")
    return {"status": "cancelled", "run_id": run_id}


@router.post("/sync-runs/recover", response_model=SyncRunRecoverResult)
def recover_stale_runs(user=Depends(admin_user)):
    from ..sync_audit import recover_stale_runs
    from ..audit import log_admin_action
    count = recover_stale_runs()
    log_admin_action("recover_stale_runs", actor_id=str(user.get("id")), actor_email=user.get("email"),
                     details={"recovered": count})
    return {"recovered": count}


@router.get("/audit-log", response_model=list[AuditLogEntry])
def list_audit_log(limit: int = 50, _: dict = Depends(admin_user)):
    from ..audit import get_audit_log
    return get_audit_log(limit)


@router.get("/diagnostics", response_model=DiagnosticsResponse)
def diagnostics(_: dict = Depends(admin_user)):
    """Provider metrics and cache diagnostics (Issue #15)."""
    from ..metrics import metrics
    result = metrics.snapshot()
    # Add sync run summary
    with db.db_cursor() as cur:
        cur.execute("""SELECT status, COUNT(*) as cnt FROM sync_runs
                       WHERE started_at > NOW() - INTERVAL '24 hours'
                       GROUP BY status ORDER BY cnt DESC""")
        result["sync_runs_24h"] = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT stage, status, started_at, finished_at
                       FROM sync_runs ORDER BY started_at DESC LIMIT 5""")
        result["recent_syncs"] = [dict(r) for r in cur.fetchall()]
    return result


@router.get("/health", response_model=HealthResponse)
def health_check():
    """Full dependency health status (Issue #11, #14, #20). No auth required."""
    from ..version import get_version
    checks = {}

    # PostgreSQL
    try:
        with db.db_cursor() as cur:
            cur.execute("SELECT 1")
        checks["postgresql"] = {"status": "healthy"}
    except Exception as exc:
        checks["postgresql"] = {"status": "not_ready", "error": type(exc).__name__}

    # Kurumi API
    try:
        from .. import config
        import urllib.request
        url = f"{config.KURUMI_API_URL}/api/config"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            checks["kurumi"] = {"status": "healthy" if resp.status == 200 else "degraded"}
    except Exception:
        checks["kurumi"] = {"status": "degraded"}

    # Futu OpenD
    try:
        from .. import config
        import socket
        with socket.create_connection((config.FUTU_HOST, config.FUTU_PORT), timeout=2):
            checks["futu"] = {"status": "healthy"}
    except Exception:
        checks["futu"] = {"status": "degraded"}

    # Longbridge CLI
    try:
        import subprocess
        p = subprocess.run(["longbridge", "--version"], capture_output=True, timeout=5)
        checks["longbridge"] = {"status": "healthy" if p.returncode == 0 else "degraded"}
    except Exception:
        checks["longbridge"] = {"status": "degraded"}

    statuses = [c["status"] for c in checks.values()]
    if all(s == "healthy" for s in statuses):
        overall = "healthy"
    elif any(s == "not_ready" for s in statuses):
        overall = "not_ready"
    else:
        overall = "degraded"

    return {"status": overall, "version": get_version(), "checks": checks}


@router.get("/ready", response_model=ReadyResponse)
def readiness_check():
    """Readiness probe (Issue #14). Returns 200 only when core deps are OK.

    Core: PostgreSQL must be healthy. External providers are optional.
    """
    try:
        with db.db_cursor() as cur:
            cur.execute("SELECT 1")
        return {"status": "ready"}
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"status": "not_ready"})


@router.post("/sync-runs/{run_id}/retry", response_model=SyncRunRetryResult)
def retry_sync_run(run_id: int, user=Depends(admin_user)):
    """Create a retry run from a failed/interrupted/cancelled run (Issue #19)."""
    from ..sync_audit import start_run
    from ..audit import log_admin_action

    with db.db_cursor() as cur:
        cur.execute(
            "SELECT stage, source, symbol_count, status, idempotency_key FROM sync_runs WHERE id=%s",
            (run_id,),
        )
        original = cur.fetchone()
        if not original:
            raise NotFoundError("sync_run")
        if original["status"] not in ("failed", "interrupted", "cancelled"):
            raise ConflictError("run_not_retryable",
                                f"cannot retry run in '{original['status']}' state")

    # Create new run with same stage/source but new idempotency key
    new_key = original["idempotency_key"]
    if new_key:
        new_key = f"{new_key}:retry:{run_id}"

    new_id = start_run(
        original["stage"], original["source"],
        symbol_count=original["symbol_count"],
        idempotency_key=new_key,
    )

    if new_id is None:
        raise ConflictError("retry_already_running", "a retry is already in progress")

    log_admin_action("retry_sync_run", actor_id=str(user.get("id")),
                     actor_email=user.get("email"),
                     target=f"sync_run:{run_id}",
                     details={"new_run_id": new_id})

    return {"status": "retry_created", "original_run_id": run_id, "new_run_id": new_id}
