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
