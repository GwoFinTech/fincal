"""Keyed singleflight utility (Issue #8).

Ensures that concurrent requests for the same key only trigger one
upstream call. Other callers wait for the first result.
"""
from __future__ import annotations

import threading
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class Singleflight:
    """Per-key singleflight with timeout."""

    def __init__(self):
        self._inflight: dict[str, tuple[threading.Event, Any, Exception | None]] = {}
        self._mutex = threading.Lock()

    def do(self, key: str, fn: Callable[[], Any], timeout: float = 30.0) -> Any:
        """Execute fn(), deduplicating concurrent calls for the same key.

        Returns the result of fn(). If another caller is already executing
        for this key, waits for their result.
        """
        with self._mutex:
            if key in self._inflight:
                event, result, error = self._inflight[key]
                # Another caller is working — wait
                self._mutex.release()
                event.wait(timeout=timeout)
                self._mutex.acquire()
                _, result, error = self._inflight.get(key, (None, None, None))
                if error:
                    raise error
                return result

            event = threading.Event()
            self._inflight[key] = (event, None, None)

        # We are the caller
        try:
            result = fn()
            with self._mutex:
                self._inflight[key] = (event, result, None)
            event.set()
            return result
        except Exception as exc:
            with self._mutex:
                self._inflight[key] = (event, None, exc)
            event.set()
            raise
        finally:
            # Cleanup after a short delay to let waiters read
            def _cleanup():
                with self._mutex:
                    entry = self._inflight.get(key)
                    if entry and entry[0].is_set():
                        self._inflight.pop(key, None)

            t = threading.Thread(target=_cleanup, daemon=True)
            t.start()


# Global instances for common use cases
company_name_flight = Singleflight()
watchlist_flight = Singleflight()
