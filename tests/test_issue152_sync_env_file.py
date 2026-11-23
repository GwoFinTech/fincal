"""Issue #152: the host-side scheduling entrypoint must read the deployed `.env`.

Issue #145 moved the Futu stage onto the signed WebAPI, whose credentials
(`FUTU_API_KEY`, `FUTU_API_SIGN_KEY`) reach the **container** through
`env_file: .env`.  The weekly job on the host runs `bash scripts/sync_all.sh`
with no such mechanism, so the first run after the switch recorded
`stage='futu' status='skipped' error_code='opend_unavailable'` while the very
same credentials probed `{'ok': True}` once `FUTU_ENV_FILE` was supplied by
hand.  The contract existed (`app/futu_webapi._env_file_settings()` only reads
`FUTU_*` keys from that file) — the entrypoint just never set the pointer.

What these tests pin down:

* a real `sync_all.sh` run in a temp tree, with a stub `uv`, exports
  `FUTU_ENV_FILE=<app dir>/.env` to the stage processes;
* the exported pointer really resolves into a credential pair (the stage would
  otherwise still degrade to "Futu unavailable → skipped");
* the file is **not** `source`d: its container-network dialect
  (`DB_HOST=host.docker.internal`, `KURUMI_API_URL=http://tsummt-api:8000`)
  must not replace the host-reachable defaults;
* an explicit `FUTU_ENV_FILE` in the environment wins;
* the image (no `.env`) keeps the variable empty — compose still supplies the
  credentials there;
* removing the wiring makes the first test red (reproduction, not just a
  description of the bug).
"""
from pathlib import Path
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

from app import config, futu_webapi  # noqa: E402

# Minimal stub `uv`: logs the invocation, and dumps the environment of the Futu
# stage so the test can assert what the stage subprocess actually received.
_MOCK_UV = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${CALL_LOG}"
if [[ "$*" == *"sync_futu.py"* ]]; then
    env > "${ENV_LOG}"
fi
exit 0
"""

_WIRING_START = "# Host-side scheduling entrypoint"


def _workspace(tmp_path, *, pipeline=None):
    """Copy the entrypoint chain into ``tmp_path`` with a stub ``uv`` on PATH."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("sync_all.sh", "cron_sync.sh"):
        body = pipeline if (pipeline is not None and name == "sync_all.sh") \
            else (SCRIPTS / name).read_text(encoding="utf-8")
        target = scripts / name
        target.write_text(body, encoding="utf-8")
        target.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(_MOCK_UV, encoding="utf-8")
    (bin_dir / "uv").chmod(0o755)
    return scripts, bin_dir


def _sign_key(tmp_path) -> Path:
    """A real Ed25519 key, so the env-file round trip can build a client."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = tmp_path / "futu_ed25519"
    path.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return path


def _deployed_env(tmp_path, key_path: Path) -> Path:
    """The deployed `.env`: real credentials plus container-network names."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DB_HOST=host.docker.internal\n"
        "DB_PASSWORD=docker-only\n"
        "KURUMI_API_URL=http://tsummt-api:8000\n"
        "FUTU_API_KEY=from-host-env-file\n"
        f"FUTU_API_SIGN_KEY={key_path}\n",
        encoding="utf-8",
    )
    return env_file


def _run(tmp_path, scripts, bin_dir, env=None, entrypoint="cron_sync.sh"):
    env_log = tmp_path / "futu.env.log"
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("FUTU_")  # a developer shell must not leak credentials
    }
    environment.update({
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALL_LOG": str(tmp_path / "calls.log"),
        "ENV_LOG": str(env_log),
        "TMPDIR": str(tmp_path),
        **(env or {}),
    })
    result = subprocess.run(["bash", str(scripts / entrypoint)], env=environment,
                            text=True, capture_output=True, cwd=str(tmp_path))
    stage_env = {}
    if env_log.exists():
        for line in env_log.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            stage_env[name] = value
    return result, stage_env


def _stage_client(stage_env, monkeypatch):
    """Resolve credentials the way the Futu stage does: through the exported
    pointer, not through the test process's own environment."""
    monkeypatch.setattr(config, "FUTU_ENV_FILE", stage_env.get("FUTU_ENV_FILE", ""))
    return futu_webapi.create_client()


def _without_wiring(pipeline: str) -> str:
    """The pipeline as it was before #152 (idempotent, so it also works when the
    fix is already missing — the reproduction test must not error on that)."""
    if _WIRING_START not in pipeline:
        return pipeline
    start = pipeline.index(_WIRING_START)
    end = pipeline.index("fi\n", pipeline.index('export FUTU_ENV_FILE="${APP_DIR}/.env"')) \
        + len("fi\n")
    return pipeline[:start] + pipeline[end:]


def test_the_futu_stage_receives_the_deployed_env_file(tmp_path, monkeypatch):
    """Acceptance 1: one `sync_all.sh` run is enough for the stage to see the
    credentials — no outside wrapper, no exported variable."""
    key_path = _sign_key(tmp_path)
    env_file = _deployed_env(tmp_path, key_path)
    scripts, bin_dir = _workspace(tmp_path)

    result, stage_env = _run(tmp_path, scripts, bin_dir)

    assert result.returncode == 0, result.stderr + result.stdout
    assert stage_env.get("FUTU_ENV_FILE") == str(env_file), stage_env.get("FUTU_ENV_FILE")

    # ... and the pointer is what the Futu stage actually consumes.  Resolve it
    # exactly like `scripts/sync_futu.py` does at import time: through config.
    assert "FUTU_API_KEY" not in stage_env  # values stay in the file, not the env
    client = _stage_client(stage_env, monkeypatch)
    assert client is not None, "the exported FUTU_ENV_FILE must yield a client (else: skipped)"
    assert client.api_key == "from-host-env-file"
    assert client.sign_key_path == str(key_path)


def test_the_env_file_is_not_sourced_into_the_stage_environment(tmp_path):
    """`DB_HOST`/`KURUMI_API_URL` in the deployed file speak container-network
    dialect; sourcing it would break the host run that this fix exists for."""
    key_path = _sign_key(tmp_path)
    _deployed_env(tmp_path, key_path)
    scripts, bin_dir = _workspace(tmp_path)

    _result, stage_env = _run(tmp_path, scripts, bin_dir)

    assert stage_env.get("DB_HOST") == "localhost"
    assert "KURUMI_API_URL" not in stage_env
    assert "DB_PASSWORD" not in stage_env


def test_an_explicit_env_file_in_the_environment_wins(tmp_path):
    """An operator override must not be replaced by the default."""
    key_path = _sign_key(tmp_path)
    explicit = tmp_path / "elsewhere.env"
    explicit.write_text(f"FUTU_API_KEY=explicit\nFUTU_API_SIGN_KEY={key_path}\n",
                        encoding="utf-8")
    _deployed_env(tmp_path, key_path)
    scripts, bin_dir = _workspace(tmp_path)

    _result, stage_env = _run(tmp_path, scripts, bin_dir, env={"FUTU_ENV_FILE": str(explicit)})

    assert stage_env.get("FUTU_ENV_FILE") == str(explicit)


def test_the_image_without_a_dot_env_is_unchanged(tmp_path):
    """Acceptance 3: the container has no `.env`; compose keeps supplying the
    credential pair and the entrypoint adds nothing."""
    scripts, bin_dir = _workspace(tmp_path)

    _result, stage_env = _run(tmp_path, scripts, bin_dir)

    assert not (tmp_path / ".env").exists()
    assert stage_env.get("FUTU_ENV_FILE", "") == ""


def test_without_the_wiring_the_futu_stage_gets_no_pointer(tmp_path, monkeypatch):
    """Reproduction: the same tree, minus those lines, is the 2026-11-23 run."""
    pipeline = _without_wiring((SCRIPTS / "sync_all.sh").read_text(encoding="utf-8"))
    assert "export FUTU_ENV_FILE" not in pipeline

    key_path = _sign_key(tmp_path)
    _deployed_env(tmp_path, key_path)
    scripts, bin_dir = _workspace(tmp_path, pipeline=pipeline)

    _result, stage_env = _run(tmp_path, scripts, bin_dir)

    assert stage_env.get("FUTU_ENV_FILE", "") == ""
    # The credentials in `.env` were fine all along; without the pointer the
    # stage still degrades to the `skipped / opend_unavailable` row #152 reports.
    assert _stage_client(stage_env, monkeypatch) is None


@pytest.fixture(autouse=True)
def _no_ambient_futu_credentials(monkeypatch):
    """`create_client()` reads config; keep it pointed at the test's temp file."""
    for name in ("FUTU_API_KEY", "FUTU_API_SIGN_KEY", "FUTU_API_BASE", "FUTU_HTTP_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "FUTU_API_KEY", "")
    monkeypatch.setattr(config, "FUTU_API_SIGN_KEY", "")
    monkeypatch.setattr(config, "FUTU_HTTP_PROXY", "")
    monkeypatch.setattr(config, "FUTU_ENV_FILE", "")
    yield
