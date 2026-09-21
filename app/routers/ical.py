"""iCal subscription endpoint — no auth required, uses token.
Cached with TTL to reduce DB pressure from calendar clients polling."""
from fastapi import APIRouter, Request, Response, Query
from hashlib import sha256
from email.utils import formatdate, parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any
import logging

logger = logging.getLogger(__name__)


def _strip_weak(etag: str) -> str:
    """Drop an optional ``W/`` weak-validator prefix (RFC 7232 §2.3)."""
    value = etag.strip()
    return value[2:].strip() if value[:2].upper() == "W/" else value


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """``If-None-Match`` matching: ``*``, comma-separated list, weak compare."""
    header = (if_none_match or "").strip()
    if not header:
        return False
    if header == "*":
        return True
    target = _strip_weak(etag)
    return any(target == _strip_weak(candidate) for candidate in header.split(",") if candidate.strip())


def _not_modified(request: Request, etag: str, last_modified: str) -> bool:
    """Conditional-request decision (RFC 7232 §3.3).

    ``If-None-Match`` is authoritative and *excludes* ``If-Modified-Since``:
    when a request carries both, only the ETag decides, so a mismatching ETag
    always yields a fresh 200. The two validators come from different sources
    (feed content hash vs. earnings row timestamps) and were previously OR-ed
    together, which let the timestamp branch veto a changed body and keep
    clients on a stale calendar (Issue #51).
    """
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None:
        return _etag_matches(if_none_match, etag)
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

# Content versions: cache_key -> (etag, changed_at). Mirrors the feed cache
# (same maxsize/ttl) so it cannot grow unbounded, and is intentionally
# in-memory only — a restarted process regenerates every feed on its first
# request anyway. Recorded on the cold path, i.e. whenever the feed is really
# regenerated, so Last-Modified advances with the *content* and not only with
# earnings row timestamps (Issue #51).
_content_versions: TTLCache[Any, tuple[str, datetime]] = TTLCache[Any, tuple[str, datetime]](maxsize=512, ttl=3600)


def invalidate_ical_cache(token: str | None = None) -> None:
    """Invalidate one user's feed, or all feeds when token is omitted."""
    if token is None:
        _ical_cache.clear()
    else:
        for key in list(_ical_cache):
            if isinstance(key, tuple) and key[0] == token:
                _ical_cache.pop(key, None)


def _feed_last_modified(cache_key: tuple, etag: str, latest_dt: datetime) -> datetime:
    """Last-Modified for a freshly generated feed (Issue #51).

    ``max(earnings.updated_at)`` alone stalls whenever the feed content changes
    without any earnings row being rewritten — adding/removing a watchlist
    symbol, or an event entering/leaving the report window — so a client
    revalidating with ``If-Modified-Since`` was told "not modified" while the
    body had in fact changed. Return the later of the row timestamps and the
    moment this key's content (ETag) last changed.

    The content-change moment is not a request timestamp: it is recorded once
    per distinct feed content and reused while the content stays identical
    (criterion: an unchanged feed keeps a stable validator), so conditional
    requests keep working across polls and across cache expiries.
    """
    known = _content_versions.get(cache_key)
    if known is None or known[0] != etag:
        changed_at = datetime.now(timezone.utc)
        _content_versions[cache_key] = (etag, changed_at)
    else:
        changed_at = known[1]
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    return max(latest_dt, changed_at)


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

    from ..earnings import fetch_earnings_from_db

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
            if not syms:
                return generate_ical([], user.get("email", ""), title_lang=lang), []
        # The window is the shared calendar horizon (config.CALENDAR_FORWARD_DAYS,
        # default = predictor MAX_FUTURE_DAYS), not a local literal: the feed is a
        # subscription, so an event outside the window has no other path to the
        # user — it used to stop at a hardcoded 120 days and silently dropped
        # every prediction the app itself could show (Issue #59).
        earn = fetch_earnings_from_db(
            symbols=syms, markets=selected_markets,
            start=date.today() - timedelta(days=7),
            end=date.today() + timedelta(days=config.CALENDAR_FORWARD_DAYS),
        )
        if not predicted:
            earn = [e for e in earn if not e.get("is_predicted")]
        return generate_ical(earn, user.get("email", ""), title_lang=lang), earn

    try:
        ical_content, earnings = _ical_flight.do(str(cache_key), _generate)
    except TimeoutError:
        # Issue #32: don't silently serve a stale/empty feed (and don't let a
        # raw exception become a 500). Tell the calendar client to retry later.
        from fastapi.responses import JSONResponse
        logger.warning("ical feed generation timed out for token=%s scope=%s lang=%s",
                       token, scope, lang)
        return JSONResponse(
            status_code=503,
            content={"error": "calendar generation timed out, retry shortly"},
            headers={"Retry-After": "30"},
        )
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
    last_modified = formatdate(_feed_last_modified(cache_key, etag, latest_dt).timestamp(), usegmt=True)
    headers = _feed_headers(etag, last_modified)
    if _not_modified(request, etag, last_modified):
        return Response(status_code=304, headers=headers)
    response = Response(content=ical_content, media_type="text/calendar; charset=utf-8", headers=headers)

    _ical_cache[cache_key] = response
    return response
