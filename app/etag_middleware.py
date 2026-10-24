"""ETag middleware for JSON GET responses (Issue #10).

Generates content-hash ETags for cacheable GET responses.
Supports If-None-Match → 304 Not Modified.

Excludes:
- Error responses (4xx/5xx)
- User-specific endpoints (/me, /watchlist, /earnings)
- Real-time endpoints

Since Issue #99 the body this middleware re-emits is compressed by a gzip layer
*outside* it, so the validator it mints is weak (one stored representation, two
content-codings) and the 304 branch declares ``Vary: Accept-Encoding``.
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
    """Content hash of the *uncompressed* body, emitted as a weak validator.

    Issue #99 put a gzip layer outside this middleware, so one resource now has
    two content-codings on the wire (identity and gzip) generated from the same
    stored bytes. A strong validator asserts byte-identical representations
    (RFC 7232 §2.3.1); these two are only *semantically* equivalent, so the
    hash must be declared weak. Recomputing it over the coded bytes instead
    would put the validator behind the compression stage — two hashes per
    resource, one cache entry each, and a 304 that depends on which coding the
    client asked for — which is the more invasive fix.
    """
    h = hashlib.sha256(body).hexdigest()[:16]
    return f'W/"{h}"'


def _strip_weak(etag: str) -> str:
    """Drop an optional ``W/`` prefix (RFC 7232 §2.3.2)."""
    value = etag.strip()
    return value[2:].strip() if value[:2].upper() == "W/" else value


def _if_none_match_hits(header: str, etag: str) -> bool:
    """Weak comparison of a comma-separated ``If-None-Match`` (RFC 7232 §3.2).

    ``If-None-Match`` always uses the weak comparison function, so the ``W/``
    on either side is irrelevant — a client echoing ``W/"h"`` and a client that
    normalised it to ``"h"`` mean the same thing, and both must revalidate to a
    304 rather than re-download a body they already have.
    """
    target = _strip_weak(etag)
    if not target:
        return False
    for candidate in (header or "").split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        if candidate == "*" or _strip_weak(candidate) == target:
            return True
    return False


def _with_accept_encoding_vary(headers: dict[str, str]) -> dict[str, str]:
    """Declare the content-coding negotiation the gzip layer performs (#99).

    Only the *conditional* branch needs this. ``GZipMiddleware`` stamps
    ``Vary: Accept-Encoding`` on the 200 it rewrites, but a 304 has no body to
    compress, so it never passes through that layer — and a shared cache is then
    free to answer a gzip-incapable client with the compressed variant it stored
    earlier (RFC 7234 §4.1: Vary is part of the stored response). Declaring it on
    the 200s as well would only repeat a token, since ``Vary`` is a set.
    """
    parts: list[str] = []
    for name in [n for n in headers if n.lower() == "vary"]:
        parts.extend(p.strip() for p in headers.pop(name).split(",") if p.strip())
    if not any(p.lower() == "accept-encoding" for p in parts):
        parts.append("Accept-Encoding")
    headers["Vary"] = ", ".join(parts)
    return headers


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
    if _if_none_match_hits(if_none_match, etag):
        headers = _with_accept_encoding_vary(_forwarded_headers(response, _BODYLESS_DROP_HEADERS))
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
