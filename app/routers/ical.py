"""iCal subscription endpoint — no auth required, uses token.
Cached with TTL to reduce DB pressure from calendar clients polling."""
from fastapi import APIRouter, Response
from .. import db, config
from ..symbol import normalize
from ..ical import generate_ical
from datetime import date, timedelta
from cachetools import TTLCache

router = APIRouter(tags=["ical"])

# Cache iCal feeds per token for 1 hour
_ical_cache = TTLCache(maxsize=256, ttl=3600)


@router.get("/ical/{token}")
def ical_feed(token: str):
    """Generate iCal feed for user based on their ical_token."""
    # Check cache first
    cached = _ical_cache.get(token)
    if cached is not None:
        return cached

    with db.db_cursor() as cur:
        cur.execute("SELECT id, email, name FROM users WHERE ical_token = %s", (token,))
        user = cur.fetchone()
        if not user:
            return Response(content="Not Found", status_code=404)

        # Get user watchlist symbols
        cur.execute("SELECT symbol, market FROM watchlist WHERE user_id = %s", (user["id"],))
        watchlist = cur.fetchall()

    from ..earnings import fetch_earnings_from_db, POPULAR_STOCKS_US, POPULAR_STOCKS_HK

    if not watchlist:
        symbols = POPULAR_STOCKS_US + POPULAR_STOCKS_HK
        markets = ["US", "HK"]
    else:
        symbols = [normalize(r["symbol"], r["market"]) for r in watchlist]
        markets = list(set(r["market"] for r in watchlist))

    earnings = fetch_earnings_from_db(
        symbols=symbols,
        markets=markets,
        start=date.today() - timedelta(days=7),
        end=date.today() + timedelta(days=120),
    )

    ical_content = generate_ical(earnings, user.get("email", ""))
    response = Response(
        content=ical_content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=fincal-earnings.ics",
            "Cache-Control": "public, max-age=3600",
        },
    )

    _ical_cache[token] = response
    return response
