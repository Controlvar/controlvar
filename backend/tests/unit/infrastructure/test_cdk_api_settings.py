"""Every CDK environment runs the API with staging or production settings.

``infrastructure/cdk/app.py`` builds four environments: ``dev`` (the default),
``demo``, ``staging`` and ``prod``. The API and migration task definitions name
the application's environment with ``APP_ENV``, and the application reads
``dev`` and ``demo`` as development, whose settings are for a local trial. So
the task definitions carry ``APP_ENV=staging`` in ``dev``, ``demo`` and
``staging``, and ``APP_ENV=prod`` in ``prod`` (``api_app_env`` in
``infrastructure/cdk/stacks/environments.py``).

Each case runs ``app.py`` directly (``python3 app.py``, the ``app`` command in
``cdk.json``), in a subprocess whose environment holds only the synth inputs: a
placeholder account, fake ARNs, no AWS credentials and a scratch ``HOME``, so
nothing reaches AWS. It does not receive the ``context`` object in ``cdk.json``
that ``cdk synth`` adds; with it, some IAM, key and scaling-target settings
differ (more in the full profile), and no task definition does, which is all
this test reads. The task definitions' environment is read out of the
synthesized templates, as ECS hands it to the container, and the application's
settings are built from it in a fresh interpreter, with the database and Redis
on port 1, where nothing listens. With strong secrets the settings must be the
hardened class for the expected environment. With weak ones they must refuse to
load, and an API whose settings do not load does not start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.core.settings_rules import HARDENED_ENVIRONMENTS

REPO_ROOT = Path(__file__).resolve().parents[4]
CDK_DIR = REPO_ROOT / "infrastructure" / "cdk"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.regression,
    pytest.mark.skipif(
        not CDK_DIR.is_dir(), reason="this tree has no infrastructure/cdk"
    ),
]

#: CDK environment -> (the APP_ENV both task definitions carry, the
#: ENVIRONMENT the application's settings resolve it to).
EXPECTED = {
    "dev": ("staging", "staging"),
    "demo": ("staging", "staging"),
    "staging": ("staging", "staging"),
    "prod": ("prod", "production"),
}

#: The documented placeholder account (the repository's checks accept it).
ACCOUNT = "111111111111"
REGION = "us-west-2"
PUBLIC_BASE_URL = "https://controlvar.example.com"

#: The synth inputs that carry each secret's complete ARN: the secret's name,
#: and a fake six-character suffix.
SECRET_ARN_INPUTS = {
    "JWT_SECRET_ARN": ("jwt-secret", "JwtFak"),
    "FIRST_SUPERUSER_PASSWORD_SECRET_ARN": ("first-superuser-password", "SupFak"),
    "AUDIT_HMAC_KEY_SECRET_ARN": ("audit-hmac-key", "AudFak"),
}

#: What every secret a task definition names gets from Secrets Manager here:
#: long enough for every staging and production rule, and no known placeholder.
STRONG_SECRET = "s" * 48

#: The class defaults the hardened settings refuse.
WEAK_SECRETS = {"SECRET_KEY": "secret", "FIRST_SUPERUSER_PASSWORD": "admin"}

#: Where the settings would find the database and Redis: nothing listens on
#: port 1, and building the settings connects to neither.
UNREACHABLE = {
    "POSTGRES_SERVER": "127.0.0.1",
    "POSTGRES_PORT": "1",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "1",
    "REDIS_URL": "redis://127.0.0.1:1",
}

#: Builds the settings the way the API does on start, by importing them, and
#: prints what it got, or why it got nothing, as one JSON line.
_PROBE = """
import json
try:
    from backend.app.core import config
except Exception as exc:
    print("PROBE " + json.dumps({"refused": type(exc).__name__, "detail": str(exc)}))
else:
    print("PROBE " + json.dumps({
        "class": type(config.settings).__name__,
        "ENVIRONMENT": config.settings.ENVIRONMENT,
        "module": config.__file__,
    }))
"""


def _synth(env_name: str, outdir: Path, home: Path) -> list[dict]:
    """Run ``app.py`` for ``env_name`` directly, without ``cdk.json``'s context; its templates."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": env_name,
        "CDK_DEFAULT_ACCOUNT": ACCOUNT,
        "CDK_DEFAULT_REGION": REGION,
        "CDK_OUTDIR": str(outdir),
        "CERTIFICATE_ARN": (
            f"arn:aws:acm:{REGION}:{ACCOUNT}:certificate/"
            "00000000-0000-0000-0000-000000000000"
        ),
        "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        # Required in staging and prod; `.test` delivers nowhere.
        "ALARM_EMAIL": "alerts@controlvar.test",
        # No credential of any kind: a synth makes no AWS call.
        "AWS_ACCESS_KEY_ID": "",
        "AWS_SECRET_ACCESS_KEY": "",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    for variable, (name, suffix) in SECRET_ARN_INPUTS.items():
        environment[variable] = (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:"
            f"/{env_name}/experimentation/{name}-{suffix}"
        )
    done = subprocess.run(
        [sys.executable, "app.py"],
        cwd=CDK_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, (
        f"the synth of {env_name} failed:\n{done.stderr[-3000:]}"
    )
    templates = [
        json.loads(path.read_text()) for path in sorted(outdir.glob("*.template.json"))
    ]
    assert templates, f"the synth of {env_name} wrote no templates"
    return templates


def _backend_container(templates: list[dict], family: str) -> dict:
    """The ``backend`` container of task family ``family``.

    Exactly one: an empty scan must fail here rather than make every
    assertion below vacuous.
    """
    containers = [
        container
        for template in templates
        for resource in template.get("Resources", {}).values()
        if resource["Type"] == "AWS::ECS::TaskDefinition"
        and resource["Properties"].get("Family") == family
        for container in resource["Properties"].get("ContainerDefinitions", [])
        if container["Name"] == "backend"
    ]
    assert len(containers) == 1, (
        f"expected one 'backend' container in task family {family}, "
        f"found {len(containers)}"
    )
    (container,) = containers
    return {
        "family": family,
        "environment": {
            e["Name"]: e["Value"] for e in container.get("Environment", [])
        },
        "secrets": sorted(s["Name"] for s in container.get("Secrets", [])),
    }


@pytest.fixture(scope="module", params=sorted(EXPECTED))
def synthesized(request, tmp_path_factory) -> dict:
    """One real synth per environment, shared by the tests below."""
    env_name = request.param
    root = tmp_path_factory.mktemp(f"synth-{env_name}")
    home = root / "home"
    home.mkdir()
    templates = _synth(env_name, root / "cdk.out", home)
    return {
        "env": env_name,
        "api": _backend_container(templates, f"experimentation-backend-{env_name}"),
        "migrate": _backend_container(templates, f"experimentation-migrate-{env_name}"),
    }


def _settings(task: dict, secrets: dict[str, str], home: Path) -> dict:
    """Build the application's settings from ``task``'s environment.

    Exactly what the container would have, and nothing from this process: the
    test run's own ``APP_ENV=test`` and ``TESTING=true`` would switch the
    hardening off. Every literal the task definition sets is passed as ECS
    passes it; the hosts are CloudFormation references at synth time, and
    UNREACHABLE takes their place. Each secret the task definition names gets
    ``secrets``' value, or STRONG_SECRET. The working directory is empty, so no
    dotenv file is read.
    """
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.update(
        {
            name: value
            for name, value in task["environment"].items()
            if isinstance(value, str)
        }
    )
    environment.update(UNREACHABLE)
    environment.update(
        {name: secrets.get(name, STRONG_SECRET) for name in task["secrets"]}
    )
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=home,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    lines = [line for line in done.stdout.splitlines() if line.startswith("PROBE ")]
    assert len(lines) == 1, done.stdout[-3000:]
    result = json.loads(lines[0][len("PROBE ") :])
    if "module" in result:
        # The settings of this tree, not of some other installed copy.
        assert result["module"].startswith(str(REPO_ROOT)), result["module"]
    return result


def test_the_task_definitions_carry_staging_or_prod(synthesized):
    env_name = synthesized["env"]
    app_env, _ = EXPECTED[env_name]
    for task in (synthesized["api"], synthesized["migrate"]):
        environment = task["environment"]
        assert environment.get("APP_ENV") == app_env, (
            f"{task['family']} carries APP_ENV={environment.get('APP_ENV')!r}; "
            f"in the {env_name} environment it must be {app_env!r}"
        )
        # The application reads ENVIRONMENT before APP_ENV, so a task
        # definition that set it would decide the settings on its own.
        assert "ENVIRONMENT" not in environment, (
            f"{task['family']} sets ENVIRONMENT={environment['ENVIRONMENT']!r}"
        )


def test_the_api_runs_with_staging_or_production_settings(synthesized, tmp_path):
    _, expected = EXPECTED[synthesized["env"]]
    result = _settings(synthesized["api"], {}, tmp_path)
    assert result.get("class") == "ProdSettings", result
    assert result["ENVIRONMENT"] == expected, result
    assert result["ENVIRONMENT"] in HARDENED_ENVIRONMENTS, result


def test_the_migration_task_builds_the_same_settings(synthesized, tmp_path):
    _, expected = EXPECTED[synthesized["env"]]
    result = _settings(synthesized["migrate"], {}, tmp_path)
    assert result.get("class") == "ProdSettings", result
    assert result["ENVIRONMENT"] == expected, result


def test_weak_secrets_stop_the_api_from_starting(synthesized, tmp_path):
    result = _settings(synthesized["api"], WEAK_SECRETS, tmp_path)
    assert result.get("refused") == "ValidationError", result
    for name in WEAK_SECRETS:
        assert name in result["detail"], result
