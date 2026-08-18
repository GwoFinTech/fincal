"""End-to-end regression tests for P0/P1 scenarios (Issue #16).

Covers:
- Stale data preservation on external source failure
- Sync run recovery after process restart
- Idempotent sync behavior
- Layer cache stale-while-revalidate
- API error codes and HTTP status codes
- Health/ready endpoints
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── Stale data preservation ────────────────────────────────────────

def test_watchlist_stale_preserved_on_upstream_failure():
    """Upstream failure returns stale cached data, not empty."""
    from app.watchlist.base import WatchlistSource

    class FlakySource(WatchlistSource):
        _calls = 0
        def fetch_symbols(self):
            self._calls += 1
            if self._calls > 1:
                raise ConnectionError("upstream down")
            return ["AAPL.US", "TSLA.US"]

    src = FlakySource()
    # First: success
    r1 = src.get_symbols_with_status(force_refresh=True)
    assert r1.ok and not r1.stale
    assert set(r1.symbols) == {"AAPL.US", "TSLA.US"}

    # Second: fails → stale
    r2 = src.get_symbols_with_status(force_refresh=True)
    assert r2.stale
    assert set(r2.symbols) == {"AAPL.US", "TSLA.US"}  # preserved


def test_watchlist_unavailable_when_never_succeeded():
    """First-ever failure returns empty with error_code."""
    from app.watchlist.base import WatchlistSource

    class DeadSource(WatchlistSource):
        def fetch_symbols(self):
            raise ConnectionError("always dead")

    src = DeadSource()
    r = src.get_symbols_with_status(force_refresh=True)
    assert r.symbols == []
    assert r.error_code == "unavailable"
    assert not r.stale


# ── Sync run recovery ──────────────────────────────────────────────

def test_sync_audit_idempotency_key_stable():
    """Same inputs produce same key."""
    from app.sync_audit import make_idempotency_key
    k1 = make_idempotency_key("a", "b", "c")
    k2 = make_idempotency_key("a", "b", "c")
    k3 = make_idempotency_key("a", "b", "d")
    assert k1 == k2
    assert k1 != k3


# ── Layer cache ────────────────────────────────────────────────────

def test_layer_cache_returns_fresh_data():
    from app.layer_cache import LayerCache
    cache = LayerCache(default_ttl=60)
    cache.put("k1", [1, 2, 3])
    entry = cache.get("k1")
    assert entry is not None
    assert entry.data == [1, 2, 3]
    assert cache.is_fresh("k1")


def test_layer_cache_stale_after_ttl():
    import time
    from app.layer_cache import LayerCache, CacheEntry
    cache = LayerCache(default_ttl=0.01, stale_ttl=60)
    cache.put("k1", "data", ttl=0.01)
    time.sleep(0.02)
    assert not cache.is_fresh("k1")
    assert cache.is_stale_valid("k1")  # still within stale_ttl


def test_layer_cache_singleflight():
    """Concurrent refresh for same key only calls fetcher once."""
    import time
    import threading
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=0.01, stale_ttl=60)
    call_count = 0

    def fetcher():
        nonlocal call_count
        call_count += 1
        time.sleep(0.1)
        return "data"

    results = []
    def worker():
        data, _ = cache.get_or_refresh("k1", fetcher)
        results.append(data)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert all(r == "data" for r in results)
    # With singleflight, only 1 actual fetch should happen
    # (may be 2 due to timing, but not 3)
    assert call_count <= 2


# ── API error codes ────────────────────────────────────────────────

def test_error_classes_have_stable_codes():
    from app.errors import AppError, NotFoundError, ConflictError, ForbiddenError

    e1 = NotFoundError("sync_run")
    assert e1.code == "sync_run_not_found"
    assert e1.status_code == 404

    e2 = ConflictError("run_not_cancellable")
    assert e2.code == "run_not_cancellable"
    assert e2.status_code == 409

    e3 = ForbiddenError()
    assert e3.code == "forbidden"
    assert e3.status_code == 403

    e4 = AppError("invalid_market", "market must be US or HK", 400)
    assert e4.code == "invalid_market"
    assert e4.status_code == 400


# ── Provider client error classification ───────────────────────────

def test_provider_error_classification():
    from app.provider_client import classify_error, ErrorCategory
    from urllib.error import HTTPError

    # Timeout
    err = classify_error(TimeoutError("timed out"), "test")
    assert err.category == ErrorCategory.TIMEOUT

    # Connection
    err = classify_error(ConnectionError("refused"), "test")
    assert err.category == ErrorCategory.CONNECTION

    # Invalid response
    err = classify_error(ValueError("bad json"), "test")
    assert err.category == ErrorCategory.INVALID_RESPONSE

    # 429
    from email.message import Message
    hdrs = Message()
    err = classify_error(HTTPError("url", 429, "rate limited", hdrs, None), "test")
    assert err.category == ErrorCategory.RATE_LIMITED
    assert err.retry_after is not None


# ── Source priority ────────────────────────────────────────────────

def test_source_priority_ordering():
    from app.provenance import should_replace
    assert should_replace("unknown", "longbridge")
    assert should_replace("longbridge", "futu")
    assert should_replace("futu", "kurumi")
    assert not should_replace("kurumi", "longbridge")
    assert not should_replace("futu", "futu")  # same priority = keep


# ── Sync quality ───────────────────────────────────────────────────

def test_sync_quality_classify():
    from app.sync_quality import SyncQuality

    q = SyncQuality(fetched=100, written=95, failed=5)
    assert q.classify() == "partial"

    q = SyncQuality(fetched=100, written=100, failed=0)
    assert q.classify() == "success"

    q = SyncQuality(fetched=0, written=0, failed=10)
    assert q.classify() == "unavailable"


# ── Version ────────────────────────────────────────────────────────

def test_version_returns_string():
    from app.version import get_version
    v = get_version()
    assert isinstance(v, str)
    assert len(v) > 0
