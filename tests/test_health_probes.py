"""Issue #70: the Kurumi dependency probe must be able to fail.

``/api/admin/health``'s ``checks.kurumi`` used to fetch ``{KURUMI_API_URL}/api/config``
— a path FinCal answers itself with a constant 200 (``app/routers/api.py``) —
while ``KURUMI_API_URL`` defaulted to ``http://localhost:8000``, i.e. FinCal's own
listen port inside the production container. The "external dependency" probe
therefore reported ``healthy`` for as long as the process was alive, whatever the
state of Kurumi / tsummt-api.

Covered here:

* the probe asks the endpoint the client reads (the URL ``fetch_from_kurumi``
  builds), not a path FinCal serves itself;
* a self-referencing or unset base URL is ``degraded`` with the language-neutral
  ``dependency_not_configured`` and **no** call is made;
* a refused / timed out / 404 / empty upstream is ``degraded`` with
  ``kurumi_unreachable``;
* ``/api/admin/health`` surfaces the verdict without leaking credentials;
* ``.env.example``, the README table and ``scripts/deploy.sh`` document and gate
  the variable, so a missing value cannot stay invisible.
"""
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase, mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import company_name, config, db  # noqa: E402


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


def _forbid_call(*args, **kwargs):
    raise AssertionError("the probe must not call a base URL that points at FinCal")


class SelfReferenceTests(TestCase):
    """A base URL that resolves back to FinCal is not a probeable dependency."""

    def test_shipped_default_is_read_as_self(self):
        with mock.patch.object(config, "KURUMI_API_URL", "http://localhost:8000"), \
                mock.patch.object(config, "PORT", 8000):
            self.assertTrue(config.kurumi_target_is_self())

    def test_localhost_alias_and_any_scheme(self):
        for url in ("http://127.0.0.1:8000", "localhost:8000", "http://0.0.0.0:8000"):
            with self.subTest(url=url), \
                    mock.patch.object(config, "KURUMI_API_URL", url), \
                    mock.patch.object(config, "PORT", 8000):
                self.assertTrue(config.kurumi_target_is_self(), url)

    def test_unset_or_empty_counts_as_not_configured(self):
        for url in ("", "   "):
            with self.subTest(url=url), \
                    mock.patch.object(config, "KURUMI_API_URL", url), \
                    mock.patch.object(config, "PORT", 8000):
                self.assertTrue(config.kurumi_target_is_self())

    def test_another_host_or_port_is_not_self(self):
        for url in ("http://tsummt-api:8000", "http://localhost:9000",
                    "http://localhost", "http://kurumi.internal:8080"):
            with self.subTest(url=url), \
                    mock.patch.object(config, "KURUMI_API_URL", url), \
                    mock.patch.object(config, "PORT", 8000):
                self.assertFalse(config.kurumi_target_is_self(), url)

    def test_self_reference_is_reported_without_any_call(self):
        with mock.patch.object(config, "KURUMI_API_URL", "http://localhost:8000"), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", _forbid_call):
            result = company_name.probe_kurumi()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"],
                         company_name.ERROR_DEPENDENCY_NOT_CONFIGURED)

    def test_unset_base_url_is_reported_as_not_configured(self):
        with mock.patch.object(config, "KURUMI_API_URL", ""), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", _forbid_call):
            result = company_name.probe_kurumi()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"],
                         company_name.ERROR_DEPENDENCY_NOT_CONFIGURED)


class ProbePathTests(TestCase):
    """The probe must ask exactly what the client asks."""

    def _capture(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            return _Resp()

        return seen, fake_urlopen

    def test_probe_url_matches_the_client_url(self):
        seen, fake_urlopen = self._capture()
        with mock.patch.object(config, "KURUMI_API_URL", "http://kurumi.internal:9000"), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            result = company_name.probe_kurumi()
            client_url = company_name.kurumi_overview_url(
                company_name.KURUMI_PROBE_SYMBOL, company_name.KURUMI_PROBE_MARKET)
            seen.pop("url", None)
            name = company_name.fetch_from_kurumi(company_name.KURUMI_PROBE_SYMBOL,
                                                  company_name.KURUMI_PROBE_MARKET)

        self.assertEqual(result, {"status": "healthy"})
        self.assertEqual(name, "TENCENT")
        # Leading zeros are stripped by ``kurumi_symbol`` (existing client
        # behaviour), which the probe now inherits instead of hard-coding a path.
        self.assertEqual(client_url,
                         "http://kurumi.internal:9000/api/stock/700.HK/overview")
        self.assertEqual(seen["url"], client_url)
        self.assertNotIn("/api/config", seen["url"])

    def test_us_symbol_path_is_the_client_path(self):
        with mock.patch.object(config, "KURUMI_API_URL", "http://kurumi.internal:9000"):
            self.assertEqual(company_name.kurumi_overview_url("AAPL", "US"),
                             "http://kurumi.internal:9000/api/stock/AAPL.US/overview")

    def test_health_endpoint_no_longer_asks_for_its_own_config(self):
        source = (ROOT / "app" / "routers" / "admin.py").read_text()
        self.assertIn("probe_kurumi", source)
        self.assertNotIn('f"{config.KURUMI_API_URL}/api/config"', source)

    def test_trailing_slash_in_the_base_url_is_not_doubled(self):
        with mock.patch.object(config, "KURUMI_API_URL", "http://kurumi.internal:9000/"):
            self.assertEqual(company_name.kurumi_overview_url("0700.HK", "HK"),
                             "http://kurumi.internal:9000/api/stock/700.HK/overview")


class UnreachableTests(TestCase):
    """A real outage (refused / timed out / 404 / junk) is degraded."""

    def _probe(self, side_effect):
        with mock.patch.object(config, "KURUMI_API_URL", "http://kurumi.internal:9000"), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(side_effect=side_effect)):
            return company_name.probe_kurumi()

    def test_connection_refused_is_degraded(self):
        result = self._probe(URLError(ConnectionRefusedError(111, "refused")))
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"], company_name.ERROR_KURUMI_UNREACHABLE)

    def test_timeout_is_degraded(self):
        result = self._probe(URLError(TimeoutError("timed out")))
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"], company_name.ERROR_KURUMI_UNREACHABLE)

    def test_http_404_is_degraded(self):
        from email.message import Message
        result = self._probe(HTTPError("http://kurumi.internal:9000/x", 404,
                                       "Not Found", Message(), None))
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"], company_name.ERROR_KURUMI_UNREACHABLE)

    def test_empty_name_is_degraded(self):
        with mock.patch.object(config, "KURUMI_API_URL", "http://kurumi.internal:9000"), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(return_value=_Resp(b"{}"))):
            result = company_name.probe_kurumi()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"], company_name.ERROR_KURUMI_UNREACHABLE)

    def test_verdict_carries_no_url_or_credentials(self):
        result = self._probe(URLError(ConnectionRefusedError(111, "refused")))
        self.assertLessEqual(set(result), {"status", "error_code", "error"})
        for value in result.values():
            self.assertNotIn("://", str(value))


class HealthEndpointTests(TestCase):
    """The verdict reaches /api/admin/health, and the aggregate stops lying."""

    def _call_health(self):
        from app.routers import admin

        @contextmanager
        def _db_cursor():
            yield mock.MagicMock()

        ok_process = mock.MagicMock(return_value=mock.MagicMock(returncode=0))
        with mock.patch.object(db, "db_cursor", _db_cursor), \
                mock.patch("socket.create_connection", mock.MagicMock()), \
                mock.patch("subprocess.run", ok_process):
            return admin.health_check()

    def test_self_referencing_kurumi_is_not_healthy(self):
        with mock.patch.object(config, "KURUMI_API_URL", "http://localhost:8000"), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", _forbid_call):
            response = self._call_health()

        check = response["checks"]["kurumi"]
        self.assertNotEqual(check["status"], "healthy")
        self.assertEqual(check["error_code"],
                         company_name.ERROR_DEPENDENCY_NOT_CONFIGURED)
        self.assertNotEqual(response["status"], "healthy")

    def test_reachable_kurumi_is_healthy(self):
        with mock.patch.object(config, "KURUMI_API_URL", "http://kurumi.internal:9000"), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", mock.MagicMock(return_value=_Resp())):
            response = self._call_health()

        self.assertEqual(response["checks"]["kurumi"]["status"], "healthy")
        self.assertEqual(response["checks"]["kurumi"], {"status": "healthy"})

    def test_probe_verdict_is_not_shaped_as_a_freshness_check(self):
        """The verdict carries a status and a code — no freshness fields."""
        from app.schemas import HealthResponse

        with mock.patch.object(config, "KURUMI_API_URL", "http://localhost:8000"), \
                mock.patch.object(config, "PORT", 8000), \
                mock.patch("urllib.request.urlopen", _forbid_call):
            payload = HealthResponse(**self._call_health()).model_dump()

        check = payload["checks"]["kurumi"]
        self.assertLessEqual(set(check), {"status", "error_code", "error"})
        self.assertEqual(check["status"], "degraded")
        self.assertEqual(check["error_code"],
                         company_name.ERROR_DEPENDENCY_NOT_CONFIGURED)


class DocsAndGateTests(TestCase):
    """A missing KURUMI_API_URL must be visible, not silent."""

    def test_env_example_documents_the_variable(self):
        self.assertIn("KURUMI_API_URL", (ROOT / ".env.example").read_text())

    def test_readme_documents_the_variable(self):
        self.assertIn("KURUMI_API_URL", (ROOT / "README.md").read_text())

    def test_deploy_script_probes_the_running_container(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text()
        self.assertIn("probe_kurumi", deploy)
        self.assertIn("ACTION REQUIRED (Issue #70)", deploy)
