"""Regression tests for the cron sync entrypoint (Issue #27)."""
from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"


def test_sync_shell_scripts_have_valid_syntax():
    for name in ("cron_sync.sh", "sync_all.sh"):
        result = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_cron_entrypoint_runs_pipeline_once_with_stock_names(tmp_path):
    """Mock uv and verify the wrapper invokes each stage exactly once."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("cron_sync.sh", "sync_all.sh"):
        target = scripts / name
        target.write_text((SCRIPTS / name).read_text())
        target.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"${CALL_LOG}\"\n"
    )
    (bin_dir / "uv").chmod(0o755)
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALL_LOG": str(log)}
    result = subprocess.run(["bash", str(scripts / "cron_sync.sh")], env=env, text=True,
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert calls == [
        "run python scripts/sync_earnings.py",
        "run python scripts/sync_futu.py",
        "run python scripts/sync_stock_names.py",
        "run python scripts/sync_consensus.py",
        "run python scripts/predict_earnings.py",
    ]


def test_cron_entrypoint_rejects_recursive_invocation(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("cron_sync.sh", "sync_all.sh"):
        (scripts / name).write_text((SCRIPTS / name).read_text())
    result = subprocess.run(["env", "_FINCAL_CRON_RUNNING=1", "bash", str(scripts / "cron_sync.sh")],
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert "called recursively" in result.stderr