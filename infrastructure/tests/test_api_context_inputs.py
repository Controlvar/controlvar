"""The API task's optional inputs for the local demo apps, as CDK context (#1299).

A trial install serves ShopLab (http://localhost:3200) and StreamPulse
(http://localhost:3300) from an operator's laptop, and runs its three
schedulers every minute. fargate_service_stack.py takes four optional CDK
context values for that, ``-c cors_origins=...`` and one per cadence, and
gives each to the API task alone, under its setting's name. Pinned here, on
real synthesis wherever a template is read, and every synthesis goes through
infrastructure/cdk/synth_context.py (cdk.json's context, as the CLI passes
it; see test_synth_context.py):

* unset, the API container's environment is exactly what it was, the booted
  settings allow no other origin, and the cadences are their defaults;
* exported in the process environment instead, under either name, they change
  no template: a shell that sourced .env.example, which sets CORS_ORIGINS,
  must not carry it into a hand-run ``cdk deploy``;
* set, the API task gets each value verbatim and the booted settings read it,
  and neither the dashboard nor the migration task gets any of them;
* a value the backend would take differently from how it is written is
  refused at synth, naming the input.

The core profile is synthesised from a copy of the app with no modules/ beside
it, which is how app.py tells the profiles apart.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_app_profiles import _app_environment
from .test_synth_context import (
    CDK_DIR,
    MODULES_PRESENT,
    REPO_ROOT,
    core_copy,
    run_synth,
    synth,
)

ENVIRONMENTS = ("staging", "prod")
PROFILES = (
    "core",
    pytest.param(
        "full",
        marks=pytest.mark.skipif(
            not MODULES_PRESENT, reason="core checkout: no modules/"
        ),
    ),
)

#: The demo apps' origins, as the installer sets them.
DEMO_ORIGINS = "http://localhost:3200,http://localhost:3300"

#: Each input, and the variable it becomes in the API task's environment.
VARIABLES = {
    "cors_origins": "CORS_ORIGINS",
    "safety_check_interval_minutes": "SAFETY_CHECK_INTERVAL_MINUTES",
    "rollout_check_interval_minutes": "ROLLOUT_CHECK_INTERVAL_MINUTES",
    "bandit_update_interval_minutes": "BANDIT_UPDATE_INTERVAL_MINUTES",
}
CADENCES = {
    name: variable for name, variable in VARIABLES.items() if name != "cors_origins"
}

#: All four set. The three cadences differ, so one sent to the wrong setting
#: is seen.
SET = {
    "cors_origins": DEMO_ORIGINS,
    "safety_check_interval_minutes": "1",
    "rollout_check_interval_minutes": "5",
    "bandit_update_interval_minutes": "3",
}

#: Every setting through which a browser origin could be allowed.
CORS_SETTINGS = {"CORS_ORIGINS", "BACKEND_CORS_ORIGINS", "DASHBOARD_ORIGINS"}

#: The shell of an operator who sourced .env.example (its CORS_ORIGINS and
#: FIRST_SUPERUSER lines) and exported fast cadences for a local demo, under
#: the settings' names and the context names alike.
EXPORTED = {
    "CORS_ORIGINS": (
        "http://localhost:3100,http://localhost:3200,"
        "http://localhost:3300,http://localhost:8000"
    ),
    "FIRST_SUPERUSER": "admin@demo.com",
    **dict.fromkeys(CADENCES.values(), "1"),
    "cors_origins": DEMO_ORIGINS,
    **dict.fromkeys(CADENCES, "1"),
}

#: The API container's environment keys with no input set, in order, read from
#: a synthesis of main at 71453b83 in the helper's mode: core's thirteen, and
#: the full profile's six more (the etl module's Glue names, then the counters
#: table and the region its client needs).
CORE_API_ENVIRONMENT = [
    "APP_ENV",
    "PUBLIC_BASE_URL",
    "LOG_LEVEL",
    "PYTHONUNBUFFERED",
    "POSTGRES_SERVER",
    "POSTGRES_DB",
    "POSTGRES_SCHEMA",
    "POSTGRES_PORT",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_SSL",
    "RUN_MIGRATIONS",
    "SEED",
]
FULL_API_ENVIRONMENT = [
    *CORE_API_ENVIRONMENT,
    "GLUE_ETL_JOB_NAME",
    "GLUE_METRICS_JOB_NAME",
    "GLUE_DATABASE",
    "GLUE_CRAWLER_NAME",
    "DYNAMODB_COUNTERS_TABLE",
    "AWS_DEFAULT_REGION",
]


def _families(env_name: str) -> dict[str, str]:
    return {
        "api": f"experimentation-backend-{env_name}",
        "dashboard": f"experimentation-dashboard-{env_name}",
        "migration": f"experimentation-migrate-{env_name}",
    }


# --- reading an assembly ---------------------------------------------------------


def _compared(assembly: Path) -> dict[str, bytes]:
    """Every template, asset manifest and manifest.json, as bytes.

    The *.metadata.json files are left out: they differ between any two runs
    by the jsii temporary paths in their stack traces.
    """
    files = {
        path.name: path.read_bytes()
        for path in sorted(assembly.iterdir())
        if path.name.endswith((".template.json", ".assets.json"))
        or path.name == "manifest.json"
    }
    assert any(name.endswith(".template.json") for name in files), sorted(files)
    return files


def _containers(assembly: Path, env_name: str) -> dict[str, dict]:
    """The API, dashboard and migration containers, by role."""
    found = {}
    for template in sorted(assembly.glob("*.template.json")):
        for resource in json.loads(template.read_text())["Resources"].values():
            if resource["Type"] == "AWS::ECS::TaskDefinition":
                properties = resource["Properties"]
                (container,) = properties["ContainerDefinitions"]
                found[properties["Family"]] = container
    families = _families(env_name)
    assert sorted(found) == sorted(families.values()), sorted(found)
    return {role: found[family] for role, family in families.items()}


def _environment(container: dict) -> dict:
    return {entry["Name"]: entry["Value"] for entry in container.get("Environment", [])}


#: Stand-ins for the values CloudFormation resolves at deploy (imports from the
#: database and Redis stacks). Nothing below connects to them.
_RESOLVED_AT_DEPLOY = {
    "POSTGRES_SERVER": "aurora.invalid",
    "REDIS_HOST": "redis.invalid",
    "REDIS_PORT": "1",
}

#: Builds the settings the way the API process does at import, and reports what
#: these tests compare.
_BOOT = """
import json
import sys

from backend.app.core import config

settings = config.settings
names = json.loads(sys.argv[1])
print("BOOTED " + json.dumps({
    "environment": settings.ENVIRONMENT,
    "allow_list": settings.cors_allowed_origins,
    "cadences": {name: getattr(settings, name) for name in names},
    "defaults": {name: type(settings).model_fields[name].default for name in names},
}))
"""


def _boot(container: dict, home: Path) -> dict:
    """The backend's settings, booted in a clean process from ``container``.

    Its environment as the template gives it, with the deploy-time values
    replaced by stand-ins and every secret given a strong fake value. The test
    process's own environment is not passed on: it runs with TESTING=true,
    which switches the hardening off, and the container does not.
    """
    environment = {}
    for name, value in _environment(container).items():
        if not isinstance(value, str):
            assert name in _RESOLVED_AT_DEPLOY, (
                f"{name} is a value CloudFormation resolves, with no stand-in: {value}"
            )
            value = _RESOLVED_AT_DEPLOY[name]
        environment[name] = value
    for secret in container.get("Secrets", []):
        environment[secret["Name"]] = "f" * 64
    result = subprocess.run(
        [sys.executable, "-c", _BOOT, json.dumps(sorted(CADENCES.values()))],
        cwd=REPO_ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "PYTHONPATH": str(REPO_ROOT),
            **environment,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("BOOTED ")]
    assert result.returncode == 0 and lines, (
        f"the settings do not boot from {sorted(environment)}:\n{result.stderr[-3000:]}"
    )
    return json.loads(lines[-1][len("BOOTED ") :])


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def core_dir(tmp_path_factory) -> Path:
    return core_copy(tmp_path_factory.mktemp("core"))


@pytest.fixture(scope="module")
def home(tmp_path_factory) -> Path:
    """One scratch HOME for every synthesis here, run one after another."""
    return tmp_path_factory.mktemp("synth-home")


@pytest.fixture(scope="module")
def assembly(tmp_path_factory, core_dir, home):
    """``assembly(env, profile, inputs=None, exported=None)``, synthesised once."""
    root = tmp_path_factory.mktemp("assemblies")
    made: dict[str, Path] = {}

    def make(env_name, profile, inputs=None, exported=None) -> Path:
        key = json.dumps([env_name, profile, inputs, exported], sort_keys=True)
        if key not in made:
            made[key] = synth(
                root / f"assembly-{len(made)}",
                env_name,
                cdk_dir=core_dir if profile == "core" else CDK_DIR,
                inputs=inputs,
                exported=exported,
                home=home,
            )
        return made[key]

    return make


@pytest.fixture(scope="module")
def booted(tmp_path_factory):
    """``booted(container)``: :func:`_boot`, once per container."""
    home = tmp_path_factory.mktemp("boot-home")
    made: dict[str, dict] = {}

    def boot(container: dict) -> dict:
        key = json.dumps(container, sort_keys=True)
        if key not in made:
            made[key] = _boot(container, home)
        return made[key]

    return boot


@pytest.fixture(scope="module")
def stack_module():
    """stacks/fargate_service_stack.py, imported from the app's directory."""
    with _app_environment(CDK_DIR):
        return importlib.import_module("stacks.fargate_service_stack")


# --- unset: nothing changes --------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_unset_the_api_environment_is_exactly_what_it_was(assembly, env_name, profile):
    """The keys, in order, that the API container had before these inputs
    existed. A default for any input would add one."""
    api = _containers(assembly(env_name, profile), env_name)["api"]
    expected = FULL_API_ENVIRONMENT if profile == "full" else CORE_API_ENVIRONMENT
    assert [entry["Name"] for entry in api["Environment"]] == expected


@pytest.mark.regression
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_unset_no_task_sets_cors_and_the_allow_list_is_empty(
    assembly, booted, env_name, profile
):
    containers = _containers(assembly(env_name, profile), env_name)
    for role, container in containers.items():
        assert not CORS_SETTINGS & set(_environment(container)), role
    settings = booted(containers["api"])
    assert settings["environment"] == (
        "staging" if env_name == "staging" else "production"
    )
    assert settings["allow_list"] == []


@pytest.mark.regression
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_unset_no_task_gets_a_cadence_and_the_defaults_apply(
    assembly, booted, env_name, profile
):
    containers = _containers(assembly(env_name, profile), env_name)
    for role, container in containers.items():
        assert not set(CADENCES.values()) & set(_environment(container)), role
    settings = booted(containers["api"])
    assert settings["cadences"] == settings["defaults"]


# --- exported: still nothing changes ------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_the_inputs_exported_in_the_environment_change_no_template(
    assembly, env_name, profile
):
    """Only `-c` sets them. Exported under the settings' names or the context
    names, with FIRST_SUPERUSER too, every template, asset manifest and
    manifest.json is the unset synthesis's, byte for byte."""
    unset = _compared(assembly(env_name, profile))
    exported = _compared(assembly(env_name, profile, exported=EXPORTED))
    assert sorted(exported) == sorted(unset)
    differing = sorted(name for name in unset if exported[name] != unset[name])
    assert not differing, (
        "exporting the inputs in the process environment changed these files, "
        f"so a shell's CORS_ORIGINS would reach a hand-run cdk deploy: {differing}"
    )


# --- set: the API task alone gets them ------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("profile", PROFILES)
def test_set_the_cadences_reach_the_api_task_alone_and_are_read(
    assembly, booted, profile
):
    containers = _containers(assembly("staging", profile, inputs=SET), "staging")
    api = _environment(containers["api"])
    assert {variable: api.get(variable) for variable in CADENCES.values()} == {
        variable: SET[name] for name, variable in CADENCES.items()
    }
    for role in ("dashboard", "migration"):
        given = set(_environment(containers[role]))
        assert not (set(CADENCES.values()) | CORS_SETTINGS) & given, role
    settings = booted(containers["api"])
    assert settings["cadences"] == {
        variable: int(SET[name]) for name, variable in CADENCES.items()
    }


@pytest.mark.regression
@pytest.mark.parametrize("profile", PROFILES)
def test_set_the_demo_origins_are_exactly_the_allow_list(assembly, booted, profile):
    containers = _containers(assembly("staging", profile, inputs=SET), "staging")
    api = _environment(containers["api"])
    assert api["CORS_ORIGINS"] == DEMO_ORIGINS
    assert not {"BACKEND_CORS_ORIGINS", "DASHBOARD_ORIGINS"} & set(api)
    names = [entry["Name"] for entry in containers["api"]["Environment"]]
    expected = FULL_API_ENVIRONMENT if profile == "full" else CORE_API_ENVIRONMENT
    assert [name for name in names if name not in VARIABLES.values()] == expected
    settings = booted(containers["api"])
    assert settings["environment"] == "staging"
    assert settings["allow_list"] == ["http://localhost:3200", "http://localhost:3300"]


# --- what is accepted and what is refused ---------------------------------------

#: Each input's accepted values; each is passed on as written.
ACCEPTED = {
    "cors_origins": [
        DEMO_ORIGINS,
        "https://shop.example.com",
        "http://localhost",
        "http://127.0.0.1:3200",
        "https://a-b.example.co.uk:8443",
    ],
    **{name: ["1", "5", "15", "1440"] for name in CADENCES},
}

#: Each input's refused values. For CORS, the backend would take each
#: differently from how it is written: allow every site (`*`), drop it with an
#: error log (not an origin), or rewrite it (case, spaces, a trailing slash, a
#: default port, a repeat). For a cadence, the setting would refuse it at start
#: (no circuit breaker stops that loop), spin (0), or read it differently from
#: how it is written.
REFUSED = {
    "cors_origins": [
        "*",
        "http://localhost:3200/",
        "localhost:3200",
        "",
        "http://localhost:3200,",
        ",http://localhost:3200",
        "http://localhost:3200,,http://localhost:3300",
        "http://localhost:3200, http://localhost:3300",
        " http://localhost:3200",
        "HTTP://LOCALHOST:3200",
        "http://localhost:80",
        "https://shop.example.com:443",
        "http://localhost:3200,http://localhost:3200",
        "http://localhost:3200/app",
        "http://user@localhost:3200",
        "ftp://localhost:3200",
        "http://localhost:99999",
        "http://localhost:0",
        "http://-bad.example.com",
        "http://[::1]:3200",
        "null",
    ],
    **{
        name: ["0", "-1", "1.5", "1m", "", "01", " 1", "1 ", "+1", "1e3", "１"]
        for name in CADENCES
    },
}

#: Not strings: what cdk.json or ~/.cdk.json, rather than `-c`, would supply.
NOT_STRINGS = [1, True, ["http://localhost:3200"], {"origin": "x"}]


@pytest.mark.parametrize(
    ("name", "value"),
    [(name, value) for name, values in ACCEPTED.items() for value in values],
)
def test_an_accepted_value_is_passed_on_as_written(stack_module, name, value):
    given = stack_module.api_context_environment({name: value}.get)
    assert given == {VARIABLES[name]: value}


@pytest.mark.regression
@pytest.mark.parametrize(
    ("name", "value"),
    [(name, value) for name, values in REFUSED.items() for value in values]
    + [(name, value) for name in VARIABLES for value in NOT_STRINGS],
)
def test_a_refused_value_is_refused_naming_its_input(stack_module, name, value):
    with pytest.raises(ValueError) as refused:
        stack_module.api_context_environment({name: value}.get)
    assert str(refused.value).startswith(f"{name} context"), str(refused.value)


def test_unset_inputs_give_no_entry_at_all(stack_module):
    assert stack_module.api_context_environment({}.get) == {}
    assert stack_module.api_context_environment({"other": "x"}.get) == {}


@pytest.mark.regression
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("cors_origins", "*"),
        ("cors_origins", ""),
        ("safety_check_interval_minutes", "0"),
        ("rollout_check_interval_minutes", "1m"),
        ("bandit_update_interval_minutes", "1.5"),
    ],
)
def test_a_refused_value_stops_the_synthesis_naming_its_input(
    tmp_path, core_dir, home, name, value
):
    """Each input refused through app.py itself, as `-c name=value` (an empty
    `-c cors_origins=` included): the synthesis fails before writing a
    template, and says which input. Every other value above takes the same
    path, through the function the test above calls."""
    outdir = tmp_path / "assembly"
    result = run_synth(
        outdir, "staging", cdk_dir=core_dir, inputs={name: value}, home=home
    )
    assert result.returncode != 0, result.stdout[-2000:]
    assert f"ValueError: {name} context" in result.stderr, result.stderr[-3000:]
    assert not list(outdir.glob("*.template.json"))
