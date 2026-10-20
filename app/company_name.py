"""Company-name resolution for FinCal symbols.

Priority order (per user requirement):
1. Kurumi API      — `/api/stock/{symbol}/overview` returns `name`
2. Longbridge CLI  — `longbridge static` returns `name` (English/display)
3. Futu OpenD      — `get_stock_basicinfo` returns display name

Every successful lookup is cached in the `stock_names` table so subsequent
runs never re-hit the upstream providers for the same symbol.

Issue #4: returns NameResult with error metadata.
Issue #6: uses unified provider_client for timeouts and error classification.
Issue #70: the URL the health probe asks and the URL ``fetch_from_kurumi``
reads come from one place (``kurumi_overview_url``), and a base URL that
resolves back to FinCal itself is reported as not configured rather than probed.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlsplit

import app.config as config
from app.metrics import metrics
from app.provider_client import (
    ErrorCategory, ProviderConfig, ProviderError, classify_error,
)

logger = logging.getLogger(__name__)

_KURUMI_CFG = ProviderConfig(name="kurumi", timeout=config.KURUMI_API_TIMEOUT, max_retries=1)
_LB_CFG = ProviderConfig(name="longbridge", timeout=30, max_retries=0)


def _count_call(provider: str, started: float, *, success: bool,
                category: str = "") -> None:
    """Report one upstream attempt to ``app.metrics`` (Issue #83).

    The three fetchers below build their own request/CLI/SDK call instead of
    going through ``provider_client.http_get_json`` (which is the only place
    that used to count), so without this the ``providers`` section of
    ``/api/admin/diagnostics`` stayed empty — even though
    ``/api/admin/health`` really calls Kurumi on every poll. Keyed by provider
    name only: no symbol, no URL, no credential.
    """
    metrics.record_provider_call(provider, success=success, category=category,
                                 duration_ms=(time.time() - started) * 1000)


@dataclass
class NameResult:
    """Result of a company-name lookup with error metadata.

    ``name`` is the display name the calendar shows (Kurumi's, usually Chinese);
    ``name_en`` is the Latin name an English iCal subscription needs (Issue #55).
    They come from different providers on purpose — Kurumi has no English field
    and Longbridge ``static`` has no Chinese one — so one must never be written
    into the other's column.
    """
    name: str
    source: str = ""
    name_en: str = ""
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.name) and self.error_code is None

    @property
    def unavailable(self) -> bool:
        return not self.name and self.error_code is not None


# CJK ideographs, kana and Hangul: a name containing any of them cannot be the
# English title of an ``en`` subscription (Issue #55, criterion 3).
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)


def has_cjk(value) -> bool:
    """Whether ``value`` contains Chinese/Japanese/Korean characters."""
    return bool(value) and bool(_CJK_RE.search(str(value)))


def is_english_name(value) -> bool:
    """A usable English name: non-empty and free of CJK characters."""
    text = str(value or "").strip()
    return bool(text) and not has_cjk(text)


def kurumi_symbol(symbol: str, market: str) -> str:
    if market == "HK":
        return (symbol.split(".")[0].lstrip("0") or "0") + ".HK"
    return f"{symbol}.US"


def kurumi_base_url() -> str:
    """Configured Kurumi base URL without a trailing slash (Issue #70).

    Single source for both the client path and the health probe: they used to
    diverge (`fetch_from_kurumi` asked for ``/api/stock/{symbol}/overview`` while
    the probe asked FinCal itself for ``/api/config``).
    """
    return (config.KURUMI_API_URL or "").strip().rstrip("/")


def kurumi_overview_url(symbol: str, market: str) -> str:
    """URL of the Kurumi overview endpoint FinCal actually reads."""
    return f"{kurumi_base_url()}/api/stock/{kurumi_symbol(symbol, market)}/overview"


def kurumi_target_label() -> str:
    """Credential-free label for the configured Kurumi address (Issue #70).

    ``scheme://host:port`` only — it is printed by ``scripts/deploy.sh`` and
    must never carry userinfo, a path or a query, whatever the operator put in
    ``KURUMI_API_URL``. Returns ``<unset>`` / ``<invalid>`` for values that
    cannot be parsed.
    """
    base = (config.KURUMI_API_URL or "").strip()
    if not base:
        return "<unset>"
    try:
        parsed = urlsplit(base if "://" in base else f"//{base}")
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return "<invalid>"
    if not host:
        return "<invalid>"
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


# Symbol used by ``probe_kurumi``. It must be one the upstream always knows, so
# that a 404 means "this base URL is not Kurumi" rather than "no such stock".
KURUMI_PROBE_SYMBOL = "0700.HK"
KURUMI_PROBE_MARKET = "HK"

# Language-neutral probe verdicts (Issue #70).
ERROR_DEPENDENCY_NOT_CONFIGURED = "dependency_not_configured"
ERROR_KURUMI_UNREACHABLE = "kurumi_unreachable"


def probe_kurumi(symbol: str = KURUMI_PROBE_SYMBOL,
                 market: str = KURUMI_PROBE_MARKET) -> dict:
    """Probe the Kurumi dependency the way the client uses it (Issue #70).

    Returns ``{"status": ..., "error_code": ...}`` — never a credential or an
    upstream message. A base URL that resolves back to FinCal's own port is
    reported as ``dependency_not_configured`` instead of being called: the
    service answers its own port, so such a probe could only ever "succeed".
    """
    if config.kurumi_target_is_self():
        return {"status": "degraded", "error_code": ERROR_DEPENDENCY_NOT_CONFIGURED}
    try:
        name = fetch_from_kurumi(symbol, market)
    except ProviderError as exc:
        return {
            "status": "degraded",
            "error_code": ERROR_KURUMI_UNREACHABLE,
            "error": exc.error_code,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "degraded",
            "error_code": ERROR_KURUMI_UNREACHABLE,
            "error": type(exc).__name__,
        }
    if not name:
        return {"status": "degraded", "error_code": ERROR_KURUMI_UNREACHABLE}
    return {"status": "healthy"}


def fetch_from_kurumi(symbol: str, market: str) -> str:
    """Query Kurumi /api/stock/{symbol}/overview. Raises ProviderError on failure.

    A transport success that carries no usable name still counts as one
    successful call: the counters describe the upstream, not the caller's
    fallback chain (Issue #83).
    """
    url = kurumi_overview_url(symbol, market)
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_KURUMI_CFG.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        name = str(data.get("name") or "").strip()
        _count_call("kurumi", started, success=True)
        return name
    except Exception as exc:
        err = classify_error(exc, "kurumi")
        _count_call("kurumi", started, success=False, category=err.category.value)
        raise err from exc


def fetch_from_longbridge(symbol: str, market: str) -> str:
    """Query `longbridge static`. Raises ProviderError on failure."""
    lb_sym = (symbol.split(".")[0].lstrip("0") or "0") if market == "HK" else symbol
    lb_sym = f"{lb_sym}.{market}"
    started = time.time()
    try:
        p = subprocess.run(
            ["longbridge", "static", lb_sym, "--format", "json"],
            capture_output=True, text=True, timeout=_LB_CFG.timeout,
        )
        if p.returncode != 0:
            raise RuntimeError(f"longbridge_cli_exit_{p.returncode}")
        rows = json.loads(p.stdout or "[]")
        if rows and isinstance(rows, list):
            name = str(rows[0].get("name", "")).strip()
        else:
            name = ""
        _count_call("longbridge", started, success=True)
        return name
    except Exception as exc:
        err = classify_error(exc, "longbridge")
        _count_call("longbridge", started, success=False, category=err.category.value)
        raise err from exc


def fetch_from_futu(symbol: str, market: str) -> str:
    """Query Futu OpenD get_stock_basicinfo. Raises ProviderError on failure.

    The SDK is not reachable through ``provider_client``, so its attempt is
    counted explicitly (Issue #83). A missing SDK returns "" without counting
    anything: no call was made to anybody.
    """
    try:
        from futu import RET_OK, Market, SecurityType, OpenQuoteContext
        from app.symbol import to_futu_code
    except ImportError:
        return ""
    # Build a canonical TICKER.MARKET before converting so both markets yield a
    # complete Futu code (Issue #41): HK rows already carry ".HK" (e.g. 0700.HK),
    # US rows are bare tickers (e.g. AAPL) and must gain the ".US" suffix.
    canonical = f"{symbol}.{market}" if "." not in symbol else symbol
    code = to_futu_code(canonical)
    ctx = None
    started = time.time()
    try:
        ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
        ret, data = ctx.get_stock_basicinfo(
            Market.HK if market == "HK" else Market.US,
            SecurityType.STOCK, [code],
        )
        if ret == RET_OK and data is not None and not data.empty:
            name = str(data.iloc[0].get("name", "")).strip()
            _count_call("futu", started, success=True)
            return name
        # OpenD answered with an error or with nothing usable: a real call that
        # produced no name, reported as such instead of silently vanishing.
        _count_call("futu", started, success=False,
                    category=ErrorCategory.INVALID_RESPONSE.value)
        return ""
    except Exception as exc:  # noqa: BLE001
        err = classify_error(exc, "futu")
        _count_call("futu", started, success=False, category=err.category.value)
        raise err from exc
    finally:
        if ctx is not None:
            ctx.close()


def resolve_company_name(symbol: str, market: str) -> tuple[str, str]:
    """Resolve a company name. Returns (name, source); ("", "") if unavailable."""
    result = resolve_company_name_result(symbol, market)
    return (result.name, result.source) if result.ok else ("", "")


def resolve_english_name(symbol: str, market: str) -> str:
    """Resolve the Latin name used by an ``en`` subscription (Issue #55).

    Deliberately independent of the display-name chain: the display name is
    Kurumi's (usually Chinese) and only Longbridge ``static``/Futu carry a Latin
    spelling. A candidate is accepted only when it is free of CJK characters,
    because a Chinese name in an English title is the bug this fixes — returning
    "" lets the caller fall back explicitly instead.
    """
    for fetcher in (fetch_from_longbridge, fetch_from_futu):
        try:
            candidate = fetcher(symbol, market)
        except ProviderError:
            continue
        except Exception:  # noqa: BLE001 - a fallback may never be fatal
            continue
        if is_english_name(candidate):
            return str(candidate).strip()
    return ""


def resolve_company_name_result(symbol: str, market: str) -> NameResult:
    """Resolve a company name with error metadata (issue #4, #6, #55).

    ``name``/``source`` keep the documented priority (Kurumi → Longbridge →
    Futu) so the calendar's display name does not change. ``name_en`` is resolved
    separately: an already-Latin display name is its own English name (no extra
    upstream call), otherwise Longbridge/Futu are asked for one.
    """
    errors: list[str] = []
    resolved: NameResult | None = None
    for fetcher, source in (
        (fetch_from_kurumi, "kurumi"),
        (fetch_from_longbridge, "longbridge"),
        (fetch_from_futu, "futu"),
    ):
        try:
            name = fetcher(symbol, market)
            if name:
                resolved = NameResult(name=name, source=source)
                break
        except ProviderError as exc:
            errors.append(f"{source}:{exc.error_code}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}:unknown")
            continue

    if resolved is None:
        if errors:
            return NameResult(name="", source="", error_code="all_sources_failed")
        return NameResult(name="", source="", error_code="unavailable")

    if is_english_name(resolved.name):
        resolved.name_en = str(resolved.name).strip()
    else:
        resolved.name_en = resolve_english_name(symbol, market)
    return resolved
