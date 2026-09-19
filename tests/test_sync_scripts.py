"""Regression tests for the cron sync entrypoint (Issues #27, #57).

Issue #27 covered the entrypoint running the pipeline once, including the
``stock_names`` stage.  Issue #57 adds the execution policy the out-of-repo
cron wrapper had carried — and which the stages silently lost when the
scheduler kept its own copy of the stage list:

* every stage runs under ``timeout`` (a hung provider cannot stall the weekly
  job), with a per-stage budget overridable through ``FINCAL_STAGE_TIMEOUT_*``;
* a failing stage is isolated: the remaining stages still run, and the run
  still ends with a non-zero exit code;
* ``scripts/check_sync_freshness.py`` is wired as a hard gate **after** the
  stages, and its verdict is propagated to the scheduled job's exit code;
* the entrypoint chain (``cron_sync.sh`` → ``sync_all.sh``) invokes exactly the
  scripts registered in ``app.freshness.STAGE_SCRIPTS`` plus the gate, so a
  stage cannot be dropped from the entrypoint without a test failing.
"""
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

from app.freshness import STAGE_SCRIPTS  # noqa: E402

PIPELINE_SCRIPTS = ("sync_all.sh", "cron_sync.sh")

# Mock `uv` behaviour is driven by these environment variables:
#   FAIL_MATCH/FAIL_CODE  → exit FAIL_CODE when the args contain FAIL_MATCH
#   STDERR_MATCH/STDERR_LINE → print a line on stderr for matching args
#   SLEEP_MATCH/SLEEP_SECONDS → sleep before exiting (for the timeout test)
_MOCK_UV = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${CALL_LOG}"
if [[ -n "${STDERR_MATCH:-}" && "$*" == *"${STDERR_MATCH}"* ]]; then
    printf '%s\\n' "${STDERR_LINE:-mock error}" >&2
fi
if [[ -n "${FAIL_MATCH:-}" && "$*" == *"${FAIL_MATCH}"* ]]; then
    exit "${FAIL_CODE:-1}"
fi
if [[ -n "${SLEEP_MATCH:-}" && "$*" == *"${SLEEP_MATCH}"* ]]; then
    sleep "${SLEEP_SECONDS:-3}"
fi
exit 0
"""

# Same mock, but the freshness gate's verdict is simulated from the stage
# calls that were actually made — used to show that a stage dropped from the
# pipeline fails the scheduled run.  (Real gate verdicts for missing/stale
# stages are covered by tests/test_sync_freshness.py.)
_GATE_MOCK_UV = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${CALL_LOG}"
if [[ "$*" == *"check_sync_freshness.py"* ]]; then
    status=0
    for stage in sync_earnings sync_futu sync_stock_names sync_consensus predict_earnings; do
        if ! grep -q "scripts/${stage}.py" "${CALL_LOG}"; then
            printf 'STALE: stage %s never recorded a successful run\\n' "${stage}" >&2
            status=1
        fi
    done
    exit "${status}"
fi
exit 0
"""


def _workspace(tmp_path, *, pipeline=None, uv=_MOCK_UV):
    """Copy the entrypoint chain into tmp_path, with a mock `uv` on PATH."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in PIPELINE_SCRIPTS:
        body = pipeline if (pipeline is not None and name == "sync_all.sh") \
            else (SCRIPTS / name).read_text()
        target = scripts / name
        target.write_text(body)
        target.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(uv)
    (bin_dir / "uv").chmod(0o755)
    return scripts, bin_dir


def _run(tmp_path, scripts, bin_dir, env=None, entrypoint="cron_sync.sh"):
    log = tmp_path / "calls.log"
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALL_LOG": str(log),
        # Keep the per-stage logs of a failing/timeouting stage inside the test
        # directory instead of littering the system temp dir.
        "TMPDIR": str(tmp_path),
        **(env or {}),
    }
    result = subprocess.run(["bash", str(scripts / entrypoint)], env=environment,
                            text=True, capture_output=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_sync_shell_scripts_have_valid_syntax():
    for name in PIPELINE_SCRIPTS:
        result = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_cron_entrypoint_runs_pipeline_once_with_stock_names(tmp_path):
    """Mock uv and verify the wrapper invokes each stage exactly once."""
    scripts, bin_dir = _workspace(tmp_path)
    result, calls = _run(tmp_path, scripts, bin_dir)

    assert result.returncode == 0, result.stderr
    assert calls == [
        "run python scripts/sync_earnings.py",
        "run python scripts/sync_futu.py",
        "run python scripts/sync_stock_names.py",
        "run python scripts/sync_consensus.py",
        "run python scripts/predict_earnings.py",
        "run python scripts/check_sync_freshness.py",
    ]
    # The gate must run last: judging freshness before the stages would fail
    # the first catch-up run instead of the run that skipped a stage.
    assert calls[-1].endswith("check_sync_freshness.py")
    for stage in ("longbridge", "futu", "stock_names", "consensus", "prediction"):
        assert re.search(rf"^{stage}\s+ok\s+\d+s$", result.stdout, re.M), result.stdout


def test_cron_entrypoint_rejects_recursive_invocation(tmp_path):
    scripts, _ = _workspace(tmp_path)
    result = subprocess.run(["env", "_FINCAL_CRON_RUNNING=1", "bash", str(scripts / "cron_sync.sh")],
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert "called recursively" in result.stderr


def test_a_failing_stage_does_not_stop_the_remaining_stages(tmp_path):
    """Issue #57: the old `set -e` pipeline aborted at the first failure, which
    is how `stock_names` and `consensus` stopped being reached at all."""
    scripts, bin_dir = _workspace(tmp_path)
    result, calls = _run(tmp_path, scripts, bin_dir, env={"FAIL_MATCH": "sync_futu.py",
                                                          "FAIL_CODE": "3"})

    assert result.returncode != 0
    invoked = [call.split()[-1] for call in calls]
    assert invoked == [
        "scripts/sync_earnings.py",
        "scripts/sync_futu.py",
        "scripts/sync_stock_names.py",
        "scripts/sync_consensus.py",
        "scripts/predict_earnings.py",
        "scripts/check_sync_freshness.py",
    ]
    assert "futu: FAILED with exit 3" in result.stdout
    assert re.search(r"^futu\s+failed\s+\d+s$", result.stdout, re.M), result.stdout


def test_a_stage_that_exceeds_its_budget_is_timed_out_and_reported(tmp_path):
    scripts, bin_dir = _workspace(tmp_path)
    result, calls = _run(tmp_path, scripts, bin_dir, env={
        "SLEEP_MATCH": "sync_earnings.py",
        "SLEEP_SECONDS": "10",
        "FINCAL_STAGE_TIMEOUT_LONGBRIDGE": "1",
    })

    assert result.returncode != 0
    assert "longbridge: TIMED OUT after 1s" in result.stdout
    assert re.search(r"^longbridge\s+timeout\s+\d+s$", result.stdout, re.M), result.stdout
    # A timed-out stage must not block the rest of the pipeline.
    assert len(calls) == 6
    assert "stage logs kept in" in result.stdout


def test_stale_stages_fail_the_scheduled_run_and_name_the_stage(tmp_path):
    """Acceptance 3: a skipped/stale stage makes the job exit non-zero and the
    stale stage names are printed for the operator."""
    scripts, bin_dir = _workspace(tmp_path)
    result, calls = _run(tmp_path, scripts, bin_dir, env={
        "FAIL_MATCH": "check_sync_freshness.py",
        "FAIL_CODE": "1",
        "STDERR_MATCH": "check_sync_freshness.py",
        "STDERR_LINE": "STALE: stage 'consensus' last succeeded at 2026-08-02T09:27:43Z (1141.6h ago)",
    })

    assert result.returncode != 0
    assert calls[-1].endswith("check_sync_freshness.py")
    assert "consensus" in result.stderr
    assert "FRESHNESS GATE FAILED" in result.stderr
    # The pipeline itself succeeded; only the gate failed.
    assert "pipeline_status=0 gate_status=1" in result.stdout


def test_removing_a_stage_from_the_pipeline_fails_the_scheduled_run(tmp_path):
    """Acceptance 3 (dry-run demonstration): drop `sync_consensus` from the
    pipeline and the run ends non-zero, naming the missing stage."""
    pipeline = (SCRIPTS / "sync_all.sh").read_text()
    trimmed = "\n".join(line for line in pipeline.splitlines()
                        if "scripts/sync_consensus.py" not in line) + "\n"
    assert "scripts/sync_consensus.py" not in trimmed

    scripts, bin_dir = _workspace(tmp_path, pipeline=trimmed, uv=_GATE_MOCK_UV)
    result, _ = _run(tmp_path, scripts, bin_dir)

    assert result.returncode != 0
    assert "stage sync_consensus never recorded a successful run" in result.stderr
    assert "FRESHNESS GATE FAILED" in result.stderr


def test_entrypoint_chain_covers_every_registered_stage_and_the_gate():
    """Acceptance 4: the scheduling entrypoint — never a copy of the stage list
    — must reach every stage of the pipeline plus the freshness gate."""
    entrypoint = (SCRIPTS / "cron_sync.sh").read_text()
    pipeline = (SCRIPTS / "sync_all.sh").read_text()

    assert "sync_all.sh" in entrypoint
    assert "scripts/check_sync_freshness.py" in entrypoint
    gate_at = entrypoint.index("scripts/check_sync_freshness.py")
    pipeline_at = entrypoint.index("sync_all.sh")
    assert pipeline_at < gate_at, "the gate must be invoked after the pipeline"

    invoked = set(re.findall(r"python\s+(scripts/[\w./-]+\.py)", pipeline))
    assert invoked == set(STAGE_SCRIPTS.values()), (
        "scripts/sync_all.sh must invoke exactly the scripts registered in "
        "app/freshness.STAGE_SCRIPTS"
    )


def test_every_pipeline_stage_declares_a_timeout_budget():
    """Each stage runs through `run_stage` with its own wall-clock budget."""
    pipeline = (SCRIPTS / "sync_all.sh").read_text()
    pairs = re.findall(
        r'run_stage\s+(\w+)\s+"\$\{(TIMEOUT_[A-Z_]+)\}"\s+uv run python (scripts/[\w./-]+\.py)',
        pipeline,
    )
    assert pairs, "sync_all.sh must invoke every stage through run_stage"
    for stage, budget_var, script in pairs:
        assert re.search(rf'^{budget_var}="\$\{{FINCAL_STAGE_TIMEOUT_', pipeline, re.M), \
            f"{budget_var} must be defined and overridable via the environment"
        assert STAGE_SCRIPTS[stage] == script, f"{stage} must map to {script}"
    assert {script for _, _, script in pairs} == set(STAGE_SCRIPTS.values())
    assert {stage for stage, _, _ in pairs} == set(STAGE_SCRIPTS)
    # The budget is only real if the stage actually runs under `timeout`.
    assert "timeout --kill-after=" in pipeline
