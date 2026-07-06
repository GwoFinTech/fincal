"""API routes for watchlist management and earnings data."""
import json
from fastapi import APIRouter, Depends
from datetime import date, timedelta
from ..auth import get_current_user, ensure_user
from .. import db, config
from ..symbol import normalize, sort_key, from_lb_counter_id

router = APIRouter(prefix="/api", tags=["api"])


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
        return {"status": "removed"}


@router.get("/earnings")
def api_earnings(
    start: date | None = None,
    end: date | None = None,
    watchlistOnly: bool = False,
    user=Depends(get_current_user),
):
    """Get earnings calendar data."""
    from ..earnings import fetch_earnings_from_db, POPULAR_STOCKS_US, POPULAR_STOCKS_HK

    fincal_user = ensure_user(user["id"], user["email"], user["name"])

    if start is None:
        start = date.today() - timedelta(days=7)
    if end is None:
        end = date.today() + timedelta(days=90)

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


@router.get("/popular")
def api_popular():
    """Get the list of popular stocks shown by default."""
    from ..earnings import POPULAR_STOCKS_US, POPULAR_STOCKS_HK
    return {
        "US": POPULAR_STOCKS_US,
        "HK": POPULAR_STOCKS_HK,
    }


@router.get("/search")
def api_search_stocks(q: str):
    """Search for stocks to add to watchlist. Handles various HK code formats."""
    with db.db_cursor() as cur:
        cur.execute(
            """SELECT DISTINCT symbol, market, company_name FROM earnings
            WHERE (symbol ILIKE %s OR company_name ILIKE %s)
            ORDER BY market, symbol LIMIT 20""",
            (f"%{q}%", f"%{q}%"),
        )
        results = [dict(row) for row in cur.fetchall()]

    # Fallback: if no results in DB, try Longbridge search
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

    # CSV output
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
