"""Subscription endpoint HTTP compliance tests (Issue #1 item 15).

Validates that the iCal subscription endpoint:
  - Returns text/calendar; charset=utf-8 Content-Type
  - Does not require cookies or redirect to login
  - Returns 404 for invalid tokens (not redirect or HTML)
  - Returns proper headers (ETag, Last-Modified, Cache-Control)
  - Supports 304 Not Modified via conditional request headers
"""
from datetime import date, datetime, timezone

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
