"""A production task definition must carry every secret the image needs to boot.

The image ships no ``.env`` file -- ``.dockerignore`` keeps ``.env.*`` out of the
build context -- so the ECS task definition is the *only* thing that can supply
one.  Two secrets were missing from both task definitions, and each one is a
container that never serves a request:

* ``FIRST_SUPERUSER_PASSWORD`` defaults to ``"admin"``, which
  ``Settings.validate_superuser_password`` rejects in staging/production.
  ``import backend.app.core.config`` therefore raised, on **both** profiles, so
  uvicorn never bound and every ``python -m alembic`` died at import.
* ``AUDIT_HMAC_KEY`` defaults to ``dev-audit-key-change-in-production``, which
  ``ModulesSettings``' validator rejects the same way.  ``modules.register``
  builds those settings as its first step, so on a **full** image the modules
  fail to register -- ``abort_if_modules_broken()`` refuses to start the API,
  and ``migrations/env.py``'s ``require_modules_or_absent()`` refuses every
  alembic command.

The check is derived rather than typed: the environment is read out of the CDK
stacks and handed to the real settings classes in a clean subprocess.  A secret
that stops being injected fails this test with the field that rejected its
default, whatever that field turns out to be.
"""

from __future__ import annotations

import ast
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
STACKS_DIR = REPO_ROOT / "infrastructure" / "cdk" / "stacks"
STACKS = {
    "fargate": STACKS_DIR / "fargate_service_stack.py",
    "migration": STACKS_DIR / "migration_task_stack.py",
}

#: Secrets only module code reads.  The stacks gate them on ``include_modules``
#: because ECS cannot start a task whose definition names a secret that was
#: never created, and a core deployment creates none of these.
MODULE_ONLY_SECRETS = {"AUDIT_HMAC_KEY"}

#: Values a production deployment would really hold.  Anything not named here
#: gets a long random-looking string, which is what every hardening validator
#: asks for (>= 32 characters, not a known placeholder).
_STRONG = "f" * 64
_REALISTIC = {
    # `"APP_ENV": env_name`, and env_name is "prod" in production.
    "APP_ENV": "prod",
    # The Redis stack's primary endpoint and port (CloudFormation tokens in
    # the stack, so not literals the AST can read).
    "REDIS_HOST": "master.cache.example.internal",
    "REDIS_PORT": "6379",
    "POSTGRES_SERVER": "aurora.example.internal",
    # Not a secret and not a random string: the settings validator requires an
    # absolute http(s) origin with no path, and ALLOWED_HOSTS derives its host.
    "PUBLIC_BASE_URL": "https://controlvar.example.com",
}

pytestmark = pytest.mark.skipif(
    not STACKS_DIR.is_dir(), reason="this tree has no infrastructure/cdk"
)


def _add_container_call(stack: Path) -> ast.Call:
    for node in ast.walk(ast.parse(stack.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_container"
        ):
            return node
    raise AssertionError(f"no add_container(...) call in {stack.name}")


def _keyword(stack: Path, name: str) -> ast.AST:
    for keyword in _add_container_call(stack).keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"{stack.name}: add_container(...) has no {name}=")


def _all_names(stack: Path, kwarg: str) -> set[str]:
    """Every literal string key under ``kwarg``, nested dicts included."""
    return {
        key.value
        for mapping in ast.walk(_keyword(stack, kwarg))
        if isinstance(mapping, ast.Dict)
        for key in mapping.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _unconditional_names(stack: Path, kwarg: str) -> set[str]:
    """The keys of the top-level ``kwarg`` dict only.

    The profile-dependent entries sit in a nested dict behind
    ``**({...} if ... else {})``, so they are exactly the difference between
    this and :func:`_all_names`.
    """
    mapping = _keyword(stack, kwarg)
    return {
        key.value
        for key in mapping.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _literal_values(stack: Path, kwarg: str) -> dict[str, str]:
    mapping = _keyword(stack, kwarg)
    return {
        key.value: value.value
        for key, value in zip(mapping.keys, mapping.values)
        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
    }


def container_environment(stack: Path, profile: str) -> dict[str, str]:
    """The environment the container really receives, as name -> value."""
    names = _all_names(stack, "environment") | _all_names(stack, "secrets")
    if profile == "core":
        names -= MODULE_ONLY_SECRETS
    literals = _literal_values(stack, "environment")
    return {name: literals.get(name, _REALISTIC.get(name, _STRONG)) for name in names}


def _run(environment: dict[str, str], script: str) -> subprocess.CompletedProcess:
    """Run *script* with exactly *environment* (plus what python itself needs).

    A clean environment on purpose: the test process runs with ``TESTING=true``,
    which switches every hardening validator off -- the container does not.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(REPO_ROOT),
        **environment,
    }
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


_CORE_SETTINGS = """
import backend.app.core.config as config
assert config.settings.ENVIRONMENT == "production", config.settings.ENVIRONMENT
print("core settings built")
"""

_MODULE_SETTINGS = (
    _CORE_SETTINGS
    + """
from modules.backend.app.settings import build_modules_settings
build_modules_settings()
print("modules settings built")
"""
)


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(STACKS))
def test_the_task_definition_boots_the_application_in_production(name: str):
    """Every secret the settings refuse a default for is in the task definition."""
    environment = container_environment(STACKS[name], profile="full")
    result = _run(environment, _CORE_SETTINGS)
    assert result.returncode == 0, (
        f"{STACKS[name].name} gives the container "
        f"{sorted(environment)}, and the application cannot start with it:\n"
        f"{result.stderr[-2000:]}"
    )


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(STACKS))
def test_the_full_profile_can_register_its_modules(name: str):
    """The same, for the settings ``modules.register(hooks)`` builds first."""
    if not (REPO_ROOT / "modules" / "backend" / "app" / "settings.py").is_file():
        pytest.skip("core checkout: no modules package")
    environment = container_environment(STACKS[name], profile="full")
    assert MODULE_ONLY_SECRETS <= set(environment), sorted(environment)
    result = _run(environment, _MODULE_SETTINGS)
    assert result.returncode == 0, (
        f"{STACKS[name].name} cannot register the modules it deployed:\n"
        f"{result.stderr[-2000:]}"
    )


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(STACKS))
def test_the_module_secrets_are_gated_on_the_profile(name: str):
    """A core task definition must not name a secret nobody created.

    ECS fails the task at start-up when a secret in the definition does not
    resolve, so the module-only ones may only ever be conditional.
    """
    stack = STACKS[name]
    assert MODULE_ONLY_SECRETS <= _all_names(stack, "secrets"), (
        f"{stack.name} never injects {sorted(MODULE_ONLY_SECRETS)}"
    )
    assert not MODULE_ONLY_SECRETS & _unconditional_names(stack, "secrets"), (
        f"{stack.name} injects {sorted(MODULE_ONLY_SECRETS)} unconditionally; "
        "a core deployment has no such secret and ECS would refuse the task"
    )
    assert "include_modules" in stack.read_text()


DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


SECRET_ARNS = STACKS_DIR / "secret_arns.py"


def _stack_secret_names() -> set[str]:
    """Every ``/<env>/experimentation/<name>`` secret the two stacks import.

    The stacks import each secret by its complete ARN (#636), from a synth
    input, so the name no longer sits in the stack source: it is
    ``SECRET_INPUTS`` in ``stacks/secret_arns.py`` (input -> name), the table
    the validator checks every input against. A name counts only when BOTH
    stacks import its input with ``from_secret_complete_arn``, and neither may
    import a secret by name any more.
    """
    inputs = runpy.run_path(str(SECRET_ARNS))["SECRET_INPUTS"]
    names = set()
    for variable, name in inputs.items():
        for stack in STACKS.values():
            text = stack.read_text()
            imports = set(re.findall(r"secretsmanager\.Secret\.(from_\w+)\(", text))
            assert imports == {"from_secret_complete_arn"}, (
                f"{stack.name} imports secrets with {sorted(imports)}; only a "
                "complete ARN is a valueFrom ECS documents (a by-name import "
                "renders a partial ARN it cannot resolve)"
            )
            assert re.search(
                r"from_secret_complete_arn\(\s*self,\s*\"\w+\",\s*"
                + re.escape(f'secret_arns["{variable}"]'),
                text,
            ), f"{stack.name} does not import {variable} by complete ARN"
        names.add(name)
    return names


CHECK_TASK_SECRETS = REPO_ROOT / "scripts" / "check_task_secrets.py"


def _preflight_secret_names() -> tuple[set[str], set[str]]:
    """``(always, full-only)``: what the deploy pre-flight requires (#636).

    The pre-flight is ``scripts/check_task_secrets.py``, which reads each task
    definition's ``valueFrom`` and requires the app families to reference
    ``APP_SECRETS`` (and ``FULL_ONLY_SECRETS`` on the full profile). The
    workflow must run that script for exactly those families.
    """
    text = DEPLOY_WORKFLOW.read_text()
    assert "python3 scripts/check_task_secrets.py" in text
    for flag in (
        '--app-family "$ECS_BACKEND_TASK_FAMILY"',
        '--app-family "$MIGRATE_TASK_FAMILY"',
        '--profile "$PROFILE"',
    ):
        assert flag in text, f"deploy.yml's secrets pre-flight lost {flag}"
    script = runpy.run_path(str(CHECK_TASK_SECRETS))
    return set(script["APP_SECRETS"]), set(script["FULL_ONLY_SECRETS"])


@pytest.mark.unit
@pytest.mark.regression
def test_the_deploy_preflight_checks_exactly_the_secrets_the_stacks_name():
    """The pre-flight and the task definitions agree on which secrets exist.

    A secret the stacks name but the pre-flight does not check is a task ECS
    refuses to start, found twenty minutes into a deployment; one the pre-flight
    demands but nothing reads (the old Redis URL secret was that, #147) blocks every
    deployment on a secret an operator has to hand-make for nothing.
    """
    if not DEPLOY_WORKFLOW.is_file():
        pytest.skip("this tree has no deploy workflow")
    always, full_only = _preflight_secret_names()
    names = _stack_secret_names()
    module_only = {"audit-hmac-key"}
    assert always == names - module_only, (
        f"pre-flight checks {sorted(always)}; the task definitions name "
        f"{sorted(names - module_only)} unconditionally"
    )
    assert full_only == module_only & names


@pytest.mark.unit
@pytest.mark.regression
def test_without_the_audit_key_the_modules_refuse_to_register():
    """The negative control: the secret above is load-bearing, not decoration.

    This is the state every full-profile deployment was in -- the core settings
    build, and then `modules.register(hooks)` raises on AUDIT_HMAC_KEY.
    """
    if not (REPO_ROOT / "modules" / "backend" / "app" / "settings.py").is_file():
        pytest.skip("core checkout: no modules package")
    environment = container_environment(STACKS["fargate"], profile="core")
    assert "AUDIT_HMAC_KEY" not in environment

    result = _run(environment, _MODULE_SETTINGS)
    assert result.returncode != 0, result.stdout
    assert "AUDIT_HMAC_KEY" in result.stderr, result.stderr[-2000:]
    # ...while the core profile itself is perfectly happy with that same set.
    assert _run(environment, _CORE_SETTINGS).returncode == 0


# ---------------------------------------------------------------------------
# The container's start-up check (#237) passes both task definitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("profile", ["core", "full"])
@pytest.mark.parametrize("name", sorted(STACKS))
def test_the_start_up_check_passes_the_task_definition(name: str, profile: str):
    """`backend/docker-entrypoint.sh` runs the preflight before anything else,
    in the API service and the migration task alike. Either one refused would
    be a deployment whose tasks exit 78 on start."""
    from backend.app.core import preflight

    environment = container_environment(STACKS[name], profile=profile)
    problems = preflight.check(
        environment, full_profile=profile == "full", root=STACKS_DIR / "no-dotenv-here"
    )
    assert problems == [], (
        f"{STACKS[name].name} ({profile}) gives the container {sorted(environment)}, "
        f"and the start-up check refuses it:\n" + preflight.render(problems)
    )


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(STACKS))
def test_without_the_database_host_the_start_up_check_refuses_the_task(name: str):
    """The negative control: the check is not passing vacuously."""
    from backend.app.core import preflight

    environment = container_environment(STACKS[name], profile="full")
    environment.pop("POSTGRES_SERVER")
    problems = preflight.check(
        environment, full_profile=True, root=STACKS_DIR / "no-dotenv-here"
    )
    assert [p.split(" ", 1)[0] for p in problems] == ["POSTGRES_SERVER"]


# ---------------------------------------------------------------------------
# The browser origins each deployment allows (#130)
# ---------------------------------------------------------------------------

#: `(APP_ENV, PUBLIC_BASE_URL)` for the two hardened deployments the stacks
#: make. Staging uses the URL it is published at; production the one above.
DEPLOYMENTS = {
    "staging": ("staging", "https://app.staging.getexperimently.com"),
    "production": ("prod", _REALISTIC["PUBLIC_BASE_URL"]),
}

#: Imports the real app in the container's environment and reports the
#: allow-list, what main.py logged at start-up, and the CORS headers on the
#: wire. A core profile hides the modules package, as a core image has none.
_CORS_PROBE = """
import json, logging, sys

if sys.argv[1] == "core":
    sys.modules["modules"] = None

logged = []


class _Keep(logging.Handler):
    def emit(self, record):
        logged.append([record.levelname, record.getMessage()])


logging.getLogger("backend.app.main").addHandler(_Keep(logging.INFO))

from fastapi.testclient import TestClient

import backend.app.core.config as config
from backend.app.main import app
from backend.app.modules_loader import modules_active

client = TestClient(app, base_url=config.settings.PUBLIC_BASE_URL)
wire = {}
for origin in json.loads(sys.argv[2]):
    preflight = client.options(
        "/api/v1/experiments/",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    simple = client.get("/health/live", headers={"Origin": origin})
    wire[origin] = {
        "preflight_status": preflight.status_code,
        "preflight_acao": preflight.headers.get("access-control-allow-origin"),
        "preflight_acac": preflight.headers.get("access-control-allow-credentials"),
        "get_status": simple.status_code,
        "get_acao": simple.headers.get("access-control-allow-origin"),
        "get_acac": simple.headers.get("access-control-allow-credentials"),
    }
print("RESULT " + json.dumps({
    "environment": config.settings.ENVIRONMENT,
    "modules": modules_active(),
    "allow_list": config.settings.cors_allowed_origins,
    "logged": [entry for entry in logged if "CORS" in entry[1]],
    "wire": wire,
}))
"""

_OTHER_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3100",
    "https://elsewhere.example",
]


def _run_argv(
    environment: dict[str, str], script: str, *argv: str
) -> subprocess.CompletedProcess:
    """:func:`_run`, with arguments for *script*."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(REPO_ROOT),
        **environment,
    }
    return subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _cors_probe(name: str, profile: str, deployment: str, **extra: str) -> dict:
    import json

    has_modules = (REPO_ROOT / "modules" / "backend" / "app").is_dir()
    if profile == "full" and not has_modules:
        pytest.skip("core checkout: no modules package")
    app_env, public_base_url = DEPLOYMENTS[deployment]
    environment = container_environment(STACKS[name], profile=profile)
    configured = {"CORS_ORIGINS", "BACKEND_CORS_ORIGINS", "DASHBOARD_ORIGINS"}
    assert not configured & set(environment), (
        f"{STACKS[name].name} now sets {sorted(configured & set(environment))}; "
        "these tests expect it to set none"
    )
    environment.update(APP_ENV=app_env, PUBLIC_BASE_URL=public_base_url, **extra)
    result = _run_argv(environment, _CORS_PROBE, profile, json.dumps(_OTHER_ORIGINS))
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")]
    assert result.returncode == 0 and lines, (
        f"{STACKS[name].name} ({profile}, {deployment}) did not start:\n"
        f"{result.stderr[-3000:]}"
    )
    report = json.loads(lines[-1][len("RESULT ") :])
    assert report["modules"] is (profile == "full"), report["modules"]
    return report


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("deployment", sorted(DEPLOYMENTS))
@pytest.mark.parametrize("profile", ["core", "full"])
@pytest.mark.parametrize("name", sorted(STACKS))
def test_the_task_definition_allows_no_other_browser_origin(
    name: str, profile: str, deployment: str
):
    """Neither task definition sets a CORS setting, and its dashboard is served
    from the API's own origin: the allow-list is exactly empty, the API still
    starts and says so once, and a browser on any other origin gets no
    Access-Control-Allow-Origin."""
    result = _cors_probe(name, profile, deployment)
    environment = "staging" if deployment == "staging" else "production"
    assert result["environment"] == environment
    assert result["allow_list"] == []
    assert result["logged"] == [
        [
            "INFO",
            f"CORS allow-list ({environment}): none: same-origin only. To let a "
            "site on another origin call this API from a browser, add its origin "
            "to CORS_ORIGINS.",
        ]
    ]
    for origin, wire in result["wire"].items():
        assert wire["preflight_status"] == 400, (origin, wire)
        assert wire["preflight_acao"] is None, (origin, wire)
        assert wire["get_status"] == 200, (origin, wire)
        assert wire["get_acao"] is None, (origin, wire)
        assert wire["preflight_acac"] is None, (origin, wire)
        assert wire["get_acac"] is None, (origin, wire)


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("deployment", sorted(DEPLOYMENTS))
@pytest.mark.parametrize("profile", ["core", "full"])
@pytest.mark.parametrize("name", sorted(STACKS))
def test_a_wildcard_is_logged_and_never_allows_credentials(
    name: str, profile: str, deployment: str
):
    """The negative control: the same deployment with CORS_ORIGINS=* starts,
    logs a WARNING, and answers every origin with `*` and no credentials."""
    result = _cors_probe(name, profile, deployment, CORS_ORIGINS="*")
    assert result["allow_list"] == ["*"]
    assert [level for level, _ in result["logged"]] == ["INFO", "WARNING"]
    assert result["logged"][1][1].startswith(
        "The CORS allow-list contains '*', so every website can call this API"
    )
    for origin, wire in result["wire"].items():
        assert wire["preflight_status"] == 200, (origin, wire)
        assert wire["preflight_acao"] == "*", (origin, wire)
        assert wire["get_acao"] == "*", (origin, wire)
        assert wire["preflight_acac"] is None, (origin, wire)
        assert wire["get_acac"] is None, (origin, wire)
