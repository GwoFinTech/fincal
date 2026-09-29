"""Issue #67: the ``stock_names`` stage must carry its own verdict.

``stock_names`` is an on-demand cache: it is written only when a missing company
name is resolved, so ``MAX(stock_names.fetched_at)`` cannot distinguish "the
stage stopped running" from "there was nothing left to resolve".  It was
therefore removed from ``app/freshness.DERIVED_TABLES`` — the stage row in
``sync_runs`` is the signal now, which only works if the stage records a real
verdict:

* no pending target  ⇒ ``success`` and **zero** writes to ``stock_names``;
* targets but none resolved (or some) ⇒ ``failed`` with a language-neutral
  ``error_code``, so the freshness gate reports the stage instead of staying
  green while nothing was resolved.

The shell entrypoint wiring (the stage runs at all, and the gate runs after it)
stays covered by ``tests/test_sync_scripts.py``.
"""
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "sync_stock_names", ROOT / "scripts" / "sync_stock_names.py")
assert SPEC is not None and SPEC.loader is not None
sync_stock_names = importlib.util.module_from_spec(SPEC)
sys.modules["sync_stock_names"] = sync_stock_names
SPEC.loader.exec_module(sync_stock_names)

from app.company_name import NameResult  # noqa: E402

RESOLVED = NameResult(name="APPLE INC", source="kurumi")
UNRESOLVED = NameResult(name="", source="", error_code="all_sources_failed")


class _Cursor:
    """Records executed SQL so "no write" can be asserted directly."""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


def _targets(*symbols, market="US"):
    return [{"symbol": symbol, "market": market} for symbol in symbols]


class StageVerdictTests(TestCase):
    def _run(self, targets, results, *, run_id: int | None = 7, error=None):
        """Call ``run()`` with every IO surface mocked.

        Returns ``(exit_code, finish_run, cache_name, resolve)``.
        """
        cursor = _Cursor()

        @contextmanager
        def _db_cursor():
            yield cursor

        resolve = mock.MagicMock(side_effect=results)
        finish = mock.MagicMock(return_value=True)
        cache = mock.MagicMock()
        missing = mock.MagicMock(return_value=targets)
        if error is not None:
            missing.side_effect = error

        with mock.patch.object(sync_stock_names, "db_cursor", _db_cursor), \
                mock.patch.object(sync_stock_names, "init_db", mock.MagicMock()), \
                mock.patch.object(sync_stock_names, "missing_name_targets", missing), \
                mock.patch.object(sync_stock_names, "resolve_company_name_result", resolve), \
                mock.patch.object(sync_stock_names, "cache_name", cache), \
                mock.patch.object(sync_stock_names, "start_run",
                                  mock.MagicMock(return_value=run_id)), \
                mock.patch.object(sync_stock_names, "finish_run", finish):
            code = sync_stock_names.run()

        return code, finish, cache, resolve

    def test_no_pending_targets_writes_nothing_and_records_success(self):
        """The steady state: nothing to resolve is a *successful* run, and it
        must not touch ``stock_names`` (that write is what used to be mistaken
        for the stage's liveness)."""
        code, finish, cache, resolve = self._run([], [])

        self.assertEqual(code, 0)
        finish.assert_called_once()
        args, kwargs = finish.call_args
        self.assertEqual(args[0], 7)
        self.assertEqual(kwargs["status"], "success")
        self.assertIsNone(kwargs["error_code"])
        self.assertEqual(kwargs["record_count"], 0)
        self.assertEqual(kwargs["details"]["unresolved"], 0)
        cache.assert_not_called()
        resolve.assert_not_called()

    def test_resolved_names_are_cached_and_the_run_succeeds(self):
        code, finish, cache, _ = self._run(_targets("AAPL"), [RESOLVED])

        self.assertEqual(code, 0)
        cache.assert_called_once_with("AAPL", "US", "APPLE INC", "kurumi")
        self.assertEqual(finish.call_args.kwargs["status"], "success")
        self.assertEqual(finish.call_args.kwargs["record_count"], 1)

    def test_targets_that_all_fail_are_recorded_as_a_failed_run(self):
        """Acceptance 3: three providers down used to be a green run."""
        targets = _targets("AAPL", "MSFT")
        code, finish, cache, _ = self._run(targets, [UNRESOLVED, UNRESOLVED])

        self.assertEqual(code, 1)
        cache.assert_not_called()
        kwargs = finish.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["error_code"], "stock_names_unresolved")
        self.assertEqual(kwargs["record_count"], 0)
        self.assertEqual(kwargs["details"]["filled"], 0)
        self.assertEqual(kwargs["details"]["unresolved"], 2)
        self.assertEqual(kwargs["details"]["unresolved_symbols"], ["AAPL", "MSFT"])
        self.assertEqual(kwargs["details"]["unresolved_errors"], ["all_sources_failed"])

    def test_partial_resolution_is_reported_with_counts(self):
        targets = _targets("AAPL", "MSFT")
        code, finish, cache, _ = self._run(targets, [RESOLVED, UNRESOLVED])

        self.assertEqual(code, 1)
        cache.assert_called_once()
        kwargs = finish.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["error_code"], "stock_names_unresolved")
        self.assertEqual(kwargs["details"]["filled"], 1)
        self.assertEqual(kwargs["details"]["unresolved_symbols"], ["MSFT"])

    def test_a_run_already_in_flight_is_skipped_without_recording(self):
        code, finish, cache, _ = self._run(_targets("AAPL"), [RESOLVED], run_id=None)

        self.assertEqual(code, 0)
        finish.assert_not_called()
        cache.assert_not_called()

    def test_an_exception_marks_the_run_failed_and_propagates(self):
        code = None
        cursor = _Cursor()

        @contextmanager
        def _db_cursor():
            yield cursor

        finish = mock.MagicMock(return_value=True)
        with mock.patch.object(sync_stock_names, "db_cursor", _db_cursor), \
                mock.patch.object(sync_stock_names, "init_db", mock.MagicMock()), \
                mock.patch.object(sync_stock_names, "missing_name_targets",
                                  mock.MagicMock(side_effect=RuntimeError("db down"))), \
                mock.patch.object(sync_stock_names, "start_run",
                                  mock.MagicMock(return_value=7)), \
                mock.patch.object(sync_stock_names, "finish_run", finish):
            with self.assertRaises(RuntimeError):
                code = sync_stock_names.run()

        self.assertIsNone(code)
        kwargs = finish.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["error_code"], "stock_names_sync_failed")


class MainResultTests(TestCase):
    def test_main_returns_filled_and_unresolved(self):
        targets = _targets("AAPL", "MSFT", market="HK")
        cached = []
        with mock.patch.object(sync_stock_names, "init_db", mock.MagicMock()), \
                mock.patch.object(sync_stock_names, "missing_name_targets",
                                  mock.MagicMock(return_value=targets)), \
                mock.patch.object(sync_stock_names, "resolve_company_name_result",
                                  mock.MagicMock(side_effect=[RESOLVED, UNRESOLVED])), \
                mock.patch.object(sync_stock_names, "cache_name",
                                  mock.MagicMock(side_effect=lambda *a: cached.append(a))):
            filled, unresolved = sync_stock_names.main()

        self.assertEqual(filled, 1)
        self.assertEqual(unresolved, [("MSFT", "all_sources_failed")])
        self.assertEqual(cached, [("AAPL", "HK", "APPLE INC", "kurumi")])
