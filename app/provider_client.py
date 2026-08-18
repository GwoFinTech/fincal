"""Unified external request policy for FinCal providers (Issue #6).

Centralizes timeout, retry, backoff, error classification and log sanitization
for all external HTTP/CLI calls.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from enum import Enum
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection_error"
    RATE_LIMITED = "rate_limited"
    INVALID_RESPONSE = "invalid_response"
    HTTP_ERROR = "http_error"
    UNKNOWN = "unknown"


@dataclass
class ProviderError(Exception):
    """Classified provider error with stable error code."""
    category: ErrorCategory
    message: str
    status_code: int | None = None
    retry_after: float | None = None

    @property
    def error_code(self) -> str:
        return f"provider_{self.category.value}"

    def __str__(self) -> str:
        return f"[{self.category.value}] {self.message}"


@dataclass
class ProviderConfig:
    """Configuration for a provider client."""
    name: str
    timeout: float = 15.0
    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 30.0
    retry_on_timeout: bool = True
    retry_on_connection: bool = True
    retry_on_rate_limit: bool = True


def _classify_http_error(exc: HTTPError) -> ProviderError:
    code = exc.code
    if code == 429:
        retry_after = None
        raw = exc.headers.get("Retry-After") if exc.headers else None
        if raw:
            try:
                retry_after = float(raw)
            except (ValueError, TypeError):
                pass
        return ProviderError(
            ErrorCategory.RATE_LIMITED,
            f"HTTP 429 from {exc.url}",
            status_code=429,
            retry_after=retry_after or 60.0,
        )
    if code >= 500:
        return ProviderError(ErrorCategory.HTTP_ERROR, f"HTTP {code} from {exc.url}", status_code=code)
    return ProviderError(ErrorCategory.HTTP_ERROR, f"HTTP {code} from {exc.url}", status_code=code)


def classify_error(exc: Exception, provider: str = "") -> ProviderError:
    """Classify an exception into a stable error category."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, HTTPError):
        return _classify_http_error(exc)
    if isinstance(exc, (TimeoutError, OSError)) and "timed out" in str(exc).lower():
        return ProviderError(ErrorCategory.TIMEOUT, f"{provider} request timed out")
    if isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError):
        return ProviderError(ErrorCategory.TIMEOUT, f"{provider} request timed out")
    if isinstance(exc, (URLError, ConnectionError, OSError)):
        return ProviderError(ErrorCategory.CONNECTION, f"{provider} connection failed: {type(exc).__name__}")
    if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
        return ProviderError(ErrorCategory.INVALID_RESPONSE, f"{provider} invalid response: {type(exc).__name__}")
    return ProviderError(ErrorCategory.UNKNOWN, f"{provider} unknown error: {type(exc).__name__}")


def _should_retry(cat: ErrorCategory, cfg: ProviderConfig) -> bool:
    if cat == ErrorCategory.TIMEOUT:
        return cfg.retry_on_timeout
    if cat == ErrorCategory.CONNECTION:
        return cfg.retry_on_connection
    if cat == ErrorCategory.RATE_LIMITED:
        return cfg.retry_on_rate_limit
    return False


def _backoff_delay(attempt: int, cfg: ProviderConfig, retry_after: float | None = None) -> float:
    if retry_after:
        return min(retry_after, cfg.max_delay)
    delay = cfg.base_delay * (2 ** attempt)
    return min(delay, cfg.max_delay)


def _sanitize_log_url(url: str) -> str:
    """Strip query params that may contain tokens."""
    if "?" in url:
        return url.split("?")[0] + "?[REDACTED]"
    return url


# ── High-level helpers ──────────────────────────────────────────────

def http_get_json(url: str, *, cfg: ProviderConfig | None = None) -> dict | list:
    """Fetch JSON from URL with retries and classified errors."""
    from .metrics import metrics
    cfg = cfg or ProviderConfig(name="http")
    last_error: ProviderError | None = None

    for attempt in range(cfg.max_retries + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode())
            metrics.record_provider_call(cfg.name, success=True,
                                          duration_ms=(time.time() - t0) * 1000)
            return data
        except Exception as exc:
            err = classify_error(exc, cfg.name)
            last_error = err
            metrics.record_provider_call(cfg.name, success=False,
                                          category=err.category.value,
                                          duration_ms=(time.time() - t0) * 1000)
            if attempt < cfg.max_retries and _should_retry(err.category, cfg):
                delay = _backoff_delay(attempt, cfg, err.retry_after)
                logger.warning("%s attempt %d failed (%s); retrying in %.1fs",
                               cfg.name, attempt + 1, err.category.value, delay)
                time.sleep(delay)
            else:
                break

    raise last_error or ProviderError(ErrorCategory.UNKNOWN, f"{cfg.name} failed")


def cli_json(command: list[str], *, cfg: ProviderConfig | None = None) -> dict | list:
    """Run a CLI command and parse JSON output with retries."""
    cfg = cfg or ProviderConfig(name="cli")
    last_error: ProviderError | None = None

    for attempt in range(cfg.max_retries + 1):
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=cfg.timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"exit {proc.returncode}: {proc.stderr[:200]}")
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(ErrorCategory.INVALID_RESPONSE, f"{cfg.name} invalid JSON") from exc
        except Exception as exc:
            err = classify_error(exc, cfg.name)
            last_error = err
            if attempt < cfg.max_retries and _should_retry(err.category, cfg):
                delay = _backoff_delay(attempt, cfg, err.retry_after)
                logger.warning("%s attempt %d failed (%s); retrying in %.1fs",
                               cfg.name, attempt + 1, err.category.value, delay)
                time.sleep(delay)
            else:
                break

    raise last_error or ProviderError(ErrorCategory.UNKNOWN, f"{cfg.name} failed")
