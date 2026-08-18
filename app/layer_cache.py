"""Layer cache with stale-while-revalidate semantics (Issue #7).

Provides per-key TTL cache that returns stale data while refreshing
in the background. Singleflight prevents concurrent duplicate refreshes.
"""
from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    data: Any = None
    fetched_at: float = 0.0
    expires_at: float = 0.0
    last_success_at: float | None = None
    stale: bool = False
    error_code: str | None = None
    refreshing: bool = False


class LayerCache:
    """TTL cache with stale-while-revalidate and singleflight."""

    def __init__(self, default_ttl: float = 300.0, stale_ttl: float = 3600.0):
        self._entries: dict[str, CacheEntry] = {}
        self._locks: dict[str, threading.Event] = {}
        self._default_ttl = default_ttl
        self._stale_ttl = stale_ttl
        self._mutex = threading.Lock()

    def get(self, key: str) -> CacheEntry | None:
        """Return the cache entry if it exists (may be stale)."""
        return self._entries.get(key)

    def is_fresh(self, key: str) -> bool:
        """Check if cache entry is within TTL."""
        entry = self._entries.get(key)
        if entry is None:
            return False
        return time.time() < entry.expires_at

    def is_stale_valid(self, key: str) -> bool:
        """Check if stale data is still within stale_ttl."""
        entry = self._entries.get(key)
        if entry is None or entry.last_success_at is None:
            return False
        return time.time() - entry.last_success_at < self._stale_ttl

    def put(self, key: str, data: Any, ttl: float | None = None) -> None:
        """Store fresh data in cache."""
        now = time.time()
        self._entries[key] = CacheEntry(
            data=data,
            fetched_at=now,
            expires_at=now + (ttl or self._default_ttl),
            last_success_at=now,
            stale=False,
            error_code=None,
        )

    def mark_stale(self, key: str, error_code: str) -> None:
        """Mark existing entry as stale after a failed refresh."""
        entry = self._entries.get(key)
        if entry:
            entry.stale = True
            entry.error_code = error_code
            entry.refreshing = False

    def get_or_refresh(
        self,
        key: str,
        fetcher: Callable[[], Any],
        ttl: float | None = None,
    ) -> tuple[Any, CacheEntry]:
        """Get from cache or refresh. Returns (data, entry).

        If cache is fresh: return cached data.
        If cache is stale: return stale data, trigger background refresh (singleflight).
        If no cache: block and fetch.
        """
        entry = self._entries.get(key)

        # Fresh cache hit
        if entry and time.time() < entry.expires_at:
            return entry.data, entry

        # Stale but valid — return stale data, trigger refresh
        if entry and entry.last_success_at and self.is_stale_valid(key):
            if not entry.refreshing:
                self._trigger_refresh(key, fetcher, ttl)
            return entry.data, entry

        # No cache or expired stale — block and fetch
        return self._do_fetch(key, fetcher, ttl)

    def _trigger_refresh(self, key: str, fetcher: Callable, ttl: float | None) -> None:
        """Start a background refresh with singleflight."""
        with self._mutex:
            entry = self._entries.get(key)
            if entry and entry.refreshing:
                return  # already refreshing
            if entry:
                entry.refreshing = True

        def _refresh():
            try:
                data = fetcher()
                self.put(key, data, ttl)
                logger.debug("cache refresh ok: %s", key)
            except Exception as exc:
                self.mark_stale(key, f"refresh_failed:{type(exc).__name__}")
                logger.warning("cache refresh failed for %s: %s", key, exc)

        t = threading.Thread(target=_refresh, daemon=True, name=f"cache-refresh-{key}")
        t.start()

    def _do_fetch(self, key: str, fetcher: Callable, ttl: float | None) -> tuple[Any, CacheEntry]:
        """Blocking fetch with singleflight."""
        with self._mutex:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Event()
                self._locks[key] = lock
            elif not lock.is_set():
                # Another thread is fetching — wait
                self._mutex.release()
                lock.wait(timeout=30)
                self._mutex.acquire()
                entry = self._entries.get(key)
                if entry:
                    return entry.data, entry

        # We are the fetcher
        try:
            data = fetcher()
            self.put(key, data, ttl)
            return data, self._entries[key]
        except Exception as exc:
            # If we have stale data, return it
            entry = self._entries.get(key)
            if entry and entry.last_success_at:
                self.mark_stale(key, f"fetch_failed:{type(exc).__name__}")
                return entry.data, entry
            raise
        finally:
            with self._mutex:
                lock.set()
                self._locks.pop(key, None)

    def invalidate(self, key: str | None = None) -> None:
        """Clear cache entry or all entries."""
        if key:
            self._entries.pop(key, None)
        else:
            self._entries.clear()
