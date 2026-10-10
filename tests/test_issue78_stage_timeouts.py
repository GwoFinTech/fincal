"""Regression tests for Issue #78: a stage's own budget must reach its run row.

`sync_runs.timeout_seconds` is what decides when the reaper may reclaim a
`running` row (Issue #33), but only the *reader* half had been wired: `start_run()`
wrote its 3600s default for every run while the real per-stage budget lived in
`scripts/sync_all.sh` (900 / 1500 / 900 / 2400 / 600) and nothing read it.  Two
consequences:

* a stage killed by the cron wrapper's `timeout` kept a ghost `running` row until
  the 3600s cutoff — ~55 minutes for the 600s-budgeted prediction stage, during
  which a manual re-run answered "already running, skipping" and refreshed
  nothing;
* a stage whose real budget exceeded 3600s was reaped *while alive*, and its real
  terminal state was silently discarded (`finish_run` only transitions rows still
  in `running`), which then made `app/freshness.py` report the stage as stale.

Covers:
- the shell defaults and `app.config.STAGE_TIMEOUT_SECONDS` are one table, and
  the budgets are exported so a stage process can actually read them;
- every stage script declares `timeout_seconds=…stage_timeout("<stage>")`;
- the checkers catch one-sided drift (reverse verification of both halves);
- the long loops heartbeat progress, never more often than once a minute;
- `finish_run` warns instead of silently dropping a terminal state.
"""
from pathlib import Path
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from unittest import TestCase, skipUnless
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, db, sync_audit  # noqa: E402

PIPELINE = ROOT / "scripts" / "sync_all.sh"

#: The stages the pipeline drives, with the script that owns each one.
STAGE_SCRIPTS = {
    "longbridge": "scripts/sync_earnings.py",
    "futu": "scripts/sync_futu.py",
    "stock_names": "scripts/sync_stock_names.py",
    "consensus": "scripts/sync_consensus.py",
    "prediction": "scripts/predict_earnings.py",
}

#: An on-demand script outside the pipeline; its budget lives in the same table.
OFF_PIPELINE_SCRIPTS = {"futu_eps_field_fix": "scripts/fix_futu_eps_field.py"}

_RUN_STAGE = re.compile(
    r'run_stage\s+(?P<stage>\w+)\s+"\$\{(?P<var>TIMEOUT_[A-Z_]+)\}"\s+'
    r'uv run python (?P<script>scripts/[\w./-]+\.py)'
)
_BUDGET_DEFAULT = re.compile(
    r'^(?P<var>TIMEOUT_[A-Z_]+)="\$\{FINCAL_STAGE_TIMEOUT_(?P<env>[A-Z_]+):-(?P<default>\d+)\}"',
    re.M,
)


def _shell_stage_budgets(text: str) -> dict[str, int | None]:
    """``stage -> default budget`` as written in ``sync_all.sh``.

    ``None`` when a `run_stage` line's budget variable is not declared, so a
    stage that lost its budget is reported rather than silently skipped.
    """
    defaults = {m.group("var"): int(m.group("default")) for m in _BUDGET_DEFAULT.finditer(text)}
    return {m.group("stage"): defaults.get(m.group("var")) for m in _RUN_STAGE.finditer(text)}


def _budget_mismatches(budgets: dict[str, int | None]) -> dict[str, tuple]:
    """Stages whose shell default disagrees with the canonical Python table."""
    return {
        stage: (budget, config.STAGE_TIMEOUT_SECONDS.get(stage))
        for stage, budget in budgets.items()
        if budget != config.STAGE_TIMEOUT_SECONDS.get(stage)
    }


_WIRING = re.compile(
    r'timeout_seconds=(?:config\.)?stage_timeout\(\s*'
    r'(?:"(?P<literal>[a-z_]+)"|(?P<const>[A-Z_][A-Z0-9_]*))\s*\)'
)


def _declared_timeouts(source: str, stage: str) -> int:
    """How many `start_run` calls in ``source`` declare ``stage``'s own budget.

    The stage may be written literally or through a module constant (the refill
    script uses ``REFILL_STAGE``); either way the value must be the stage's own
    name, so a copy-pasted call site cannot claim another stage's budget.
    """
    count = 0
    for match in _WIRING.finditer(source):
        if match.group("literal") == stage:
            count += 1
        elif re.search(rf'^{match.group("const")} = "{re.escape(stage)}"', source, re.M):
            count += 1
    return count


def _start_run_calls(source: str) -> int:
    return source.count("start_run(")


# ── the two halves of the budget ───────────────────────────────────────────

class StageBudgetContractTests(TestCase):
    def setUp(self):
        self.pipeline = PIPELINE.read_text()

    def test_the_shell_defaults_are_the_canonical_table(self):
        budgets = _shell_stage_budgets(self.pipeline)
        self.assertEqual(set(budgets), set(STAGE_SCRIPTS),
                         "sync_all.sh must drive exactly the registered stages")
        self.assertEqual(_budget_mismatches(budgets), {},
                         "app.config.STAGE_TIMEOUT_SECONDS and the shell defaults "
                         "are one table; change both or neither")

    def test_the_canonical_table_covers_every_stage_and_nothing_else(self):
        self.assertEqual(set(config.STAGE_TIMEOUT_SECONDS),
                         set(STAGE_SCRIPTS) | set(OFF_PIPELINE_SCRIPTS))

    def test_the_pipeline_exports_every_stage_budget(self):
        """Without `export` the budget reaches `timeout` but never the stage."""
        for match in _BUDGET_DEFAULT.finditer(self.pipeline):
            env = f"FINCAL_STAGE_TIMEOUT_{match.group('env')}"
            self.assertIn(f'export {env}="', self.pipeline,
                          f"{env} must be exported, not just used by run_stage")

    def test_the_override_variable_is_the_one_the_shell_reads(self):
        name = "FINCAL_STAGE_TIMEOUT_LONGBRIDGE"
        self.assertIn(f"${{{name}:-", self.pipeline)
        with patch.dict(os.environ, {name: "1234"}):
            self.assertEqual(config.stage_timeout("longbridge"), 1234)

    def test_the_exports_reach_a_stage_process(self):
        """The budget must be in the *stage's* environment, not just in `timeout`."""
        block = "\n".join(
            line for line in self.pipeline.splitlines()
            if line.startswith(("TIMEOUT_", "export FINCAL_STAGE_TIMEOUT_"))
        )
        self.assertIn("export FINCAL_STAGE_TIMEOUT_", block)
        result = subprocess.run(
            ["bash", "-c", f'{block}\nenv | grep -E "^FINCAL_STAGE_TIMEOUT_" | sort'],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = dict(line.split("=", 1) for line in result.stdout.split())
        self.assertEqual(
            seen,
            {f"FINCAL_STAGE_TIMEOUT_{stage.upper()}": str(budget)
             for stage, budget in _shell_stage_budgets(self.pipeline).items()},
        )

    def test_an_unregistered_stage_cannot_fall_back_to_a_default(self):
        with self.assertRaises(KeyError):
            config.stage_timeout("not_a_stage")

    # ── reverse verification: one-sided changes must fail ──────────────────

    def test_a_changed_shell_default_is_detected(self):
        drifted = self.pipeline.replace(
            'TIMEOUT_LONGBRIDGE="${FINCAL_STAGE_TIMEOUT_LONGBRIDGE:-900}"',
            'TIMEOUT_LONGBRIDGE="${FINCAL_STAGE_TIMEOUT_LONGBRIDGE:-901}"',
            1,
        )
        self.assertNotEqual(drifted, self.pipeline,
                            "the fixture no longer matches sync_all.sh")
        self.assertIn("longbridge", _budget_mismatches(_shell_stage_budgets(drifted)))

    def test_a_dropped_stage_budget_is_detected(self):
        drifted = "\n".join(
            line for line in self.pipeline.splitlines()
            if "TIMEOUT_CONSENSUS=" not in line
        ) + "\n"
        self.assertIsNone(_shell_stage_budgets(drifted)["consensus"])


class StageScriptWiringTests(TestCase):
    """The declared budget must be the one the stage's own run row carries."""

    def _assert_wired(self, stage: str, relative: str):
        source = (ROOT / relative).read_text()
        calls = _start_run_calls(source)
        self.assertGreater(calls, 0, f"{relative} never starts a run")
        self.assertEqual(
            _declared_timeouts(source, stage), calls,
            f'{relative}: every start_run() must declare '
            f'timeout_seconds=stage_timeout("{stage}")',
        )

    def test_every_pipeline_stage_declares_its_own_budget(self):
        for stage, relative in STAGE_SCRIPTS.items():
            with self.subTest(stage=stage):
                self._assert_wired(stage, relative)

    def test_the_off_pipeline_refill_declares_its_own_budget(self):
        for stage, relative in OFF_PIPELINE_SCRIPTS.items():
            with self.subTest(stage=stage):
                self._assert_wired(stage, relative)

    def test_dropping_a_declared_budget_is_detected(self):
        """Acceptance 5: reverting one call site to the default must fail."""
        relative = STAGE_SCRIPTS["longbridge"]
        source = (ROOT / relative).read_text()
        drifted = source.replace(
            'timeout_seconds=stage_timeout("longbridge"),', "", 1)
        self.assertNotEqual(drifted, source, "the fixture no longer matches the call site")
        self.assertEqual(_declared_timeouts(source, "longbridge"),
                         _start_run_calls(source))
        self.assertNotEqual(_declared_timeouts(drifted, "longbridge"),
                            _start_run_calls(drifted))


# ── heartbeats keep a live run alive ───────────────────────────────────────

class HeartbeatThrottleTests(TestCase):
    def _throttle(self, interval, now):
        return sync_audit.HeartbeatThrottle(interval, clock=lambda: now[0])

    def test_the_first_beat_is_written_and_the_next_ones_are_throttled(self):
        now = [100.0]
        with patch.object(sync_audit, "heartbeat") as beat:
            beats = self._throttle(60.0, now)
            self.assertTrue(beats.maybe(1, phase="dates", current=0, total=10))
            now[0] += 59.0
            self.assertFalse(beats.maybe(1, phase="dates", current=5, total=10))
            now[0] += 1.0
            self.assertTrue(beats.maybe(1, phase="dates", current=6, total=10))
        self.assertEqual(beat.call_count, 2)
        beat.assert_called_with(1, phase="dates", current=6, total=10)

    def test_runs_are_throttled_independently(self):
        now = [0.0]
        with patch.object(sync_audit, "heartbeat") as beat:
            beats = self._throttle(60.0, now)
            self.assertTrue(beats.maybe(1))
            self.assertFalse(beats.maybe(1))
            self.assertTrue(beats.maybe(2))
        self.assertEqual([call.args[0] for call in beat.call_args_list], [1, 2])

    def test_the_default_interval_is_never_per_symbol(self):
        self.assertGreaterEqual(sync_audit.HEARTBEAT_MIN_INTERVAL_SECONDS, 60.0)

    def test_every_pipeline_stage_heartbeats_its_long_loop(self):
        for stage, relative in STAGE_SCRIPTS.items():
            source = (ROOT / relative).read_text()
            with self.subTest(stage=stage):
                self.assertIn("HeartbeatThrottle(", source)
                self.assertIn(".maybe(", source)


# ── a dropped terminal state is visible ────────────────────────────────────

class _FakeCursor:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def close(self):
        pass


@contextmanager
def _cursor(cursor):
    yield cursor


class FinishRunVisibilityTests(TestCase):
    """Acceptance 4: `finish_run` must not drop a terminal state in silence."""

    def _call(self, rowcount, **kwargs):
        with patch.object(sync_audit.db, "db_cursor", return_value=_cursor(_FakeCursor(rowcount))):
            return sync_audit.finish_run(42, **kwargs)

    def test_a_row_that_already_left_running_produces_a_warning(self):
        with self.assertLogs("app.sync_audit", level="WARNING") as logs:
            transitioned = self._call(0, status="success", record_count=7)

        self.assertFalse(transitioned)
        output = "\n".join(logs.output)
        self.assertIn("42", output)
        self.assertIn("success", output)

    def test_a_transitioned_row_does_not_warn(self):
        with self.assertNoLogs("app.sync_audit", level="WARNING"):
            self.assertTrue(self._call(1, status="success", record_count=7))

    def test_the_statement_still_only_touches_a_running_row(self):
        cursor = _FakeCursor(1)
        with patch.object(sync_audit.db, "db_cursor", return_value=_cursor(cursor)):
            sync_audit.finish_run(7, status="failed", error_code="x")
        self.assertIn("AND status='running'", cursor.executed[0][0])


class StageScriptsDoNotMaskTheWarningTests(TestCase):
    """A stage must not force a second terminal state over an already-closed row.

    `sync_futu` used to call `finish_run(status="interrupted")` unconditionally in
    its `finally`, which is a no-op after a recorded success — and would now warn
    on every successful run.  The belt-and-braces path must only run when the run
    could not record its own terminal state.
    """

    def test_futu_only_forces_a_terminal_state_when_none_was_written(self):
        source = (ROOT / STAGE_SCRIPTS["futu"]).read_text()
        self.assertIn("if not terminal:", source)
        self.assertRegex(source, r"terminal = finish_run\(")


# ── opt-in: the writer, the column and the reaper on real PostgreSQL ───────

@skipUnless(os.environ.get("FINCAL_TEST_DB") == "1",
            "set FINCAL_TEST_DB=1 with a reachable fincal DB (start_run's INSERT "
            "and the reaper's UPDATE run against a temp table inside a "
            "rolled-back transaction)")
class StartRunBudgetRoundTripTests(TestCase):
    """Acceptance 2: a run started by `start_run()` is reaped at its own budget.

    `tests/test_issue33_timeout_reaper.py` inserts `timeout_seconds` by hand; this
    covers the other half — the value the *stage* actually writes, through the
    production `start_run()` → `reap_timeout_runs()` path.
    """

    _TEMP_TABLE = """
        CREATE TEMP TABLE sync_runs (
            id SERIAL PRIMARY KEY,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT,
            symbol_count INTEGER NOT NULL DEFAULT 0,
            attempt INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            timeout_seconds INTEGER NOT NULL DEFAULT 3600,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            heartbeat_at TIMESTAMPTZ,
            phase TEXT,
            current INTEGER,
            total INTEGER,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            finished_at TIMESTAMPTZ,
            error_code TEXT
        )
    """

    def test_a_stage_budget_is_written_and_enforced_end_to_end(self):
        budget = config.stage_timeout("prediction")
        with db.db_connection() as conn:
            conn.autocommit = False
            with conn.cursor() as setup:
                setup.execute(self._TEMP_TABLE)

            @contextmanager
            def _transaction_cursor():
                with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
                    yield cur

            try:
                with patch.object(db, "db_cursor", _transaction_cursor):
                    stage_run = sync_audit.start_run(
                        "prediction", "algorithm", symbol_count=3,
                        idempotency_key="prediction:issue78",
                        timeout_seconds=budget)
                    # A run with no declared budget keeps the legacy default — the
                    # "before" half of the fix, so this test cannot pass by
                    # accident.
                    legacy_run = sync_audit.start_run("prediction", "algorithm")

                    self.assertIsNotNone(stage_run)
                    self.assertIsNotNone(legacy_run)
                    assert stage_run is not None and legacy_run is not None
                    with conn.cursor() as cur:
                        cur.execute("SELECT id, timeout_seconds FROM sync_runs ORDER BY id")
                        written = dict(cur.fetchall())
                    self.assertEqual(written[stage_run], budget)
                    self.assertEqual(written[legacy_run], sync_audit._DEFAULT_TIMEOUT_SECONDS)
                    self.assertLess(budget, sync_audit._DEFAULT_TIMEOUT_SECONDS)

                    # Both look like a stage killed by the cron wrapper's `timeout`:
                    # a heartbeat that stopped at its start, 700s ago — past the
                    # prediction budget, far short of the 3600s default.
                    with conn.cursor() as cur:
                        cur.execute("UPDATE sync_runs SET started_at = NOW() - INTERVAL '700 seconds', "
                                    "heartbeat_at = NOW() - INTERVAL '700 seconds'")
                    self.assertEqual(sync_audit.reap_timeout_runs(), 1)

                    with conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
                        cur.execute("SELECT id, status, error_code FROM sync_runs ORDER BY id")
                        rows = {row["id"]: row for row in cur.fetchall()}
                    self.assertEqual(rows[stage_run]["status"], "interrupted")
                    self.assertEqual(rows[stage_run]["error_code"], "timeout_reaper")
                    self.assertEqual(rows[legacy_run]["status"], "running",
                                     "the ghost row this issue is about must not be "
                                     "reclaimed before the generic default")

                    # The production table was shadowed by the temp table all along.
                    with conn.cursor() as probe:
                        probe.execute("SELECT count(*) FROM pg_catalog.pg_class "
                                      "WHERE relname='sync_runs' AND relpersistence='t'")
                        self.assertEqual(probe.fetchone()[0], 1)
            finally:
                conn.rollback()
