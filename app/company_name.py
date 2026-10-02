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
import subprocess
import urllib.request
from dataclasses import dataclass
from urllib.error import URLError

import app.config as config
from app.provider_client import (
    ErrorCategory, ProviderConfig, ProviderError, classify_error,
)

logger = logging.getLogger(__name__)

_KURUMI_CFG = ProviderConfig(name="kurumi", timeout=config.KURUMI_API_TIMEOUT, max_retries=1)
_LB_CFG = ProviderConfig(name="longbridge", timeout=30, max_retries=0)


@dataclass
class NameResult:
    """Result of a company-name lookup with error metadata."""
    name: str
    source: str = ""
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.name) and self.error_code is None

    @property
    def unavailable(self) -> bool:
        return not self.name and self.error_code is not None


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
    """Query Kurumi /api/stock/{symbol}/overview. Raises ProviderError on failure."""
    url = kurumi_overview_url(symbol, market)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_KURUMI_CFG.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        name = str(data.get("name") or "").strip()
        return name
    except Exception as exc:
        raise classify_error(exc, "kurumi") from exc


def fetch_from_longbridge(symbol: str, market: str) -> str:
    """Query `longbridge static`. Raises ProviderError on failure."""
    lb_sym = (symbol.split(".")[0].lstrip("0") or "0") if market == "HK" else symbol
    lb_sym = f"{lb_sym}.{market}"
    try:
        p = subprocess.run(
            ["longbridge", "static", lb_sym, "--format", "json"],
            capture_output=True, text=True, timeout=_LB_CFG.timeout,
        )
        if p.returncode != 0:
            raise RuntimeError(f"longbridge_cli_exit_{p.returncode}")
        rows = json.loads(p.stdout or "[]")
        if rows and isinstance(rows, list):
            return str(rows[0].get("name", "")).strip()
        return ""
    except Exception as exc:
        raise classify_error(exc, "longbridge") from exc


def fetch_from_futu(symbol: str, market: str) -> str:
    """Query Futu OpenD get_stock_basicinfo. Raises ProviderError on failure."""
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
    try:
        ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
        ret, data = ctx.get_stock_basicinfo(
            Market.HK if market == "HK" else Market.US,
            SecurityType.STOCK, [code],
        )
        if ret == RET_OK and data is not None and not data.empty:
            return str(data.iloc[0].get("name", "")).strip()
        return ""
    except Exception as exc:  # noqa: BLE001
        raise classify_error(exc, "futu") from exc
    finally:
        if ctx is not None:
            ctx.close()


def resolve_company_name(symbol: str, market: str) -> tuple[str, str]:
    """Resolve a company name. Returns (name, source); ("", "") if unavailable."""
    result = resolve_company_name_result(symbol, market)
    return (result.name, result.source) if result.ok else ("", "")


def resolve_company_name_result(symbol: str, market: str) -> NameResult:
    """Resolve a company name with error metadata (issue #4, #6)."""
    errors: list[str] = []
    for fetcher, source in (
        (fetch_from_kurumi, "kurumi"),
        (fetch_from_longbridge, "longbridge"),
        (fetch_from_futu, "futu"),
    ):
        try:
            name = fetcher(symbol, market)
            if name:
                return NameResult(name=name, source=source)
        except ProviderError as exc:
            errors.append(f"{source}:{exc.error_code}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}:unknown")
            continue
    if errors:
        return NameResult(name="", source="", error_code="all_sources_failed")
    return NameResult(name="", source="", error_code="unavailable")
