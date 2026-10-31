"""iCal subscription endpoint — no auth required, uses token.
Cached with TTL to reduce DB pressure from calendar clients polling."""
from fastapi import APIRouter, Request, Response, Query
from hashlib import sha256
from email.utils import formatdate, parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, NamedTuple
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


def _not_modified_response(etag: str, last_modified: str) -> Response:
    """The feed's 304, carrying the negotiation the stored 200 was selected by.

    Issue #99: the body leaves through the app's gzip layer, so the
    representation depends on ``Accept-Encoding``, and that layer stamps
    ``Vary`` on the 200 it rewrites — but never on a 304, there is no body to
    compress. This feed is ``Cache-Control: public``, so a shared cache holding
    the gzip variant would be free to hand it to a client that never asked for
    it (RFC 7234 §4.1: Vary belongs to the stored response). The 200 does not
    need the header twice, hence only the conditional answer declares it here.
    """
    headers = _feed_headers(etag, last_modified)
    headers["Vary"] = "Accept-Encoding"
    return Response(status_code=304, headers=headers)
from .. import db, config
from ..metrics import metrics
from ..symbol import resolve_stored_rows
from ..ical import generate_ical
from ..log_redaction import token_fingerprint
from datetime import date, timedelta
from cachetools import TTLCache
from ..singleflight import Singleflight

router = APIRouter(tags=["ical"])

# Metric label for this cache (Issue #83). Constant — the cache key below
# carries a subscription token and must never reach a counter.
FEED_CACHE_NAME = "ical"

# Cache iCal feeds per token + options for 1 hour.
#
# Entries are *immutable* (``_CachedFeed``), never live ``Response`` objects.
# A cached ``Response`` hands its own ``raw_headers`` list to the ASGI server by
# reference, and any layer that re-codes the message mutates that list in place:
# the gzip layer added ``content-encoding: gzip`` to the stored 200 and dropped
# its ``content-length`` (Issue #99), so every later request served the
# uncompressed body labelled as gzip — calendar clients could not read it at
# all. Keeping the payload and its validators in a frozen record means each
# request builds a fresh response from data nothing else can touch.
class _CachedFeed(NamedTuple):
    content: str
    etag: str
    last_modified: str

    @property
    def nbytes(self) -> int:
        """Concrete size of the only thing this record keeps alive."""
        return len(self.content.encode("utf-8"))


# Capacity is measured in **bytes of feed text**, not in slots (Issue #100).
# Keyed by token + 5 options, one subscriber alone can mint 12 legitimate keys
# (lang 2 × scope 2 × predicted 2 × markets 3), so a slot count says nothing
# about memory: with the old ``maxsize=512`` and a 1.26 MiB ``scope=all`` feed
# the worst case was ~645 MiB of resident strings. ``getsizeof`` makes the bound
# mean what it says, measured against the current universe-scoped feed
# (~190 KiB): 16 MiB ≈ 86 feeds, while the shipped 6 subscribers × 12 keys
# ≈ 13.7 MiB still fit. ``maxsize`` is therefore a byte budget, and a single
# entry that would not fit is served but not cached (see ``_store_feed``).
FEED_CACHE_MAX_BYTES = 16 * 1024 * 1024

#: Slot ceiling for the same cache, so a flood of tiny feeds cannot turn the
#: byte budget into unbounded key bookkeeping (empty feeds are a few hundred
#: bytes each).
FEED_CACHE_MAX_ENTRIES = 512


def _feed_size(feed: _CachedFeed) -> int:
    return feed.nbytes


def _make_feed_cache(max_bytes: int = FEED_CACHE_MAX_BYTES) -> TTLCache[Any, _CachedFeed]:
    """A feed cache whose ``maxsize`` is a byte budget (see ``FEED_CACHE_MAX_BYTES``)."""
    return TTLCache[Any, _CachedFeed](maxsize=max_bytes, ttl=3600, getsizeof=_feed_size)


_ical_cache: TTLCache[Any, _CachedFeed] = _make_feed_cache()


def _store_feed(cache_key: tuple, feed: _CachedFeed, cache: TTLCache[Any, _CachedFeed] | None = None) -> None:
    """Cache a feed, honouring both the byte budget and the slot ceiling.

    ``TTLCache`` evicts the least recently used entry when ``maxsize`` (bytes)
    would be exceeded, but it raises ``ValueError`` for an entry larger than the
    whole budget. That must not become a 500 on the feed that is being served
    right now, so an oversized feed is logged and simply not cached — the
    response still goes out, the next request regenerates it.
    """
    target = _ical_cache if cache is None else cache
    budget = target.maxsize
    if feed.nbytes > budget:
        logger.warning(
            "ical feed not cached: %d B exceeds the %d B budget",
            feed.nbytes, budget,
        )
        return
    while len(target) >= FEED_CACHE_MAX_ENTRIES and cache_key not in target:
        target.popitem()
    target[cache_key] = feed
# Hit/miss counting for this cache is reported to app.metrics (Issue #83) — the
# feed is the most frequently polled entry point, so "cache works" has to be
# measurable. `_content_versions` below is bookkeeping for Last-Modified, not a
# data cache, and is deliberately not counted.
_ical_flight = Singleflight()

# Content versions: cache_key -> (etag, changed_at). Mirrors the feed cache
# (same maxsize/ttl) so it cannot grow unbounded, and is intentionally
# in-memory only — a restarted process regenerates every feed on its first
# request anyway. Recorded on the cold path, i.e. whenever the feed is really
# regenerated, so Last-Modified advances with the *content* and not only with
# earnings row timestamps (Issue #51).
_content_versions: TTLCache[Any, tuple[str, datetime]] = TTLCache[Any, tuple[str, datetime]](maxsize=512, ttl=3600)


def invalidate_ical_cache(token: str | None = None) -> None:
    """Invalidate one user's feed, or all feeds when token is omitted.

    Called with no argument whenever the *symbol universe* changes (Issue
    #100): ``scope=all`` now reads ``app.universe.popular_stocks()``, so an
    admin add/rename/remove has to drop every feed, otherwise a subscriber
    keeps the old symbol set for up to the 3600 s TTL.
    """
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
        # Both answers come out of the cached feed, so both are hits (Issue
        # #83): the 304 is the conditional-request form of the same body. The
        # counters stay aggregate on purpose — the key carries the subscription
        # token, which must never become a metric label.
        metrics.record_cache_hit(FEED_CACHE_NAME)
        if _not_modified(request, cached.etag, cached.last_modified):
            return _not_modified_response(cached.etag, cached.last_modified)
        # A fresh response per request: the stored record is immutable, so no
        # outgoing codec can rewrite the entry (see ``_CachedFeed``).
        return Response(
            content=cached.content,
            media_type="text/calendar; charset=utf-8",
            headers=_feed_headers(cached.etag, cached.last_modified),
        )

    with db.db_cursor() as cur:
        cur.execute("SELECT id, email, name FROM users WHERE ical_token = %s", (token,))
        user = cur.fetchone()
        if not user:
            # An unknown token is not a cache miss: there is no feed to cache,
            # and counting scanners here would drown the real hit rate.
            return Response(content="Not Found", status_code=404)
        cur.execute("SELECT symbol, market FROM watchlist WHERE user_id = %s", (user["id"],))
        watchlist = cur.fetchall()

    from ..earnings import fetch_earnings_from_db
    from ..universe import popular_stocks_by_market

    selected_markets = [markets] if markets != "all" else ["US", "HK"]

    def _generate():
        if scope == "all":
            # The subscription's "all" scope is the *system universe*, the same
            # accessor the calendar, /api/export and /api/universe read (Issue
            # #100). It used to be ``SELECT DISTINCT symbol, market FROM
            # earnings``: in production that fed 2,544 symbols / 3,177 events
            # into one subscriber's calendar while the app showed 112 symbols /
            # ~453 events, so 86% of the events could not be reconciled with
            # anything the UI knew about. Reading the live universe also drops
            # the 9,932-parameter ``IN`` list from this path.
            universe = popular_stocks_by_market()
            syms = [
                symbol
                for market in selected_markets
                for symbol in universe.get(market, [])
            ]
        else:
            # Stored rows are resolved through the non-throwing entry point
            # (Issue #108): one legacy spelling must never turn a subscriber's
            # whole feed into a 500.  Unservable rows are skipped and logged.
            resolved = resolve_stored_rows(
                [r for r in watchlist if str(r["market"]).strip().upper() in selected_markets],
                context="GET /ical scope=watchlist",
            )
            syms = [symbol for symbol, _ in resolved]
        # Same guard for both scopes: an empty symbol set must produce a valid
        # empty VCALENDAR, never an unfiltered full-database read (Issue #25).
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
        # Cold path: this request produces the feed itself (a hit would have
        # returned above, so reaching here — 304 included — is a miss).
        metrics.record_cache_miss(FEED_CACHE_NAME)
        ical_content, earnings = _ical_flight.do(str(cache_key), _generate)
    except TimeoutError:
        # Issue #32: don't silently serve a stale/empty feed (and don't let a
        # raw exception become a 500). Tell the calendar client to retry later.
        from fastapi.responses import JSONResponse
        # Issue #85: the token is the feed's only credential, so the warning
        # carries a fingerprint (greppable, non-reversible) instead of the
        # subscription itself. Same rule the cache key already follows above.
        logger.warning("ical feed generation timed out for token_fp=%s scope=%s lang=%s",
                       token_fingerprint(token), scope, lang)
        return JSONResponse(
            status_code=503,
            content={"error": "calendar generation timed out, retry shortly"},
            headers={"Retry-After": "30"},
        )
    # Weak validator (Issue #99): the same stored feed is served identity and
    # gzip depending on Accept-Encoding, so a strong ETag would be a false claim
    # of byte identity. ``_etag_matches`` already compares weakly (RFC 7232
    # §3.2), so conditional requests keep working across both codings.
    etag = 'W/"' + sha256(ical_content.encode("utf-8")).hexdigest() + '"'
    # ``earnings`` has no ``created_at`` column, so the old fallback was dead code
    # (Issue #73): the feed's validator is the row's write timestamp or nothing.
    timestamps = [e.get("updated_at") for e in earnings]
    timestamps = [value for value in timestamps if value is not None]
    latest = max(timestamps) if timestamps else None
    if isinstance(latest, datetime):
        latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    else:
        # Empty feeds still need a deterministic validator; request time would
        # defeat conditional requests and make clients refresh forever.
        latest_dt = datetime(2000, 1, 1, tzinfo=timezone.utc)
    last_modified = formatdate(_feed_last_modified(cache_key, etag, latest_dt).timestamp(), usegmt=True)
    if _not_modified(request, etag, last_modified):
        return _not_modified_response(etag, last_modified)

    _store_feed(cache_key, _CachedFeed(ical_content, etag, last_modified))
    return Response(
        content=ical_content,
        media_type="text/calendar; charset=utf-8",
        headers=_feed_headers(etag, last_modified),
    )
