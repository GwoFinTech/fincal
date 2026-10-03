"""Layer cache with stale-while-revalidate semantics (Issue #7).

Provides per-key TTL cache that returns stale data while refreshing
in the background. Singleflight prevents concurrent duplicate refreshes
(Issue #8) — the background refresh path shares the same per-key slot as
the blocking path, and every producer carries an ordering ticket so a
snapshot produced by an earlier-started fetch can never overwrite a newer
one (Issue #34).
"""
from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass
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

    def __init__(self, default_ttl: float = 300.0, stale_ttl: float = 3600.0,
                 waiter_timeout: float = 30.0):
        self._entries: dict[str, CacheEntry] = {}
        self._locks: dict[str, threading.Event] = {}
        # Ordering tickets per key (Issue #34): every fetch takes a ticket when
        # it starts, and a snapshot may only be stored if no later ticket has
        # been stored already. Without this, a slow fetch that started first
        # could land after a faster one that started later and overwrite fresher
        # data with older data.
        self._tickets: dict[str, int] = {}
        self._applied: dict[str, int] = {}
        self._default_ttl = default_ttl
        self._stale_ttl = stale_ttl
        self._waiter_timeout = waiter_timeout
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

    def begin_fetch(self, key: str) -> int:
        """Reserve the ordering ticket for a fetch that is about to start.

        Tickets increase per key, so a later-started fetch always gets a
        higher ticket than an earlier-started one (Issue #34).
        """
        with self._mutex:
            ticket = self._next_ticket_locked(key)
        return ticket

    def _next_ticket_locked(self, key: str) -> int:
        ticket = self._tickets.get(key, 0) + 1
        self._tickets[key] = ticket
        return ticket

    def put(self, key: str, data: Any, ttl: float | None = None,
            ticket: int | None = None) -> CacheEntry | None:
        """Store fresh data in cache and return the stored entry.

        ``ticket`` (from :meth:`begin_fetch`) orders concurrent producers of
        the same key: a snapshot belonging to an earlier-started fetch is
        dropped when a snapshot from a later-started fetch has already landed,
        and the newer entry is returned instead (Issue #34). ``None`` stores
        unconditionally (seeding, tests).
        """
        now = time.time()
        with self._mutex:
            if ticket is not None and ticket < self._applied.get(key, 0):
                logger.debug(
                    "cache: dropped out-of-order snapshot for %s (ticket %s < %s)",
                    key, ticket, self._applied[key],
                )
                return self._entries.get(key)
            if ticket is not None:
                self._applied[key] = ticket
            entry = CacheEntry(
                data=data,
                fetched_at=now,
                expires_at=now + (ttl or self._default_ttl),
                last_success_at=now,
                stale=False,
                error_code=None,
            )
            self._entries[key] = entry
            return entry

    def mark_stale(self, key: str, error_code: str, ticket: int | None = None) -> None:
        """Mark existing entry as stale after a failed refresh.

        ``ticket`` applies the same ordering rule as :meth:`put`: a failed
        fetch that started before the snapshot currently in cache must not
        relabel that newer snapshot as stale.
        """
        with self._mutex:
            entry = self._entries.get(key)
            if entry is None:
                return
            if ticket is not None and ticket < self._applied.get(key, 0):
                return
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
            self._trigger_refresh(key, fetcher, ttl)
            return entry.data, entry

        # No cache or expired stale — block and fetch
        return self._do_fetch(key, fetcher, ttl)

    def _trigger_refresh(self, key: str, fetcher: Callable, ttl: float | None) -> None:
        """Start a background refresh that owns the key's singleflight slot.

        The refresh registers the same per-key slot the blocking path uses
        (Issue #34), so a request that finds the entry fully stale while the
        background refresh is still running waits for its result instead of
        calling the fetcher a second time. It also takes a ticket, so a
        snapshot from this (earlier) refresh cannot overwrite a newer one.
        """
        with self._mutex:
            if key in self._locks:
                return  # a producer (background or blocking) already owns the key
            entry = self._entries.get(key)
            if entry is not None:
                entry.refreshing = True
            lock = threading.Event()
            self._locks[key] = lock
            ticket = self._next_ticket_locked(key)

        def _refresh():
            try:
                data = fetcher()
                self.put(key, data, ttl, ticket=ticket)
                logger.debug("cache refresh ok: %s", key)
            except Exception as exc:
                self.mark_stale(key, f"refresh_failed:{type(exc).__name__}", ticket=ticket)
                logger.warning("cache refresh failed for %s: %s", key, exc)
            finally:
                with self._mutex:
                    if self._locks.get(key) is lock:
                        self._locks.pop(key, None)
                    lock.set()

        t = threading.Thread(target=_refresh, daemon=True, name=f"cache-refresh-{key}")
        t.start()

    def _do_fetch(self, key: str, fetcher: Callable, ttl: float | None) -> tuple[Any, CacheEntry]:
        """Blocking fetch with singleflight (Issues #8, #34).

        A caller that finds another fetch of the same key in flight — a
        background refresh included — waits (bounded by ``waiter_timeout``)
        for that fetch's snapshot instead of calling the fetcher a second time.
        """
        ticket = self.begin_fetch(key)
        with self._mutex:
            lock = self._locks.get(key)
            if lock is not None and lock.is_set():
                lock = None  # leftover event from a producer that already finished
            owns = lock is None
            if owns:
                lock = threading.Event()
                self._locks[key] = lock

        if not owns:
            # Another producer owns the key: join it rather than duplicating
            # the upstream call (Issue #34).
            lock.wait(timeout=self._waiter_timeout)
            entry = self._entries.get(key)
            if entry is not None:
                return entry.data, entry
            # The producer released the key without storing anything (it raced
            # with invalidate()): fall through and fetch it ourselves.

        # We are the fetcher
        try:
            data = fetcher()
            entry = self.put(key, data, ttl, ticket=ticket)
            if entry is None:  # pragma: no cover - defensive, put always returns an entry
                now = time.time()
                return data, CacheEntry(data=data, fetched_at=now,
                                        expires_at=now + (ttl or self._default_ttl),
                                        last_success_at=now)
            return data, entry
        except Exception as exc:
            # If we have stale data, return it
            self.mark_stale(key, f"fetch_failed:{type(exc).__name__}", ticket=ticket)
            entry = self._entries.get(key)
            if entry and entry.last_success_at:
                return entry.data, entry
            raise
        finally:
            if owns:
                with self._mutex:
                    if self._locks.get(key) is lock:
                        self._locks.pop(key, None)
                    lock.set()

    def invalidate(self, key: str | None = None) -> None:
        """Clear cache entry or all entries."""
        if key:
            self._entries.pop(key, None)
        else:
            self._entries.clear()
