"""Issue #145: the signed Futu WebAPI transport that replaced the OpenD SDK.

Everything here is offline: a fake session records the signed request, and a
throwaway Ed25519 key proves the signature covers what it claims to. The live
endpoint is exercised once by the deploy probe, not by the suite.

What is pinned:

* the signature covers method + path + query + body digest, and verifies against
  the key pair (a changed path, query, body or timestamp must break it);
* provider answers map onto the sync's vocabulary — ``-7``/``-10`` unsupported,
  ``-11``/429/439 rate limited, anything else a plain error — because the stage
  classifies those strings into symbol failures;
* the three adapters keep the shapes the pipeline reads (report_list / item_list
  / rows), without pandas;
* a metric is resolved by **label**, including the issuer structures whose ids
  are not the ones the OpenD SDK used (a bank's ``id 1`` is 净利息收入), and never
  by a substring that would take ``Cost of Revenue`` or ``Dividend Per Share``.
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, futu_webapi  # noqa: E402
from app.futu_webapi import (  # noqa: E402
    EPS_LABELS,
    REVENUE_LABELS,
    FutuWebClient,
    FutuWebError,
    FutuWebThrottled,
    select_statement_field,
)


# ── fixtures ───────────────────────────────────────────────────────────────
class _Response:
    def __init__(self, payload=None, status=200, text=None):
        self.status_code = status
        self._payload = payload
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


class _Session:
    """Records every prepared request instead of sending it."""

    def __init__(self, response):
        self.response = response
        self.requests: list[dict] = []
        self.closed = False
        self.proxies: dict[str, str] = {}

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture()
def key_path(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    path = tmp_path / "futu_ed25519"
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return path, key.public_key()


@pytest.fixture()
def client(key_path, monkeypatch):
    path, public = key_path
    monkeypatch.setattr(config, "FUTU_API_KEY", "test-key")
    monkeypatch.setattr(config, "FUTU_API_SIGN_KEY", str(path))
    monkeypatch.setattr(config, "FUTU_ENV_FILE", "")
    monkeypatch.setattr(config, "FUTU_HTTP_PROXY", "")
    monkeypatch.setattr(config, "FUTU_API_BASE", "https://webapi.example/api/v1.0/quote")
    session = _Session(_Response({"ret_code": 0, "data": {}}))
    built = FutuWebClient(session=session)
    built.public_key = public
    return built


def _verified(client, request: dict) -> bool:
    """True when the request's Authorization verifies against the key pair."""
    headers = request["headers"]
    from urllib.parse import urlsplit

    parts = urlsplit(request["url"])
    body = request.get("data") or b""
    digest = hashlib.sha256(body).hexdigest() if body else ""
    message = "\n".join((headers["X-Timestamp"], request["method"], parts.path,
                         parts.query, digest)).encode()
    try:
        client.public_key.verify(base64.b64decode(headers["Authorization"]), message)
        return True
    except Exception:  # noqa: BLE001 - any verification failure is "not signed"
        return False


# ── signing ────────────────────────────────────────────────────────────────
class TestSigning:
    def test_a_get_is_signed_over_method_path_query(self, client):
        client.request("US.AAPL/financials/statements",
                       params={"statement_type": 1, "num": 2})
        request = client.session.requests[-1]
        assert request["method"] == "GET"
        assert request["url"].endswith(
            "US.AAPL/financials/statements?statement_type=1&num=2")
        assert _verified(client, request)
        assert request["headers"]["X-Api-Key"] == "test-key"
        assert request["headers"]["X-Nonce"]

    def test_a_post_signs_the_body_digest(self, client):
        client.request("stock-basicinfo", body={"code_list": ["US.AAPL"]})
        request = client.session.requests[-1]
        assert request["method"] == "POST"
        assert request["data"] == json.dumps({"code_list": ["US.AAPL"]}).encode()
        assert request["headers"]["Content-Type"] == "application/json"
        assert _verified(client, request)

    def test_changing_the_query_breaks_the_signature(self, client):
        """The query is part of the signed message, not an afterthought."""
        client.request("US.AAPL/financials/statements", params={"num": 1})
        request = dict(client.session.requests[-1])
        request["url"] = request["url"].replace("num=1", "num=2")
        assert not _verified(client, request)

    def test_the_timestamp_is_milliseconds(self, client):
        client.request("US.AAPL/financials/statements")
        stamp = client.session.requests[-1]["headers"]["X-Timestamp"]
        assert stamp.isdigit() and len(stamp) == 13


# ── provider answers → the sync's vocabulary ─────────────────────────────
class TestErrorMapping:
    """The stage classifies on message substrings, so these spellings matter."""

    def _client_with(self, key_path, monkeypatch, response):
        path, _public = key_path
        monkeypatch.setattr(config, "FUTU_API_KEY", "k")
        monkeypatch.setattr(config, "FUTU_API_SIGN_KEY", str(path))
        monkeypatch.setattr(config, "FUTU_ENV_FILE", "")
        monkeypatch.setattr(config, "FUTU_HTTP_PROXY", "")
        return FutuWebClient(session=_Session(response))

    def test_http_429_is_a_throttle(self, key_path, monkeypatch):
        client = self._client_with(key_path, monkeypatch, _Response({}, status=429))
        with pytest.raises(FutuWebThrottled):
            client.request("US.AAPL/snapshot")

    def test_ret_code_minus_11_is_a_throttle(self, key_path, monkeypatch):
        client = self._client_with(key_path, monkeypatch, _Response(
            {"ret_code": -11, "ret_msg": "frequency too high"}, status=200))
        with pytest.raises(FutuWebThrottled):
            client.request("US.AAPL/snapshot")

    @pytest.mark.parametrize("code", [-7, -10])
    def test_invalid_symbol_and_no_data_are_unsupported(self, key_path, monkeypatch, code):
        client = self._client_with(key_path, monkeypatch, _Response(
            {"ret_code": code, "ret_msg": "invalid symbol"}, status=200))
        with pytest.raises(FutuWebError) as excinfo:
            client.request("HK.02217/financials/statements")
        assert excinfo.value.unsupported

    def test_non_json_is_an_error(self, key_path, monkeypatch):
        client = self._client_with(key_path, monkeypatch, _Response(text="<html>", status=502))
        with pytest.raises(FutuWebError):
            client.request("US.AAPL/snapshot")

    def test_the_adapter_returns_the_sdk_tuple_on_failure(self, key_path, monkeypatch):
        """``futu_call`` expects ``(ret, message)``; a raise would skip its retry."""
        client = self._client_with(key_path, monkeypatch, _Response(
            {"ret_code": -7, "ret_msg": "invalid symbol"}, status=200))
        ret, data = client.get_financials_earnings_price_history("HK.02217")
        assert ret == -1
        assert "invalid symbol" in data


# ── adapters ───────────────────────────────────────────────────────────────
class TestAdapters:
    def test_earnings_history_rows_are_plain_and_typed(self, client):
        client.session.response = _Response({"ret_code": 0, "data": {"records": [
            {"fiscal_year": "2026", "financial_type": "3", "pub_type": "2",
             "pub_trading_day_str": "2026-11-18", "period_text": "2026/Q3"},
            {"fiscal_year": None, "financial_type": 4, "pub_trading_day_str": "2027-03-17"},
        ]}})
        ret, rows = client.get_financials_earnings_price_history("HK.00700")
        assert ret == 0
        assert rows[0]["fiscal_year"] == 2026 and rows[0]["financial_type"] == 3
        assert rows[0]["pub_type"] == 2
        assert rows[1]["fiscal_year"] is None

    def test_statements_keep_the_report_list_contract(self, client):
        payload = {"report_list": [{"fiscal_year": 2026, "financial_type": 4,
                                    "currency_code": "USD",
                                    "accounting_standards": "US_GAAP",
                                    "item_list": [{"field_id": 47, "display_name": "Basic EPS",
                                                   "data": 2.11}]}]}
        client.session.response = _Response({"ret_code": 0, "data": payload,
                                            "pagination": {"next_key": "x"}})
        ret, data = client.get_financials_statements("US.AAPL", statement_type=1,
                                                     financial_type=9, num=2)
        assert ret == 0
        assert data["report_list"] == payload["report_list"]
        assert data["next_key"] == "x"
        query = client.session.requests[-1]["url"]
        assert "statement_type=1" in query and "financial_type=9" in query and "num=2" in query

    def test_basicinfo_returns_rows(self, client):
        client.session.response = _Response({"ret_code": 0, "data": {"basic_list": [
            {"code": "US.AAPL", "name": "Apple", "sc_name": "苹果"}]}})
        ret, rows = client.get_stock_basicinfo("US", "STOCK", ["US.AAPL"])
        assert ret == 0 and rows[0]["sc_name"] == "苹果"


# ── label resolution (the ids differ per issuer structure) ────────────────
def _report(*items):
    return {"item_list": [{"field_id": fid, "display_name": label, "data": data}
                          for fid, label, data in items]}


class TestLabelResolution:
    def test_us_structure(self):
        """NORMAL_MSTAR: revenue 1/2, Basic EPS 47, Diluted 48."""
        report = _report((1, "Total Revenue as Reported", 114477000000),
                         (2, "Total Operating Revenue", 114477000000),
                         (3, "Cost of Revenue", 59657000000),
                         (47, "Basic EPS", 2.11), (48, "Diluted EPS", 2.10),
                         (49, "Dividend Per Share", 0.27))
        assert select_statement_field(report, REVENUE_LABELS).value == 114477000000
        eps = select_statement_field(report, EPS_LABELS)
        assert (eps.value, eps.field_id) == (2.11, 47)

    def test_hk_structure(self):
        """NORMAL_HK: revenue 1, Basic EPS 54 — the SDK's 5001/5054 do not exist."""
        report = _report((1, "Total Revenue", 207336000000),
                         (5, "Cost of Revenue", 91425000000),
                         (54, "Basic EPS", 7.059), (55, "Diluted EPS", 6.947))
        assert select_statement_field(report, REVENUE_LABELS).value == 207336000000
        assert select_statement_field(report, EPS_LABELS).value == 7.059

    def test_a_bank_statement_does_not_take_net_interest_income_for_revenue(self):
        """BANK_HK: id 1 is 净利息收入, operating revenue is id 15."""
        report = _report((1, "Net Interest Income", 9307000000),
                         (15, "Total Operating Revenue", 18620000000),
                         (61, "Basic EPS", 0.41), (62, "Diluted EPS", 0.41),
                         (63, "Dividend Per Share", 0.10))
        assert select_statement_field(report, REVENUE_LABELS).value == 18620000000
        assert select_statement_field(report, EPS_LABELS).value == 0.41

    def test_cost_of_revenue_and_dividend_per_share_are_never_selected(self):
        report = _report((3, "Cost of Revenue", 1.0), (49, "Dividend Per Share", 9.9))
        assert select_statement_field(report, REVENUE_LABELS).value is None
        assert select_statement_field(report, EPS_LABELS).value is None

    def test_chinese_labels_from_the_sdk_surface_still_resolve(self):
        report = _report((5001, "营业总收入", 192869000000.0),
                         (5054, "基本每股收益", 6.952))
        assert select_statement_field(report, REVENUE_LABELS).value == 192869000000.0
        assert select_statement_field(report, EPS_LABELS).value == 6.952

    def test_a_null_value_is_skipped_not_written_as_zero(self):
        report = _report((1, "Total Revenue", None), (2, "Total Operating Revenue", 5.0))
        assert select_statement_field(report, REVENUE_LABELS).value == 5.0


# ── construction / probe ──────────────────────────────────────────────────
class TestConstruction:
    def test_missing_credentials_yield_no_client(self, monkeypatch):
        monkeypatch.setattr(config, "FUTU_API_KEY", "")
        monkeypatch.setattr(config, "FUTU_API_SIGN_KEY", "")
        monkeypatch.setattr(config, "FUTU_ENV_FILE", "")
        assert futu_webapi.create_client() is None

    def test_env_file_supplies_the_key_pair(self, key_path, tmp_path, monkeypatch):
        path, _public = key_path
        env_file = tmp_path / "futu.env"
        env_file.write_text(f"FUTU_API_KEY=from-file\nFUTU_API_SIGN_KEY={path}\n")
        monkeypatch.setattr(config, "FUTU_API_KEY", "")
        monkeypatch.setattr(config, "FUTU_API_SIGN_KEY", "")
        monkeypatch.setattr(config, "FUTU_ENV_FILE", str(env_file))
        monkeypatch.setattr(config, "FUTU_HTTP_PROXY", "")
        client = FutuWebClient(session=_Session(_Response({"ret_code": 0, "data": {}})))
        assert client.api_key == "from-file"
        assert client.sign_key_path == str(path)

    def test_probe_reports_a_failure_instead_of_raising(self, client):
        client.session.response = _Response({"ret_code": -11, "ret_msg": "slow down"})
        probe = client.probe()
        assert probe["ok"] is False and probe["code"] == "rate_limited"

    def test_probe_reports_latency_when_healthy(self, client):
        probe = client.probe()
        assert probe["ok"] is True and probe["code"] == "ok"
