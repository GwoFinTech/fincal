"""Regression tests for Issue #79: the service must report itself truthfully.

``/api/admin/health`` is the only self-report fincal has, and for ~7 weeks both
of its readings were wrong in production:

* the aggregate ``status`` was pinned at ``degraded`` by ``checks.longbridge``:
  the Longbridge CLI is a **host-side** pipeline tool (``scripts/sync_*.py``
  under cron) while the image ships only ``app/`` plus the web dependencies, so
  ``subprocess.run(["longbridge", ...])`` raised ``FileNotFoundError`` on every
  request.  PostgreSQL / Kurumi / sync-freshness could be perfectly healthy and
  a real outage still looked exactly like that permanent noise;
* ``version`` was the constant ``"dev"``: ``app/version.py`` read ``VERSION``
  next to the source tree, which is outside the image build context, so no
  deployment could ever attest the commit it was running.

Covered here:

* the host-tool probe distinguishes "not installed here" (``not_applicable`` +
  ``tool_not_in_image``, no subprocess call) from "installed but failing", and
  the verdicts are asserted with ``shutil.which`` patched — never depending on
  whether the *test host* happens to have the CLI;
* ``not_applicable`` does not vote on the aggregate, so all-healthy dependencies
  report ``healthy`` while a real Kurumi outage still reports ``degraded`` with
  ``kurumi_unreachable``;
* ``get_version`` prefers the commit injected at build time, then a ``VERSION``
  file, then ``dev`` — and the build path actually injects it;
* ``scripts/check_service_health.py`` re-derives the aggregate and fails the
  deploy on a version mismatch, a host tool that votes again, or an aggregate
  that does not follow from the probes.
"""
import importlib.util
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase, mock
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import company_name, config, db, version  # noqa: E402
from app.routers import admin  # noqa: E402


def _load_gate():
    """Load ``scripts/check_service_health.py`` (a host-side script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "check_service_health", ROOT / "scripts" / "check_service_health.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_gate()

#: A freshness summary the health endpoint reads as ``healthy``.
HEALTHY_FRESHNESS = {
    "status": "healthy", "error_code": None, "threshold_hours": 36.0,
    "stale_stages": [], "never_run_stages": [], "stale_data": [],
    "checked_at": "2026-10-12T00:00:00+00:00", "entries": [],
}


class _Resp:
    """Minimal stand-in for an ``urlopen`` context manager."""

    def __init__(self, payload: bytes = b'{"name": "TENCENT"}', status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HostToolProbeTests(TestCase):
    """`not_applicable` is not `degraded`: the two reasons differ."""

    def _probe(self, which_result, run_side_effect=None, returncode=0):
        run = mock.MagicMock(return_value=mock.MagicMock(returncode=returncode))
        if run_side_effect is not None:
            run.side_effect = run_side_effect
        with mock.patch("shutil.which", mock.MagicMock(return_value=which_result)), \
                mock.patch("subprocess.run", run):
            return admin.probe_host_tool("longbridge"), run

    def test_missing_tool_is_not_applicable_and_never_runs(self):
        verdict, run = self._probe(None)
        self.assertEqual(verdict, {"status": "not_applicable",
                                   "error_code": "tool_not_in_image"})
        run.assert_not_called()

    def test_present_tool_that_answers_is_healthy(self):
        verdict, run = self._probe("/usr/local/bin/longbridge", returncode=0)
        self.assertEqual(verdict, {"status": "healthy"})

    def test_present_tool_with_nonzero_exit_is_degraded(self):
        verdict, _ = self._probe("/usr/local/bin/longbridge", returncode=2)
        self.assertEqual(verdict["status"], "degraded")
        self.assertEqual(verdict["error_code"], "tool_nonzero_exit")

    def test_present_tool_that_cannot_be_run_is_degraded(self):
        verdict, _ = self._probe("/usr/local/bin/longbridge",
                                 run_side_effect=FileNotFoundError("gone"))
        self.assertEqual(verdict["status"], "degraded")
        self.assertEqual(verdict["error_code"], "tool_probe_failed")

    def test_verdict_shape_stays_a_probe(self):
        for which_result in (None, "/usr/local/bin/longbridge"):
            with self.subTest(which=which_result):
                verdict, _ = self._probe(which_result)
                self.assertLessEqual(set(verdict), {"status", "error_code", "error"})

    def test_the_two_statuses_are_distinct_constants(self):
        self.assertNotEqual(admin.NOT_APPLICABLE, "degraded")
        # the gate re-derives the aggregate, so the constant must not drift
        self.assertEqual(GATE.NOT_APPLICABLE, admin.NOT_APPLICABLE)


class AggregateHealthTests(TestCase):
    """The aggregate must follow from the probes that can actually fail."""

    def _health(self, *, kurumi_url="http://kurumi.internal:9000",
                urlopen_side_effect=None, which_result=None, tool_returncode=0,
                futu_reachable=True):
        @contextmanager
        def _db_cursor():
            yield mock.MagicMock()

        socket_calls = (mock.MagicMock() if futu_reachable
                        else mock.MagicMock(side_effect=OSError("refused")))
        urlopen = mock.MagicMock(return_value=_Resp())
        if urlopen_side_effect is not None:
            urlopen = mock.MagicMock(side_effect=urlopen_side_effect)

        with mock.patch.object(db, "db_cursor", _db_cursor), \
                mock.patch("socket.create_connection", socket_calls), \
                mock.patch("shutil.which", mock.MagicMock(return_value=which_result)), \
                mock.patch("subprocess.run",
                           mock.MagicMock(return_value=mock.MagicMock(returncode=tool_returncode))), \
                mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(config, "KURUMI_API_URL", kurumi_url), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("app.freshness.check_freshness",
                           mock.MagicMock(return_value=dict(HEALTHY_FRESHNESS))):
            return admin.health_check()

    def test_host_tool_absent_while_dependencies_are_healthy_is_healthy(self):
        """The production reading: this used to be a permanent `degraded`."""
        response = self._health()
        self.assertEqual(response["checks"]["longbridge"],
                         {"status": "not_applicable",
                          "error_code": "tool_not_in_image"})
        self.assertEqual(response["checks"]["kurumi"]["status"], "healthy")
        self.assertEqual(response["checks"]["sync_freshness"]["status"], "healthy")
        self.assertEqual(response["status"], "healthy")

    def test_a_host_tool_that_exists_still_votes(self):
        response = self._health(which_result="/usr/local/bin/longbridge")
        self.assertEqual(response["checks"]["longbridge"]["status"], "healthy")
        self.assertEqual(response["status"], "healthy")

    def test_a_failing_host_tool_votes(self):
        response = self._health(which_result="/usr/local/bin/longbridge",
                                tool_returncode=1)
        self.assertEqual(response["checks"]["longbridge"]["status"], "degraded")
        self.assertEqual(response["status"], "degraded")

    def test_an_unreachable_kurumi_still_degrades(self):
        """AC 2, asserted without touching a deployed environment."""
        response = self._health(
            urlopen_side_effect=URLError(ConnectionRefusedError(111, "refused")))
        self.assertEqual(response["checks"]["kurumi"]["status"], "degraded")
        self.assertEqual(response["checks"]["kurumi"]["error_code"],
                         company_name.ERROR_KURUMI_UNREACHABLE)
        self.assertEqual(response["status"], "degraded")

    def test_an_unreachable_futu_still_degrades(self):
        response = self._health(futu_reachable=False)
        self.assertEqual(response["checks"]["futu"]["status"], "degraded")
        self.assertEqual(response["status"], "degraded")

    def test_the_health_endpoint_reports_the_injected_commit(self):
        with mock.patch.dict(os.environ, {version.COMMIT_ENV: "abc1234"}):
            response = self._health()
        self.assertEqual(response["version"], "abc1234")

    def test_the_schema_documents_the_not_applicable_verdict(self):
        from app.schemas import DependencyProbe
        doc = DependencyProbe.__doc__ or ""
        self.assertIn("not_applicable", doc)
        self.assertIn("tool_not_in_image", doc)


class VersionAttestationTests(TestCase):
    """`version` must describe the running code, not a file outside the image."""

    def _get(self, env, version_file=None):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(version, "_VERSION_FILE", str(version_file)):
            os.environ.pop(version.COMMIT_ENV, None)
            os.environ.update(env)
            return version.get_version()

    def test_injected_commit_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            path.write_text("from-file\n")
            self.assertEqual(self._get({version.COMMIT_ENV: "ebca105"}, path), "ebca105")

    def test_blank_injection_falls_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            path.write_text("7481023\n")
            self.assertEqual(self._get({version.COMMIT_ENV: "   "}, path), "7481023")

    def test_file_is_used_when_nothing_is_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            path.write_text("41b2958\n")
            self.assertEqual(self._get({}, path), "41b2958")

    def test_missing_file_falls_back_to_dev(self):
        self.assertEqual(self._get({}, Path("/nonexistent/VERSION")), "dev")

    def test_empty_file_falls_back_to_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "VERSION"
            path.write_text("\n")
            self.assertEqual(self._get({}, path), "dev")


class BuildPlumbingTests(TestCase):
    """The injection path has to exist end to end, or `version` is `dev` again."""

    def test_dockerfile_declares_the_build_argument(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("ARG GIT_COMMIT=dev", dockerfile)
        self.assertIn(f"ENV {version.COMMIT_ENV}=$GIT_COMMIT", dockerfile)

    def test_compose_passes_the_build_argument(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("GIT_COMMIT: ${GIT_COMMIT:-dev}", compose)

    def test_deploy_exports_the_commit_before_building(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text()
        self.assertIn('export GIT_COMMIT="$COMMIT"', deploy)
        self.assertLess(deploy.index('export GIT_COMMIT="$COMMIT"'),
                        deploy.index("docker compose build"))

    def test_deploy_runs_the_self_report_gate(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text()
        self.assertIn("scripts/check_service_health.py", deploy)
        self.assertIn('--expected-version "$COMMIT"', deploy)

    def test_env_example_and_readme_declare_the_variable(self):
        self.assertIn("FINCAL_GIT_COMMIT", (ROOT / ".env.example").read_text())
        self.assertIn("FINCAL_GIT_COMMIT", (ROOT / "README.md").read_text())


class DeployGateTests(TestCase):
    """`scripts/check_service_health.py` is the falsifier of the two readings."""

    def setUp(self):
        self.healthy = {
            "status": "healthy",
            "version": "ebca105",
            "checks": {
                "postgresql": {"status": "healthy"},
                "kurumi": {"status": "healthy"},
                "futu": {"status": "healthy"},
                "longbridge": {"status": "not_applicable",
                               "error_code": "tool_not_in_image"},
                "sync_freshness": {"status": "healthy", "error_code": None},
            },
        }

    def test_a_truthful_report_passes(self):
        self.assertEqual(GATE.evaluate(json.loads(json.dumps(self.healthy)), "ebca105"), [])

    def test_a_stale_dev_version_fails(self):
        report = dict(self.healthy, version="dev")
        problems = GATE.evaluate(report, "ebca105")
        self.assertTrue(problems)
        self.assertIn("dev", problems[0])
        self.assertIn("ebca105", problems[0])

    def test_a_host_tool_that_votes_again_fails(self):
        report = json.loads(json.dumps(self.healthy))
        report["checks"]["longbridge"] = {"status": "degraded"}
        problems = GATE.evaluate(report, "ebca105")
        self.assertTrue(any("longbridge" in problem for problem in problems))

    def test_a_missing_host_tool_check_fails(self):
        report = json.loads(json.dumps(self.healthy))
        del report["checks"]["longbridge"]
        problems = GATE.evaluate(report, "ebca105")
        self.assertTrue(any("longbridge" in problem for problem in problems))

    def test_a_degraded_aggregate_with_healthy_dependencies_fails(self):
        report = dict(self.healthy, status="degraded")
        problems = GATE.evaluate(report, "ebca105")
        self.assertTrue(any("expected 'healthy'" in problem for problem in problems))

    def test_an_aggregate_that_hides_a_real_outage_fails(self):
        report = json.loads(json.dumps(self.healthy))
        report["checks"]["kurumi"] = {"status": "degraded",
                                      "error_code": "kurumi_unreachable"}
        problems = GATE.evaluate(report, "ebca105")
        self.assertTrue(any("expected 'degraded'" in problem for problem in problems))

    def test_a_real_outage_reported_honestly_passes(self):
        report = json.loads(json.dumps(self.healthy))
        report["status"] = "degraded"
        report["checks"]["kurumi"] = {"status": "degraded",
                                      "error_code": "kurumi_unreachable"}
        self.assertEqual(GATE.evaluate(report, "ebca105"), [])

    def test_not_ready_outranks_degraded(self):
        self.assertEqual(GATE.expected_aggregate(["healthy", "degraded", "not_ready"]),
                         "not_ready")
        self.assertEqual(GATE.expected_aggregate(["not_ready", "not_applicable"]),
                         "not_ready")

    def test_not_applicable_alone_is_healthy(self):
        self.assertEqual(GATE.expected_aggregate(["not_applicable"]), "healthy")

    def test_the_gate_is_read_only(self):
        source = (ROOT / "scripts" / "check_service_health.py").read_text()
        for forbidden in ("POST", "PUT", "PATCH", "DELETE", "psycopg2", "UPDATE "):
            self.assertNotIn(forbidden, source)

    def test_default_version_file_is_the_deployed_tree(self):
        self.assertEqual(Path(GATE.default_version_file()).name, "VERSION")
