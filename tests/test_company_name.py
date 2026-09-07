"""Regression coverage for company-name resolution priority and caching."""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import company_name as MOD  # noqa: E402


def test_kurumi_symbol_normalizes_hk_padding():
    assert MOD.kurumi_symbol("0700.HK", "HK") == "700.HK"
    assert MOD.kurumi_symbol("0001.HK", "HK") == "1.HK"
    assert MOD.kurumi_symbol("AAPL", "US") == "AAPL.US"
    assert MOD.kurumi_symbol("9988.HK", "HK") == "9988.HK"


def test_resolve_prefers_kurumi_then_longbridge(monkeypatch):
    calls = []

    def fake_kurumi(symbol, market):
        calls.append(("kurumi", symbol))
        return "腾讯控股"

    monkeypatch.setattr(MOD, "fetch_from_kurumi", fake_kurumi)
    monkeypatch.setattr(MOD, "fetch_from_longbridge", lambda s, m: "TENCENT")
    name, source = MOD.resolve_company_name("0700.HK", "HK")
    assert (name, source) == ("腾讯控股", "kurumi")
    assert calls == [("kurumi", "0700.HK")]


def test_resolve_falls_back_when_kurumi_raises(monkeypatch):
    """Issue #4: fetchers now raise on failure instead of returning empty."""
    def fake_kurumi(s, m):
        raise ConnectionError("kurumi down")

    monkeypatch.setattr(MOD, "fetch_from_kurumi", fake_kurumi)
    monkeypatch.setattr(MOD, "fetch_from_longbridge", lambda s, m: "TENCENT")
    name, source = MOD.resolve_company_name("0700.HK", "HK")
    assert (name, source) == ("TENCENT", "longbridge")


def test_resolve_returns_empty_when_all_sources_fail(monkeypatch):
    def fail(s, m):
        raise ConnectionError("fail")

    monkeypatch.setattr(MOD, "fetch_from_kurumi", fail)
    monkeypatch.setattr(MOD, "fetch_from_longbridge", fail)
    monkeypatch.setattr(MOD, "fetch_from_futu", fail)
    assert MOD.resolve_company_name("ZZZZ", "US") == ("", "")


def test_resolve_result_returns_error_metadata(monkeypatch):
    """Issue #4: resolve_company_name_result returns NameResult with error_code."""
    def fail(s, m):
        raise ConnectionError("fail")

    monkeypatch.setattr(MOD, "fetch_from_kurumi", fail)
    monkeypatch.setattr(MOD, "fetch_from_longbridge", fail)
    monkeypatch.setattr(MOD, "fetch_from_futu", fail)
    result = MOD.resolve_company_name_result("ZZZZ", "US")
    assert not result.ok
    assert result.unavailable
    assert result.error_code == "all_sources_failed"


def test_resolve_result_returns_ok_on_success(monkeypatch):
    monkeypatch.setattr(MOD, "fetch_from_kurumi", lambda s, m: "Apple Inc.")
    result = MOD.resolve_company_name_result("AAPL", "US")
    assert result.ok
    assert result.name == "Apple Inc."
    assert result.source == "kurumi"


def _install_fake_futu(monkeypatch, name):
    """Install a fake `futu` bindings so the REAL `fetch_from_futu` code-assembly
    path runs without a live OpenD connection, capturing the code/market passed
    to get_stock_basicinfo. Exercises the actual symbol→code conversion (Issue #41)."""
    from types import SimpleNamespace
    import futu as futu_mod

    captured = {"code_list": None, "market": None}

    class _Row:
        def __init__(self, n): self._n = n
        def get(self, key, default=None):
            return self._n if key == "name" else default

    class _Data:
        def __init__(self, n):
            self.empty = False
            self._row = _Row(n)
        @property
        def iloc(self):
            return [self._row]

    class _FakeCtx:
        def __init__(self, host, port):
            self.host = host
            self.port = port
        def get_stock_basicinfo(self, market, sec_type, code_list):
            captured["market"] = market
            captured["code_list"] = list(code_list)
            return (0, _Data(name))
        def close(self):
            pass

    monkeypatch.setattr(futu_mod, "RET_OK", 0)
    monkeypatch.setattr(futu_mod, "Market", SimpleNamespace(HK="HK", US="US"))
    monkeypatch.setattr(futu_mod, "SecurityType", SimpleNamespace(STOCK="STOCK"))
    monkeypatch.setattr(futu_mod, "OpenQuoteContext", _FakeCtx)
    return captured


def test_fetch_from_futu_builds_canonical_hk_code(monkeypatch):
    """Issue #41: HK rows already carry .HK, so we must NOT append a second
    suffix. 0700.HK → HK.00700 (not the old broken HK.0700.HK)."""
    captured = _install_fake_futu(monkeypatch, "Tencent Holdings")
    result = MOD.fetch_from_futu("0700.HK", "HK")
    assert result == "Tencent Holdings"
    assert captured["code_list"] == ["HK.00700"]
    assert captured["market"] == "HK"


def test_fetch_from_futu_builds_canonical_us_code(monkeypatch):
    """Issue #41: US rows are bare tickers and must gain the .US suffix.
    AAPL → US.AAPL (not the old broken bare AAPL)."""
    captured = _install_fake_futu(monkeypatch, "Apple Inc.")
    result = MOD.fetch_from_futu("AAPL", "US")
    assert result == "Apple Inc."
    assert captured["code_list"] == ["US.AAPL"]
    assert captured["market"] == "US"


def test_fetch_from_futu_keeps_existing_us_suffix(monkeypatch):
    """Robustness: a US symbol that already carries .US is not double-suffixed."""
    captured = _install_fake_futu(monkeypatch, "Apple Inc.")
    MOD.fetch_from_futu("AAPL.US", "US")
    assert captured["code_list"] == ["US.AAPL"]


def test_resolve_falls_back_to_futu_when_others_fail(monkeypatch):
    """Issue #41 acceptance #3: with Kurumi/Longbridge down, Futu (via the
    corrected code) resolves the name through the existing fallback chain."""
    def fail(s, m):
        raise ConnectionError("source down")

    monkeypatch.setattr(MOD, "fetch_from_kurumi", fail)
    monkeypatch.setattr(MOD, "fetch_from_longbridge", fail)
    _install_fake_futu(monkeypatch, "Apple Inc.")
    name, source = MOD.resolve_company_name("AAPL", "US")
    assert (name, source) == ("Apple Inc.", "futu")