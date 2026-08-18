"""Provider metrics and diagnostics (Issue #15).

Low-cardinality counters for provider calls, errors, cache stats.
Thread-safe, in-process only (no external metrics backend).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ProviderStats:
    calls: int = 0
    success: int = 0
    timeout: int = 0
    rate_limited: int = 0
    connection_error: int = 0
    invalid_response: int = 0
    total_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.calls if self.calls else 0.0


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stale_returns: int = 0
    refresh_ok: int = 0
    refresh_fail: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class Metrics:
    """Thread-safe in-process metrics collector."""

    def __init__(self):
        self._mutex = threading.Lock()
        self._providers: dict[str, ProviderStats] = defaultdict(ProviderStats)
        self._cache = CacheStats()

    def record_provider_call(self, provider: str, *, success: bool,
                              category: str = "", duration_ms: float = 0.0):
        with self._mutex:
            s = self._providers[provider]
            s.calls += 1
            s.total_ms += duration_ms
            if success:
                s.success += 1
            elif category == "timeout":
                s.timeout += 1
            elif category == "rate_limited":
                s.rate_limited += 1
            elif category == "connection_error":
                s.connection_error += 1
            elif category == "invalid_response":
                s.invalid_response += 1

    def record_cache_hit(self):
        with self._mutex:
            self._cache.hits += 1

    def record_cache_miss(self):
        with self._mutex:
            self._cache.misses += 1

    def record_cache_stale(self):
        with self._mutex:
            self._cache.stale_returns += 1

    def record_cache_refresh(self, success: bool):
        with self._mutex:
            if success:
                self._cache.refresh_ok += 1
            else:
                self._cache.refresh_fail += 1

    def snapshot(self) -> dict:
        with self._mutex:
            return {
                "providers": {
                    name: {
                        "calls": s.calls,
                        "success": s.success,
                        "success_rate": round(s.success_rate, 3),
                        "avg_ms": round(s.avg_ms, 1),
                        "errors": {
                            "timeout": s.timeout,
                            "rate_limited": s.rate_limited,
                            "connection": s.connection_error,
                            "invalid_response": s.invalid_response,
                        },
                    }
                    for name, s in self._providers.items()
                },
                "cache": {
                    "hits": self._cache.hits,
                    "misses": self._cache.misses,
                    "hit_rate": round(self._cache.hit_rate, 3),
                    "stale_returns": self._cache.stale_returns,
                    "refresh_ok": self._cache.refresh_ok,
                    "refresh_fail": self._cache.refresh_fail,
                },
            }

    def reset(self):
        with self._mutex:
            self._providers.clear()
            self._cache = CacheStats()


# Global instance
metrics = Metrics()
