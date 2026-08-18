"""API routes for watchlist management and earnings data.

Issue #7: layer cache for earnings and popular stocks.
"""
import json
from fastapi import APIRouter, Depends
from datetime import date, timedelta
from ..auth import get_current_user, ensure_user
from .. import db, config
from ..symbol import normalize, sort_key, from_lb_counter_id
from ..layer_cache import LayerCache

router = APIRouter(prefix="/api", tags=["api"])

# Per-endpoint caches
_earnings_cache = LayerCache(default_ttl=120.0, stale_ttl=1800.0)
_popular_cache = LayerCache(default_ttl=3600.0, stale_ttl=86400.0)


@router.get("/config")
def api_config():
    """Public config (no auth required)."""
    return {
        "auth_login_url": config.AUTH_LOGIN_URL,
    }


@router.get("/me")
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


@router.get("/watchlist")
def api_watchlist(user=Depends(get_current_user)):
    """Get user's watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    with db.db_cursor() as cur:
        cur.execute(
            "SELECT symbol, market FROM watchlist WHERE user_id = %s ORDER BY market, symbol",
            (fincal_user["id"],),
        )
        return [dict(row) for row in cur.fetchall()]


@router.post("/watchlist")
def api_add_watchlist(symbol: str, market: str = "US", user=Depends(get_current_user)):
    """Add a stock to watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    market = market.strip().upper()
    if market not in ("US", "HK"):
        return {"error": "market must be US or HK"}
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


@router.delete("/watchlist")
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


@router.get("/earnings")
def api_earnings(
    start: date | None = None,
    end: date | None = None,
    watchlistOnly: bool = False,
    user=Depends(get_current_user),
):
    """Get earnings calendar data with layer cache (Issue #7)."""
    from ..earnings import fetch_earnings_from_db, POPULAR_STOCKS_US, POPULAR_STOCKS_HK

    fincal_user = ensure_user(user["id"], user["email"], user["name"])

    if start is None:
        start = date.today() - timedelta(days=7)
    if end is None:
        end = date.today() + timedelta(days=90)

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
            all_symbols = list(set(POPULAR_STOCKS_US + POPULAR_STOCKS_HK))
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


@router.get("/earnings/{earning_id}/decision")
def api_earning_decision(earning_id: int, user=Depends(get_current_user)):
    """Decision-support facts with source-specific unavailable states, never synthetic values."""
    from ..phase3 import build_decision_metrics, revision_trend

    ensure_user(user["id"], user["email"], user["name"])
    with db.db_cursor() as cur:
        cur.execute("SELECT * FROM earnings WHERE id=%s", (earning_id,))
        earning = cur.fetchone()
        if not earning:
            return {"status": "not_found"}
        earning = dict(earning)
        cur.execute("SELECT id,fiscal_year,fiscal_quarter,report_date,eps_actual,revenue_actual,eps_estimate FROM earnings WHERE symbol=%s AND market=%s ORDER BY report_date", (earning["symbol"], earning["market"]))
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
        "provenance": {"revision_trend": "earnings_estimate_snapshots / Longbridge finance-calendar", "institution_rating": "Longbridge institution-rating", "actual_growth": "earnings actuals (Longbridge/Futu as recorded)", "price_reaction": "unavailable: no reliable provider configured"},
    }


@router.get("/popular")
def api_popular():
    """Get the list of popular stocks shown by default. Cached (Issue #7)."""
    from ..earnings import POPULAR_STOCKS_US, POPULAR_STOCKS_HK

    def _fetch():
        return {"US": POPULAR_STOCKS_US, "HK": POPULAR_STOCKS_HK}

    data, _ = _popular_cache.get_or_refresh("popular", _fetch, ttl=3600.0)
    return data


@router.get("/search")
def api_search_stocks(q: str):
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
def api_export(start: str, end: str, format: str = "csv"):
    """Export earnings data as CSV or JSON."""
    from ..earnings import fetch_earnings_from_db, POPULAR_STOCKS_US, POPULAR_STOCKS_HK
    from fastapi.responses import StreamingResponse
    import csv, io, json as json_mod

    symbols = POPULAR_STOCKS_US + POPULAR_STOCKS_HK
    markets = ["US", "HK"]
    data = fetch_earnings_from_db(symbols=symbols, markets=markets, start=start, end=end)

    if format == "json":
        return data

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["symbol", "market", "company_name", "report_date", "fiscal_year",
                     "fiscal_quarter", "before_after", "eps_estimate", "eps_actual",
                     "revenue_estimate", "revenue_actual", "is_predicted"])
    for r in data:
        writer.writerow([
            r.get("symbol"), r.get("market"), r.get("company_name", ""),
            r.get("report_date"), r.get("fiscal_year"), r.get("fiscal_quarter"),
            r.get("before_after", ""), r.get("eps_estimate", ""),
            r.get("eps_actual", ""), r.get("revenue_estimate", ""),
            r.get("revenue_actual", ""), r.get("is_predicted", False),
        ])
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=fincal-earnings-{start}-{end}.csv"},
    )
