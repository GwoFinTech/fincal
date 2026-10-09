"""Periodic reaper for sync runs that outstayed their declared timeout (Issue #33).

``sync_runs.timeout_seconds`` is written for every run by
``sync_audit.start_run()`` and nothing read it: ``reap_timeout_runs()`` had no
caller anywhere in the repository, so the "timeout recovery" mechanism
described by Issues #3/#19 did not exist in practice.  A run whose process died
(a stage killed by the cron wrapper's ``timeout``, a container restart mid-run,
a provider call that never returned) kept ``status='running'`` until the next
container start, and until then ``/api/admin/sync-runs`` and the admin panel
rendered a job that was not running.

This module is the missing caller: the application lifespan starts one daemon
thread that runs a reaping pass every ``SYNC_RUN_REAPER_INTERVAL_SECONDS``
(default 300s) for as long as the process lives, and stops it on shutdown.
The web process is the only long-lived process in the deployment (the sync
stages themselves are short-lived cron runs), so this is where "periodically"
can actually be honoured.
"""
from __future__ import annotations

import logging
import threading

from . import config
from .sync_audit import reap_timeout_runs

logger = logging.getLogger(__name__)

THREAD_NAME = "sync-run-reaper"

_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None


def reap_once() -> int:
    """Run one reaping pass.

    Never raises: a transient database error must not kill the loop (the reaper
    is the only automatic recovery for a stranded ``running`` row, so silently
    stopping would be worse than a missed pass).  ``reap_timeout_runs`` reads
    the per-run ``timeout_seconds`` column.
    """
    try:
        return reap_timeout_runs()
    except Exception as exc:  # noqa: BLE001 — one failed pass must not end the loop
        logger.warning("timeout reaper pass failed: %s: %s", type(exc).__name__, exc)
        return 0


def run_reaper_loop(interval_seconds: float, stop_event: threading.Event) -> None:
    """Reap every ``interval_seconds`` until ``stop_event`` is set.

    The first pass happens one interval after startup: the lifespan has already
    called ``recover_stale_runs()`` for the runs of the previous process.
    """
    while not stop_event.wait(interval_seconds):
        reap_once()


def start_timeout_reaper(interval_seconds: float | None = None) -> threading.Event | None:
    """Start the reaper thread.

    Returns the stop event, or ``None`` when the reaper is disabled by
    ``SYNC_RUN_REAPER_ENABLED=false``. Calling it twice keeps the single running
    thread instead of starting a second one.
    """
    global _stop_event, _thread

    if not config.SYNC_RUN_REAPER_ENABLED:
        logger.info("timeout reaper disabled (SYNC_RUN_REAPER_ENABLED=false)")
        return None
    if _thread is not None and _thread.is_alive():
        return _stop_event

    interval = float(
        interval_seconds if interval_seconds is not None
        else config.SYNC_RUN_REAPER_INTERVAL_SECONDS
    )
    _stop_event = threading.Event()
    _thread = threading.Thread(
        target=run_reaper_loop,
        args=(interval, _stop_event),
        name=THREAD_NAME,
        daemon=True,
    )
    _thread.start()
    logger.info(
        "timeout reaper started: every %.0fs, cutoff per run (timeout_seconds)", interval)
    return _stop_event


def stop_timeout_reaper() -> None:
    """Signal the reaper thread to leave its loop and wait for it."""
    global _stop_event, _thread

    if _stop_event is not None:
        _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
    _stop_event = None
    _thread = None


def reaper_thread() -> threading.Thread | None:
    """The live reaper thread, or ``None`` (used by tests and diagnostics)."""
    return _thread
