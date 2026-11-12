"""History depth: the sync window must be configurable, paced and retryable.

Why this exists: every stored symbol had at most 4 past quarters (measured
2026-11-12: 4,150 of 9,989 symbols had exactly 4, 2,530 had 2, 1,003 had 1, and
only 160 had 5 or more), because ``sync_earnings`` looked back a hard-coded 180
days and ``sync_futu`` 365.  A period ratio needs the *same quarter one year
earlier*, so 同比 was structurally unavailable: only 634 fiscal periods in the
whole table had a prior-year counterpart, and the default universe could compute
zero of them.

Deepening the window is one CLI call per calendar day, so it has to survive two
provider realities:

* the calendar asks for ~1,100 pages over three years — the old fixed
  ``max_iterations = 800`` would have stopped the loop and silently truncated the
  history it was asked for;
* the provider rate-limits per window (``429002``) — a burst fails the whole
  stage, which is what the user asked to avoid, so calls are paced and a refusal
  is retried with exponential backoff instead of killing the run.

Pinned here: the depth comes from config, the window drives the iteration budget,
the interval is honoured between calls, a quota refusal is retried (and a
non-quota failure is not), and exhausting the retries still fails the stage.
"""
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SYNC_EARNINGS = ROOT / "scripts" / "sync_earnings.py"
SYNC_FUTU = ROOT / "scripts" / "sync_futu.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class CalendarDepthConfigTests(TestCase):
    def setUp(self):
        self.mod = _load("sync_earnings_depth", SYNC_EARNINGS)

    def test_the_window_comes_from_config(self):
        from app import config
        captured = {}

        def fake_fetch(market, start, end, **kwargs):
            captured.setdefault(market, []).append(start)
            return []

        with mock.patch.object(config, "SYNC_LOOKBACK_DAYS", 1095), \
             mock.patch.object(self.mod, "fetch_calendar", fake_fetch), \
             mock.patch.object(self.mod, "check_cancelled", lambda *a, **k: None):
            self.mod.sync_earnings(1, self.mod.SyncStats())

        depth = (date.today() - date.fromisoformat(captured["US"][0])).days
        self.assertGreaterEqual(depth, 1094, "a 3-year lookback must reach three years back")
        self.assertLessEqual(depth, 1096)
        self.assertEqual(captured["US"], captured["HK"], "both markets share one window")

    def test_a_run_can_override_the_depth_and_the_pacing(self):
        captured = {}

        def fake_fetch(market, start, end, **kwargs):
            captured[market] = (start, kwargs.get("interval_seconds"))
            return []

        with mock.patch.object(self.mod, "fetch_calendar", fake_fetch), \
             mock.patch.object(self.mod, "check_cancelled", lambda *a, **k: None):
            self.mod.sync_earnings(1, self.mod.SyncStats(), lookback_days=730,
                                   interval_seconds=1.5)

        start, interval = captured["US"]
        self.assertGreaterEqual((date.today() - date.fromisoformat(start)).days, 729)
        self.assertEqual(interval, 1.5)


class CalendarPacingTests(TestCase):
    def setUp(self):
        self.mod = _load("sync_earnings_pacing", SYNC_EARNINGS)

    def test_the_iteration_budget_follows_a_deep_window(self):
        """800 fixed iterations cannot page three years — the budget must scale."""
        sleeps = []
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            day = cmd[cmd.index("--start") + 1]
            # One event per call, cursor = next day, until the window ends.
            return _Result(json.dumps({
                "list": [{"date": day, "infos": [{"counter_id": "ST/US/AAPL"}]}],
                "next_date": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
            }))

        start = "2023-11-01"
        end = (date.fromisoformat(start) + timedelta(days=1200)).isoformat()
        with mock.patch.object(self.mod.subprocess, "run", fake_run), \
             mock.patch.object(self.mod, "_sleep_seconds", lambda s: sleeps.append(s), create=True):
            pages = self.mod.fetch_calendar("US", start, end, interval_seconds=0)

        self.assertEqual(calls["n"], 1201, "every day of the window must be paged, not 800")

    def test_each_call_is_separated_by_the_interval(self):
        sleeps = []

        def fake_run(cmd, **kwargs):
            day = cmd[cmd.index("--start") + 1]
            return _Result(json.dumps({
                "list": [{"date": day, "infos": []}],
                "next_date": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
            }))

        with mock.patch.object(self.mod.subprocess, "run", fake_run), \
             mock.patch("time.sleep", lambda s: sleeps.append(s)):
            self.mod.fetch_calendar("US", "2026-01-01", "2026-01-06", interval_seconds=0.75)

        self.assertTrue(sleeps, "pacing must actually wait between calls")
        self.assertTrue(all(s == 0.75 for s in sleeps), f"unexpected waits: {set(sleeps)}")

    def test_a_quota_refusal_is_waited_out_and_retried(self):
        sleeps = []
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return _Result(stderr='{"code":"429002","message":"频率太高"}', returncode=1)
            return _Result(json.dumps({"list": []}))

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            data = self.mod.fetch_calendar_page(
                "US", "2026-11-01", "2026-11-02",
                interval_seconds=0, max_retries=3, sleep=lambda s: sleeps.append(s),
            )

        self.assertEqual(data, {"list": []})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [5.0, 10.0], "backoff must grow exponentially")

    def test_a_non_quota_failure_is_not_retried(self):
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            return _Result(stderr="Authentication failed: token invalid", returncode=1)

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                self.mod.fetch_calendar_page("US", "2026-11-01", "2026-11-02",
                                             interval_seconds=0, max_retries=3, sleep=lambda s: None)
        self.assertEqual(calls["n"], 1, "retrying a dead token just multiplies the failure")
        self.assertIn("longbridge_cli_failed", str(ctx.exception))

    def test_exhausted_retries_still_fail_the_stage(self):
        def fake_run(cmd, **kwargs):
            return _Result(stderr="rate limit exceeded", returncode=1)

        with self.assertRaises(RuntimeError) as ctx:
            self.mod.fetch_calendar_page("US", "2026-11-01", "2026-11-02",
                                         interval_seconds=0, max_retries=1, sleep=lambda s: None)
        self.assertIn("longbridge_cli_failed", str(ctx.exception))

    def test_the_nested_failure_reports_a_fetch_error(self):
        with mock.patch.object(self.mod, "fetch_calendar_page",
                               side_effect=RuntimeError("longbridge_cli_failed:US:x")):
            with self.assertRaises(RuntimeError) as ctx:
                self.mod.fetch_calendar("US", "2026-11-01", "2026-11-02", interval_seconds=0)
        self.assertIn("longbridge_fetch_failed", str(ctx.exception))


class FutuDepthTests(TestCase):
    def test_the_futu_cutoff_comes_from_config(self):
        source = SYNC_FUTU.read_text(encoding="utf-8")
        self.assertIn("config.SYNC_FUTU_LOOKBACK_DAYS", source)
        self.assertNotIn("cutoff = date.today() - timedelta(days=365)", source)

        from app import config
        self.assertEqual(config.SYNC_FUTU_LOOKBACK_DAYS, 365, "the scheduled default must not change")

    def test_the_statement_depth_comes_from_config(self):
        """``num`` is one call's parameter, so depth costs no extra API budget."""
        source = SYNC_FUTU.read_text(encoding="utf-8")
        self.assertIn("num=config.SYNC_FUTU_STATEMENT_PERIODS", source)
        self.assertNotIn("financial_type=9, num=4,", source)

        from app import config
        self.assertEqual(config.SYNC_FUTU_STATEMENT_PERIODS, 4, "the scheduled default must not change")

    def test_the_longbridge_default_stays_at_the_scheduled_window(self):
        """Only an explicit backfill run deepens the window; the cron path does not."""
        from app import config
        self.assertEqual(config.SYNC_LOOKBACK_DAYS, 180)
        self.assertEqual(config.SYNC_CALENDAR_INTERVAL_SECONDS, 0.5)
        source = SYNC_EARNINGS.read_text(encoding="utf-8")
        self.assertIn("config.SYNC_LOOKBACK_DAYS if lookback_days is None", source)
