#!/usr/bin/env python3
"""Deployment gate: the identity trust boundary must be configured on BOTH sides.

Issue #102 moved "who is the caller" from the network to a verified assertion: the
app only believes ``X-User-*`` when the request also carries
``AUTH_TRUST_SECRET`` in ``AUTH_TRUST_HEADER``, which only Traefik's
``fincal-identity`` headers middleware can add. That is fail-closed, which is the
right default — but it also means a half-configured pair (env set, middleware
label missing / label set, container env missing) turns *every* authenticated
request into a 401. This gate makes that state a deploy failure with a named
cause instead of a silent outage a user discovers.

What it checks (all read-only):

1. What the *running container* thinks. The proxy header value Traefik will
   inject is read from the container's own labels on the host and handed to the
   container on stdin — no secret is printed on either side, and the container
   answers the two questions that matter:
   * ``label_matches_secret``: does the value Traefik stamps equal the value the
     app requires? (``false`` = exactly the half-configured case).
   * the enforcement itself, over real HTTP to ``127.0.0.1:PORT``:
     - forged ``X-User-Id``/``X-User-Role: admin`` with no proxy header → 401
       ``untrusted_proxy`` (this is the request a co-tenant container sends);
     - the same request carrying the label value → not rejected by the boundary;
     - the dependency alone, DB-free, with the label value → identity parsed.
2. That the label chain is actually wired: the ``fincal-identity`` middleware
   exists and the main router's ``middlewares`` list contains it. A middleware
   that exists but is not attached injects nothing.

Exit codes:
  0  the boundary is consistent (or intentionally disabled and inert)
  1  the two sides disagree / the boundary does not hold
  2  the container could not be asked

Secrets: neither the secret nor the label value is ever printed; only booleans,
status codes and error codes leave the host script.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_UNKNOWN = 2

MIDDLEWARE = "fincal-identity"
LABEL_KEY = f"traefik.http.middlewares.{MIDDLEWARE}.headers.customrequestheaders."
ROUTER_LABEL = "traefik.http.routers.fincal.middlewares"

#: Value the compose label falls back to while AUTH_TRUST_SECRET is unset; the app
#: ignores it in that mode. Kept in sync with docker-compose.yml by hand — a
#: mismatch is reported as an inert-config warning, not as a failure.
PLACEHOLDER = "fincal-identity-unset"

#: Runs *inside* the container: it has the app, its env and its port. Reads
#: ``{"label_value": ...}`` (or nothing) from stdin and prints one JSON object.
PROBE = r'''
import json, sys, urllib.error, urllib.request

from app import config
from app.auth import get_current_user
from app.errors import AppError


def probe(headers):
    """One GET to this very process, the way a caller without a session would."""
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/admin/overview" % config.PORT,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"status": resp.status, "code": ""}
    except urllib.error.HTTPError as exc:
        code = ""
        try:
            code = json.loads(exc.read().decode("utf-8", "replace"))["error"]["code"]
        except Exception:
            code = ""
        return {"status": exc.code, "code": code}
    except Exception as exc:                      # no listener, socket error
        return {"status": 0, "code": type(exc).__name__}


class Req:
    """The only surface ``get_current_user`` uses: ``request.headers.get``.

    Starlette's ``Headers`` is case-insensitive; a plain dict is not, and the
    configured header names are ``X-User-Id`` / ``X-Fincal-Proxy`` style.
    """

    def __init__(self, headers):
        from starlette.datastructures import Headers
        self.headers = Headers(headers)


def dependency(headers):
    """Call the real dependency, no database involved (it fails before any I/O)."""
    try:
        user = get_current_user(Req(headers))
        return {"ok": True, "id": user.get("id"), "role": user.get("role")}
    except AppError as exc:
        return {"ok": False, "code": exc.code, "status": exc.status_code}
    except Exception as exc:
        return {"ok": False, "code": type(exc).__name__, "status": 0}


try:
    payload = json.loads(sys.stdin.read() or "{}")
except Exception:
    payload = {}

label_value = payload.get("label_value")
secret = config.AUTH_TRUST_SECRET
forged = {"X-User-Id": "999", "X-User-Role": "admin",
          "X-User-Email": "nobody@example.invalid"}

result = {
    "secret_configured": bool(secret),
    "trust_header": config.AUTH_TRUST_HEADER,
    "label_value_present": bool(label_value),
    "label_matches_secret": (
        None if not label_value or not secret
        else config.AUTH_TRUST_SECRET == label_value
    ),
}
if secret:
    result["forged_no_header"] = probe(forged)
    result["forged_empty_header"] = probe(dict(forged, **{config.AUTH_TRUST_HEADER: ""}))
    result["forged_wrong_secret"] = probe(
        dict(forged, **{config.AUTH_TRUST_HEADER: "not-the-secret"})
    )
    result["dependency_trusted"] = dependency(
        dict(forged, **{config.AUTH_TRUST_HEADER: label_value or secret})
    )
    result["http_trusted"] = probe(dict(forged, **{config.AUTH_TRUST_HEADER: label_value or secret}))
else:
    # Boundary disabled: the forged request must behave exactly as it used to
    # (it reaches the route; the DB decides the answer), and the placeholder
    # header must be invisible to it.
    result["forged_no_header"] = dependency(forged)
    result["forged_placeholder"] = (
        dependency(dict(forged, **{config.AUTH_TRUST_HEADER: label_value}))
        if label_value else None
    )

print(json.dumps(result))
'''


def run(cmd: list[str], *, stdin: str | None = None, timeout: int = 60):
    return subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout)


def container_labels(container: str) -> dict | None:
    proc = run(["docker", "inspect", "--format", "{{json .Config.Labels}}", container])
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "{}") or {}
    except ValueError:
        return None


def ask_container(container: str, label_value: str | None) -> dict | None:
    proc = run(
        ["docker", "exec", "-i", container, "python", "-c", PROBE],
        stdin=json.dumps({"label_value": label_value}),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        print(f"FAIL: docker exec {container} exited {proc.returncode}: "
              f"{detail[-1] if detail else '<no stderr>'}", file=sys.stderr)
        return None
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        print("FAIL: the container returned no probe output", file=sys.stderr)
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        print("FAIL: the container probe did not answer JSON", file=sys.stderr)
        return None


def evaluate(report: dict, labels: dict) -> list[str]:
    """Return the list of problems; empty means both sides agree."""
    problems: list[str] = []
    header = report.get("trust_header") or "X-Fincal-Proxy"
    label_value = labels.get(LABEL_KEY + header)

    if label_value is None:
        problems.append(
            f"label missing: {LABEL_KEY}{header} — Traefik would not inject the "
            f"trust header, so every authenticated request would answer 401"
        )
    chain = labels.get(ROUTER_LABEL) or ""
    if MIDDLEWARE not in chain:
        problems.append(
            f"middleware not attached: {ROUTER_LABEL}='{chain}' does not include "
            f"'{MIDDLEWARE}' — the middleware exists but injects nothing"
        )

    if report.get("secret_configured"):
        if report.get("label_matches_secret") is False:
            problems.append(
                "pair mismatch: the label value Traefik injects is NOT the "
                "AUTH_TRUST_SECRET the container requires — set the secret in "
                ".env (compose interpolates the same value) or remove it entirely"
            )
        for key in ("forged_no_header", "forged_empty_header", "forged_wrong_secret"):
            probe = report.get(key) or {}
            if probe.get("status") != 401 or probe.get("code") != "untrusted_proxy":
                problems.append(
                    f"{key}: expected 401 untrusted_proxy, got "
                    f"{probe.get('status')} {probe.get('code')!r} — a request "
                    f"that skipped the proxy is still believed"
                )
        dep = report.get("dependency_trusted") or {}
        if not dep.get("ok"):
            problems.append(
                f"trusted dependency call failed: {dep.get('status')} "
                f"{dep.get('code')!r} — the value Traefik injects is not accepted"
            )
        http = report.get("http_trusted") or {}
        if http.get("status") != 200:
            # Not a boundary problem: the identity was accepted, the read behind
            # it may legitimately be degraded (db_busy / dependency view down).
            print(f"WARN: trusted HTTP probe answered {http.get('status')} "
                  f"{http.get('code')!r} (identity accepted; data dependency)")
    else:
        dep = report.get("forged_no_header") or {}
        if not dep.get("ok"):
            problems.append(
                f"AUTH_TRUST_SECRET is empty, so the header must be trusted as "
                f"before, but the dependency rejected it: {dep.get('code')!r}"
            )
        if label_value and label_value != PLACEHOLDER:
            problems.append(
                "inert-config mismatch: compose would inject a non-placeholder "
                "value while the container has no AUTH_TRUST_SECRET — the .env "
                "was not what the container started with"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="fincal")
    args = parser.parse_args()

    labels = container_labels(args.container)
    if labels is None:
        print(f"FAIL: could not read labels of container '{args.container}'", file=sys.stderr)
        return EXIT_UNKNOWN

    header = labels.get(ROUTER_LABEL)
    label_key = next((k for k in labels if k.startswith(LABEL_KEY)), None)
    header_name = label_key[len(LABEL_KEY):] if label_key else None
    label_value = labels.get(label_key) if label_key else None

    report = ask_container(args.container, label_value)
    if report is None:
        return EXIT_UNKNOWN

    mode = "enforced" if report.get("secret_configured") else "not configured (trusted network)"
    print(f"auth trust : {mode}")
    print(f"header     : {report.get('trust_header')} (label "
          f"{'present' if label_value is not None else 'MISSING'})")
    print(f"router     : {header!r}")
    for key in ("forged_no_header", "forged_empty_header", "forged_wrong_secret",
                "http_trusted", "dependency_trusted"):
        if key in report:
            print(f"{key:24s}: {report[key]}")

    problems = evaluate(report, labels)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return EXIT_PROBLEM
    if report.get("secret_configured"):
        print("PASS: forged identities are refused; the proxy's header is accepted")
        print("NOTE: the edge path still needs one browser session to confirm "
              "end-to-end (forwardAuth + fincal-identity) — the probes above run "
              "against this container only.")
    else:
        print("PASS: boundary inert (no AUTH_TRUST_SECRET) and headers trusted as before")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
