"""What the front door says about the repository, read from the repository.

The README, the getting-started pages and the contributor set-up pages state
facts about files in this checkout: the licences, the version, the local
development stack, the SDK manifests, the pre-commit hooks and the
requirements profiles. Each test reads the file that decides the fact, never
the page; the pages' sentences are tied to these tests by their registries
under ``docs/_claims/``, so a change to either side fails one of the two.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = Path(__file__).resolve().parents[4]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(_text("docker-compose.yml"))


# ---------------------------------------------------------------------------
# Licences and version
# ---------------------------------------------------------------------------
def test_the_tree_is_apache_and_the_sdks_are_mit():
    reuse = tomllib.loads(_text("REUSE.toml"))
    licences = {
        tuple(a["path"]): a["SPDX-License-Identifier"] for a in reuse["annotations"]
    }
    assert licences == {("**",): "Apache-2.0", ("sdk/**",): "MIT"}
    assert "Apache License" in _text("LICENSE").split("\n", 3)[1]
    assert _text("sdk/LICENSE").startswith("MIT License")


def test_the_version_is_before_1_0():
    version = _text("VERSION").strip()
    assert re.fullmatch(r"0\.[0-9]+\.[0-9]+", version), version


# ---------------------------------------------------------------------------
# The local development stack (docker-compose.yml)
# ---------------------------------------------------------------------------
def test_the_default_stack_is_postgres_redis_the_api_and_the_dashboard(compose):
    default = {
        name
        for name, service in compose["services"].items()
        if "profiles" not in service
    }
    assert default == {"postgres", "redis", "api", "frontend"}
    assert compose["services"]["postgres"]["image"] == "postgres:16-alpine"
    assert compose["services"]["redis"]["image"] == "redis:7-alpine"


def test_the_api_and_dashboard_images_are_built_from_this_checkout(compose):
    for name, dockerfile in (
        ("api", "backend/Dockerfile"),
        ("frontend", "frontend/Dockerfile"),
    ):
        build = compose["services"][name]["build"]
        assert build["context"] == "." and build["dockerfile"] == dockerfile
    text = _text("docker-compose.yml")
    assert (
        "experimently-api:${EXPERIMENTLY_IMAGE_TAG:-${EXPERIMENTLY_PROFILE:-core}}"
        in text
    )
    assert (
        "experimently-web:${EXPERIMENTLY_IMAGE_TAG:-${EXPERIMENTLY_PROFILE:-core}}"
        in text
    )


def test_without_a_profile_the_stack_is_core(compose):
    assert (
        compose["services"]["api"]["build"]["target"] == "${EXPERIMENTLY_PROFILE:-core}"
    )
    args = compose["services"]["frontend"]["build"]["args"]
    assert args["EXPERIMENTLY_PROFILE"] == "${EXPERIMENTLY_PROFILE:-core}"


def test_the_stack_runs_development_settings_and_the_demo_seed(compose):
    env = compose["services"]["api"]["environment"]
    assert env["ENVIRONMENT"] == "${ENVIRONMENT:-development}"
    assert env["SEED"] == "${SEED-demo}"
    assert env["FIRST_SUPERUSER_PASSWORD"] == "${FIRST_SUPERUSER_PASSWORD:-Demo1234!}"


def test_three_settings_are_read_from_a_dot_env_file(compose):
    api = compose["services"]["api"]["environment"]
    for name in ("SECRET_KEY", "FIRST_SUPERUSER_PASSWORD", "POSTGRES_PASSWORD"):
        assert api[name].startswith("${" + name + ":-"), (name, api[name])
    postgres = compose["services"]["postgres"]["environment"]
    assert postgres["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:-postgres}"


@pytest.mark.parametrize(
    ("service", "variable"),
    [
        ("postgres", "POSTGRES_HOST_PORT"),
        ("redis", "REDIS_HOST_PORT"),
        ("api", "API_HOST_PORT"),
        ("frontend", "FRONTEND_HOST_PORT"),
    ],
)
def test_each_host_port_has_its_override(compose, service, variable):
    (port,) = compose["services"][service]["ports"]
    assert port.startswith("127.0.0.1:${" + variable + ":-"), port


#: The one command that starts the demo profile with the seeds its apps need
#: (#1266). The bare `docker compose --profile demo up -d` starts ShopLab,
#: StreamPulse and their simulators with no API key in the database: the apps
#: answer 401 and the simulators exit at start.
DEMO_COMMAND = (
    "SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait"
)


def _header() -> str:
    return _text("docker-compose.yml").split("\n\n", 1)[0]


def test_the_header_describes_the_profiles_and_the_port_overrides():
    header = _header()
    for needle in (
        "--profile tools",
        "--profile aws",
        "*_HOST_PORT",
    ):
        assert needle in header, needle
    demo = [line for line in header.split("\n") if "--profile demo" in line]
    assert [line.lstrip("#").strip() for line in demo] == [DEMO_COMMAND], demo


def test_the_demo_banner_and_both_simulators_print_the_demo_command(compose):
    text = _text("docker-compose.yml")
    banner = text.split("# profile: demo", 1)[1].split("# ====", 1)[0]
    assert f"#   {DEMO_COMMAND}\n" in banner, banner
    for app in ("shoplab", "streampulse"):
        command = " ".join(compose["services"][f"{app}-simulator"]["command"])
        assert (
            f"no /data/demo/{app}/.api_key: the {app} seed has not run. "
            f"Start the stack with: {DEMO_COMMAND}"
        ) in command, command


def test_make_demo_runs_the_demo_command_with_build():
    makefile = _text("Makefile")
    assert "COMPOSE := docker compose\n" in makefile
    recipe = makefile.split("\ndemo:", 1)[1].split("\n")[1].strip()
    assert recipe.replace("$(COMPOSE)", "docker compose") == DEMO_COMMAND.replace(
        "up -d --wait", "up -d --build --wait"
    ), recipe


def test_the_demo_seed_creates_three_experiments_and_two_flags():
    from backend.scripts import seed_demo_data

    assert len(seed_demo_data.DEMO_EXPERIMENT_KEYS) == 3
    source = _text("backend/scripts/seed_demo_data.py")
    body = source.split("def seed_feature_flags", 1)[1].split("\ndef ", 1)[0]
    assert len(re.findall(r"= FeatureFlag\(", body)) == 2


# ---------------------------------------------------------------------------
# Self-hosting: what CI runs
# ---------------------------------------------------------------------------
def _workflow(name: str) -> dict:
    document = yaml.safe_load(_text(f".github/workflows/{name}"))
    # PyYAML reads the bare key `on` as True.
    document["on"] = document.pop(True, document.get("on"))
    return document


def test_the_chart_is_installed_on_a_kind_cluster():
    workflow = _workflow("chart-kind.yml")
    assert "pull_request" in workflow["on"]
    runs = [
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]
    assert "bash scripts/chart_kind.sh cluster" in runs
    assert "bash scripts/chart_kind.sh guide" in runs
    script = _text("scripts/chart_kind.sh")
    assert "kind create cluster" in script and "helm install" in script


def test_only_a_person_starts_a_deploy():
    assert list(_workflow("deploy.yml")["on"]) == ["workflow_dispatch"]


def test_the_licence_report_and_notice_are_present():
    report = _text("THIRD_PARTY_LICENSES.md")
    assert "fastapi" in report.lower(), "the report lists no backend package"
    assert _text("NOTICE").strip()
    assert "step_licences()" in _text("scripts/core_build.sh")


def test_the_database_lives_in_a_named_volume(compose):
    assert (
        "postgres_data:/var/lib/postgresql/data"
        in compose["services"]["postgres"]["volumes"]
    )
    assert "postgres_data" in compose["volumes"]


def test_the_dashboard_dev_server_listens_on_3100_and_proxies_nothing():
    package = json.loads(_text("frontend/package.json"))
    assert re.search(r"\bnext dev -p 3100\b", package["scripts"]["dev"])
    assert "rewrites" not in _text("frontend/next.config.js")


# ---------------------------------------------------------------------------
# Toolchain versions
# ---------------------------------------------------------------------------
def test_python_3_11_and_node_22():
    project = tomllib.loads(_text("pyproject.toml"))["project"]
    assert project["requires-python"] == ">=3.11"
    assert _text(".python-version").strip() == "3.11.10"
    assert _text(".nvmrc").strip() == "22"


# ---------------------------------------------------------------------------
# SDKs
# ---------------------------------------------------------------------------
def _live_contract_sdks() -> list:
    gate = yaml.safe_load(_text(".github/workflows/pr-qa-gate.yml"))
    found = [
        job["with"]["sdks"].split()
        for job in gate["jobs"].values()
        if "sdks" in (job.get("with") or {})
    ]
    assert len(found) == 1, found
    return found[0]


def test_thirteen_of_the_sixteen_sdks_run_the_live_contract():
    sdks = sorted(p.name for p in (ROOT / "sdk").iterdir() if p.is_dir())
    live = _live_contract_sdks()
    assert len(sdks) == 16 and len(live) == 13
    assert sorted(set(sdks) - set(live)) == ["elixir", "flutter", "ios"]


def test_the_sdk_package_names():
    assert json.loads(_text("sdk/js/package.json"))["name"] == "@getexperimently/js-sdk"
    assert (
        json.loads(_text("sdk/react/package.json"))["name"]
        == "@getexperimently/react-sdk"
    )
    python = tomllib.loads(_text("sdk/python/pyproject.toml"))["project"]
    assert python["name"] == "experimently"
    assert (ROOT / "sdk/python/experimentation/__init__.py").is_file()
    pom = _text("sdk/java/core/pom.xml")
    assert "<groupId>com.getexperimently</groupId>" in pom
    assert re.search(r"</parent>\s*<artifactId>experimently-sdk</artifactId>", pom)


def test_the_java_sdk_has_spring_boot_auto_configuration():
    found = list((ROOT / "sdk/java").rglob("ExperimentationAutoConfiguration.java"))
    assert found and "@AutoConfiguration" in found[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Contributor set-up: pre-commit, requirements profiles, packaging, editor
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def precommit() -> dict:
    config = yaml.safe_load(_text(".pre-commit-config.yaml"))
    return {repo["repo"]: repo for repo in config["repos"]}


def test_the_file_hygiene_hooks(precommit):
    hooks = {
        h["id"]: h
        for h in precommit["https://github.com/pre-commit/pre-commit-hooks"]["hooks"]
    }
    assert list(hooks) == [
        "check-yaml",
        "check-json",
        "check-added-large-files",
        "check-merge-conflict",
        "debug-statements",
        "detect-private-key",
        "check-toml",
        "check-case-conflict",
    ]
    assert hooks["check-yaml"]["exclude"] == "^charts/[^/]+/templates/"
    assert re.search(hooks["check-json"]["exclude"], "frontend/tsconfig.core.json")
    assert hooks["check-added-large-files"]["args"] == ["--maxkb=1024"]


def test_the_ruff_hooks_cover_the_four_trees_at_the_pinned_version(precommit):
    ruff = precommit["https://github.com/astral-sh/ruff-pre-commit"]
    assert [h["id"] for h in ruff["hooks"]] == ["ruff", "ruff-format"]
    assert {h["files"] for h in ruff["hooks"]} == {
        "^(backend|modules|scripts|tests/acceptance)/"
    }
    (pin,) = re.findall(r"^ruff==(\S+)", _text("backend/requirements.txt"), re.M)
    assert ruff["rev"] == f"v{pin}"


def test_two_local_hooks_run_the_publication_check(precommit):
    local = precommit["local"]["hooks"]
    assert [(h["entry"], h["stages"]) for h in local] == [
        ("python3 scripts/leak_guard.py --staged", ["pre-commit"]),
        ("python3 scripts/leak_guard.py --push", ["pre-push"]),
    ]


def test_pre_commit_runs_no_frontend_check(precommit):
    ids = {h["id"] for repo in precommit.values() for h in repo["hooks"]}
    assert not {i for i in ids if re.search(r"eslint|tsc|prettier", i)}


def test_the_config_has_three_groups(precommit):
    assert list(precommit) == [
        "https://github.com/pre-commit/pre-commit-hooks",
        "https://github.com/astral-sh/ruff-pre-commit",
        "local",
    ]


def test_make_lint_and_the_lint_job_run_the_same_checks():
    makefile = _text("Makefile")
    lint = makefile.split("\nlint:", 1)[1].split("\n\n", 1)[0]
    boundary = makefile.split("\nlint-boundary:", 1)[1].split("\n\n", 1)[0]
    recipe = lint + boundary
    for needle in (
        "ruff check",
        "ruff format --check",
        "lint-imports",
        "reuse lint",
        "check_requirements_lock.py",
        "check_legacy_name.py",
        "npm run lint",
        "npx tsc --noEmit",
        "hadolint",
        "actionlint",
    ):
        assert needle in recipe, needle
    job = _text(".github/workflows/lint.yml")
    for needle in (
        "ruff check",
        "ruff format --check",
        "check_requirements_lock.py",
        "check_legacy_name.py",
        "npm run lint",
        "npx tsc --noEmit",
        "actionlint",
    ):
        assert needle in job, needle
    trees = "backend/ modules/ scripts/ tests/acceptance/"
    assert f"ruff check {trees}" in job and f"ruff format --check {trees}" in job


def test_the_test_jobs_install_backend_requirements():
    gate = _text(".github/workflows/pr-qa-gate.yml")
    assert gate.count("pip install -r backend/requirements.txt") >= 3


def test_the_requirements_profiles_resolve_to_one_file():
    def lines(name):
        return [
            line
            for line in _text(f"requirements/{name}.txt").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    assert lines("base") == ["-r ../backend/requirements.txt"]
    assert lines("test") == ["-r base.txt"]
    assert [line.split("==")[0] for line in lines("dev")] == [
        "-r base.txt",
        "ipython",
        "debugpy",
    ]
    assert [line.split("==")[0] for line in lines("prod")] == [
        "-r base.txt",
        "gunicorn",
    ]


def test_the_api_image_installs_the_runtime_lock():
    dockerfile = _text("backend/Dockerfile")
    assert "COPY backend/requirements/runtime.lock /tmp/runtime.lock" in dockerfile
    assert "requirements/prod.txt" not in dockerfile


def test_the_platform_package_is_declared_in_pyproject_only():
    pyproject = tomllib.loads(_text("pyproject.toml"))
    assert pyproject["project"]["name"] == "experimently-platform"
    assert "build-system" in pyproject
    assert not (ROOT / "setup.py").exists() and not (ROOT / "setup.cfg").exists()


def test_ruff_is_the_one_python_formatter_and_linter():
    pyproject = tomllib.loads(_text("pyproject.toml"))
    assert "ruff" in pyproject["tool"]
    assert not {"black", "isort", "flake8"} & set(pyproject["tool"])
    assert not (ROOT / ".flake8").exists()


def test_the_editor_folder_is_ignored():
    assert ".vscode/" in _text(".gitignore").splitlines()


def test_there_is_no_prettier_and_eslint_allows_no_warning():
    package = json.loads(_text("frontend/package.json"))
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    assert "prettier" not in deps
    assert not list(ROOT.glob(".prettierrc*")) and not list(
        (ROOT / "frontend").glob(".prettierrc*")
    )
    assert "--max-warnings 0" in package["scripts"]["lint"]


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def test_nothing_in_the_backend_reads_experimently_profile():
    hits = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "backend" / "app").rglob("*.py")
        if "EXPERIMENTLY_PROFILE" in p.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_a_full_dashboard_image_built_without_the_modules_stops():
    dockerfile = _text("frontend/Dockerfile")
    assert "test -d /app/modules/frontend/src" in dockerfile


def test_each_dashboard_image_checks_its_bundle_against_the_profile():
    dockerfile = _text("frontend/Dockerfile")
    assert (
        "! grep -rlE '/api/v1/workspaces|/api/v1/rbac/' out/_next/static" in dockerfile
    )
    assert "grep -rlE '/api/v1/rbac/' out/_next/static" in dockerfile


def test_ci_runs_the_core_dashboard_seam_on_a_complete_checkout():
    gate = _text(".github/workflows/pr-qa-gate.yml")
    for command in (
        "npx tsc --noEmit -p tsconfig.core.json",
        "EXPERIMENTLY_PROFILE=core npx jest",
        "EXPERIMENTLY_PROFILE=core npm run build",
    ):
        assert command in gate, command


def test_there_is_no_licence_key_or_tier_check():
    pattern = re.compile(r"licen[cs]e_?key|LICEN[CS]E_KEY|plan_tier|tier_check", re.I)
    roots = [ROOT / "backend" / "app"]
    if (ROOT / "modules" / "backend" / "app").is_dir():
        roots.append(ROOT / "modules" / "backend" / "app")
    hits = [
        p.relative_to(ROOT).as_posix()
        for root in roots
        for p in root.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert hits == []
