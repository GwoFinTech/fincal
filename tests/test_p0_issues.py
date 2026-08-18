"""Tests for P0 features: stale recovery, idempotency, stale-while-error.

Covers Issues #3, #4, #5.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── Issue #3: sync_audit idempotency key ────────────────────────────

def test_make_idempotency_key_is_stable():
    from app.sync_audit import make_idempotency_key
    k1 = make_idempotency_key("longbridge", "earnings", "full")
    k2 = make_idempotency_key("longbridge", "earnings", "full")
    assert k1 == k2
    assert len(k1) == 32


def test_make_idempotency_key_differs_for_different_inputs():
    from app.sync_audit import make_idempotency_key
    k1 = make_idempotency_key("a", "b")
    k2 = make_idempotency_key("a", "c")
    assert k1 != k2


# ── Issue #4: watchlist stale-while-error ────────────────────────────

def test_fetch_result_ok_when_no_error():
    from app.watchlist.base import FetchResult
    r = FetchResult(symbols=["AAPL.US", "0700.HK"], source="test")
    assert r.ok
    assert not r.stale


def test_fetch_result_not_ok_when_error():
    from app.watchlist.base import FetchResult
    r = FetchResult(symbols=[], source="test", error_code="unavailable")
    assert not r.ok
    assert r.unavailable


def test_base_source_returns_stale_on_failure():
    """When fetch_symbols raises, get_symbols_with_status returns stale cache."""
    from app.watchlist.base import WatchlistSource

    class FailingSource(WatchlistSource):
        _should_fail = False

        def fetch_symbols(self):
            if self._should_fail:
                raise ConnectionError("test failure")
            return ["AAPL.US"]

    src = FailingSource()
    # First call succeeds
    r1 = src.get_symbols_with_status(force_refresh=True)
    assert r1.ok
    assert r1.symbols == ["AAPL.US"]
    assert not r1.stale

    # Second call fails — should return stale data
    src._should_fail = True
    r2 = src.get_symbols_with_status(force_refresh=True)
    assert r2.stale
    assert r2.symbols == ["AAPL.US"]  # preserved from last success
    assert r2.error_code is not None


def test_base_source_returns_empty_when_never_succeeded():
    """When fetch_symbols raises on first call, returns empty with error."""
    from app.watchlist.base import WatchlistSource

    class AlwaysFail(WatchlistSource):
        def fetch_symbols(self):
            raise ConnectionError("always fails")

    src = AlwaysFail()
    r = src.get_symbols_with_status(force_refresh=True)
    assert r.symbols == []
    assert r.error_code == "unavailable"
    assert not r.stale


def test_get_symbols_delegates_to_status():
    """get_symbols() should return symbols even when stale."""
    from app.watchlist.base import WatchlistSource

    class Flaky(WatchlistSource):
        _calls = 0
        def fetch_symbols(self):
            self._calls += 1
            if self._calls > 1:
                raise ConnectionError("fail after first")
            return ["TSLA.US"]

    src = Flaky()
    # First: success
    assert src.get_symbols(force_refresh=True) == ["TSLA.US"]
    # Second: fails but returns stale
    symbols = src.get_symbols(force_refresh=True)
    assert symbols == ["TSLA.US"]


def test_combined_preserves_upstream_stale_data():
    """CombinedWatchlistSource preserves upstream stale data on failure."""
    from app.watchlist.base import WatchlistSource
    from app.watchlist.managed_source import CombinedWatchlistSource

    class FlakyUpstream(WatchlistSource):
        _fail = False
        def fetch_symbols(self):
            if self._fail:
                raise ConnectionError("upstream down")
            return ["AAPL.US", "MSFT.US"]

    class EmptyLocal(WatchlistSource):
        def fetch_symbols(self):
            return []

    upstream = FlakyUpstream()
    combined = CombinedWatchlistSource(upstream, EmptyLocal())

    # First call — upstream works
    r1 = combined.get_symbols_with_status(force_refresh=True)
    assert "AAPL.US" in r1.symbols
    assert not r1.stale

    # Second call — upstream fails
    upstream._fail = True
    r2 = combined.get_symbols_with_status(force_refresh=True)
    assert "AAPL.US" in r2.symbols  # stale data preserved
    assert r2.stale
    assert r2.error_code is not None


# ── Issue #4: company_name result ───────────────────────────────────

def test_name_result_ok():
    from app.company_name import NameResult
    r = NameResult(name="Apple Inc.", source="kurumi")
    assert r.ok
    assert not r.unavailable


def test_name_result_unavailable():
    from app.company_name import NameResult
    r = NameResult(name="", source="", error_code="all_sources_failed")
    assert not r.ok
    assert r.unavailable


def test_resolve_prefers_first_success(monkeypatch):
    from app import company_name as MOD

    def fake_kurumi(s, m):
        return "腾讯控股"

    monkeypatch.setattr(MOD, "fetch_from_kurumi", fake_kurumi)
    monkeypatch.setattr(MOD, "fetch_from_longbridge", lambda s, m: "TENCENT")
    result = MOD.resolve_company_name_result("0700.HK", "HK")
    assert result.name == "腾讯控股"
    assert result.source == "kurumi"
    assert result.ok


# ── Issue #5: idempotency ───────────────────────────────────────────

def test_idempotency_key_format():
    from app.sync_audit import make_idempotency_key
    key = make_idempotency_key("longbridge:earnings:full")
    assert isinstance(key, str)
    assert len(key) == 32  # sha256 hex[:32]
