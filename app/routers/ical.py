"""iCal subscription endpoint — no auth required, uses token.
Cached with TTL to reduce DB pressure from calendar clients polling."""
from fastapi import APIRouter, Request, Response, Query
from hashlib import sha256
from email.utils import formatdate, parsedate_to_datetime
from datetime import datetime, timezone


def _not_modified(request: Request, etag: str, last_modified: str) -> bool:
    if request.headers.get("if-none-match") == etag:
        return True
    value = request.headers.get("if-modified-since")
    if value:
        try:
            return parsedate_to_datetime(value).timestamp() >= parsedate_to_datetime(last_modified).timestamp()
        except (TypeError, ValueError, OverflowError):
            return False
    return False


def _feed_headers(etag: str, last_modified: str) -> dict[str, str]:
    return {
        "Content-Disposition": "attachment; filename=fincal-earnings.ics",
        "Cache-Control": "public, max-age=3600, must-revalidate",
        "ETag": etag,
        "Last-Modified": last_modified,
    }
from .. import db, config
from ..symbol import normalize
from ..ical import generate_ical
from datetime import date, timedelta
from cachetools import TTLCache
from ..singleflight import Singleflight

router = APIRouter(tags=["ical"])

# Cache iCal feeds per token + options for 1 hour
_ical_cache = TTLCache(maxsize=512, ttl=3600)
_ical_flight = Singleflight()


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
    request: Request,
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
        cached_response = cached  # TTLCache stores FastAPI Response objects.
        etag = cached_response.headers.get("etag", "")
        last_modified = cached_response.headers.get("last-modified", formatdate(946684800, usegmt=True))
        if _not_modified(request, etag, last_modified):
            return Response(status_code=304, headers=_feed_headers(etag, last_modified))
        return cached_response

    with db.db_cursor() as cur:
        cur.execute("SELECT id, email, name FROM users WHERE ical_token = %s", (token,))
        user = cur.fetchone()
        if not user:
            return Response(content="Not Found", status_code=404)
        cur.execute("SELECT symbol, market FROM watchlist WHERE user_id = %s", (user["id"],))
        watchlist = cur.fetchall()

    from ..earnings import fetch_earnings_from_db, POPULAR_STOCKS_US, POPULAR_STOCKS_HK
    selected_markets = [markets] if markets != "all" else ["US", "HK"]

    def _generate():
        if scope == "all":
            with db.db_cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT symbol, market FROM earnings WHERE market = ANY(%s)",
                    (selected_markets,),
                )
                syms = [normalize(r["symbol"], r["market"]) for r in cur.fetchall()]
        else:
            syms = [normalize(r["symbol"], r["market"]) for r in watchlist if r["market"] in selected_markets]
        earn = fetch_earnings_from_db(
            symbols=syms, markets=selected_markets,
            start=date.today() - timedelta(days=7), end=date.today() + timedelta(days=120),
        )
        if not predicted:
            earn = [e for e in earn if not e.get("is_predicted")]
        return generate_ical(earn, user.get("email", ""), title_lang=lang), earn

    ical_content, earnings = _ical_flight.do(str(cache_key), _generate)
    etag = '"' + sha256(ical_content.encode("utf-8")).hexdigest() + '"'
    timestamps = [e.get("updated_at") or e.get("created_at") for e in earnings]
    timestamps = [value for value in timestamps if value is not None]
    latest = max(timestamps) if timestamps else None
    if isinstance(latest, datetime):
        latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    else:
        # Empty feeds still need a deterministic validator; request time would
        # defeat conditional requests and make clients refresh forever.
        latest_dt = datetime(2000, 1, 1, tzinfo=timezone.utc)
    last_modified = formatdate(latest_dt.timestamp(), usegmt=True)
    headers = _feed_headers(etag, last_modified)
    if _not_modified(request, etag, last_modified):
        return Response(status_code=304, headers=headers)
    response = Response(content=ical_content, media_type="text/calendar; charset=utf-8", headers=headers)

    _ical_cache[cache_key] = response
    return response
