# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The Host header is attacker-controlled; these pin what we do about it (#220).

Three properties, and the second is the one that would have broken a
deployment rather than leaked anything:

  * a foreign Host is refused before any handler sees it;
  * the ALB's health probes are NOT refused, because they arrive with the
    target's own IP as Host and Starlette's TrustedHostMiddleware has no path
    exemption -- with that middleware and a correct ALLOWED_HOSTS the target
    group never turns healthy and the deploy rolls back with the app fine;
  * the OIDC redirect_uri does not move when the Host header does.
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.middleware.trusted_host_middleware import (
    PROBE_PATHS,
    TrustedHostMiddleware,
)


def _app(allowed_hosts):
    app = FastAPI()

    @app.get("/anything")
    def anything():
        return {"ok": True}

    for path in PROBE_PATHS:
        app.get(path)(lambda: {"ok": True})

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    return TestClient(app)


ALLOWED = ["api.example.com", "*.example.net"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "host,expected",
    [
        ("api.example.com", 200),
        ("API.EXAMPLE.COM", 200),  # Host is a DNS name; DNS is case-insensitive
        ("api.example.com:8000", 200),  # a port is legitimate and not part of the name
        ("sub.example.net", 200),  # the *. wildcard
        ("deep.sub.example.net", 200),
        ("evil.com", 400),
        ("example.net", 400),  # `*.example.net` must not match the bare apex
        ("api.example.com.evil.com", 400),  # suffix confusion
        ("", 400),
    ],
)
def test_host_is_checked(host, expected):
    r = _app(ALLOWED).get("/anything", headers={"Host": host})
    assert r.status_code == expected, f"Host: {host!r} -> {r.status_code}"


@pytest.mark.unit
@pytest.mark.parametrize("path", sorted(PROBE_PATHS))
def test_infrastructure_probes_are_exempt(path):
    """The ALB health-checks /health with the TARGET'S OWN IP as Host.

    `infrastructure/cdk/stacks/fargate_service_stack.py` sets
    `health_check=elbv2.HealthCheck(path="/health")`, and an ALB sends the
    target's IP:port as Host because it has no hostname for it. Refuse that and
    the target group never turns healthy -- a deployment failure with no
    application failure behind it, which is the expensive kind to diagnose.
    """
    r = _app(ALLOWED).get(path, headers={"Host": "10.0.3.47:8000"})
    assert r.status_code == 200, (
        f"{path} refused a probe arriving with the task's IP as Host; the target "
        "group would never turn healthy"
    )


@pytest.mark.unit
def test_a_new_route_under_a_probe_prefix_is_not_exempt():
    """The exemption is exact paths, not prefixes.

    A prefix allow-list is how a new route under an existing prefix silently
    inherits an exemption, which this repository has shipped before.
    """
    app = FastAPI()

    @app.get("/health/secrets")
    def leaky():
        return {"secret": "s"}

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED)
    r = TestClient(app).get("/health/secrets", headers={"Host": "evil.com"})
    assert r.status_code == 400


@pytest.mark.unit
@pytest.mark.parametrize("allowed", [[], ["*"], None])
def test_an_empty_or_wildcard_list_allows_everything(allowed):
    """The development default. Staging/production cannot reach it.

    `Settings` refuses to construct with ALLOWED_HOSTS unset or `*` in a
    hardened environment -- asserted in test_config_allowed_hosts.py -- so this
    open branch is not a path by which a real deployment ends up permissive.
    """
    r = _app(allowed).get("/anything", headers={"Host": "evil.com"})
    assert r.status_code == 200


@pytest.mark.unit
def test_the_rejection_does_not_echo_the_host_back():
    """The refused value is attacker-controlled; it must not come back in the body."""
    marker = "<script>alert(1)</script>.evil.com"
    r = _app(ALLOWED).get("/anything", headers={"Host": marker})
    assert r.status_code == 400
    assert marker not in r.text


@pytest.mark.unit
@pytest.mark.parametrize(
    "pattern,host",
    [
        ("API.Example.COM", "api.example.com"),
        ("api.example.com", "API.EXAMPLE.COM"),
        ("API.Example.COM", "API.EXAMPLE.COM"),
        ("*.Example.NET", "SUB.example.net"),
    ],
)
def test_case_is_folded_on_BOTH_sides(pattern, host):
    """The earlier attempt lower-cased the PATTERN and not the HEADER.

    That reintroduced the silent-outage shape the round before it had removed:
    a correctly-spelled allow-list that matches nothing, refusing every request
    while the exempt health probes stayed green. One-sided folding passes any
    test whose fixtures are all lower-case, which is why the parameters here
    vary which side carries the capitals.
    """
    assert _app([pattern]).get("/anything", headers={"Host": host}).status_code == 200


@pytest.mark.unit
def test_it_guards_the_REAL_application_not_a_fixture():
    """Drive `backend.app.main:app`, not a FastAPI built in this file.

    The earlier attempt's gates matched on the middleware class NAME, so
    swapping in Starlette's own -- which has no path exemption and would have
    failed every ALB health check -- passed all three of them. A test that
    builds its own app can only ever prove the middleware works in isolation;
    it cannot prove the application is wired to it, in the right order, with
    the settings it actually reads.

    In a SUBPROCESS, deliberately. The first version of this test called
    `importlib.reload` on the config module to pick up ALLOWED_HOSTS, which
    replaces the `settings` singleton -- and every module that did
    `from ...config import settings` at import time keeps the OLD object. It
    broke four SSO tests in the same run, in a different package, through a
    reference this file never mentions. `test_deployment_secrets.py` boots the
    real settings in a clean subprocess for the same reason; this follows it.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]
    script = """
import os, json
from fastapi.testclient import TestClient
from backend.app.main import app
c = TestClient(app)
print(json.dumps({
    "foreign": c.get("/api/v1/openapi.json", headers={"Host": "evil.com"}).status_code,
    "ours": c.get("/api/v1/openapi.json", headers={"Host": "api.example.com"}).status_code,
    "probe": c.get("/health/live", headers={"Host": "10.0.3.47:8000"}).status_code,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ALLOWED_HOSTS": "api.example.com",
            "TESTING": "true",
            "APP_ENV": "test",
            "ENVIRONMENT": "test",
            "PYTHONPATH": str(repo_root),
        },
    )
    assert result.returncode == 0, result.stderr[-3000:]
    got = json.loads(result.stdout.strip().splitlines()[-1])

    assert got["foreign"] == 400, (
        f"the real application served a request with a foreign Host: {got}"
    )
    assert got["ours"] == 200, f"the real application refused its own host: {got}"
    # The probe exemption, on the real app. A 400 here means the ALB never
    # turns the target group healthy and the deployment rolls back.
    assert got["probe"] != 400, (
        f"the real application refused a health probe carrying the task IP: {got}"
    )


class _Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def rejections(monkeypatch):
    """The middleware's warnings, as formatted lines.

    A private `logging.Logger`, swapped in for the middleware's own, so the
    lines are the middleware's and nothing else's.
    """
    from backend.app.middleware import trusted_host_middleware

    lines = _Lines()
    private = logging.Logger("trusted-host-under-test", logging.WARNING)  # noqa: LOG001
    private.addHandler(lines)
    monkeypatch.setattr(trusted_host_middleware, "logger", private)
    return lines.lines


@pytest.mark.unit
@pytest.mark.regression
def test_the_rejection_log_names_the_host_and_the_setting_that_fixes_it(rejections):
    """A wrong Host is a silent outage: /api/* answers 400, the probes stay 200.

    The log line is where an operator finds out why, so it has to say what was
    refused, what this deployment answers on, and which setting to change. The
    response body stays the terse `Invalid host header` -- no contract change.
    """
    r = _app(ALLOWED).get("/anything", headers={"Host": "10.0.0.5:8000"})

    assert r.status_code == 400
    assert r.json() == {"detail": "Invalid host header"}
    [line] = rejections
    assert "Host '10.0.0.5:8000'" in line, line
    assert "this deployment answers on api.example.com, *.example.net" in line, line
    assert "Set PUBLIC_BASE_URL (or ALLOWED_HOSTS" in line, line


@pytest.mark.unit
@pytest.mark.regression
def test_the_logged_host_is_truncated_and_escaped(rejections):
    """The Host is attacker-sized and attacker-shaped.

    A 10,000-character value must reach the log cut to MAX_LOGGED_HOST, and a
    control character must arrive escaped, never raw: a raw control character
    in a log line lets the header author shape what the log shows.
    """
    from backend.app.middleware.trusted_host_middleware import MAX_LOGGED_HOST

    app = _app(ALLOWED)

    assert app.get("/anything", headers={"Host": "a" * 10_000}).status_code == 400
    # A tab is a header-value character the HTTP stack lets through; it stands
    # in for the control characters the repr escapes.
    assert app.get("/anything", headers={"Host": "x\tforged.evil"}).status_code == 400

    long_line, ctrl_line = rejections
    assert MAX_LOGGED_HOST == 200
    assert f"Host '{'a' * MAX_LOGGED_HOST}';" in long_line, long_line[:300]
    assert "a" * (MAX_LOGGED_HOST + 1) not in long_line
    assert "\t" not in ctrl_line and "\\t" in ctrl_line, ctrl_line
