"""Subscription endpoint HTTP compliance tests (Issue #1 item 15).

Validates that the iCal subscription endpoint:
  - Returns text/calendar; charset=utf-8 Content-Type
  - Does not require cookies or redirect to login
  - Returns 404 for invalid tokens (not redirect or HTML)
  - Returns proper headers (ETag, Last-Modified, Cache-Control)
  - Supports 304 Not Modified via conditional request headers
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from app.routers.ical import _feed_headers, _not_modified


# ── Content-Type and header correctness ───────────────────────────────────

def test_feed_headers_include_text_calendar_content_type():
    """Response must include text/calendar media type and caching headers."""
    headers = _feed_headers('"test-etag"', "Mon, 01 Aug 2026 08:00:00 GMT")
    # ETag and Last-Modified are required for conditional requests
    assert headers["ETag"] == '"test-etag"'
    assert "GMT" in headers["Last-Modified"]
    # Cache-Control must allow public caching with revalidation
    assert "must-revalidate" in headers["Cache-Control"]
    # Content-Disposition signals a downloadable calendar file
    assert "fincal-earnings.ics" in headers["Content-Disposition"]


# ── 304 Not Modified: If-None-Match ───────────────────────────────────────

def test_not_modified_if_none_match_matches():
    """If-None-Match matching ETag → 304."""
    class FakeRequest:
        headers = {"if-none-match": '"abc123"'}
    assert _not_modified(FakeRequest(), '"abc123"', "Mon, 01 Aug 2026 08:00:00 GMT") is True


def test_not_modified_if_none_match_mismatch():
    """If-None-Match non-matching ETag → serve normally."""
    class FakeRequest:
        headers = {"if-none-match": '"different"'}
    assert _not_modified(FakeRequest(), '"abc123"', "Mon, 01 Aug 2026 08:00:00 GMT") is False


# ── Issue #51: If-None-Match excludes If-Modified-Since (RFC 7232 §3.3) ────

def test_not_modified_if_none_match_mismatch_beats_matching_ims():
    """Both validators present, ETag differs, IMS says "unchanged" → 200 (not 304).

    The regression: the timestamp branch used to be OR-ed with the ETag, so a
    feed whose content changed while no earnings row timestamp moved was
    answered with an empty 304 and the client kept its stale calendar.
    """
    class FakeRequest:
        headers = {
            "if-none-match": '"stale-etag"',
            "if-modified-since": "Tue, 02 Aug 2026 08:00:00 GMT",
        }
    assert _not_modified(FakeRequest(), '"fresh-etag"', "Mon, 01 Aug 2026 08:00:00 GMT") is False


def test_not_modified_if_none_match_match_wins_over_older_ims():
    """ETag matches → 304 even when If-Modified-Since is older than Last-Modified."""
    class FakeRequest:
        headers = {
            "if-none-match": '"abc123"',
            "if-modified-since": "Sun, 01 Jan 2026 00:00:00 GMT",
        }
    assert _not_modified(FakeRequest(), '"abc123"', "Mon, 01 Aug 2026 08:00:00 GMT") is True


def test_not_modified_if_none_match_weak_and_list_forms():
    """Weak (W/"x") and comma-separated validators compare, ``*`` matches anything."""
    class WeakRequest:
        headers = {"if-none-match": 'W/"abc123"'}
    assert _not_modified(WeakRequest(), '"abc123"', "Mon, 01 Aug 2026 08:00:00 GMT") is True

    class ListRequest:
        headers = {"if-none-match": '"other", "abc123"'}
    assert _not_modified(ListRequest(), '"abc123"', "Mon, 01 Aug 2026 08:00:00 GMT") is True

    class StarRequest:
        headers = {"if-none-match": "*"}
    assert _not_modified(StarRequest(), '"abc123"', "Mon, 01 Aug 2026 08:00:00 GMT") is True

    class EmptyRequest:
        headers = {"if-none-match": ""}
    assert _not_modified(EmptyRequest(), '"abc123"', "Mon, 01 Aug 2026 08:00:00 GMT") is False


# ── 304 Not Modified: If-Modified-Since ───────────────────────────────────

def test_not_modified_if_modified_since_not_changed():
    """If-Modified-Since >= Last-Modified → 304."""
    class FakeRequest:
        headers = {"if-modified-since": "Tue, 02 Aug 2026 08:00:00 GMT"}
    assert _not_modified(FakeRequest(), '"etag"', "Mon, 01 Aug 2026 08:00:00 GMT") is True


def test_not_modified_if_modified_since_older():
    """If-Modified-Since < Last-Modified → serve normally."""
    class FakeRequest:
        headers = {"if-modified-since": "Sun, 01 Jan 2026 00:00:00 GMT"}
    assert _not_modified(FakeRequest(), '"etag"', "Mon, 01 Aug 2026 08:00:00 GMT") is False


# ── No conditional headers → serve normally ───────────────────────────────

def test_no_conditional_headers_serves_normally():
    """No If-None-Match or If-Modified-Since → not 304."""
    class FakeRequest:
        headers = {}
    assert _not_modified(FakeRequest(), '"etag"', "Mon, 01 Aug 2026 08:00:00 GMT") is False


# ── Invalid If-Modified-Since value → serve normally ──────────────────────

def test_invalid_if_modified_since_serves_normally():
    """Malformed If-Modified-Since → not 304 (don't crash)."""
    class FakeRequest:
        headers = {"if-modified-since": "not-a-date"}
    assert _not_modified(FakeRequest(), '"etag"', "Mon, 01 Aug 2026 08:00:00 GMT") is False


# ── Endpoint behavior: source-level assertions ────────────────────────────

def test_endpoint_returns_404_not_redirect_for_invalid_token():
    """The source code returns 404 for invalid tokens — not a redirect or HTML login.

    This is a source-level check because running the endpoint requires a DB.
    The router function explicitly returns Response(content="Not Found", status_code=404)
    when the token does not match any user.
    """
    import inspect
    from app.routers.ical import ical_feed
    src = inspect.getsource(ical_feed)
    # Must return 404 for missing token, not redirect
    assert "404" in src
    assert "Not Found" in src or "not_found" in src.lower()
    # Must NOT contain login redirect logic
    assert "redirect" not in src.lower()
    assert "login" not in src.lower() or "auth" not in src.lower()


def test_endpoint_media_type_is_text_calendar():
    """The response media_type must be text/calendar; charset=utf-8."""
    import inspect
    from app.routers.ical import ical_feed
    src = inspect.getsource(ical_feed)
    assert "text/calendar" in src
    assert "charset=utf-8" in src


def test_endpoint_uses_token_not_cookie():
    """Subscription URL uses path-based token, not cookie-based auth."""
    import inspect
    from app.routers.ical import ical_feed
    src = inspect.getsource(ical_feed)
    # Token is a path parameter
    assert "token" in src
    # No cookie dependency
    assert "cookie" not in src.lower()


# ── Issue #25: empty watchlist must not leak full data ─────────────────────

def test_watchlist_scope_returns_empty_when_syms_empty():
    """scope=watchlist with empty watchlist must return empty calendar, not full data.

    Source-level assertion: the _generate() closure inside ical_feed must
    contain an early-return guard that short-circuits when syms is empty.
    """
    import inspect
    from app.routers.ical import ical_feed
    src = inspect.getsource(ical_feed)
    # The guard: if not syms → return empty calendar immediately
    assert "if not syms" in src, (
        "Missing empty watchlist guard — would leak full earnings data"
    )
    # Must return generate_ical([], ...) for the empty case
    assert "generate_ical([]" in src or "generate_ical([]," in src, (
        "Empty watchlist guard must return generate_ical([]) to produce valid empty VCALENDAR"
    )


# ── Issue #51: Last-Modified must follow the feed content ─────────────────

def test_feed_last_modified_tracks_content_versions():
    """Content change advances Last-Modified; unchanged content keeps it stable."""
    from app.routers import ical as ical_router

    ical_router._content_versions.clear()
    key = ("tok-abc", "zh", "watchlist", 1, "all")
    row_ts = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)

    first = ical_router._feed_last_modified(key, '"etag-1"', row_ts)
    again = ical_router._feed_last_modified(key, '"etag-1"', row_ts)
    assert again == first, "identical content must not drift its validator"

    changed = ical_router._feed_last_modified(key, '"etag-2"', row_ts)
    assert changed > first, "a new ETag means the content changed: Last-Modified must advance"
    assert changed > row_ts, "content change must outrank the earnings row timestamps"


def test_feed_last_modified_never_older_than_rows():
    """Row timestamps in the future still win over the content version."""
    from app.routers import ical as ical_router

    ical_router._content_versions.clear()
    key = ("tok-abc", "en", "all", 0, "US")
    row_ts = datetime.now(timezone.utc) + timedelta(days=30)
    assert ical_router._feed_last_modified(key, '"etag"', row_ts) == row_ts


# ── Issue #51: end-to-end conditional requests through /ical/{token} ───────

class _FakeCursor:
    def __init__(self, user, watchlist):
        self._user = user
        self._watchlist = watchlist

    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        return self._user

    def fetchall(self):
        return self._watchlist


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kw):
        return self._cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *a):
        return False


def _ical_client(user, watchlist, events):
    """TestClient serving /ical/{token} with stubbed DB rows and earnings."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import db
    from app import earnings as earnings_mod
    from app.routers import ical as ical_router

    ical_router._ical_cache.clear()
    ical_router._content_versions.clear()
    app.dependency_overrides = {}
    cursor = _FakeCursor(user, watchlist)
    patchers = [
        patch.object(db, "db_cursor", lambda: _FakeConn(cursor)),
        patch.object(earnings_mod, "fetch_earnings_from_db", lambda **kw: list(events)),
    ]
    for p in patchers:
        p.start()
    return TestClient(app, raise_server_exceptions=False), patchers


def test_subscription_serves_new_event_when_only_content_changed():
    """A watchlist change that leaves every row timestamp untouched must be

    delivered: revalidating with the old ETag *and* the old Last-Modified has to
    return 200 with the new VEVENT, never an empty-body 304 (Issue #51).
    """
    from app.routers.ical import invalidate_ical_cache

    user = {"id": 1, "email": "t@t.com", "name": "T"}
    watchlist = [{"symbol": "AAPL", "market": "US"}]
    row_ts = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    aapl = {
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 9, 20), "before_after": "after",
        "updated_at": row_ts,
    }
    msft = {
        "symbol": "MSFT", "market": "US", "company_name": "Microsoft",
        "report_date": date(2026, 9, 21), "before_after": "after",
        "updated_at": row_ts,
    }

    client, patchers = _ical_client(user, watchlist, [aapl])
    try:
        first = client.get("/ical/tok-abc")
        assert first.status_code == 200
        validators = {
            "if-none-match": first.headers["etag"],
            "if-modified-since": first.headers["last-modified"],
        }

        # The user adds MSFT: POST /api/watchlist invalidates the feed cache,
        # earnings rows themselves are not rewritten (same updated_at).
        invalidate_ical_cache("tok-abc")
        from app import earnings as earnings_mod
        with patch.object(earnings_mod, "fetch_earnings_from_db", lambda **kw: [aapl, msft]):
            second = client.get("/ical/tok-abc", headers=validators)

        assert second.status_code == 200, (
            f"stale 304 while the feed content changed (status={second.status_code})"
        )
        assert "MSFT" in second.text, "the newly added symbol must reach the subscriber"
    finally:
        for p in patchers:
            p.stop()


def test_subscription_returns_304_when_nothing_changed():
    """Unchanged content keeps both validators stable → 304 on revalidation."""
    from app.routers import ical as ical_router

    user = {"id": 1, "email": "t@t.com", "name": "T"}
    watchlist = [{"symbol": "AAPL", "market": "US"}]
    aapl = {
        "symbol": "AAPL", "market": "US", "company_name": "Apple",
        "report_date": date(2026, 9, 20), "before_after": "after",
        "updated_at": datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
    }

    client, patchers = _ical_client(user, watchlist, [aapl])
    try:
        first = client.get("/ical/tok-abc")
        assert first.status_code == 200
        validators = {
            "if-none-match": first.headers["etag"],
            "if-modified-since": first.headers["last-modified"],
        }

        # Force the cold path (cache expiry) while keeping the content version.
        ical_router._ical_cache.clear()
        second = client.get("/ical/tok-abc", headers=validators)
        assert second.status_code == 304, (
            f"unchanged feed must stay 304 (status={second.status_code}, "
            f"last-modified={second.headers.get('last-modified')})"
        )
    finally:
        for p in patchers:
            p.stop()
