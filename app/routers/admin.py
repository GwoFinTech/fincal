"""Administrator APIs backed by kazusa-home-portal forwardAuth roles."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from .. import config, db
from ..admin_watchlist import normalize_managed_symbol
from ..auth import get_current_user, require_admin
from ..watchlist import get_source

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ManagedWatchlistInput(BaseModel):
    symbol: str
    market: str = "US"


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


@router.get("/overview")
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


@router.get("/watchlist")
def list_managed_watchlist(_: dict = Depends(admin_user)):
    with db.db_cursor() as cur:
        cur.execute("SELECT id, symbol, market, created_at, updated_at FROM managed_watchlist ORDER BY market, symbol")
        return [dict(row) for row in cur.fetchall()]


@router.post("/watchlist", status_code=201)
def add_managed_watchlist(payload: ManagedWatchlistInput, _: dict = Depends(admin_user)):
    try:
        symbol, market = normalize_managed_symbol(payload.symbol, payload.market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO managed_watchlist (symbol, market) VALUES (%s, %s)
               ON CONFLICT (symbol, market) DO UPDATE SET updated_at=NOW()
               RETURNING id, symbol, market, created_at, updated_at""",
            (symbol, market),
        )
        return dict(cur.fetchone())


@router.put("/watchlist/{watchlist_id}")
def update_managed_watchlist(watchlist_id: int, payload: ManagedWatchlistInput, _: dict = Depends(admin_user)):
    try:
        symbol, market = normalize_managed_symbol(payload.symbol, payload.market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with db.db_cursor() as cur:
        cur.execute("SELECT 1 FROM managed_watchlist WHERE id=%s", (watchlist_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="managed_watchlist_not_found")
        cur.execute("SELECT id FROM managed_watchlist WHERE symbol=%s AND market=%s AND id != %s", (symbol, market, watchlist_id))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="managed_watchlist_duplicate")
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
            raise HTTPException(status_code=404, detail="managed_watchlist_not_found")
    return {"status": "removed"}


@router.get("/sync-runs")
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


@router.post("/sync-runs/{run_id}/cancel")
def cancel_sync_run(run_id: int, _: dict = Depends(admin_user)):
    from ..sync_audit import request_cancel
    cancelled = request_cancel(run_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="run_not_cancellable")
    return {"status": "cancelled", "run_id": run_id}


@router.post("/sync-runs/recover")
def recover_stale_runs(_: dict = Depends(admin_user)):
    from ..sync_audit import recover_stale_runs
    count = recover_stale_runs()
    return {"recovered": count}
