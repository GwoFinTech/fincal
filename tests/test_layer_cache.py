"""Layer cache singleflight between the stale-while-revalidate background
refresh and the blocking path (Issue #34).

Reproduces the issue scenario: while a background refresh is still running,
a request that finds the entry fully stale used to call the fetcher a second
time, doubling upstream calls and letting the older snapshot land last.
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _entry(cache, key):
    entry = cache.get(key)
    assert entry is not None, f"no cache entry for {key!r}"
    return entry


def test_slow_background_refresh_is_joined_not_duplicated():
    """Acceptance #1: the issue repro must yield exactly one upstream call."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=0.2, stale_ttl=0.3)
    calls = []

    def slow_fetch():
        calls.append(1)
        time.sleep(0.6)
        return {"n": len(calls)}

    cache.put("k", {"init": True})
    time.sleep(0.25)  # expired -> stale-valid: triggers the background refresh
    first, _ = cache.get_or_refresh("k", slow_fetch)
    assert first == {"init": True}, "stale data must be served without blocking"

    time.sleep(0.15)  # stale_ttl exceeded while the refresh is still running
    second, entry = cache.get_or_refresh("k", slow_fetch)

    assert len(calls) == 1, f"expected one upstream call, got {len(calls)}"
    assert second == {"n": 1}, "the waiter must receive the refresh's own result"
    assert entry.data == {"n": 1}
    assert cache.is_fresh("k"), "the joined snapshot must be stored as fresh"


def test_background_refresh_still_revalidates():
    """The singleflight change must not break stale-while-revalidate itself."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=0.05, stale_ttl=5.0)

    def fetch():
        time.sleep(0.1)
        return "new"

    cache.put("k", "old")
    time.sleep(0.06)
    data, _ = cache.get_or_refresh("k", fetch)
    assert data == "old"  # stale served immediately, refresh runs in background

    assert _wait_for(lambda: _entry(cache, "k").data == "new"), "background refresh did not land"
    assert cache.is_fresh("k")


def test_concurrent_cold_start_fetches_once():
    """Concurrent misses on the same key share a single upstream call."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=60.0)
    calls = []
    results = []
    barrier = threading.Barrier(3)

    def fetcher():
        calls.append(1)
        time.sleep(0.3)
        return "data"

    def worker():
        barrier.wait(timeout=5)
        data, _ = cache.get_or_refresh("cold", fetcher)
        results.append(data)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert results == ["data", "data", "data"]
    assert len(calls) == 1, f"expected one upstream call, got {len(calls)}"


def test_waiter_is_bounded_by_waiter_timeout():
    """Waiting on an in-flight refresh is capped, then serves the stale entry."""
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=0.2, stale_ttl=0.3, waiter_timeout=0.05)
    calls = []

    def slow_fetch():
        calls.append(1)
        time.sleep(0.6)
        return "new"

    cache.put("k", "old")
    time.sleep(0.25)  # stale-valid -> background refresh takes the key
    first, _ = cache.get_or_refresh("k", slow_fetch)
    assert first == "old"

    time.sleep(0.15)  # stale_ttl exceeded while the refresh is still running
    started = time.monotonic()
    data, _ = cache.get_or_refresh("k", slow_fetch)
    elapsed = time.monotonic() - started

    assert data == "old", "a bounded wait must still serve the stale entry"
    assert elapsed < 0.4, f"waiter must respect waiter_timeout, waited {elapsed:.3f}s"
    assert len(calls) == 1


def test_late_snapshot_does_not_overwrite_newer_one():
    """Ordering tickets: an earlier-started fetch cannot overwrite newer data."""
    from app.layer_cache import LayerCache

    cache = LayerCache()
    earlier = cache.begin_fetch("k")
    later = cache.begin_fetch("k")
    assert later > earlier

    cache.put("k", "newer", ticket=later)
    returned = cache.put("k", "older", ticket=earlier)

    assert _entry(cache, "k").data == "newer"
    assert returned is not None and returned.data == "newer"
    # Unticketed writes (seeding, tests) are unaffected.
    cache.put("k", "seeded")
    assert _entry(cache, "k").data == "seeded"


def test_stale_marking_does_not_relabel_newer_snapshot():
    """A failed fetch that started earlier must not mark newer data stale."""
    from app.layer_cache import LayerCache

    cache = LayerCache()
    earlier = cache.begin_fetch("k")
    later = cache.begin_fetch("k")
    cache.put("k", "new", ticket=later)

    cache.mark_stale("k", "refresh_failed:RuntimeError", ticket=earlier)
    entry = _entry(cache, "k")
    assert entry.data == "new"
    assert entry.stale is False
    assert entry.error_code is None

    # The producer that owns the current snapshot still marks it stale.
    cache.mark_stale("k", "refresh_failed:RuntimeError", ticket=later)
    assert _entry(cache, "k").stale is True


def test_failed_background_refresh_marks_stale():
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=0.05, stale_ttl=5.0)

    def boom():
        raise RuntimeError("upstream down")

    cache.put("k", "old")
    time.sleep(0.06)
    data, _ = cache.get_or_refresh("k", boom)
    assert data == "old"

    assert _wait_for(lambda: _entry(cache, "k").stale), "failed refresh must mark the entry stale"
    entry = _entry(cache, "k")
    assert entry.data == "old"
    assert entry.error_code == "refresh_failed:RuntimeError"


def test_blocking_fetch_failure_serves_stale_data():
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=0.01, stale_ttl=0.01)

    def boom():
        raise RuntimeError("upstream down")

    cache.put("k", "old")
    time.sleep(0.03)  # fully stale -> blocking path
    data, entry = cache.get_or_refresh("k", boom)
    assert data == "old"
    assert entry.stale is True
    assert entry.error_code == "fetch_failed:RuntimeError"


def test_blocking_fetch_failure_without_stale_data_raises():
    from app.layer_cache import LayerCache

    cache = LayerCache()

    def boom():
        raise RuntimeError("upstream down")

    try:
        cache.get_or_refresh("k", boom)
    except RuntimeError as exc:
        assert "upstream down" in str(exc)
    else:
        raise AssertionError("expected the fetcher error to propagate")


def test_fresh_hit_does_not_call_fetcher():
    from app.layer_cache import LayerCache

    cache = LayerCache(default_ttl=60.0)
    calls = []

    def fetcher():
        calls.append(1)
        return "data"

    cache.put("k", "data")
    data, _ = cache.get_or_refresh("k", fetcher)
    assert data == "data"
    assert calls == []
