"""Response compression contract (Issue #99).

Measured on 2026-10-24 against the production data set: the month view
(``/api/earnings``, 92 rows, 112-symbol universe) left the process as 95,656 B
and a ``scope=all`` feed (450 events) as 186,710 B — per request, for every
subscriber poll — with no ``Content-Encoding``: the app had no gzip layer and
the edge (Traefik) had no ``compress`` middleware either. Both are text and
compress 10–13×.

What this module pins:

* the compression layer is the outermost one, so the validators above it keep
  describing a single uncompressed representation;
* a client that negotiates gzip gets the same bytes back after decoding, and a
  client that does not is served byte-for-byte what it was served before;
* the wire size of both hot outlets drops by an order of magnitude;
* conditional requests still answer 304, and the 304 declares the negotiation
  (``Vary``) that ``GZipMiddleware`` only stamps on bodies;
* the CSV export still decompresses into the same file.

One framing note these tests document rather than hide: ``/api/error`` and the
ETag layer are ``BaseHTTPMiddleware``, which re-emits every response as a
*stream* of body messages. ``GZipMiddleware``'s ``minimum_size`` gate only sees
a size when the body arrives in one message — true for the paths the ETag
middleware re-serializes itself (it returns a complete ``Response``), false for
the streamed ones. So the whitelisted JSON endpoints keep their
``Content-Length`` and stay identity below 1 KiB, while streamed paths (the
month view, the iCal feed, the health probe) are always coded and lose
``Content-Length``.
"""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from app.etag_middleware import _if_none_match_hits, etag_middleware
from app.main import install_middleware
from app.routers import ical as ical_router

# The two production sizes from the issue, used as the yardstick for the
# fixtures below (a fixture an order of magnitude smaller would let the
# "compressed" assertion pass for the wrong reason).
MONTH_VIEW_RAW_BYTES = 95_656
FEED_RAW_BYTES = 186_710


# ── Fixtures that reproduce the measured payloads ──────────────────────────

_SYMBOLS_US = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "INTC",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "AXP", "PYPL",
    "UNH", "JNJ", "PFE", "MRK", "LLY", "ABBV", "TMO", "DHR", "COST", "WMT",
]
_SYMBOLS_HK = [
    "0700", "0941", "1299", "0388", "0005", "0939", "3690", "9988", "1810", "2318",
    "0386", "0857", "0883", "1088", "2628", "2319", "0027", "0175", "1211", "2020",
]


def _month_view_rows(count: int = 92) -> list[dict]:
    """``count`` rows shaped like ``/api/earnings`` answers (all EarningItem fields)."""
    rows: list[dict] = []
    base = date(2026, 9, 1)
    for i in range(count):
        hk = i % 5 == 4
        pool = _SYMBOLS_HK if hk else _SYMBOLS_US
        symbol = pool[(i * 7) % len(pool)]
        report_date = base + timedelta(days=(i * 3) % 90)
        currency = "HKD" if hk else "USD"
        eps = round(((i * 37) % 500) / 100 - 0.5, 2)
        rows.append({
            "id": 5000 + i,
            "symbol": symbol,
            "market": "HK" if hk else "US",
            "company_name": f"{symbol} 股份有限公司" if hk else f"{symbol} Holdings Incorporated",
            "company_name_en": f"{symbol} Holdings Incorporated",
            "report_date": report_date.isoformat(),
            "report_type": "Q" if i % 4 else "A",
            "fiscal_year": 2026,
            "fiscal_quarter": (i % 4) + 1,
            "before_after": "after" if i % 2 else "before",
            "eps_estimate": eps,
            "eps_actual": None,
            "revenue_estimate": round(1_000_000_000 * ((i % 17) + 1) / 7, 2),
            "revenue_actual": None,
            "is_predicted": i % 3 == 0,
            "date_source": "futu" if i % 2 else "longbridge",
            "date_status": "confirmed" if i % 3 else "estimated",
            "estimate_source": "consensus",
            "estimate_as_of": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc).isoformat(),
            "actual_source": None,
            "actual_as_of": None,
            "estimate_currency": currency,
            "estimate_basis": "gaap",
            "actual_currency": currency,
            "actual_basis": "gaap",
            "comparison_unavailable_reason": None,
            "consensus_eps_gaap": eps,
            "consensus_eps_adjusted": round(eps + 0.11, 2),
            "consensus_revenue": round(1_000_000_000 * ((i % 17) + 1) / 7, 2),
            "consensus_ebit": None,
            "consensus_net_income": None,
            "consensus_normalized_net_income": None,
            "consensus_currency": currency,
            "consensus_fetched_at": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc).isoformat(),
            "updated_at": datetime(2026, 8, 21, 3, 30, tzinfo=timezone.utc).isoformat(),
        })
    return rows


def _feed_events(count: int = 450) -> list[dict]:
    """``count`` earnings rows for the feed generator (the ``scope=all`` shape)."""
    events: list[dict] = []
    today = date.today()
    for i in range(count):
        hk = i % 5 == 4
        pool = _SYMBOLS_HK if hk else _SYMBOLS_US
        stem = pool[i % len(pool)]
        # Unique per event: the generator de-duplicates by symbol+date.
        symbol = f"{stem}{i:03d}" if not hk else f"{stem[:4]}{i:03d}"
        events.append({
            "symbol": symbol,
            "market": "HK" if hk else "US",
            "company_name": f"{symbol} 股份有限公司" if hk else f"{symbol} Holdings Incorporated",
            "company_name_en": f"{symbol} Holdings Incorporated",
            "report_date": today + timedelta(days=(i % 400) - 6),
            "report_type": "Q" if i % 4 else "A",
            "fiscal_year": 2026,
            "fiscal_quarter": (i % 4) + 1,
            "before_after": "after" if i % 2 else "before",
            "eps_estimate": round(((i * 37) % 500) / 100 - 0.5, 2),
            "revenue_estimate": round(1_000_000_000 * ((i % 17) + 1) / 7, 2),
            "is_predicted": i % 3 == 0,
            "updated_at": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        })
    return events


# ── A probe app with the production middleware order ───────────────────────

HEALTH_BODY = {
    "status": "ok",
    "commit": "b5e592e",
    "uptime_seconds": 12345.678,
    "checks": {"database": "ok", "futu": "ok", "scheduler": "ok"},
    "version": "0.1.0",
    "probe": "x" * 307,  # lands the payload on the production 453 B
}
CSV_BODY = "".join(
    f"{symbol},{'HK' if i % 5 == 4 else 'US'},2026-09-{(i % 28) + 1:02d},Q,{i}\n"
    for i, symbol in enumerate(_SYMBOLS_US + _SYMBOLS_HK)
).encode()
# A cacheable JSON answer below ``minimum_size`` (the ETag whitelist re-emits it
# as a complete body, i.e. the one case where the size gate can fire).
SMALL_CACHEABLE_BODY = {"symbols": ["AAPL", "MSFT"]}


def _probe_app(with_gzip: bool = True) -> FastAPI:
    """Same routes, production middleware order, optionally without gzip."""
    probe = FastAPI()
    if with_gzip:
        install_middleware(probe)
    else:
        probe.middleware("http")(etag_middleware)

    @probe.get("/api/earnings")
    def earnings():
        return JSONResponse(_month_view_rows())

    @probe.get("/api/search")
    def search():
        return JSONResponse([{"symbol": r["symbol"], "market": r["market"]} for r in _month_view_rows()])

    @probe.get("/api/popular")
    def popular():
        return JSONResponse(SMALL_CACHEABLE_BODY)

    @probe.get("/api/admin/health")
    def health():
        return JSONResponse(HEALTH_BODY)

    @probe.get("/api/export")
    def export():
        def chunks():
            yield CSV_BODY[:400]
            yield CSV_BODY[400:]

        return StreamingResponse(
            chunks(), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="fincal.csv"'},
        )

    return probe


def _wire(client: TestClient, url: str, *, accept_encoding: str = "gzip",
          extra_headers: dict | None = None):
    """Status, headers and the *raw on-the-wire* body (no client-side decoding)."""
    headers = {"accept-encoding": accept_encoding}
    if extra_headers:
        headers.update(extra_headers)
    with client.stream("GET", url, headers=headers) as response:
        raw = b"".join(response.iter_raw())
        return response.status_code, dict(response.headers), raw


def _vary(header_map: dict[str, str]) -> str:
    return (header_map.get("vary") or "").lower()


# ── Stack order ────────────────────────────────────────────────────────────

def test_gzip_is_the_outermost_layer_of_the_production_stack():
    """The last registration wraps everything — compression must not sit inside.

    Registered first it would be the *innermost* user middleware, the ETag
    middleware would hash already-coded bytes, and one resource would need one
    validator — and one cache entry — per content-coding.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.gzip import GZipMiddleware

    from app.main import app

    stack = app.user_middleware
    assert stack[0].cls is GZipMiddleware
    assert stack[0].kwargs == {"minimum_size": 1024, "compresslevel": 6}
    assert stack[1].cls is BaseHTTPMiddleware
    assert stack[1].kwargs["dispatch"] is etag_middleware


# ── Criterion 1 + 3: the month view is coded, smaller and lossless ─────────

def test_fixture_is_the_measured_size_class():
    """Guard the fixture: the size assertions below must be meaningful."""
    identity = json.dumps(_month_view_rows(), ensure_ascii=False).encode("utf-8")
    assert abs(len(identity) - MONTH_VIEW_RAW_BYTES) < MONTH_VIEW_RAW_BYTES * 0.15, (
        f"month-view fixture is {len(identity)} B, production measured "
        f"{MONTH_VIEW_RAW_BYTES} B"
    )


def test_month_view_is_gzipped_and_decodes_to_the_identity_bytes():
    client = TestClient(_probe_app())

    ident_status, ident_headers, ident_raw = _wire(client, "/api/earnings", accept_encoding="identity")
    gz_status, gz_headers, gz_raw = _wire(client, "/api/earnings", accept_encoding="gzip")

    assert (ident_status, gz_status) == (200, 200)
    assert "content-encoding" not in ident_headers
    assert gz_headers["content-encoding"] == "gzip"
    assert "accept-encoding" in _vary(gz_headers)
    assert gzip.decompress(gz_raw) == ident_raw
    assert json.loads(ident_raw) == _month_view_rows()


def test_month_view_wire_size_drops_below_the_issue_budget():
    client = TestClient(_probe_app())

    _, headers, raw = _wire(client, "/api/earnings", accept_encoding="gzip")
    _, _, ident_raw = _wire(client, "/api/earnings", accept_encoding="identity")

    assert headers["content-encoding"] == "gzip"
    # Acceptance criterion 3: 95,656 B → ≤ 15,000 B on the wire.
    assert len(raw) <= 15_000, f"month view still {len(raw)} B on the wire"
    assert len(raw) * 6 < len(ident_raw), f"{len(raw)} B vs {len(ident_raw)} B identity"


# ── Criterion 2: identity clients are unaffected ───────────────────────────

def test_identity_clients_receive_exactly_what_they_received_before():
    """Byte- and header-identical to a stack with no compression layer at all."""
    before = TestClient(_probe_app(with_gzip=False))
    after = TestClient(_probe_app())

    for url in ("/api/earnings", "/api/search", "/api/admin/health", "/api/export"):
        old = before.get(url, headers={"accept-encoding": "identity"})
        new = after.get(url, headers={"accept-encoding": "identity"})
        assert new.status_code == old.status_code, url
        assert new.content == old.content, url
        assert new.headers.get("content-encoding") is None, url
        # Same validators, same framing: the identity representation did not move.
        assert new.headers.get("etag") == old.headers.get("etag"), url
        assert new.headers.get("content-length") == old.headers.get("content-length"), url


# ── Criterion 4: validators across two content-codings ─────────────────────

def test_etag_is_weak_so_one_validator_covers_both_codings():
    client = TestClient(_probe_app())

    _, gz_headers, _ = _wire(client, "/api/search", accept_encoding="gzip")
    _, ident_headers, _ = _wire(client, "/api/search", accept_encoding="identity")

    etag = gz_headers["etag"]
    assert etag.startswith('W/"'), etag
    assert ident_headers["etag"] == etag, "both codings must share the same (weak) validator"
    assert not etag.startswith('"'), "a strong validator would claim byte-identical representations"


def test_cached_json_response_keeps_its_content_length():
    """The whitelisted JSON path is re-serialized by the ETag layer (§ framing)."""
    client = TestClient(_probe_app())

    _, headers, raw = _wire(client, "/api/search", accept_encoding="gzip")

    assert headers["content-encoding"] == "gzip"
    assert headers["content-length"] == str(len(raw))


def test_conditional_request_still_answers_304_under_both_codings():
    client = TestClient(_probe_app())
    _, headers, _ = _wire(client, "/api/search", accept_encoding="gzip")
    etag = headers["etag"]

    for coding in ("gzip", "identity"):
        status, cond_headers, raw = _wire(
            client, "/api/search", accept_encoding=coding, extra_headers={"if-none-match": etag})
        assert status == 304, coding
        assert raw == b"", coding
        assert cond_headers["etag"] == etag, coding
        assert "accept-encoding" in _vary(cond_headers), coding
        assert "content-encoding" not in cond_headers, coding


def test_if_none_match_compares_weakly_and_understands_lists():
    assert _if_none_match_hits('W/"abc"', 'W/"abc"')
    assert _if_none_match_hits('"abc"', 'W/"abc"'), "W/ is not part of the comparison"
    assert _if_none_match_hits('"abc"', '"abc"')
    assert _if_none_match_hits('"other", W/"abc"', 'W/"abc"')
    assert _if_none_match_hits("*", 'W/"abc"')
    assert not _if_none_match_hits('"abcd"', 'W/"abc"')
    assert not _if_none_match_hits("", 'W/"abc"')


# ── Criterion 5: small answers and the CSV export ──────────────────────────

def test_cacheable_answer_below_the_threshold_stays_identity():
    """``minimum_size`` is wired: a small *complete* body is not coded."""
    client = TestClient(_probe_app())

    status, headers, raw = _wire(client, "/api/popular", accept_encoding="gzip")

    assert status == 200
    assert "content-encoding" not in headers
    assert json.loads(raw) == SMALL_CACHEABLE_BODY


def test_streamed_health_probe_is_coded_but_never_inflated():
    """A 453 B probe is compressed because it arrives as a stream (§ module doc).

    ``GZipMiddleware`` only sees a body size when the whole body is one message,
    and the two ``BaseHTTPMiddleware`` layers re-emit every streamed response as
    chunks. The probe is 453 B, so coding it still *shrinks* it; what must not
    happen is a client without gzip noticing any difference.
    """
    client = TestClient(_probe_app())

    status, coded_headers, coded = _wire(client, "/api/admin/health", accept_encoding="gzip")
    _, identity_headers, identity = _wire(client, "/api/admin/health", accept_encoding="identity")

    assert status == 200
    assert coded_headers["content-encoding"] == "gzip"
    assert gzip.decompress(coded) == identity
    assert len(coded) <= len(identity), f"{len(coded)} B coded vs {len(identity)} B identity"
    assert 400 < len(identity) < 1024, "the fixture must stay under minimum_size"
    assert "content-encoding" not in identity_headers


def test_csv_export_still_decompresses_into_the_same_file():
    client = TestClient(_probe_app())

    status, headers, raw = _wire(client, "/api/export", accept_encoding="gzip")

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert headers["content-disposition"] == 'attachment; filename="fincal.csv"'
    assert gzip.decompress(raw) == CSV_BODY
    assert headers["content-length"] == str(len(raw)), "a declared length must match the coded body"


# ── The iCal feed: the most expensive repeated transfer ────────────────────

class _FakeCursor:
    def __init__(self, user):
        self._user = user

    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        return self._user

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kw):
        return self._cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *a):
        return False


def _feed_client(events):
    """The real app (real middleware order) with DB and earnings stubbed out."""
    from app import db
    from app import earnings as earnings_mod
    from app.main import app

    ical_router._ical_cache.clear()
    ical_router._content_versions.clear()
    app.dependency_overrides = {}
    cursor = _FakeCursor({"id": 1, "email": "t@t.com", "name": "T"})
    patchers = [
        patch.object(db, "db_cursor", lambda: _FakeConn(cursor)),
        patch.object(earnings_mod, "fetch_earnings_from_db", lambda **kw: list(events)),
    ]
    for p in patchers:
        p.start()
    return TestClient(app, raise_server_exceptions=False), patchers


FEED_URL = "/ical/tok-abc?scope=all"


def test_ical_feed_is_gzipped_and_decodes_to_the_identity_bytes():
    client, patchers = _feed_client(_feed_events())
    try:
        ident_status, ident_headers, ident_raw = _wire(client, FEED_URL, accept_encoding="identity")
        gz_status, gz_headers, gz_raw = _wire(client, FEED_URL, accept_encoding="gzip")
    finally:
        for p in patchers:
            p.stop()

    assert (ident_status, gz_status) == (200, 200)
    assert ident_headers["content-type"].startswith("text/calendar")
    assert "content-encoding" not in ident_headers
    assert gz_headers["content-encoding"] == "gzip"
    assert "accept-encoding" in _vary(gz_headers)
    assert gzip.decompress(gz_raw) == ident_raw
    assert ident_raw.startswith(b"BEGIN:VCALENDAR")


def test_ical_feed_wire_size_drops_below_the_issue_budget():
    client, patchers = _feed_client(_feed_events())
    try:
        _, headers, raw = _wire(client, FEED_URL, accept_encoding="gzip")
        _, _, ident_raw = _wire(client, FEED_URL, accept_encoding="identity")
    finally:
        for p in patchers:
            p.stop()

    assert abs(len(ident_raw) - FEED_RAW_BYTES) < FEED_RAW_BYTES * 0.2, (
        f"feed fixture is {len(ident_raw)} B, production measured {FEED_RAW_BYTES} B")
    assert headers["content-encoding"] == "gzip"
    # Acceptance criterion 3: 186,710 B → ≤ 20,000 B on the wire.
    assert len(raw) <= 20_000, f"feed still {len(raw)} B on the wire"
    assert len(raw) * 8 < len(ident_raw)


def test_cached_feed_is_not_rewritten_by_the_coding_layer():
    """A cache hit must be immune to what the coding layer does to the message.

    The cached entry used to be the ``Response`` object itself, and a
    ``Response`` hands its ``raw_headers`` list to the ASGI server by reference.
    The gzip layer rewrites that list in place (adds ``content-encoding``, drops
    ``content-length``), so from the second request on the process served the
    *uncompressed* feed labelled ``gzip`` and no calendar client could read it.
    """
    from app.routers.ical import _ical_cache

    client, patchers = _feed_client(_feed_events())
    try:
        _, _, cold = _wire(client, FEED_URL, accept_encoding="identity")
        # Two coded requests: the first one compresses the cold response, the
        # second is served from the cache (that is where the corruption showed).
        _, _, first_gz = _wire(client, FEED_URL, accept_encoding="gzip")
        status, headers, second_gz = _wire(client, FEED_URL, accept_encoding="gzip")
        _, ident_headers, ident = _wire(client, FEED_URL, accept_encoding="identity")
    finally:
        for p in patchers:
            p.stop()

    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(first_gz) == cold
    assert gzip.decompress(second_gz) == cold, "the cached feed must still be coded on a hit"
    assert ident == cold, "an identity client must never receive a coded body"
    assert "content-encoding" not in ident_headers
    # The stored entry is data, not a live response object.
    assert all(not hasattr(entry, "raw_headers") for entry in _ical_cache.values())


def test_ical_304_carries_the_negotiation_and_a_weak_validator():
    """The conditional answer must describe the same negotiation as the body did."""
    client, patchers = _feed_client(_feed_events())
    try:
        _, headers, _ = _wire(client, FEED_URL, accept_encoding="gzip")
        etag = headers["etag"]
        assert etag.startswith('W/"'), etag

        status, cond_headers, raw = _wire(
            client, FEED_URL, accept_encoding="gzip", extra_headers={"if-none-match": etag})
    finally:
        for p in patchers:
            p.stop()

    assert status == 304
    assert raw == b""
    assert "accept-encoding" in _vary(cond_headers)
    assert cond_headers["cache-control"].startswith("public")
    assert "content-encoding" not in cond_headers


def test_ical_identity_client_revalidates_with_the_cached_gzip_validator():
    """One validator, two codings: a gzip-minted ETag revalidates the identity variant."""
    client, patchers = _feed_client(_feed_events())
    try:
        _, headers, _ = _wire(client, FEED_URL, accept_encoding="gzip")
        etag = headers["etag"]
        ical_router._ical_cache.clear()  # force the cold path
        status, _, raw = _wire(
            client, FEED_URL, accept_encoding="identity", extra_headers={"if-none-match": etag})
    finally:
        for p in patchers:
            p.stop()

    assert status == 304, "the weak comparison must not depend on the coding that minted the ETag"
    assert raw == b""
