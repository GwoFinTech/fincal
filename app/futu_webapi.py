"""Signed Futu WebAPI transport — the replacement for the OpenD SDK.

FinCal used to talk to a local Futu OpenD over the ``futu-api`` protobuf SDK. That
transport needs a long-lived daemon on the same host, an SSH tunnel for the CN
endpoint and a per-call ``SIGALRM`` watchdog, and it cannot run anywhere the
daemon isn't. The signed WebAPI (the same one tsummt-web moved to) answers the
two F10 reads this app needs over plain HTTPS:

* ``{code}/financials/earnings-price-history`` → every historical report date;
* ``{code}/financials/statements`` → income statements (revenue, EPS, currency,
  accounting standard).

Authentication is an Ed25519 signature over ``timestamp\\nMETHOD\\npath\\nquery\\n
sha256(body)`` sent as ``Authorization`` next to ``X-Api-Key``/``X-Timestamp``/
``X-Nonce``.

**The statement schema is not the SDK's.** OpenD addressed revenue/EPS by fixed
F10 ids (US 8002/8047/8048, HK 5001/5054/5055); the WebAPI numbers its income
statement ``1..N`` and the numbering *moves with the issuer's ``structure``*
(``NORMAL_HK`` puts revenue at 1 and EPS at 54, ``BANK_HK`` puts warehouse
revenue at 15 and EPS at 61 while id 1 is 净利息收入). So a metric is resolved by
**label**, in a fixed preference order, and never by a hard-coded id —
:func:`select_statement_field` is that resolver, and it keeps the #62 guard's
spirit: an unrecognised label yields no value instead of a number that measures
something else.

Return shapes mirror the SDK so the sync pipeline (rate limiter, watchdog,
outcome classification, audits) is unchanged:
``(ret, data)`` with ``ret == 0`` on success and ``(ret, "<message>")`` on failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from . import config

logger = logging.getLogger(__name__)

__all__ = [
    "FutuWebError",
    "FutuWebThrottled",
    "FutuWebClient",
    "select_statement_field",
    "REVENUE_LABELS",
    "EPS_LABELS",
]


class FutuWebError(RuntimeError):
    """Upstream answered with an HTTP or quote-level error."""

    def __init__(self, message: str, *, code: int | None = None,
                 status: int | None = None, unsupported: bool = False,
                 throttled: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.unsupported = unsupported
        self.throttled = throttled


class FutuWebThrottled(FutuWebError):
    """Temporary rejection of a signed request (quota window spent)."""


#: Provider codes seen in production that mean 「this instrument has no such
#: data」 rather than 「the request was wrong」: ``-7 invalid symbol`` (a delisted
#: or unknown code) and ``-10 no data`` (an ETF, an index, a ticker whose F10
#: page Futu does not publish).  They are counted as ``unsupported`` so a whole
#: watchlist is not reported as failing.
UNSUPPORTED_CODES = frozenset({-7, -10})
THROTTLED_CODES = frozenset({-11, -12})

#: Revenue labels in preference order. ``Total Revenue as Reported`` and
#: ``Total Operating Revenue`` carry the same figure on a US filing; HK uses
#: ``Total Revenue``/``Operating Revenue``, and a bank row's id 1 is 净利息收入, so
#: the bank statement's ``Total Operating Revenue`` is preferred over anything
#: that merely contains the word.
REVENUE_LABELS = (
    "total revenue as reported",
    "total revenue",
    "total operating revenue",
    "operating revenue",
    "revenue",
    # The provider localises ``display_name``: the OpenD SDK answered in Chinese
    # and the WebAPI answers in English for the same statement, so both sets are
    # accepted (a locale header would otherwise silently change what is read).
    "营业总收入",
    "营业收入",
    "营业额",
)

#: EPS labels in preference order: basic before diluted, exactly like the SDK
#: path did. ``Dividend Per Share`` is deliberately absent — it is the field a
#: substring match would wrongly accept.
EPS_LABELS = (
    "basic eps", "diluted eps", "eps",
    "基本每股收益", "稀释每股收益", "每股收益", "每股盈利",
)


@dataclass(frozen=True)
class StatementField:
    """One statement value resolved from a labelled item (see module docstring)."""

    value: float | None = None
    field_id: int | None = None
    label: str = ""

    @property
    def labelled(self) -> bool:
        return bool(self.label)


def _normalize_label(label: Any) -> str:
    return " ".join(str(label or "").casefold().split())


def select_statement_field(report: dict, labels: tuple[str, ...]) -> StatementField:
    """Read the first item whose label matches ``labels`` (preference order).

    ``labels`` are exact, case/space-normalized strings rather than substrings:
    a substring match would take ``Cost of Revenue`` for revenue and ``Dividend
    Per Share`` for EPS.  An item without a label or without a numeric ``data``
    is skipped, never guessed.
    """
    items = report.get("item_list") or []
    for wanted in labels:
        for item in items:
            if _normalize_label(item.get("display_name")) != wanted:
                continue
            if item.get("data") is None:
                continue
            try:
                value = float(item["data"])
            except (TypeError, ValueError):
                continue
            return StatementField(value=value, field_id=item.get("field_id"),
                                  label=str(item.get("display_name") or ""))
    return StatementField()


def _env_file_settings() -> dict[str, str]:
    """KEY=VALUE pairs from ``FUTU_ENV_FILE`` (optional), like tsummt-web.

    The signing key is a file reference either way; keeping both in one dotenv
    file means the credential pair can be mounted without touching compose.
    """
    path = config.FUTU_ENV_FILE
    if not path:
        return {}
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"FUTU_ENV_FILE unreadable ({type(exc).__name__})") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(name: str, file_values: dict[str, str]) -> str:
    """Environment first, then the env file (the same precedence as tsummt)."""
    return os.getenv(name) or file_values.get(name) or ""


def _load_signer(path: str):
    """Load an Ed25519 private key (OpenSSH or PEM), imported lazily."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = Path(path).expanduser().read_bytes()
    loader = (serialization.load_ssh_private_key if b"BEGIN OPENSSH PRIVATE KEY" in raw
              else serialization.load_pem_private_key)
    key = loader(raw, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("FUTU_API_SIGN_KEY must be an Ed25519 private key")
    return key


class FutuWebClient:
    """Signed, read-only Futu WebAPI client with SDK-shaped return values."""

    def __init__(self, session=None):
        file_values = _env_file_settings()
        self.base = (config.FUTU_API_BASE.rstrip("/")) + "/"
        self.api_key = config.FUTU_API_KEY or _setting("FUTU_API_KEY", file_values)
        self.sign_key_path = (config.FUTU_API_SIGN_KEY
                              or _setting("FUTU_API_SIGN_KEY", file_values)
                              or _setting("FUTU_API_SIGN_KEY_PRIVATE", file_values))
        self.proxy = (config.FUTU_HTTP_PROXY
                      or _setting("FUTU_HTTP_PROXY", file_values) or None)
        self.timeout = float(config.FUTU_API_TIMEOUT_SECONDS)
        if not self.api_key or not self.sign_key_path:
            raise ValueError("FUTU_API_KEY and FUTU_API_SIGN_KEY are required")
        self._signer = _load_signer(self.sign_key_path)
        self.session = session if session is not None else self._new_session()

    def _new_session(self):
        import requests

        session = requests.Session()
        # The proxy is an explicit setting; do not let a stray HTTP(S)_PROXY in
        # the environment redirect signed requests somewhere unexpected.
        session.trust_env = False
        if self.proxy:
            address = urlsplit(self.proxy)
            if address.scheme != "http" or not address.hostname:
                raise ValueError("FUTU_HTTP_PROXY must be an HTTP forward proxy")
            session.proxies["https"] = self.proxy
        return session

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass

    # ── signing / transport ────────────────────────────────────────────
    def _signature(self, message: bytes) -> str:
        return base64.b64encode(self._signer.sign(message)).decode()

    def request(self, endpoint: str, *, params: dict | None = None,
                body: dict | None = None, timeout: float | None = None) -> dict:
        """One signed call; raises :class:`FutuWebError` on any non-zero answer."""
        method = "POST" if body is not None else "GET"
        # The signature covers the exact path + query that goes on the wire, so
        # the URL is assembled here rather than left to the HTTP client's own
        # parameter encoding (a reordered/extra-encoded query would sign
        # something the server never sees).
        query = urlencode(params or {})
        url = self.base + endpoint.lstrip("/") + (f"?{query}" if query else "")
        payload = json.dumps(body).encode() if body is not None else None
        digest = hashlib.sha256(payload).hexdigest() if payload else ""
        timestamp = str(int(time.time() * 1000))
        parts = urlsplit(url)
        message = "\n".join((timestamp, method, parts.path, parts.query, digest)).encode()
        headers = {
            "X-Api-Key": self.api_key,
            "X-Timestamp": timestamp,
            "X-Nonce": uuid.uuid4().hex,
            "Authorization": self._signature(message),
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.session.request(method, url, data=payload, headers=headers,
                                            timeout=timeout or self.timeout,
                                            allow_redirects=False)
        except FutuWebError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify instead of leaking
            raise FutuWebError(f"Futu WebAPI transport error ({type(exc).__name__})") from None
        if response.status_code in (429, 439):
            raise FutuWebThrottled(f"Futu WebAPI HTTP {response.status_code}",
                                   status=response.status_code, throttled=True)
        try:
            result = response.json()
        except ValueError:
            raise FutuWebError(f"Futu WebAPI returned HTTP {response.status_code}",
                               status=response.status_code) from None
        code = result.get("ret_code")
        if code in THROTTLED_CODES:
            raise FutuWebThrottled(
                f"Futu WebAPI rate limit (code {code}): {result.get('ret_msg', '')}",
                code=code, status=response.status_code, throttled=True)
        if response.status_code != 200 or code != 0:
            raise FutuWebError(
                f"Futu WebAPI HTTP {response.status_code}, code {code}: "
                f"{result.get('ret_msg', '')}".strip(),
                code=code, status=response.status_code,
                unsupported=code in UNSUPPORTED_CODES)
        return result

    def _call(self, endpoint: str, **kwargs) -> tuple[int, Any]:
        """Wrap :meth:`request` into the SDK's ``(ret, data | message)`` shape."""
        try:
            return 0, self.request(endpoint, **kwargs)
        except FutuWebError as exc:
            return -1, str(exc)

    # ── the three reads FinCal needs ───────────────────────────────────
    def get_financials_earnings_price_history(self, code: str) -> tuple[int, Any]:
        """Historical report dates for one code → ``(0, DataFrame)``.

        Rows are plain dicts carrying what the dates stage reads
        (``fiscal_year``/``financial_type``/``pub_trading_day_str``/``pub_type``);
        the three numeric columns are coerced to ``int`` here so the stage does
        not have to parse provider strings.  No DataFrame: pandas was never a
        FinCal dependency, it arrived with the OpenD SDK.
        """
        ret, data = self._call(f"{code}/financials/earnings-price-history")
        if ret != 0:
            return ret, data
        records = (data.get("data") or {}).get("records") or []
        rows: list[dict] = []
        for record in records:
            row = dict(record)
            for column in ("fiscal_year", "financial_type", "pub_type"):
                try:
                    row[column] = int(row[column])
                except (KeyError, TypeError, ValueError):
                    row[column] = None
            rows.append(row)
        return 0, rows

    def get_financials_statements(self, code: str, statement_type: int = 1,
                                  financial_type: int | None = None,
                                  num: int | None = None) -> tuple[int, Any]:
        """Income statement reports → ``(0, {"report_list": [...]})``.

        Shaped like the SDK response so ``sync_actuals`` keeps reading
        ``report_list``/``item_list``/``currency_code``/``accounting_standards``.
        """
        params: dict[str, Any] = {"statement_type": statement_type}
        if financial_type is not None:
            params["financial_type"] = financial_type
        if num is not None:
            params["num"] = num
        ret, data = self._call(f"{code}/financials/statements", params=params)
        if ret != 0:
            return ret, data
        payload = data.get("data") or {}
        return 0, {"report_list": payload.get("report_list") or [],
                   "next_key": (data.get("pagination") or {}).get("next_key")}

    def get_stock_basicinfo(self, market: str, security_type: str = "STOCK",
                            codes: list[str] | None = None) -> tuple[int, Any]:
        """Batch basic info (display names) → ``(0, DataFrame)``."""
        ret, data = self._call("stock-basicinfo", body={"code_list": list(codes or [])})
        if ret != 0:
            return ret, data
        rows = (data.get("data") or {}).get("basic_list") or []
        return 0, [dict(row) for row in rows]

    # ── health ─────────────────────────────────────────────────────────
    def probe(self, code: str = "US.AAPL") -> dict:
        """Cheap signed read used by the health endpoint.

        No TCP preflight exists any more: the credential, the route and the
        upstream are all exercised by the same request the sync itself makes.
        """
        started = time.monotonic()
        try:
            self.request(f"{code}/financials/earnings-price-history", timeout=5)
            return {"ok": True, "code": "ok",
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        except FutuWebError as exc:
            return {"ok": False,
                    "code": ("rate_limited" if exc.throttled else type(exc).__name__),
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": type(exc).__name__,
                    "latency_ms": round((time.monotonic() - started) * 1000)}


def create_client() -> FutuWebClient | None:
    """Build a client from configuration, or ``None`` when it is unavailable.

    Futu is optional enrichment in FinCal: an unconfigured or broken credential
    must degrade to 「Futu stage skipped」, never block the app or the other sync
    stages. The reason is logged once, in the sync's own vocabulary.
    """
    try:
        return FutuWebClient()
    except Exception as exc:  # noqa: BLE001 - missing key, unreadable file, bad proxy
        logger.warning("Futu WebAPI unavailable (%s); skipping optional Futu sync",
                       type(exc).__name__ if not str(exc) else exc)
        return None


def find_key_file(*candidates: str) -> str:
    """First existing path among ``candidates`` (empty string when none)."""
    for candidate in candidates:
        if not candidate:
            continue
        expanded = Path(os.path.expanduser(candidate))
        if expanded.is_file():
            return str(expanded)
    return ""
