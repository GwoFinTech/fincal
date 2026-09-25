"""API routes for watchlist management and earnings data.

Issue #7: layer cache for earnings and popular stocks.
OpenAPI: all endpoints have response_model for schema generation.
"""
import json
from fastapi import APIRouter, Depends, HTTPException
from datetime import date, timedelta
from ..auth import get_current_user, ensure_user
from .. import db, config
from ..symbol import normalize, sort_key, from_lb_counter_id
from ..layer_cache import LayerCache
from ..errors import AppError, NotFoundError, ForbiddenError
from ..schemas import (
    AppConfig, UserResponse, WatchlistItem, WatchlistAddResult,
    WatchlistRemoveResult, EarningItem, DecisionResponse, PopularStocks,
    SearchItem,
)

router = APIRouter(prefix="/api", tags=["api"])

# Per-endpoint caches. The default symbol universe lives in app.universe (Issue
# #58) with its own TTL, so /api/popular no longer wraps it in a second,
# hour-long cache that could outlive an upstream change.
_earnings_cache = LayerCache(default_ttl=120.0, stale_ttl=1800.0)


def invalidate_universe_caches() -> None:
    """Drop the response caches that embed the symbol universe (Issue #58).

    Called by :func:`app.universe.invalidate_symbol_universe` after an admin
    watchlist mutation: the universe change alone would still be masked by this
    response cache until its TTL expired.
    """
    _earnings_cache.invalidate()


@router.get("/config", response_model=AppConfig)
def api_config():
    """Public config (no auth required)."""
    return {
        "auth_login_url": config.AUTH_LOGIN_URL,
    }


@router.get("/me", response_model=UserResponse)
def api_me(user=Depends(get_current_user)):
    """Get current user info + ical token."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    return {
        "id": fincal_user["id"],
        "portal_user_id": fincal_user["portal_user_id"],
        "email": fincal_user["email"],
        "name": fincal_user["name"],
        "role": user["role"],
        "is_admin": user["role"].strip().lower() == "admin",
        "ical_token": fincal_user["ical_token"],
        "ical_url": f"{config.ICAL_BASE_URL}/ical/{fincal_user['ical_token']}",
    }


@router.get("/watchlist", response_model=list[WatchlistItem])
def api_watchlist(user=Depends(get_current_user)):
    """Get user's watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    with db.db_cursor() as cur:
        cur.execute(
            "SELECT symbol, market FROM watchlist WHERE user_id = %s ORDER BY market, symbol",
            (fincal_user["id"],),
        )
        return [dict(row) for row in cur.fetchall()]


@router.post("/watchlist", response_model=WatchlistAddResult)
def api_add_watchlist(symbol: str, market: str = "US", user=Depends(get_current_user)):
    """Add a stock to watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    market = market.strip().upper()
    if market not in ("US", "HK"):
        raise AppError("invalid_market", "market must be US or HK", 400)
    normalized = normalize(symbol, market)
    with db.db_cursor() as cur:
        cur.execute(
            """INSERT INTO watchlist (user_id, symbol, market) VALUES (%s, %s, %s)
            ON CONFLICT (user_id, symbol, market) DO NOTHING RETURNING *""",
            (fincal_user["id"], normalized, market),
        )
        row = cur.fetchone()
    from .ical import invalidate_ical_cache
    invalidate_ical_cache(fincal_user.get("ical_token"))
    return dict(row) if row else {"status": "already_exists"}


@router.delete("/watchlist", response_model=WatchlistRemoveResult)
def api_remove_watchlist(symbol: str, market: str = "US", user=Depends(get_current_user)):
    """Remove a stock from watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    market = market.strip().upper()
    normalized = normalize(symbol, market)
    with db.db_cursor() as cur:
        cur.execute(
            "DELETE FROM watchlist WHERE user_id = %s AND symbol = %s AND market = %s",
            (fincal_user["id"], normalized, market),
        )
    from .ical import invalidate_ical_cache
    invalidate_ical_cache(fincal_user.get("ical_token"))
    return {"status": "removed"}


@router.get("/earnings", response_model=list[EarningItem])
def api_earnings(
    start: date | None = None,
    end: date | None = None,
    watchlistOnly: bool = False,
    user=Depends(get_current_user),
):
    """Get earnings calendar data with layer cache (Issue #7)."""
    from ..earnings import fetch_earnings_from_db
    from ..universe import popular_stocks

    fincal_user = ensure_user(user["id"], user["email"], user["name"])

    if start is None:
        start = date.today() - timedelta(days=7)
    if end is None:
        # Keep the default window on the shared calendar horizon instead of a
        # local literal: a caller that omits `end` used to get 90 days while the
        # watchlist view and the iCal feed reached 420 (Issue #59).
        end = date.today() + timedelta(days=config.CALENDAR_FORWARD_DAYS)

    cache_key = f"earnings:{start}:{end}:{watchlistOnly}:{fincal_user['id']}"

    if watchlistOnly:
        with db.db_cursor() as cur:
            cur.execute(
                "SELECT symbol, market FROM watchlist WHERE user_id = %s",
                (fincal_user["id"],),
            )
            wl = cur.fetchall()
        if not wl:
            return []
        symbols = [normalize(r["symbol"], r["market"]) for r in wl]
        markets = list(set(r["market"] for r in wl))
        return fetch_earnings_from_db(symbols=symbols, markets=markets, start=start, end=end)
    else:
        def _fetch():
            universe_us, universe_hk = popular_stocks()
            all_symbols = list(set(universe_us + universe_hk))
            all_markets = ["US", "HK"]
            with db.db_cursor() as cur:
                cur.execute(
                    "SELECT symbol, market FROM watchlist WHERE user_id = %s",
                    (fincal_user["id"],),
                )
                for r in cur.fetchall():
                    norm = normalize(r["symbol"], r["market"])
                    if norm not in all_symbols:
                        all_symbols.append(norm)
                        if r["market"] not in all_markets:
                            all_markets.append(r["market"])
            return fetch_earnings_from_db(symbols=all_symbols, markets=all_markets, start=start, end=end)

        data, entry = _earnings_cache.get_or_refresh(cache_key, _fetch, ttl=120.0)
        return data


@router.get("/earnings/{earning_id}/decision", response_model=DecisionResponse)
def api_earning_decision(earning_id: int, user=Depends(get_current_user)):
    """Decision-support facts with source-specific unavailable states, never synthetic values."""
    from ..phase3 import build_decision_metrics, revision_trend

    ensure_user(user["id"], user["email"], user["name"])
    with db.db_cursor() as cur:
        cur.execute("SELECT * FROM earnings WHERE id=%s", (earning_id,))
        earning = cur.fetchone()
        if not earning:
            raise NotFoundError("earning")
        earning = dict(earning)
        cur.execute("SELECT id,fiscal_year,fiscal_quarter,report_date,eps_actual,revenue_actual,eps_estimate,actual_currency,actual_basis,actual_source FROM earnings WHERE symbol=%s AND market=%s ORDER BY report_date", (earning["symbol"], earning["market"]))
        history = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT captured_at,eps_estimate,revenue_estimate,source FROM earnings_estimate_snapshots WHERE earning_id=%s ORDER BY captured_at", (earning_id,))
        snapshots = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT currency_symbol,target_price,strong_buy,buy,hold,underperform,sell,recommendation,provider_updated_at,fetched_at,source FROM earnings_institution_ratings WHERE symbol=%s AND market=%s AND source='longbridge'", (earning["symbol"], earning["market"]))
        rating = cur.fetchone()
        cur.execute("SELECT status,reason,source,checked_at FROM earnings_guidance_status WHERE symbol=%s AND market=%s AND source='longbridge'", (earning["symbol"], earning["market"]))
        guidance = cur.fetchone()
    return {
        "status": "available", "revision_trend": revision_trend(snapshots),
        "institution_rating": dict(rating) if rating else {"status": "unavailable", "source": "longbridge"},
        "guidance": dict(guidance) if guidance else {"status": "unavailable", "reason": "longbridge_guidance_endpoint_unavailable", "source": "longbridge"},
        **build_decision_metrics(history, earning_id),
        "provenance": {"revision_trend": "earnings_estimate_snapshots / Longbridge finance-calendar", "institution_rating": "Longbridge institution-rating", "actual_growth": "earnings actuals (Longbridge/Futu as recorded); a period pair is only compared when both actuals share a declared currency and basis", "price_reaction": "unavailable: no reliable provider configured"},
    }


@router.get("/popular", response_model=PopularStocks)
def api_popular(user=Depends(get_current_user)):
    """Get the list of popular stocks shown by default (Issue #7).

    Served straight from the live universe accessor (Issue #58): its TTL is the
    one that governs how fresh this answer is, so an upstream add/remove cannot
    hide behind a second, longer-lived cache.
    """
    from ..universe import popular_stocks_by_market

    return popular_stocks_by_market()


@router.get("/search", response_model=list[SearchItem])
def api_search_stocks(q: str, user=Depends(get_current_user)):
    """Search for stocks to add to watchlist."""
    with db.db_cursor() as cur:
        cur.execute(
            """SELECT DISTINCT symbol, market, company_name FROM earnings
            WHERE (symbol ILIKE %s OR company_name ILIKE %s)
            ORDER BY market, symbol LIMIT 20""",
            (f"%{q}%", f"%{q}%"),
        )
        results = [dict(row) for row in cur.fetchall()]

    if not results:
        try:
            import subprocess
            cmd = ["longbridge", "stock-search", "--q", q, "--count", "10", "--format", "json"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                for item in data.get("list", []):
                    cid = item.get("counter_id", "")
                    name = item.get("name", "")
                    symbol, market = from_lb_counter_id(cid)
                    if symbol and market:
                        results.append({"symbol": symbol, "market": market, "company_name": name})
        except Exception:
            pass

    return results


@router.get("/export")
def api_export(start: date, end: date, format: str = "csv", user=Depends(get_current_user)):
    """Export earnings data as CSV or JSON."""
    from ..earnings import fetch_earnings_from_db
    from ..universe import popular_stocks
    from fastapi.responses import StreamingResponse
    import csv, io, json as json_mod

    ensure_user(user["id"], user["email"], user["name"])
    universe_us, universe_hk = popular_stocks()
    symbols = universe_us + universe_hk
    markets = ["US", "HK"]
    data = fetch_earnings_from_db(symbols=symbols, markets=markets, start=start, end=end)

    if format == "json":
        return data

    output = io.StringIO()
    writer = csv.writer(output)
    # Derive the column set from the actual rows so the CSV field set stays in
    # lockstep with the JSON export (which returns `data` verbatim). This keeps
    # provenance/status (date_source, date_status, estimate_source, actual_source)
    # and consensus_* fields from drifting out of sync with the EarningItem
    # contract (Issue #47). Columns follow first-appearance order across rows;
    # empty data falls back to a canonical list so the header is still emitted.
    fields: list[str] = []
    seen: set[str] = set()
    for r in data:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    if not fields:
        fields = [
            "symbol", "market", "company_name", "report_date", "report_type",
            "fiscal_year", "fiscal_quarter", "before_after", "eps_estimate",
            "eps_actual", "revenue_estimate", "revenue_actual", "is_predicted",
            "date_source", "date_status", "estimate_source", "estimate_as_of",
            "estimate_currency", "estimate_basis", "actual_source", "actual_as_of",
            "actual_currency", "actual_basis", "comparison_unavailable_reason",
            "updated_at", "consensus_currency", "consensus_eps_gaap",
            "consensus_eps_adjusted", "consensus_revenue", "consensus_ebit",
            "consensus_net_income", "consensus_normalized_net_income",
            "consensus_fetched_at",
        ]
    writer.writerow(fields)
    for r in data:
        writer.writerow([r.get(f) for f in fields])
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=fincal-earnings-{start}-{end}.csv"},
    )
