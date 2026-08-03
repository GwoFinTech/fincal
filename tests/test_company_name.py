"""Regression coverage for company-name resolution priority and caching."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "company_name", ROOT / "app" / "company_name.py"
)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_kurumi_symbol_normalizes_hk_padding():
    assert MOD.kurumi_symbol("0700.HK", "HK") == "700.HK"
    assert MOD.kurumi_symbol("0001.HK", "HK") == "1.HK"
    assert MOD.kurumi_symbol("AAPL", "US") == "AAPL"
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


def test_resolve_falls_back_when_kurumi_empty(monkeypatch):
    monkeypatch.setattr(MOD, "fetch_from_kurumi", lambda s, m: "")
    monkeypatch.setattr(MOD, "fetch_from_longbridge", lambda s, m: "TENCENT")
    name, source = MOD.resolve_company_name("0700.HK", "HK")
    assert (name, source) == ("TENCENT", "longbridge")


def test_resolve_returns_empty_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(MOD, "fetch_from_kurumi", lambda s, m: "")
    monkeypatch.setattr(MOD, "fetch_from_longbridge", lambda s, m: "")
    monkeypatch.setattr(MOD, "fetch_from_futu", lambda s, m: "")
    assert MOD.resolve_company_name("ZZZZ", "US") == ("", "")
