"""Administrator APIs backed by kazusa-home-portal forwardAuth roles."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from .. import config, db
from ..admin_watchlist import normalize_managed_symbol
from ..auth import get_current_user, require_admin
from ..watchlist import get_source
from ..universe import invalidate_symbol_universe, universe_status
from ..errors import AppError, NotFoundError, ConflictError, ForbiddenError
from ..schemas import (
    ManagedWatchlistItem, ManagedWatchlistInput as ManagedInput, SyncRun, SyncRunCancelResult,
    SyncRunRecoverResult, SyncRunRetryResult, AuditLogEntry, HealthResponse, ReadyResponse,
    DiagnosticsResponse, OverviewResponse,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

#: Probe verdict for a subject that cannot exist where the probe runs (Issue #79).
#: Reported inside ``checks`` like any other probe, but excluded from the
#: aggregate ``status``: it expresses "not this process's business", not a fault.
NOT_APPLICABLE = "not_applicable"
#: Language-independent reason for the above: the service image ships no such tool.
ERROR_TOOL_NOT_IN_IMAGE = "tool_not_in_image"
#: The tool exists but the probe itself blew up (timeout, permission, …).
ERROR_TOOL_PROBE_FAILED = "tool_probe_failed"
#: The tool exists and ran, but exited non-zero.
ERROR_TOOL_EXIT_CODE = "tool_nonzero_exit"


def probe_host_tool(name: str) -> dict:
    """Probe a host-side pipeline CLI as a capability, not a dependency (#79).

    FinCal's sync pipeline runs the Longbridge CLI from the *host*
    (``scripts/sync_*.py`` under cron, see ``README``) while the service image
    ships only the web app (``Dockerfile`` copies ``app/`` and the web
    dependencies). Asking the container for the binary therefore answered a
    constant ``degraded``, which pinned the aggregate health status there even
    when every dependency the service actually uses was healthy — a real Kurumi
    or PostgreSQL outage became indistinguishable from that permanent noise.

    The verdict distinguishes "not installed here" from "installed but failing":

    * ``{"status": "not_applicable", "error_code": "tool_not_in_image"}`` — not
      on this host's ``PATH``, so the probe cannot express a service fault. The
      entry stays visible in ``checks`` (and is asserted by
      ``scripts/check_service_health.py``), it just does not vote;
    * ``{"status": "healthy"}`` / ``{"status": "degraded", "error_code": …}`` —
      the tool is present, so its verdict counts towards the aggregate.
    """
    import shutil
    import subprocess

    binary = shutil.which(name)
    if binary is None:
        return {"status": NOT_APPLICABLE, "error_code": ERROR_TOOL_NOT_IN_IMAGE}
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, timeout=5)
    except Exception:
        return {"status": "degraded", "error_code": ERROR_TOOL_PROBE_FAILED}
    if proc.returncode != 0:
        return {"status": "degraded", "error_code": ERROR_TOOL_EXIT_CODE}
    return {"status": "healthy"}


def admin_user(user=Depends(get_current_user)):
    return require_admin(user)


def _normalize_managed(payload: ManagedInput) -> tuple[str, str]:
    """Validate a managed-watchlist entry, mapping the code to the admin error
    contract: a symbol/market mismatch keeps its own language-neutral code so the
    caller can tell it apart from other invalid input (Issue #66), while the rest
    stays under the existing ``invalid_symbol`` with the reason in ``details``."""
    try:
        return normalize_managed_symbol(payload.symbol, payload.market)
    except ValueError as exc:
        code = str(exc)
        if code == "symbol_market_mismatch":
            raise AppError(code, "symbol suffix does not match the requested market", 422)
        raise AppError("invalid_symbol", code, 422)


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
    symbol, market = _normalize_managed(payload)
    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO managed_watchlist (symbol, market) VALUES (%s, %s)
               ON CONFLICT (symbol, market) DO UPDATE SET updated_at=NOW()
               RETURNING id, symbol, market, created_at, updated_at""",
            (symbol, market),
        )
        row = dict(cur.fetchone())
    # A managed symbol joins the default calendar/export universe, which is
    # cached; drop those caches so the change is visible immediately instead of
    # after the TTL (Issue #58).
    invalidate_symbol_universe()
    return row


@router.put("/watchlist/{watchlist_id}", response_model=ManagedWatchlistItem)
def update_managed_watchlist(watchlist_id: int, payload: ManagedInput, _: dict = Depends(admin_user)):
    symbol, market = _normalize_managed(payload)
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
        row = dict(cur.fetchone())
    invalidate_symbol_universe()  # symbol identity changed → universe is stale (Issue #58)
    return row


@router.delete("/watchlist/{watchlist_id}")
def delete_managed_watchlist(watchlist_id: int, _: dict = Depends(admin_user)):
    with db.db_cursor() as cur:
        cur.execute("DELETE FROM managed_watchlist WHERE id=%s", (watchlist_id,))
        if cur.rowcount != 1:
            raise NotFoundError("managed_watchlist")
    invalidate_symbol_universe()  # removed symbol must leave the calendar too (Issue #58)
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
    """Provider metrics and cache diagnostics (Issue #15, sync freshness #53)."""
    from ..metrics import metrics
    from ..freshness import check_freshness
    result = metrics.snapshot()
    window_hours = max(1, int(getattr(config, "SYNC_RUNS_WINDOW_HOURS", 336)))
    with db.db_cursor() as cur:
        cur.execute("""SELECT status, COUNT(*) as cnt FROM sync_runs
                       WHERE started_at > NOW() - INTERVAL '24 hours'
                       GROUP BY status ORDER BY cnt DESC""")
        result["sync_runs_24h"] = [dict(r) for r in cur.fetchall()]
        # A weekly pipeline has an empty 24h window ~6 days out of 7, so the
        # configurable window is what an operator can actually read (Issue #53).
        cur.execute("""SELECT status, COUNT(*) as cnt FROM sync_runs
                       WHERE started_at > NOW() - make_interval(hours => %s)
                       GROUP BY status ORDER BY cnt DESC""", (window_hours,))
        result["sync_runs_window"] = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT stage, status, started_at, finished_at
                       FROM sync_runs
                       WHERE started_at > NOW() - make_interval(hours => %s)
                       ORDER BY started_at DESC LIMIT 20""", (window_hours,))
        result["recent_syncs"] = [dict(r) for r in cur.fetchall()]
    result["sync_runs_window_hours"] = window_hours
    result["freshness"] = check_freshness()
    # Issue #58: surface the state of the live default universe, so a fallback
    # universe that silently replaced the real one is visible instead of hiding
    # behind four healthy dependency probes.
    result["universe"] = universe_status()
    return result


@router.get("/health", response_model=HealthResponse)
def health_check():
    """Full dependency health status (Issue #11, #14, #20).

    Also reports sync-stage / derived-data freshness (Issue #53) so a stage that
    silently stopped running surfaces here instead of hiding behind the
    reachability probes. No auth required, so that entry stays aggregate-only.
    """
    from ..version import get_version
    checks = {}

    # PostgreSQL
    try:
        with db.db_cursor() as cur:
            cur.execute("SELECT 1")
        checks["postgresql"] = {"status": "healthy"}
    except Exception as exc:
        checks["postgresql"] = {"status": "not_ready", "error": type(exc).__name__}

    # Kurumi API — preferred company-name source (Issue #70). Probes the
    # endpoint the client actually reads (`/api/stock/{symbol}/overview`, see
    # app/company_name.py) and reports `dependency_not_configured` when the base
    # URL resolves back to this service: the previous probe asked FinCal's own
    # `/api/config` (a constant 200), so it could never fail.
    try:
        from ..company_name import probe_kurumi
        checks["kurumi"] = probe_kurumi()
    except Exception as exc:
        checks["kurumi"] = {"status": "degraded", "error_code": "kurumi_probe_failed",
                            "error": type(exc).__name__}

    # Futu OpenD
    try:
        from .. import config
        import socket
        with socket.create_connection((config.FUTU_HOST, config.FUTU_PORT), timeout=2):
            checks["futu"] = {"status": "healthy"}
    except Exception:
        checks["futu"] = {"status": "degraded"}

    # Longbridge CLI — a *host-side pipeline tool*, not a service dependency
    # (Issue #79). The image ships no such binary (Dockerfile copies app/ plus
    # the web dependencies), so probing it as a dependency answered a constant
    # `degraded` and pinned the aggregate status there. Report it as a
    # capability probe instead: `not_applicable` with a language-independent
    # code, visible in `checks` but not voting on the aggregate.
    checks["longbridge"] = probe_host_tool("longbridge")

    # Sync stage / derived data freshness (Issue #53). Aggregate only: this
    # endpoint has no application-level auth, so it must not leak per-stage
    # timestamps, SQL or credentials.
    try:
        from ..freshness import check_freshness, health_snapshot
        checks["sync_freshness"] = health_snapshot(check_freshness())
    except Exception as exc:
        checks["sync_freshness"] = {
            "status": "unknown",
            "error_code": "sync_freshness_unavailable",
            "error": type(exc).__name__,
        }

    # Issue #79: a probe that reports ``not_applicable`` stays visible in
    # ``checks`` but does not vote. A host-side tool the image does not ship is
    # not a service fault, and letting it vote made ``degraded`` the permanent
    # state — i.e. a real outage could no longer be told apart from the noise.
    statuses = [c["status"] for c in checks.values() if c["status"] != NOT_APPLICABLE]
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


@router.post("/sync-runs/{run_id}/retry", response_model=SyncRunRetryResult,
             status_code=202)
def retry_sync_run(run_id: int, user=Depends(admin_user)):
    """Record a retry request for a failed/interrupted/cancelled run (Issues #19, #77).

    This service cannot execute a sync stage — the scheduled pipeline
    (``scripts/cron_sync.sh``, the only entrypoint per the scheduling contract in
    ``README``) owns every stage. The endpoint therefore does not fake a run: it
    records the intent as a ``sync_runs`` row that is terminal from the start
    (``status='skipped'``, ``phase='retry_requested'``) and answers **202** with
    the pending request's id and the language-independent reason code
    ``retry_requires_cron``. The next scheduled round runs that stage and
    marks the request fulfilled (``app.sync_audit.start_run`` →
    ``consume_retry_requests``), leaving the usual terminal state and
    ``record_count`` on the fulfilling run.
    """
    from ..freshness import SYNC_STAGES
    from ..sync_audit import RETRY_EXECUTOR, RETRY_REASON_CODE, request_retry
    from ..audit import log_admin_action

    with db.db_cursor() as cur:
        cur.execute(
            "SELECT stage, source, symbol_count, status FROM sync_runs WHERE id=%s",
            (run_id,),
        )
        original = cur.fetchone()
        if not original:
            raise NotFoundError("sync_run")
        if original["status"] not in ("failed", "interrupted", "cancelled"):
            raise ConflictError("run_not_retryable",
                                f"cannot retry run in '{original['status']}' state")
        if original["stage"] not in SYNC_STAGES:
            # No scheduled round runs this stage, so a request could only sit
            # unfulfilled forever — say so instead of promising an execution.
            raise ConflictError(
                "retry_stage_not_scheduled",
                f"stage '{original['stage']}' is not part of the scheduled pipeline; "
                f"run it by hand",
            )

    request_id = request_retry(
        run_id, original["stage"], original["source"],
        symbol_count=original["symbol_count"],
        requested_by=str(user.get("email") or user.get("id")),
    )
    if request_id is None:
        raise ConflictError(
            "retry_already_requested",
            "a retry request for this run is already waiting for the next scheduled round",
        )

    log_admin_action("retry_sync_run", actor_id=str(user.get("id")),
                     actor_email=user.get("email"),
                     target=f"sync_run:{run_id}",
                     details={"request_id": request_id, "stage": original["stage"],
                              "reason_code": RETRY_REASON_CODE})

    return {"status": "retry_scheduled", "original_run_id": run_id,
            "request_id": request_id, "stage": original["stage"],
            "executed_by": RETRY_EXECUTOR, "reason_code": RETRY_REASON_CODE}
