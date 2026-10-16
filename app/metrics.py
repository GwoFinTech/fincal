"""Provider metrics and diagnostics (Issue #15).

Low-cardinality counters for provider calls, errors, cache stats.
Thread-safe, in-process only (no external metrics backend).

Reading the numbers (Issue #83)
-------------------------------
The counters were declared by #15 but no code wrote to them, so
``/api/admin/diagnostics`` answered constants (``cache`` all zeros,
``providers`` empty) whatever the traffic. They are now written at the decision
points — ``app/layer_cache.py`` (earnings cache), ``app/routers/ical.py`` (feed
cache), ``app/provider_client.py`` and ``app/company_name.py`` (upstream calls).
Three rules keep the numbers honest, and each of them is deliberate:

* **Per process, not cumulative.** They live in this module's global
  :data:`metrics` instance and reset when the web process restarts (a deploy, a
  crash, a container recreate). Read them as "what has *this* process served
  since it started", never as a lifetime total; there is no persistence and no
  cross-process aggregation (a multi-worker deployment reports one worker).
* **Aggregate, low cardinality.** Cache counters aggregate every cache that
  reports (earnings + iCal feed); provider counters are keyed by provider name
  (``kurumi`` / ``longbridge`` / ``futu`` / …). Nothing per request may become a
  label — no cache key, symbol, token or user id: the label set is bounded by
  the code, not by traffic, so a busy feed cannot inflate the metric surface.
* **One number, one meaning.** :meth:`Metrics.record_cache_hit` counts a read
  answered without asking the origin (a cache served the caller, joins on an
  in-flight refresh included); :meth:`Metrics.record_cache_miss` counts a read
  that had to produce the value itself; :meth:`Metrics.record_cache_stale`
  counts any reply that handed the caller a *stale* snapshot;
  :meth:`Metrics.record_cache_refresh` counts the outcome of the origin call
  that (re)produced a value.
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
        """Count one attempt against an upstream ``provider``.

        Called once per attempt (a retrying client counts each try, so
        ``calls`` is upstream load rather than request count) and keyed only by
        the provider name.
        """
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
        """A read the cache answered without asking the origin."""
        with self._mutex:
            self._cache.hits += 1

    def record_cache_miss(self):
        """A read that had to produce the value itself (origin was called)."""
        with self._mutex:
            self._cache.misses += 1

    def record_cache_stale(self):
        """A reply that handed the caller a stale snapshot (Issue #83)."""
        with self._mutex:
            self._cache.stale_returns += 1

    def record_cache_refresh(self, success: bool):
        """Outcome of the origin call that (re)produced a cached value."""
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
