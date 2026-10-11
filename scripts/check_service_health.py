#!/usr/bin/env python3
"""Deployment gate: the running service must report itself truthfully (Issue #79).

Two things went unchecked for ~7 weeks, because nothing ever asked the deployed
service what it thought of itself:

* ``/api/admin/health``'s aggregate ``status`` was pinned at ``degraded`` by the
  ``longbridge`` probe — the Longbridge CLI is a *host-side* pipeline tool and
  the image ships no such binary (``Dockerfile`` copies ``app/`` plus the web
  dependencies), so the probe answered a constant ``degraded`` no matter how
  healthy PostgreSQL / Kurumi / the sync freshness actually were. Real outages
  and that permanent noise were the same reading.
* ``?version`` was the constant ``"dev"``: ``app/version.py`` read a ``VERSION``
  file next to the source tree, which is outside the image build context.

This gate is run by ``scripts/deploy.sh`` after the container starts. It asks the
container for its own ``/api/admin/health`` (the published port is Traefik's, so
the request is issued inside the container), then re-derives the verdict the
service should have produced:

* the reported ``version`` must equal the commit this deploy wrote to
  ``VERSION`` (the file next to this script, i.e. the deployed tree);
* the host-tool checks must be ``not_applicable`` inside the image, so a
  host-side toolchain can never vote on service health again;
* the aggregate must follow from the *applicable* probes only — re-derived here
  independently, so "every dependency healthy but status still degraded" fails
  the deploy instead of shipping.

Exit codes:
  0  the service's self-report agrees with the deployed commit and its probes
  1  the self-report is wrong (version mismatch, host tool voting, or an
     aggregate that does not follow from the probes)
  2  the report could not be fetched

Read-only: it issues one GET and no writes.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_UNKNOWN = 2

#: A probe status that means "this subject cannot exist here", excluded from the
#: aggregate (must mirror ``app.routers.admin.NOT_APPLICABLE``).
NOT_APPLICABLE = "not_applicable"

#: Host-side pipeline tooling that must not count as a service dependency.
HOST_TOOL_CHECKS = ("longbridge",)

#: Fetched inside the container: ``app.config.PORT`` is what the service binds,
#: so the gate cannot drift from the listening port (and no host route to it is
#: assumed — Compose publishes no port, Traefik reaches it over the network).
FETCH_SNIPPET = (
    "import json, urllib.request\n"
    "from app.config import PORT\n"
    "url = 'http://127.0.0.1:%s/api/admin/health' % PORT\n"
    "print(urllib.request.urlopen(url, timeout=10).read().decode())\n"
)


def fetch_report(container: str) -> dict | None:
    """Return the container's health payload, or ``None`` when it cannot be read."""
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "python", "-c", FETCH_SNIPPET],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # docker missing, container gone, timeout
        print(f"FAIL: could not ask container '{container}' for its health "
              f"({type(exc).__name__})", file=sys.stderr)
        return None

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        print(f"FAIL: docker exec {container} exited {proc.returncode}: "
              f"{detail[-1] if detail else '<no stderr>'}", file=sys.stderr)
        return None

    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        print("FAIL: the service returned an empty health response", file=sys.stderr)
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        print("FAIL: /api/admin/health did not answer JSON", file=sys.stderr)
        return None


def expected_aggregate(statuses: list[str]) -> str:
    """Re-derive the aggregate the service should report for these probes.

    Same rule as ``app/routers/admin.py::health_check``: ``not_ready`` outranks
    ``degraded``, and ``not_applicable`` entries do not vote (Issue #79).
    """
    applicable = [status for status in statuses if status != NOT_APPLICABLE]
    if all(status == "healthy" for status in applicable):
        return "healthy"
    if any(status == "not_ready" for status in applicable):
        return "not_ready"
    return "degraded"


def evaluate(report: dict, expected_version: str) -> list[str]:
    """Return the list of problems; empty means the self-report is trustworthy."""
    problems: list[str] = []
    checks = report.get("checks") or {}

    version = report.get("version")
    if version != expected_version:
        problems.append(f"version: expected '{expected_version}' (deployed commit), "
                        f"service reported '{version}'")

    for name in HOST_TOOL_CHECKS:
        entry = checks.get(name)
        if not isinstance(entry, dict):
            problems.append(f"checks.{name}: missing from the response")
        elif entry.get("status") != NOT_APPLICABLE:
            problems.append(f"checks.{name}: expected status '{NOT_APPLICABLE}' "
                            f"(host-side tool, absent from the image), got "
                            f"'{entry.get('status')}'")

    statuses: list[str] = [str(entry.get("status"))
                           for entry in checks.values() if isinstance(entry, dict)]
    wanted = expected_aggregate(statuses)
    if report.get("status") != wanted:
        problems.append(f"status: expected '{wanted}' for probes "
                        f"{ {name: entry.get('status') for name, entry in checks.items() if isinstance(entry, dict)} }, "
                        f"got '{report.get('status')}'")
    return problems


def default_version_file() -> Path:
    """``VERSION`` next to the deployed tree (written by ``scripts/deploy.sh``)."""
    return Path(__file__).resolve().parents[1] / "VERSION"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="assert that a running fincal service reports itself truthfully (Issue #79)")
    parser.add_argument("--container", default="fincal", help="container to ask (default: fincal)")
    parser.add_argument("--expected-version", default=None,
                        help="commit the service must report (default: the VERSION file)")
    parser.add_argument("--version-file", default=None,
                        help="file to read the expected commit from (default: <deploy root>/VERSION)")
    args = parser.parse_args()

    version_file = Path(args.version_file) if args.version_file else default_version_file()
    expected = args.expected_version
    if expected is None:
        try:
            expected = version_file.read_text().strip()
        except OSError as exc:
            print(f"FAIL: cannot read {version_file} ({type(exc).__name__}) — "
                  f"pass --expected-version", file=sys.stderr)
            return EXIT_UNKNOWN
        if not expected:
            print(f"FAIL: {version_file} is empty — pass --expected-version", file=sys.stderr)
            return EXIT_UNKNOWN

    report = fetch_report(args.container)
    if report is None:
        return EXIT_UNKNOWN

    checks = report.get("checks") or {}
    print(f"container  : {args.container}")
    print(f"version    : {report.get('version')} (expected {expected})")
    print(f"status     : {report.get('status')}")
    for name in sorted(checks):
        entry = checks[name] if isinstance(checks[name], dict) else {}
        code = entry.get("error_code")
        print(f"  {name:<16} {entry.get('status')}" + (f"  error_code={code}" if code else ""))

    problems = evaluate(report, expected)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        print(f"FAIL: the service's self-report is wrong ({len(problems)} problem(s))",
              file=sys.stderr)
        return EXIT_MISMATCH

    print("OK: the service attests the deployed commit and its aggregate follows "
          "from its own probes")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
