"""Keyed singleflight utility (Issue #8).

Ensures that concurrent requests for the same key only trigger one
upstream call. Other callers wait for the first result.
"""
from __future__ import annotations

import threading
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class _Flight:
    """Mutable holder for a single in-flight keyed call.

    Waiters keep a direct reference to the holder for the duration of
    their wait, so they can read ``result`` / ``error`` even after the
    entry has been cleaned out of the registry (which avoids losing a
    real leader result to a racing cleanup). A separate ``event`` drives
    the wait/timeout signalling.
    """

    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: Exception | None = None


class Singleflight:
    """Per-key singleflight with timeout."""

    def __init__(self):
        self._inflight: dict[str, _Flight] = {}
        self._mutex = threading.Lock()

    def do(self, key: str, fn: Callable[[], Any], timeout: float = 30.0) -> Any:
        """Execute fn(), deduplicating concurrent calls for the same key.

        Returns the result of fn(). If another caller is already executing
        for this key, waits for their result.

        Raises :class:`TimeoutError` if the leader has not completed within
        ``timeout`` seconds, so the caller can take a degrade path instead
        of silently receiving ``None``.
        """
        with self._mutex:
            flight = self._inflight.get(key)
            if flight is None:
                flight = _Flight()
                self._inflight[key] = flight
                is_leader = True
            else:
                is_leader = False

        if is_leader:
            try:
                flight.result = fn()
            except Exception as exc:
                flight.error = exc
                raise
            finally:
                flight.event.set()
                self._schedule_cleanup(key, flight)
            return flight.result

        # Waiter: wait for the leader to finish, or raise on timeout.
        if not flight.event.wait(timeout=timeout):
            raise TimeoutError(
                f"singleflight wait timed out for key={key!r} after {timeout}s"
            )
        if flight.error is not None:
            raise flight.error
        return flight.result

    def _schedule_cleanup(self, key: str, flight: _Flight) -> None:
        """Pop the resolved entry asynchronously so late joiners restart."""

        def _cleanup() -> None:
            with self._mutex:
                if self._inflight.get(key) is flight:
                    self._inflight.pop(key, None)

        threading.Thread(target=_cleanup, daemon=True).start()


# Global instances for common use cases
company_name_flight = Singleflight()
watchlist_flight = Singleflight()
