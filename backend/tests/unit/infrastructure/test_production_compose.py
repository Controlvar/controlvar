"""The production compose file runs the published images and nothing else.

``deploy/compose/compose.yml`` is what a self-hoster downloads and starts. Its
properties are all ones that fail *silently* when broken -- a ``build:`` key
that quietly builds from a checkout, a secret with a ``:-`` default that starts
fine with a value everyone can read, a published database port, a demo seed
that creates a user with a published password -- so each is pinned here.

These read the file as text and YAML, with no Docker, so they run in the unit
job on every pull request. The ``compose-production`` workflow checks the same
file through ``docker compose config`` (real interpolation) and boots it.
The version on the image lines is pinned by ``scripts/check_version_sources.py``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
COMPOSE = COMPOSE_DIR / "compose.yml"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
GENERATOR = COMPOSE_DIR / "generate-env.sh"

#: No default at all: compose must refuse to start without them.
REQUIRED = (
    "SECRET_KEY",
    "FIRST_SUPERUSER",
    "FIRST_SUPERUSER_PASSWORD",
    "POSTGRES_PASSWORD",
    "PUBLIC_BASE_URL",
)
#: Secret, but optional: the only default either may have is empty.
OPTIONAL_SECRETS = ("AUDIT_HMAC_KEY", "METRICS_TOKEN")

API_REPO = "ghcr.io/controlvar/controlvar"
WEB_REPO = "ghcr.io/controlvar/controlvar-web"


def _version() -> str:
    return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


@pytest.fixture(scope="module")
def compose() -> Dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose) -> Dict[str, Any]:
    return compose["services"]


#: The volumes, and the database user, of every install made before the
#: project was renamed: compose names a volume `<project>_<key>`, and Postgres
#: applies the user only when it initialises an empty volume.
VOLUMES_BEFORE_THE_RENAME = {
    "experimently-selfhost_postgres_data",
    "experimently-selfhost_redis_data",
}
DATABASE_USER_BEFORE_THE_RENAME = "${POSTGRES_USER:-experimently}"


@pytest.mark.regression
def test_compose_upgrade_keeps_the_postgres_volume(compose):
    """An upgrade with the new file reaches the database it had. Were the
    project name renamed, `docker compose up -d` would create new, empty
    volumes, the bootstrap would make a fresh database with the first
    administrator, and the stack would come up healthy on it."""
    volumes = {
        spec.get("name")
        if isinstance(spec, dict) and spec.get("name")
        else f"{compose['name']}_{key}"
        for key, spec in compose["volumes"].items()
    }
    assert volumes == VOLUMES_BEFORE_THE_RENAME
    for service in ("postgres", "api"):
        assert (
            compose["services"][service]["environment"]["POSTGRES_USER"]
            == DATABASE_USER_BEFORE_THE_RENAME
        ), service


class TestShape:
    def test_exactly_the_four_services(self, services):
        assert set(services) == {"postgres", "redis", "api", "web"}

    def test_nothing_is_built(self, services):
        built = sorted(name for name, svc in services.items() if "build" in svc)
        assert not built, f"services with a build: key: {built}"

    def test_the_images_are_the_published_ones_at_this_version(self, services):
        tag = "${EXPERIMENTLY_PROFILE:-core}-" + _version()
        assert services["api"]["image"] == f"{API_REPO}:{tag}"
        assert services["web"]["image"] == f"{WEB_REPO}:{tag}"
        assert services["postgres"]["image"] == "postgres:16-alpine"
        assert services["redis"]["image"] == "redis:7-alpine"

    def test_redis_persists(self, services):
        command = services["redis"]["command"]
        assert command[command.index("--appendonly") + 1] == "yes"

    def test_only_the_dashboard_publishes_a_port_on_loopback_by_default(self, services):
        published = sorted(name for name, svc in services.items() if "ports" in svc)
        assert published == ["web"], published
        assert services["web"]["ports"] == [
            "${HTTP_BIND:-127.0.0.1}:${HTTP_PORT:-8080}:8080"
        ]

    def test_the_api_does_not_wait_on_the_bundled_database(self, services):
        """An external database leaves `postgres` unstarted; the entrypoint waits."""
        assert "depends_on" not in services["api"]

    def test_the_project_name_is_not_the_development_stacks(self, compose):
        """A checkout in `controlvar/` gives docker-compose.yml that project name.

        Sharing it, `up` recreates the development containers with this file's
        settings and mounts their volumes -- measured, not hypothetical.
        """
        assert compose["name"] == "experimently-selfhost"

    def test_the_named_volumes_are_declared(self, compose):
        assert set(compose["volumes"]) == {"postgres_data", "redis_data"}


RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"


@pytest.mark.regression
@pytest.mark.skipif(not RELEASE.is_file(), reason="this tree has no .github/workflows")
class TestPlatform:
    """api and web name the one platform their images are built for (#1268).

    The images are published for ``linux/amd64`` only, so on an arm64 host
    (Apple Silicon) ``up`` failed with "no matching manifest for
    linux/arm64/v8". Pinning those two services runs them emulated there;
    postgres and redis are multi-platform and must stay native.
    ``DOCKER_DEFAULT_PLATFORM`` is not a fix: it forces them to amd64 too.

    The platform is read from release.yml's ``API_PLATFORMS`` and
    ``WEB_PLATFORMS``, the one place it is written (#239), so this file cannot
    name a platform that is not built. Once either lists more than one, the
    pin would force emulation on hosts that have a native image: remove it.
    """

    @pytest.fixture(scope="class")
    def built(self) -> Dict[str, str]:
        env = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["env"]
        built = {}
        for service, name in (("api", "API_PLATFORMS"), ("web", "WEB_PLATFORMS")):
            listed = [p.strip() for p in str(env[name]).split(",") if p.strip()]
            assert len(listed) == 1, (
                f"release.yml's {name} is {listed}: the {service} image is now "
                "built for more than one platform, so remove `platform:` from "
                "the api and web services of deploy/compose/compose.yml (#181), "
                "and this test with it"
            )
            built[service] = listed[0]
        return built

    def test_exactly_api_and_web_name_a_platform(self, services):
        pinned = sorted(name for name, svc in services.items() if "platform" in svc)
        assert pinned == ["api", "web"], (
            f"services with a platform: key: {pinned}, not exactly api and web: "
            "those two images are amd64 only, and postgres and redis publish "
            "arm64 images and must run natively"
        )

    def test_api_and_web_ask_for_the_one_built_platform(self, services, built):
        for service, platform in built.items():
            assert services[service].get("platform") == platform, (
                f"{service} must carry `platform: {platform}`, the one platform "
                "release.yml builds its image for"
            )


class TestApiEnvironment:
    @pytest.fixture(scope="class")
    def env(self, services) -> Dict[str, Any]:
        return services["api"]["environment"]

    def test_production_with_no_seed_and_no_bypass(self, env):
        assert env["ENVIRONMENT"] == "production"
        assert env["SEED"] == ""
        assert env["DEV_AUTH_BYPASS"] == "false"
        assert env["RUN_MIGRATIONS"] == "true"

    @pytest.mark.parametrize("name", REQUIRED)
    def test_a_required_setting_has_no_default(self, env, name):
        assert re.fullmatch(rf"\$\{{{name}:\?required: [^}}]+\}}", env[name]), (
            f"{name} must be ${{{name}:?required: ...}}, got {env[name]!r}"
        )

    @pytest.mark.parametrize("name", OPTIONAL_SECRETS)
    def test_an_optional_secret_defaults_to_empty(self, env, name):
        assert env[name] == f"${{{name}:-}}"

    def test_the_database_password_is_one_value(self, services, env):
        assert (
            services["postgres"]["environment"]["POSTGRES_PASSWORD"]
            == env["POSTGRES_PASSWORD"]
        )

    def test_the_host_allow_list_is_derived_not_set(self, env):
        """ALLOWED_HOSTS would take precedence and hide a wrong PUBLIC_BASE_URL."""
        assert "ALLOWED_HOSTS" not in env


class TestText:
    def test_no_secret_anywhere_has_a_default(self):
        """Every `${NAME:-default}` / `${NAME-default}` on a secret, in any service."""
        secrets = REQUIRED + OPTIONAL_SECRETS + ("REDIS_PASSWORD",)
        text = COMPOSE.read_text(encoding="utf-8")
        for name, default in re.findall(r"\$\{([A-Z_]+):?-([^}]*)\}", text):
            if name in secrets:
                assert default == "", f"{name} has the default {default!r}"

    def test_no_demo_material(self):
        text = COMPOSE.read_text(encoding="utf-8").lower()
        for needle in ("shoplab", "streampulse", "pgadmin", "demo1234", "eptk_"):
            assert needle not in text, needle

    def test_every_variable_is_documented_in_the_example(self):
        text = COMPOSE.read_text(encoding="utf-8")
        names = set(re.findall(r"\$\{([A-Z_]+)", text))
        example = ENV_EXAMPLE.read_text(encoding="utf-8")
        undocumented = sorted(
            n for n in names if not re.search(rf"^#?\s*{n}=", example, re.M)
        )
        assert not undocumented, f"not in .env.example: {undocumented}"

    def test_the_example_leaves_every_required_value_empty(self):
        example = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name in REQUIRED:
            assert re.search(rf"^{name}=$", example, re.M), name


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
class TestGenerator:
    """UX C11: a second run must not replace the secrets an install already uses."""

    def _run(self, env_file: Path) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in REQUIRED}
        env["ENV_FILE"] = str(env_file)
        return subprocess.run(
            ["sh", str(GENERATOR)], env=env, capture_output=True, text=True
        )

    def test_it_writes_every_required_setting_privately(self, tmp_path):
        env_file = tmp_path / ".env"
        first = self._run(env_file)
        assert first.returncode == 0, first.stderr
        values = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if line and not line.startswith("#")
        )
        for name in REQUIRED + ("AUDIT_HMAC_KEY",):
            assert values.get(name), f"{name} is missing or empty"
        assert len(values["SECRET_KEY"]) >= 64
        assert values["EXPERIMENTLY_PROFILE"] == "core"
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    def test_without_inputs_it_writes_the_local_url_and_the_example_admin(
        self, tmp_path
    ):
        env_file = tmp_path / ".env"
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in REQUIRED + ("EXPERIMENTLY_PROFILE",)
        }
        env["ENV_FILE"] = str(env_file)
        result = subprocess.run(
            ["sh", str(GENERATOR)], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        values = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if line and not line.startswith("#")
        )
        assert values["PUBLIC_BASE_URL"] == "http://localhost:8080"
        assert values["FIRST_SUPERUSER"] == "admin@example.com"

    def test_given_inputs_it_writes_them(self, tmp_path):
        env_file = tmp_path / ".env"
        env = {k: v for k, v in os.environ.items() if k not in REQUIRED}
        env.update(
            ENV_FILE=str(env_file),
            PUBLIC_BASE_URL="https://controlvar.example.com",
            FIRST_SUPERUSER="you@example.com",
            EXPERIMENTLY_PROFILE="full",
        )
        result = subprocess.run(
            ["sh", str(GENERATOR)], env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        text = env_file.read_text()
        assert "PUBLIC_BASE_URL=https://controlvar.example.com\n" in text
        assert "FIRST_SUPERUSER=you@example.com\n" in text
        assert "EXPERIMENTLY_PROFILE=full\n" in text

    def _bin_without_openssl(self, tmp_path: Path, python: bool) -> Path:
        """A PATH with what the script runs besides openssl: sh's builtins
        need only dirname and cat, and python3 when *python*."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("dirname", "cat"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        if python:
            (bin_dir / "python3").symlink_to(sys.executable)
        return bin_dir

    def test_without_openssl_it_generates_with_python3(self, tmp_path):
        env_file = tmp_path / ".env"
        env = {
            "PATH": str(self._bin_without_openssl(tmp_path, python=True)),
            "ENV_FILE": str(env_file),
        }
        result = subprocess.run(
            [shutil.which("sh"), str(GENERATOR)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        values = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if line and not line.startswith("#")
        )
        assert re.fullmatch(r"[0-9a-f]{64}", values["SECRET_KEY"]), values["SECRET_KEY"]

    def test_with_neither_openssl_nor_python3_it_writes_nothing(self, tmp_path):
        env_file = tmp_path / ".env"
        env = {
            "PATH": str(self._bin_without_openssl(tmp_path, python=False)),
            "ENV_FILE": str(env_file),
        }
        result = subprocess.run(
            [shutil.which("sh"), str(GENERATOR)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "Need openssl or python3" in result.stderr
        assert not env_file.exists()

    def test_a_second_run_refuses_and_leaves_the_file_alone(self, tmp_path):
        env_file = tmp_path / ".env"
        assert self._run(env_file).returncode == 0
        before = hashlib.sha256(env_file.read_bytes()).hexdigest()

        second = self._run(env_file)

        assert second.returncode != 0, "the second run overwrote .env"
        assert "not overwriting" in second.stderr
        assert hashlib.sha256(env_file.read_bytes()).hexdigest() == before

    def test_an_unknown_profile_is_refused(self, tmp_path):
        env_file = tmp_path / ".env"
        env = {**os.environ, "ENV_FILE": str(env_file), "EXPERIMENTLY_PROFILE": "x"}
        result = subprocess.run(
            ["sh", str(GENERATOR)], env=env, capture_output=True, text=True
        )
        assert result.returncode != 0
        assert not env_file.exists()


WORKFLOW = REPO_ROOT / ".github" / "workflows" / "compose-production.yml"


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="this tree has no .github/workflows")
class TestWorkflow:
    """The job that boots the file: it must not build, pull or skip its checks."""

    @pytest.fixture(scope="class")
    def runs(self) -> str:
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["compose-production"]
        for step in job["steps"]:
            assert not step.get("continue-on-error"), step.get("name")
        return "\n".join(str(step.get("run", "")) for step in job["steps"])

    def test_it_starts_the_file_without_building_or_pulling_the_app(self, runs):
        assert "up -d --wait --no-build --pull never" in runs

    def test_it_runs_the_generator_twice_and_the_config_check(self, runs):
        assert runs.count("./deploy/compose/generate-env.sh") == 2
        assert "scripts/check_compose_config.py --env-file deploy/compose/.env" in runs

    def test_it_rehearses_in_the_compose_topology(self, runs):
        assert "TOPOLOGY=compose" in runs
        assert "bash tests/alb/rehearse.sh" in runs

    def test_it_refuses_the_demo_login_and_checks_only_the_dashboard_publishes(
        self, runs
    ):
        assert '-d \'{"email":"admin@demo.com","password":"Demo1234!"}\'' in runs
        assert 'test "$code" = "401"' in runs
        assert "for svc in postgres:5432 redis:6379 api:8000; do" in runs

    def test_compose_is_never_pointed_at_the_file_through_the_environment(self):
        """COMPOSE_FILE in the environment makes compose read .env from the cwd."""
        workflow = yaml.safe_load(WORKFLOW.read_text())
        assert "COMPOSE_FILE" not in (workflow.get("env") or {})
        assert "COMPOSE_FILE" not in WORKFLOW.read_text().replace(
            "Not COMPOSE_FILE", ""
        )

    def test_docker_smoke_keeps_the_alb_topology(self):
        """rehearse.sh defaults to alb, and Docker Smoke must not change that."""
        smoke = (REPO_ROOT / ".github" / "workflows" / "pr-qa-gate.yml").read_text()
        assert "TOPOLOGY" not in smoke
        assert (
            'TOPOLOGY="${TOPOLOGY:-alb}"'
            in (REPO_ROOT / "tests" / "alb" / "rehearse.sh").read_text()
        )
