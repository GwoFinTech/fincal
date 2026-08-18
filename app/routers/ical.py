"""iCal subscription endpoint — no auth required, uses token.
Cached with TTL to reduce DB pressure from calendar clients polling."""
from fastapi import APIRouter, Response, Query
from hashlib import sha256
from email.utils import formatdate
from datetime import datetime, timezone
from .. import db, config
from ..symbol import normalize
from ..ical import generate_ical
from datetime import date, timedelta
from cachetools import TTLCache

router = APIRouter(tags=["ical"])

# Cache iCal feeds per token + options for 1 hour
_ical_cache = TTLCache(maxsize=512, ttl=3600)


def invalidate_ical_cache(token: str | None = None) -> None:
    """Invalidate one user's feed, or all feeds when token is omitted."""
    if token is None:
        _ical_cache.clear()
    else:
        for key in list(_ical_cache):
            if isinstance(key, tuple) and key[0] == token:
                _ical_cache.pop(key, None)


@router.get("/ical/{token}")
def ical_feed(
    token: str,
    lang: str = Query("zh", pattern="^(zh|en)$"),
    scope: str = Query("watchlist", pattern="^(watchlist|all)$"),
    predicted: int = Query(1, ge=0, le=1),
    markets: str = Query("all", pattern="^(US|HK|all)$"),
):
    """Generate iCal feed for user based on their ical_token."""
    cache_key = (token, lang, scope, predicted, markets)
    cached = _ical_cache.get(cache_key)
    if cached is not None:
        return cached

    with db.db_cursor() as cur:
        cur.execute("SELECT id, email, name FROM users WHERE ical_token = %s", (token,))
        user = cur.fetchone()
        if not user:
            return Response(content="Not Found", status_code=404)
        cur.execute("SELECT symbol, market FROM watchlist WHERE user_id = %s", (user["id"],))
        watchlist = cur.fetchall()

    from ..earnings import fetch_earnings_from_db, POPULAR_STOCKS_US, POPULAR_STOCKS_HK
    selected_markets = [markets] if markets != "all" else ["US", "HK"]
    if scope == "all":
        # "All" means every symbol currently recorded in FinCal, not only
        # the homepage popular lists.
        with db.db_cursor() as cur:
            cur.execute(
                "SELECT DISTINCT symbol, market FROM earnings WHERE market = ANY(%s)",
                (selected_markets,),
            )
            symbols = [normalize(r["symbol"], r["market"]) for r in cur.fetchall()]
    else:
        symbols = [normalize(r["symbol"], r["market"]) for r in watchlist if r["market"] in selected_markets]
    earnings = fetch_earnings_from_db(
        symbols=symbols, markets=selected_markets,
        start=date.today() - timedelta(days=7), end=date.today() + timedelta(days=120),
    )
    if not predicted:
        earnings = [e for e in earnings if not e.get("is_predicted")]
    ical_content = generate_ical(earnings, user.get("email", ""), title_lang=lang)
    response = Response(
        content=ical_content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=fincal-earnings.ics",
            "Cache-Control": "public, max-age=3600",
        },
    )

    _ical_cache[cache_key] = response
    return response
