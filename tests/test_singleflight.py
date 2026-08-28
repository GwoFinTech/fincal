"""Tests for singleflight (Issue #8)."""
import sys
import threading
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


# ── Issue #32: waiter must not silently receive None on leader timeout ──

def test_singleflight_waiter_times_out_raises():
    """Acceptance #1: leader exceeding waiter timeout raises, not None."""
    import time
    from app.singleflight import Singleflight
    sf = Singleflight()

    def slow_leader():
        time.sleep(0.5)  # exceeds the waiter's timeout
        return {"ok": True}

    threading.Thread(target=lambda: sf.do("tk", slow_leader), daemon=True).start()
    time.sleep(0.05)  # ensure the leader is registered before we join

    start = time.monotonic()
    try:
        sf.do("tk", lambda: {"never": True}, timeout=0.1)
    except TimeoutError as exc:
        assert time.monotonic() - start < 1.0, "waiter should time out promptly"
        assert "tk" in str(exc), f"timeout message should mention the key: {exc}"
    else:
        raise AssertionError("expected TimeoutError when leader exceeds waiter timeout")


def test_singleflight_leader_returning_none_is_not_timeout():
    """A leader that genuinely returns None must NOT be treated as a timeout."""
    import time
    from app.singleflight import Singleflight
    sf = Singleflight()

    def none_leader():
        time.sleep(0.1)
        return None

    threading.Thread(target=lambda: sf.do("nk", none_leader), daemon=True).start()
    time.sleep(0.05)

    result = sf.do("nk", lambda: {"never": True}, timeout=2.0)
    assert result is None


def test_singleflight_waiter_receives_leader_real_result():
    """Waiter must get the leader's real value, not be lost to cleanup."""
    import time
    from app.singleflight import Singleflight
    sf = Singleflight()

    def slow_leader():
        time.sleep(0.15)
        return {"ok": True}

    threading.Thread(target=lambda: sf.do("vk", slow_leader), daemon=True).start()
    time.sleep(0.05)

    result = sf.do("vk", lambda: {"never": True}, timeout=2.0)
    assert result == {"ok": True}
