"""Response compression that decides on the whole body (Issue #99).

Starlette's ``GZipMiddleware`` compares ``minimum_size`` against the *first*
``http.response.body`` message only.  Every response that arrives in more than one
message — which is every response whose body passed through
``BaseHTTPMiddleware`` (this app's error and ETag layers) or through
``StaticFiles``/``FileResponse`` — therefore skipped that check and got
compressed no matter how small it was: the 253 B ``/api/admin/health`` probe came
back gzip-coded, i.e. the coding made the answer bigger, and acceptance criterion 5
of Issue #99 ("a 453 B health probe stays uncompressed") could not hold.

It also could not keep validators honest.  ``StaticFiles`` mints a *strong* ETag
from the file's stat and reuses it for both content-codings, and a strong
validator asserts byte-identical representations (RFC 7232 §2.3.1) — the two
codings are only semantically equivalent.  The ETag layer in this app already
emits ``W/`` for what it mints (``app/etag_middleware.py``); this one weakens the
strong validator of anything *it* codes, which covers the static bundle.

So: buffer until the body is known to be at least ``minimum_size``, compress only
past that point, and then stream exactly like the stock middleware does.
"""
from __future__ import annotations

import zlib

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Content types whose bytes must never be held back: an event stream is a
#: promise that the first message leaves immediately.
_EXCLUDED_CONTENT_TYPES = ("text/event-stream",)


def _weaken(etag: str) -> str:
    """Declare a validator weak (RFC 7232 §2.3.1).

    A strong ETag promises the two representations are byte-identical; two
    content-codings of one resource are only *semantically* identical, so the
    strong form would be a lie the moment the same value is served gzipped and
    identically.
    """
    value = (etag or "").strip()
    if not value or value[:2].upper() == "W/":
        return value
    return f"W/{value}"


class CompressedResponses:
    """ASGI middleware: gzip for bodies at or above ``minimum_size``."""

    def __init__(self, app: ASGIApp, *, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        accepted = Headers(scope=scope).get("accept-encoding", "").lower()
        if "gzip" not in accepted:
            # Nothing to negotiate: the bytes are exactly what the endpoint sent,
            # which is what a client that did not ask for a coding must receive.
            await self.app(scope, receive, send)
            return
        responder = _Responder(self.app, self.minimum_size, self.compresslevel)
        await responder(scope, receive, send)


class _Responder:
    """One request's negotiation state, so no two requests share a stream."""

    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.send: Send = self._unattached
        self.initial_message: Message = {}
        self.pending: list[bytes] = []
        self.pending_size = 0
        #: ``None`` while undecided, then True (compressing) or False (identity).
        self.compressing: bool | None = None
        self.headers_started = False
        self.passthrough = False
        self.compressor: zlib._Compress | None = None

    @staticmethod
    async def _unattached(message: Message) -> None:  # pragma: no cover - defensive
        raise RuntimeError("responder used outside a request")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        await self.app(scope, receive, self.send_with_compression)

    async def send_with_compression(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.initial_message = message
            headers = Headers(raw=message["headers"])
            # An already-coded or non-bufferable response is forwarded untouched:
            # re-coding it would either double-compress or delay a stream.
            if "content-encoding" in headers or headers.get("content-type", "").startswith(
                _EXCLUDED_CONTENT_TYPES
            ):
                self.passthrough = True
            return
        if kind != "http.response.body":
            return

        body = message.get("body", b"")
        more = message.get("more_body", False)

        if self.passthrough:
            if not self.headers_started:
                self.headers_started = True
                await self.send(self.initial_message)
            await self.send(message)
            return

        if self.compressing is None:
            self.pending.append(body)
            self.pending_size += len(body)
            if self.pending_size < self.minimum_size and more:
                return  # still undecided: hold the bytes, send nothing yet
            if self.pending_size < self.minimum_size:
                # The whole body is small: identity, exactly as the endpoint sent
                # it, byte for byte.
                self.compressing = False
                return await self._send_identity()
            self.compressing = True
            buffered = b"".join(self.pending)
            self.pending.clear()
            if not more:
                # The whole body is in hand, so the client gets an exact length
                # instead of a chunked answer: the same framing the endpoint
                # declared, measured on the bytes actually sent.
                coded = zlib.compress(buffered, min(9, max(1, self.compresslevel)), 31)
                await self._start_coded_headers(content_length=len(coded))
                await self.send({"type": "http.response.body", "body": coded, "more_body": False})
                return
            self.compressor = zlib.compressobj(
                min(9, max(1, self.compresslevel)), zlib.DEFLATED, 31
            )
            await self._start_coded_headers()
            return await self._send_coded(buffered, more)

        if self.compressing:
            return await self._send_coded(body, more)

        self.pending.append(body)
        if not more:
            return await self._send_identity()
        return

    async def _send_identity(self) -> None:
        """Forward what was buffered unchanged (no coding, no new headers)."""
        body = b"".join(self.pending)
        self.pending.clear()
        if not self.headers_started:
            self.headers_started = True
            await self.send(self.initial_message)
        await self.send({"type": "http.response.body", "body": body, "more_body": False})

    async def _start_coded_headers(self, content_length: int | None = None) -> None:
        headers = MutableHeaders(raw=self.initial_message["headers"])
        headers.add_vary_header("Accept-Encoding")
        headers["Content-Encoding"] = "gzip"
        # The length the endpoint declared describes the bytes it produced, not
        # the coded ones: a stale value would truncate the answer (Issue #35's
        # framing rule). Where the whole body was in hand the exact coded length
        # is declared; a streamed one goes out chunked and the server frames it.
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        elif "content-length" in headers:
            del headers["Content-Length"]
        if "etag" in headers:
            headers["ETag"] = _weaken(headers["ETag"])
        self.headers_started = True
        await self.send(self.initial_message)

    async def _send_coded(self, body: bytes, more: bool) -> None:
        assert self.compressor is not None
        payload = self.compressor.compress(body)
        if not more:
            payload += self.compressor.flush()
        await self.send({"type": "http.response.body", "body": payload, "more_body": more})
