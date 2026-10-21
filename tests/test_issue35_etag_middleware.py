"""ETag middleware contract (Issue #35).

The middleware re-serializes the body (it has to, to hash it), so every header
copied from the upstream response has to be re-validated against the new body:

* framing headers are recomputed by the ASGI server — a copied ``content-length``
  truncated a re-emitted body, and a copied ``transfer-encoding`` breaks framing;
* ``/api/earnings`` (index and ``{id}/decision``) answers per user, so it must
  not sit in the cacheable whitelist at all;
* the 304 branch used to discard every original header, which swallowed
  rate-limit / Vary metadata.

The middleware is exercised through a real ``BaseHTTPMiddleware`` stack: that is
the only way ``call_next`` hands back the streaming wrapper production sees, and
routing/auth inside the test app is replaced by fixed payloads so no DB is hit.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from app.etag_middleware import _is_cacheable, etag_middleware

CSV_BODY = b"symbol,market\nAAPL,US\n"


def _build_client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(etag_middleware)

    @app.get("/api/search")
    def search():
        return JSONResponse([{"symbol": "AAPL"}],
                            headers={"x-ratelimit-remaining": "42"})

    @app.get("/api/earnings")
    def earnings(watchlistOnly: bool = False):
        return JSONResponse([{"symbol": "AAPL", "watchlistOnly": watchlistOnly}])

    @app.get("/api/earnings/{earning_id}/decision")
    def decision(earning_id: int):
        return JSONResponse({"id": earning_id})

    @app.get("/api/export")
    def export():
        def chunks():
            yield CSV_BODY[:8]
            yield CSV_BODY[8:]

        return StreamingResponse(
            chunks(),
            media_type="text/csv",
            headers={
                # A lying upstream length: what the middleware must not forward.
                "content-length": "5",
                "transfer-encoding": "chunked",
                "content-disposition": 'attachment; filename="x.csv"',
            },
        )

    @app.get("/api/export/json-length")
    def json_length():
        return Response(content=b'{"ok": true}', media_type="application/json",
                        headers={"content-length": "1"})

    return TestClient(app)


def test_declared_content_length_never_reaches_the_client_unchanged():
    client = _build_client()

    response = client.get("/api/export/json-length")

    assert response.status_code == 200
    assert response.content == b'{"ok": true}'
    assert response.headers["content-length"] == str(len(response.content))
    assert response.headers["ETag"]


def test_streamed_export_loses_the_upstream_framing_headers():
    client = _build_client()

    response = client.get("/api/export")

    assert response.status_code == 200
    assert response.content == CSV_BODY
    assert "transfer-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(CSV_BODY))
    assert response.headers["content-disposition"] == 'attachment; filename="x.csv"'


def test_earnings_index_and_decision_carry_no_validator():
    client = _build_client()

    for url in ("/api/earnings",
                "/api/earnings?watchlistOnly=true",
                "/api/earnings?watchlistOnly=false",
                "/api/earnings/42/decision"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert "etag" not in response.headers, url


def test_public_endpoints_keep_their_validator_and_answer_304():
    client = _build_client()

    first = client.get("/api/search")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=60"
    etag = first.headers["etag"]

    conditional = client.get("/api/search", headers={"if-none-match": etag})

    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["etag"] == etag
    # Non-entity headers survive the conditional branch (they used to be lost).
    assert conditional.headers["x-ratelimit-remaining"] == "42"
    assert "content-length" not in conditional.headers
    assert "content-type" not in conditional.headers


def test_cacheable_whitelist_still_excludes_writes_and_errors():
    from starlette.requests import Request as StarletteRequest

    def _req(path, method="GET"):
        return StarletteRequest({
            "type": "http", "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "root_path": "", "headers": [],
            "server": ("testserver", 80), "client": ("testclient", 12345),
        })

    ok = Response()

    assert _is_cacheable(_req("/api/export"), ok)
    assert _is_cacheable(_req("/api/popular"), ok)
    assert _is_cacheable(_req("/api/admin/sync-runs"), ok)
    assert not _is_cacheable(_req("/api/export", method="POST"), ok)
    assert not _is_cacheable(_req("/api/earnings"), ok)
    assert not _is_cacheable(_req("/api/earnings/42/decision"), ok)
    assert not _is_cacheable(_req("/api/me"), ok)
