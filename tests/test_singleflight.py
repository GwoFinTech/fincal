"""Tests for singleflight (Issue #8)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_singleflight_returns_result():
    from app.singleflight import Singleflight
    sf = Singleflight()
    result = sf.do("key1", lambda: "hello")
    assert result == "hello"


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
