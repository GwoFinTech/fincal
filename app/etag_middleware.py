"""ETag middleware for JSON GET responses (Issue #10).

Generates content-hash ETags for cacheable GET responses.
Supports If-None-Match → 304 Not Modified.

Excludes:
- Error responses (4xx/5xx)
- User-specific endpoints (/me, /watchlist, /earnings)
- Real-time endpoints
"""
from __future__ import annotations

import hashlib
from fastapi import Request, Response

# Paths that should NOT be cached (user-specific or real-time).
#
# ``/api/earnings`` is listed here on purpose (Issue #35): its index merges the
# caller's watchlist into the shared calendar and ``/api/earnings/{id}/decision``
# derives per-user decision facts, so a validator minted for one session is not
# a validator for the next one.  ``Cache-Control: private`` alone is not enough —
# a browser's bfcache/back-forward restore can hand the stored body to whoever
# signs in next.
_NO_CACHE_PREFIXES = (
    "/api/me",
    "/api/watchlist",
    "/api/config",
    "/api/earnings",
)

# Paths that ARE safe to cache (public, stable data)
_CACHEABLE_PREFIXES = (
    "/api/popular",
    "/api/search",
    "/api/admin/overview",
    "/api/admin/sync-runs",
    "/api/export",
)

# Hop-by-hop and framing headers describe the response the *endpoint* produced,
# not the body this middleware re-emits.  Copying them over means a declared
# ``content-length`` that no longer matches the re-serialized (or fully drained
# stream) body reaches the client as a truncated/protocol-level answer; the ASGI
# server must recompute the length from the final body (Issue #35).
_FRAMING_HEADERS = frozenset({
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "te",
    "trailer",
    "trailers",
    "upgrade",
    "proxy-connection",
})

# A 304 has no body, so body-describing headers must not be forwarded with it.
# Everything else the original response set (Vary, rate-limit counters, ...)
# survives the conditional branch (Issue #35).
_BODYLESS_DROP_HEADERS = _FRAMING_HEADERS | frozenset({
    "content-type",
    "content-encoding",
    "content-disposition",
})


def _is_cacheable(request: Request, response: Response) -> bool:
    if request.method != "GET":
        return False
    if response.status_code >= 400:
        return False
    path = request.url.path
    if any(path.startswith(p) for p in _NO_CACHE_PREFIXES):
        return False
    if any(path.startswith(p) for p in _CACHEABLE_PREFIXES):
        return True
    return False


def _compute_etag(body: bytes) -> str:
    h = hashlib.sha256(body).hexdigest()[:16]
    return f'"{h}"'


def _forwarded_headers(response: Response, drop: frozenset[str]) -> dict[str, str]:
    """Original headers minus the ones the middleware must recompute."""
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in drop
    }


async def etag_middleware(request: Request, call_next):
    """Add ETag and 304 support for cacheable GET responses."""
    response = await call_next(request)

    if not _is_cacheable(request, response):
        return response

    # Read response body
    body = b""
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            body += chunk.encode()
        else:
            body += chunk

    etag = _compute_etag(body)

    # Check If-None-Match
    if_none_match = request.headers.get("if-none-match", "")
    if etag and etag in if_none_match:
        headers = _forwarded_headers(response, _BODYLESS_DROP_HEADERS)
        headers["ETag"] = etag
        headers["Cache-Control"] = "private, max-age=60"
        return Response(status_code=304, headers=headers)

    headers = _forwarded_headers(response, _FRAMING_HEADERS)
    headers["ETag"] = etag
    headers["Cache-Control"] = "private, max-age=60"
    return Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type,
    )
