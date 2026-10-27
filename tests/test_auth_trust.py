"""Issue #102: identity must come from a verifiable proxy assertion, not a header.

Before this, ``get_current_user`` believed four plain headers, and nothing else in
the stack checked that they came from Traefik. Every container on the shared
``traefik_traefik-net`` bridge can reach ``http://fincal:8000`` directly, so a
co-tenant container could send ``X-User-Role: admin`` and get 200 from
``/api/admin/overview`` (measured in production, read-only), could read/write
another user's watchlist by sending their ``X-User-Id``, and could create rows in
``users`` through ``ensure_user``. An unparseable ``X-User-Id: abc`` was worse
still: ``int()`` raised, so a forged header produced a 500 ``internal_error`` and
a ``logger.exception`` stack trace — indistinguishable in the logs from a real
defect.

Covered here:

* with ``AUTH_TRUST_SECRET`` set, a request without the proxy header, with an
  empty one, or with a wrong one is refused with ``401 untrusted_proxy`` *before*
  any identity header is read (so a forged ``role: admin`` never reaches
  ``require_admin``, and no ``users`` row can be created);
* the value the proxy would inject (the compose label) is accepted and the
  identity is parsed;
* ``abc`` / ``0`` / ``-1`` / whitespace answer ``401 invalid_identity`` and log
  nothing above WARNING (no ``logger.exception``);
* with the secret empty, behaviour is unchanged: the same header sets are
  believed, and the response bodies that existing clients depend on
  (``{"detail": "Not authenticated"}``) are byte-identical;
* ``/api/config`` and ``/ical/{token}`` stay independent of the boundary;
* ``scripts/check_auth_trust.py`` fails the deploy when only one side of the
  pair is configured (env without the Traefik label, or a label whose value is
  not the required secret).
"""
import contextlib
import importlib.util
import io
import json
import logging
import sys
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient
from starlette.datastructures import Headers

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import auth, config  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.main import app  # noqa: E402

SECRET = "test-secret-value"
PROXY_HEADER = "X-Fincal-Proxy"
FORGED = {
    "X-User-Id": "999",
    "X-User-Role": "admin",
    "X-User-Email": "nobody@example.invalid",
    "X-User-Name": "Nobody",
}


class FakeRequest:
    """The only surface ``get_current_user`` touches, case-insensitively."""

    def __init__(self, headers: dict[str, str]):
        self.headers = Headers(headers)


def current_user(headers: dict[str, str]) -> dict:
    return auth.get_current_user(FakeRequest(headers))


def iter_routes(router):
    """Flatten this FastAPI version's lazily-wrapped included routers."""
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from iter_routes(inner)
        else:
            yield route


def dependency_calls(path: str) -> list:
    matches = [route for route in iter_routes(app) if getattr(route, "path", "") == path]
    assert len(matches) == 1, f"expected exactly one route for {path}, got {len(matches)}"
    dependant = getattr(matches[0], "dependant", None)
    assert dependant is not None, f"{path} is not an APIRoute"
    return [call.call for call in dependant.dependencies]


class TrustBoundaryWithSecret(TestCase):
    """``AUTH_TRUST_SECRET`` set: only the proxy's assertion is believed."""

    def setUp(self):
        self._secret, self._header = config.AUTH_TRUST_SECRET, config.AUTH_TRUST_HEADER
        config.AUTH_TRUST_SECRET = SECRET
        config.AUTH_TRUST_HEADER = PROXY_HEADER

    def tearDown(self):
        config.AUTH_TRUST_SECRET = self._secret
        config.AUTH_TRUST_HEADER = self._header

    # ── the forged request a co-tenant container sends ──────────────
    def test_forged_admin_header_without_the_proxy_secret_is_refused(self):
        with self.assertRaises(AppError) as ctx:
            current_user(FORGED)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.code, "untrusted_proxy")

    def test_an_empty_proxy_header_is_not_a_pass(self):
        with self.assertRaises(AppError) as ctx:
            current_user(dict(FORGED, **{PROXY_HEADER: ""}))

        self.assertEqual(ctx.exception.code, "untrusted_proxy")

    def test_a_wrong_proxy_secret_is_refused(self):
        for wrong in ("wrong", SECRET + "x", SECRET[:-1], " " + SECRET):
            with self.subTest(wrong=wrong):
                with self.assertRaises(AppError) as ctx:
                    current_user(dict(FORGED, **{PROXY_HEADER: wrong}))
                self.assertEqual(ctx.exception.code, "untrusted_proxy")

    def test_the_refusal_happens_before_any_identity_header_is_read(self):
        """A forged request with a *broken* id still reports the trust failure."""
        for broken in ("abc", "0", "-1"):
            with self.subTest(broken=broken):
                headers = dict(FORGED, **{"X-User-Id": broken})
                with self.assertRaises(AppError) as ctx:
                    current_user(headers)
                self.assertEqual(
                    ctx.exception.code, "untrusted_proxy",
                    "the trust check must not depend on the identity headers",
                )

    # ── the request the proxy produces ───────────────────────────────
    def test_the_proxy_value_is_accepted_and_the_identity_is_parsed(self):
        user = current_user(dict(FORGED, **{PROXY_HEADER: SECRET}))

        self.assertEqual(user["id"], 999)
        self.assertEqual(user["role"], "admin")
        self.assertEqual(user["email"], "nobody@example.invalid")

    def test_the_trust_header_name_is_configurable(self):
        config.AUTH_TRUST_HEADER = "X-Fincal-Forwarded"

        user = current_user(dict(FORGED, **{"X-Fincal-Forwarded": SECRET}))
        self.assertEqual(user["id"], 999)

        with self.assertRaises(AppError) as ctx:
            current_user(dict(FORGED, **{PROXY_HEADER: SECRET}))
        self.assertEqual(ctx.exception.code, "untrusted_proxy")

    def test_a_missing_identity_still_answers_the_documented_401(self):
        """The proxy let the request through, but it carries no user."""
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            current_user({PROXY_HEADER: SECRET})

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Not authenticated")

    # ── the real HTTP stack ─────────────────────────────────────────
    def test_http_admin_route_refuses_the_forged_request_with_401(self):
        app.dependency_overrides = {}
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/admin/overview", headers=FORGED)

        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "untrusted_proxy")

    def test_http_public_config_is_independent_of_the_boundary(self):
        app.dependency_overrides = {}
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/config", headers=FORGED)

        self.assertEqual(resp.status_code, 200, resp.text)


class IdentityValidation(TestCase):
    """A malformed identity is a client error, never a 500 with a traceback."""

    def setUp(self):
        self._secret = config.AUTH_TRUST_SECRET
        config.AUTH_TRUST_SECRET = ""

    def tearDown(self):
        config.AUTH_TRUST_SECRET = self._secret

    def test_non_numeric_zero_and_negative_ids_answer_401(self):
        for broken in ("abc", "0", "-1", "1.5", "999999999999999999999999"):
            with self.subTest(broken=broken):
                with self.assertRaises(AppError) as ctx:
                    current_user({"X-User-Id": broken})
                self.assertEqual(ctx.exception.status_code, 401)
                self.assertEqual(ctx.exception.code, "invalid_identity")

    def test_the_failure_logs_nothing_above_warning(self):
        """``abc`` used to produce a 500 plus a ``logger.exception`` stack."""
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with self.assertRaises(AppError):
                current_user({"X-User-Id": "abc"})
        finally:
            root.removeHandler(handler)

        severe = [record for record in records if record.levelno >= logging.WARNING]
        self.assertEqual(
            [record.getMessage() for record in severe], [],
            "a malformed header must not log at WARNING/ERROR/CRITICAL",
        )

    def test_a_valid_id_is_still_parsed_without_a_secret(self):
        user = current_user({"X-User-Id": " 42 ", "X-User-Role": "user"})
        self.assertEqual(user["id"], 42)

    def test_http_invalid_identity_is_401_not_500(self):
        app.dependency_overrides = {}
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/me", headers={"X-User-Id": "abc"})

        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "invalid_identity")


class BackwardCompatibility(TestCase):
    """No secret configured: the header is trusted exactly as before."""

    def setUp(self):
        self._secret, self._header = config.AUTH_TRUST_SECRET, config.AUTH_TRUST_HEADER
        config.AUTH_TRUST_SECRET = ""
        config.AUTH_TRUST_HEADER = PROXY_HEADER

    def tearDown(self):
        config.AUTH_TRUST_SECRET = self._secret
        config.AUTH_TRUST_HEADER = self._header

    def test_forged_headers_are_believed_when_the_boundary_is_off(self):
        user = current_user(FORGED)

        self.assertEqual(user["id"], 999)
        self.assertEqual(user["role"], "admin")

    def test_an_unknown_proxy_header_is_inert_when_the_boundary_is_off(self):
        """The compose placeholder is always injected; it must change nothing."""
        user = current_user(dict(FORGED, **{PROXY_HEADER: "fincal-identity-unset"}))
        self.assertEqual(user["id"], 999)

    def test_the_unauthenticated_body_is_byte_identical_to_the_old_contract(self):
        app.dependency_overrides = {}
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/me")

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"detail": "Not authenticated"})

    def test_the_token_feed_never_consults_the_boundary(self):
        """``/ical/{token}`` authenticates by token; it must not require a proxy."""
        self.assertEqual(dependency_calls("/ical/{token}"), [])
        # ...while the session-protected routes demonstrably do consult it.
        self.assertIn(auth.get_current_user, dependency_calls("/api/me"))

    def test_config_defaults_are_documented_no_ops(self):
        """Default = today's behaviour: empty secret, and the header compose pins."""
        source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("AUTH_TRUST_SECRET", "")', source)
        self.assertIn('os.getenv("AUTH_TRUST_HEADER", "X-Fincal-Proxy")', source)


class DeployGate(TestCase):
    """``scripts/check_auth_trust.py`` must fail on a half-configured pair."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "scripts" / "check_auth_trust.py"
        spec = importlib.util.spec_from_file_location("check_auth_trust", path)
        assert spec and spec.loader
        cls.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gate)

    def labels(self, value="test-secret-value", chain="kazusa-auth@docker,fincal-identity"):
        return {
            f"{self.gate.LABEL_KEY}X-Fincal-Proxy": value,
            self.gate.ROUTER_LABEL: chain,
        }

    def enforced_report(self, **overrides):
        report = {
            "secret_configured": True,
            "trust_header": "X-Fincal-Proxy",
            "label_value_present": True,
            "label_matches_secret": True,
            "forged_no_header": {"status": 401, "code": "untrusted_proxy"},
            "forged_empty_header": {"status": 401, "code": "untrusted_proxy"},
            "forged_wrong_secret": {"status": 401, "code": "untrusted_proxy"},
            "dependency_trusted": {"ok": True, "id": 999, "role": "admin"},
            "http_trusted": {"status": 200, "code": ""},
        }
        report.update(overrides)
        return report

    def test_a_consistent_pair_passes(self):
        self.assertEqual(self.gate.evaluate(self.enforced_report(), self.labels()), [])

    def test_a_forged_request_that_is_still_believed_fails_the_deploy(self):
        report = self.enforced_report(forged_no_header={"status": 200, "code": ""})

        problems = self.gate.evaluate(report, self.labels())

        self.assertTrue(any("forged_no_header" in problem for problem in problems), problems)

    def test_an_env_secret_without_the_label_fails_the_deploy(self):
        labels = self.labels()
        del labels[f"{self.gate.LABEL_KEY}X-Fincal-Proxy"]

        problems = self.gate.evaluate(self.enforced_report(label_matches_secret=None), labels)

        self.assertTrue(any("label missing" in problem for problem in problems), problems)

    def test_a_label_that_is_not_attached_to_the_router_fails_the_deploy(self):
        problems = self.gate.evaluate(
            self.enforced_report(), self.labels(chain="kazusa-auth@docker")
        )

        self.assertTrue(any("not attached" in problem for problem in problems), problems)

    def test_a_label_value_the_app_does_not_require_fails_the_deploy(self):
        report = self.enforced_report(label_matches_secret=False)

        problems = self.gate.evaluate(report, self.labels())

        self.assertTrue(any("pair mismatch" in problem for problem in problems), problems)

    def test_a_degraded_data_read_is_not_a_boundary_failure(self):
        report = self.enforced_report(http_trusted={"status": 503, "code": "db_busy"})
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            problems = self.gate.evaluate(report, self.labels())

        self.assertEqual(problems, [])
        self.assertIn("identity accepted", out.getvalue())

    def test_a_rejected_trust_header_is_never_reported_as_accepted(self):
        """The 401 case already FAILs; the WARN must not claim the opposite."""
        report = self.enforced_report(
            http_trusted={"status": 401, "code": "untrusted_proxy"},
            dependency_trusted={"ok": False, "code": "untrusted_proxy", "status": 401},
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            problems = self.gate.evaluate(report, self.labels())

        self.assertTrue(any("trusted dependency call failed" in p for p in problems), problems)
        self.assertNotIn("identity accepted", out.getvalue())

    def test_the_inert_default_passes(self):
        report = {
            "secret_configured": False,
            "trust_header": "X-Fincal-Proxy",
            "label_value_present": True,
            "label_matches_secret": None,
            "forged_no_header": {"ok": True, "id": 999, "role": "admin"},
            "forged_placeholder": {"ok": True, "id": 999, "role": "admin"},
        }
        labels = self.labels(value=self.gate.PLACEHOLDER)

        self.assertEqual(self.gate.evaluate(report, labels), [])

    def test_a_stale_container_env_fails_the_deploy(self):
        """Compose interpolated the secret but the container has none."""
        report = {
            "secret_configured": False,
            "trust_header": "X-Fincal-Proxy",
            "label_value_present": True,
            "label_matches_secret": None,
            "forged_no_header": {"ok": True, "id": 999, "role": "admin"},
        }

        problems = self.gate.evaluate(report, self.labels())

        self.assertTrue(any("inert-config mismatch" in problem for problem in problems), problems)

    def test_the_placeholder_is_what_compose_ships(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn(
            f"${{AUTH_TRUST_SECRET:-{self.gate.PLACEHOLDER}}}", compose,
            "the compose placeholder and the gate must name the same value",
        )
        self.assertIn(
            f"${{AUTH_MIDDLEWARE}},{self.gate.MIDDLEWARE}", compose,
            "the identity middleware must be attached to the main router",
        )

    def test_the_gate_never_prints_a_secret(self):
        """Both the report and the failure text are secret-free by construction."""
        source = (ROOT / "scripts" / "check_auth_trust.py").read_text(encoding="utf-8")

        self.assertIn("label_matches_secret", source)
        self.assertNotIn("print(label_value", source)
        self.assertNotIn("print(secret", source)
        # the value travels into the container on stdin, never on the command line
        self.assertIn('"docker", "exec", "-i"', source)


class DeployWiring(TestCase):
    def test_deploy_runs_the_auth_trust_gate(self):
        deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

        self.assertIn("check_auth_trust.py", deploy)
        self.assertLess(
            deploy.index("check_auth_trust.py"),
            deploy.rindex("deployed and ready"),
            "the gate must run before the deploy claims success",
        )
