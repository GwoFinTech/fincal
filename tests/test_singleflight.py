"""Tests for singleflight (Issue #8)."""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_singleflight_deduplicates_concurrent_calls():
    """Multiple callers for same key only execute fn once."""
    from app.singleflight import Singleflight

    sf = Singleflight()
    call_count = 0

    def slow_fn():
        nonlocal call_count
        call_count += 1
        time.sleep(0.1)
        return "result"

    results = []
    errors = []

    def caller():
        try:
            r = sf.do("key1", slow_fn)
            results.append(r)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=caller) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 5
    assert all(r == "result" for r in results)
    assert call_count == 1  # only one actual call
    assert len(errors) == 0


def test_singleflight_different_keys_run_independently():
    from app.singleflight import Singleflight

    sf = Singleflight()
    call_count = 0

    def counting_fn():
        nonlocal call_count
        call_count += 1
        return call_count

    r1 = sf.do("a", counting_fn)
    r2 = sf.do("b", counting_fn)
    assert r1 == 1
    assert r2 == 2


def test_singleflight_propagates_errors():
    from app.singleflight import Singleflight

    sf = Singleflight()

    def failing():
        raise ValueError("boom")

    errors = []
    for _ in range(3):
        try:
            sf.do("fail_key", failing)
        except ValueError as e:
            errors.append(str(e))

    assert all(e == "boom" for e in errors)
