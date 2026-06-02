"""API routes for watchlist management and earnings data."""
from fastapi import APIRouter, Depends
from datetime import date, timedelta
from ..auth import get_current_user, ensure_user
from .. import db, config

router = APIRouter(prefix="/api", tags=["api"])


def _norm_hk(symbol: str, market: str) -> str:
    """Normalize HK stock codes to 4-digit zero-padded format."""
    if market.upper() != "HK":
        return symbol.upper()
    code = symbol.upper().replace(".HK", "").strip()
    return code.zfill(4) + ".HK"


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
        rows = [dict(row) for row in cur.fetchall()]
    # Normalize HK codes for frontend display
    for r in rows:
        r["symbol"] = _norm_hk(r["symbol"], r["market"])
    return rows


@router.post("/watchlist")
def api_add_watchlist(symbol: str, market: str = "US", user=Depends(get_current_user)):
    """Add a stock to watchlist."""
    fincal_user = ensure_user(user["id"], user["email"], user["name"])
    market = market.upper()
    if market not in ("US", "HK"):
        return {"error": "market must be US or HK"}
    normalized = _norm_hk(symbol, market)
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
    normalized = _norm_hk(symbol, market.upper())
    with db.db_cursor() as cur:
        cur.execute(
            "DELETE FROM watchlist WHERE user_id = %s AND symbol = %s AND market = %s",
            (fincal_user["id"], normalized, market.upper()),
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
        symbols = [_norm_hk(r["symbol"], r["market"]) for r in wl]
        markets = list(set(r["market"] for r in wl))
        return fetch_earnings_from_db(symbols=symbols, markets=markets, start=start, end=end)
    else:
        # Popular stocks + user watchlist
        all_symbols = list(set(POPULAR_STOCKS_US + POPULAR_STOCKS_HK))
        all_markets = ["US", "HK"]
        with db.db_cursor() as cur:
            cur.execute(
                "SELECT symbol, market FROM watchlist WHERE user_id = %s",
                (fincal_user["id"],),
            )
            for r in cur.fetchall():
                norm = _norm_hk(r["symbol"], r["market"])
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
    """Search for stocks to add to watchlist. Handles both padded and non-padded HK codes."""
    with db.db_cursor() as cur:
        # Try exact match first, then ILIKE
        cur.execute(
            """SELECT DISTINCT symbol, market, company_name FROM earnings
            WHERE (symbol ILIKE %s OR company_name ILIKE %s
                   OR symbol ILIKE %s OR symbol ILIKE %s)
            ORDER BY market, symbol LIMIT 20""",
            (f"%{q}%", f"%{q}%", f"%{q.replace('.HK', '')}%", f"%{q.zfill(4)}%"),
        )
        rows = [dict(row) for row in cur.fetchall()]
    # Normalize HK symbols in results
    for r in rows:
        r["symbol"] = _norm_hk(r["symbol"], r["market"])
    return rows
