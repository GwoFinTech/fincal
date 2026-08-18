"""ETag middleware for JSON GET responses (Issue #10).

Generates content-hash ETags for cacheable GET responses.
Supports If-None-Match → 304 Not Modified.

Excludes:
- Error responses (4xx/5xx)
- User-specific endpoints (/me, /watchlist)
- Real-time endpoints
"""
from __future__ import annotations

import hashlib
from fastapi import Request, Response
from fastapi.responses import JSONResponse

# Paths that should NOT be cached (user-specific or real-time)
_NO_CACHE_PREFIXES = (
    "/api/me",
    "/api/watchlist",
    "/api/config",
)

# Paths that ARE safe to cache (public, stable data)
_CACHEABLE_PREFIXES = (
    "/api/earnings",
    "/api/popular",
    "/api/search",
    "/api/admin/overview",
    "/api/admin/sync-runs",
    "/api/export",
)


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


async def etag_middleware(request: Request, call_next):
    """Add ETag and 304 support for cacheable JSON GET responses."""
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
    if etag in if_none_match:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=60"})

    return Response(
        content=body,
        status_code=response.status_code,
        headers={
            **dict(response.headers),
            "ETag": etag,
            "Cache-Control": "private, max-age=60",
        },
        media_type=response.media_type,
    )
