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

import ast
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from itertools import zip_longest
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

import pytest
import yaml

from backend.tests.unit.infrastructure import _workflow_graph as wg
from backend.tests.unit.infrastructure._compose_feature_keys import FEATURE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
COMPOSE = COMPOSE_DIR / "compose.yml"
ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
GENERATOR = COMPOSE_DIR / "generate-env.sh"
README = COMPOSE_DIR / "README.md"

#: No default at all: compose must refuse to start without them.
REQUIRED = (
    "SECRET_KEY",
    "FIRST_SUPERUSER",
    "FIRST_SUPERUSER_PASSWORD",
    "POSTGRES_PASSWORD",
    "PUBLIC_BASE_URL",
)
#: Secret, but optional: the only default any of them may have is empty. The
#: feature keys (#1253, #256) are WAREHOUSE_CREDENTIALS_KEYS and the LLM
#: provider keys, from ``_compose_feature_keys``.
OPTIONAL_SECRETS = ("AUDIT_HMAC_KEY", "METRICS_TOKEN") + FEATURE_KEYS

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


@pytest.mark.regression
class TestFeatureKeys:
    """The api is given each feature key, empty unless .env sets it (#1253, #256).

    The file passed none of them, so a key put in `.env`, where the README
    says settings go, never reached the container: saving a warehouse
    connection answered 503 and every Anthropic, OpenAI and Gemini variant
    failed. Exactly
    ``${NAME:-}``: a default would be a value everyone can read.
    """

    @pytest.mark.parametrize("name", FEATURE_KEYS)
    def test_the_api_is_passed_the_key_empty_by_default(self, services, name):
        env = services["api"]["environment"]
        assert name in env, f"{name} is not passed to the api service"
        assert env[name] == f"${{{name}:-}}", (
            f"{name} must be ${{{name}:-}}, got {env[name]!r}"
        )


@pytest.mark.unit
@pytest.mark.regression
class TestForwardedAllowIps:
    """The proxies the API believes besides its own network reach it from
    `.env`. The file passed no such setting, so a value in `.env` never
    reached the container. Exactly ``${FORWARDED_ALLOW_IPS:-}``: empty adds
    nothing, and `-` without the colon would hand an unset value on as
    unset rather than empty."""

    def test_the_api_is_passed_the_setting_empty_by_default(self, services):
        env = services["api"]["environment"]
        assert env["FORWARDED_ALLOW_IPS"] == "${FORWARDED_ALLOW_IPS:-}"

    def test_the_config_check_follows_it_through_compose(self):
        """scripts/check_compose_config.py, which the Compose Production job
        runs through `docker compose config`, checks it the way it checks the
        feature keys: empty from a `.env` without it, its value from one
        that sets it."""
        import importlib.util

        script = REPO_ROOT / "scripts" / "check_compose_config.py"
        spec = importlib.util.spec_from_file_location("check_compose_config", script)
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        assert check.PASSED_SETTINGS == ("FORWARDED_ALLOW_IPS",) + COGNITO_SETTINGS
        names = ("FORWARDED_ALLOW_IPS",)
        assert check.feature_key_failures({"FORWARDED_ALLOW_IPS": ""}, {}, names) == []
        given = {"FORWARDED_ALLOW_IPS": "192.0.2.10"}
        assert check.feature_key_failures(dict(given), given, names) == []
        assert check.feature_key_failures({}, given, names) == [
            "FORWARDED_ALLOW_IPS is not passed to the api service"
        ]
        assert check.feature_key_failures({"FORWARDED_ALLOW_IPS": "x"}, {}, names) == [
            "FORWARDED_ALLOW_IPS on the api service is not empty, and .env does not set it"
        ]


#: Passed to the api as ``${NAME:-}``: empty unless `.env` sets them (#708).
COGNITO_SETTINGS = ("COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID", "COGNITO_DOMAIN")


@pytest.mark.unit
@pytest.mark.regression
class TestSignIn:
    """How people sign in reaches the api from `.env` (#708). The file set
    ``AUTH_PROVIDER: local`` and passed no Cognito setting and no region, so a
    Cognito deployment could not be configured through it at all."""

    def test_local_unless_env_says_otherwise(self, services):
        env = services["api"]["environment"]
        assert env["AUTH_PROVIDER"] == "${AUTH_PROVIDER:-local}"

    @pytest.mark.parametrize("name", COGNITO_SETTINGS)
    def test_each_cognito_setting_is_passed_empty_by_default(self, services, name):
        env = services["api"]["environment"]
        assert env.get(name) == f"${{{name}:-}}", (
            f"{name} must be ${{{name}:-}}, got {env.get(name)!r}"
        )

    def test_exactly_these_cognito_settings_and_no_aws_credentials(self, services):
        env = services["api"]["environment"]
        assert sorted(n for n in env if n.startswith("COGNITO_")) == sorted(
            COGNITO_SETTINGS
        )
        credentials = {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        }
        assert not credentials & set(env), sorted(credentials & set(env))

    def test_the_region_defaults_to_the_apis_own(self, services):
        """us-east-1 is what the API's Cognito client uses with no AWS_REGION
        (backend/app/services/auth_service.py), so unset is unchanged."""
        from backend.app.core.config import Settings

        env = services["api"]["environment"]
        assert env["AWS_REGION"] == "${AWS_REGION:-us-east-1}"
        assert Settings.model_fields["AWS_REGION"].default == "us-east-1"
        auth = (
            REPO_ROOT / "backend" / "app" / "services" / "auth_service.py"
        ).read_text()
        assert 'os.environ.get("AWS_REGION", "us-east-1")' in auth

    def test_the_config_check_follows_the_cognito_settings_through_compose(self):
        """scripts/check_compose_config.py, which the Compose Production job runs
        through `docker compose config`, checks each of them arrives empty from
        a `.env` without it and with its value from one that sets it."""
        import importlib.util

        script = REPO_ROOT / "scripts" / "check_compose_config.py"
        spec = importlib.util.spec_from_file_location("check_compose_config", script)
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        assert set(COGNITO_SETTINGS) <= set(check.PASSED_SETTINGS)
        empty = dict.fromkeys(COGNITO_SETTINGS, "")
        assert check.feature_key_failures(empty, {}, COGNITO_SETTINGS) == []
        given = {"COGNITO_DOMAIN": "controlvar-test.auth.us-west-2.amazoncognito.com"}
        assert (
            check.feature_key_failures({**empty, **given}, given, COGNITO_SETTINGS)
            == []
        )
        assert check.feature_key_failures(
            {"COGNITO_CLIENT_ID": "", "COGNITO_DOMAIN": ""}, {}, COGNITO_SETTINGS
        ) == ["COGNITO_USER_POOL_ID is not passed to the api service"]


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


@pytest.fixture(scope="module")
def readme_text() -> str:
    """The README as one line: no backticks, no line wrapping."""
    return " ".join(README.read_text(encoding="utf-8").replace("`", "").split())


@pytest.fixture(scope="module")
def published(services) -> str:
    """The `bind:port` the dashboard publishes on by default, from compose.yml."""
    (mapping,) = services["web"]["ports"]
    match = re.fullmatch(
        r"\$\{HTTP_BIND:-([^}]+)\}:\$\{HTTP_PORT:-([0-9]+)\}:[0-9]+", mapping
    )
    assert match, f"no default bind and port to read in {mapping!r}"
    return f"{match.group(1)}:{match.group(2)}"


@pytest.fixture(scope="module")
def default_url() -> str:
    """The PUBLIC_BASE_URL generate-env.sh writes when it is given none."""
    match = re.search(
        r"^public_base_url=\$\{PUBLIC_BASE_URL:-([^}]+)\}$",
        GENERATOR.read_text(encoding="utf-8"),
        re.M,
    )
    assert match, "generate-env.sh has no one-line default for PUBLIC_BASE_URL"
    return match.group(1)


@pytest.mark.regression
class TestReadme:
    """The README sends a reader to an address that signs in.

    The dashboard binds `127.0.0.1`, but the API answers only for the host name
    of PUBLIC_BASE_URL (`localhost` by default), so a sign-in at the loopback
    address is refused with 400 while the stack reports healthy. The sentences
    that say where to open the dashboard and where to point an SDK are held to
    the two files that decide it, read here and not retyped: the port
    compose.yml publishes and the URL generate-env.sh writes by default.
    """

    def test_it_names_the_address_the_dashboard_binds(self, readme_text, published):
        assert f"on {published} by default" in readme_text

    def test_it_says_to_open_the_default_url_not_the_loopback_address(
        self, readme_text, published, default_url
    ):
        bind, port = published.split(":")
        assert (
            f"Open it at your PUBLIC_BASE_URL, {default_url} by default" in readme_text
        )
        assert f"not at http://{published}:" in readme_text
        # The premise of "not at": the default URL's host name is not the address
        # the dashboard binds, and the default URL carries the published port.
        assert urlparse(default_url).hostname != bind
        assert urlparse(default_url).port == int(port)

    def test_it_points_sdks_at_the_dashboard_address(self, readme_text, default_url):
        sentence = f"base URL at the dashboard's address ({default_url} by default)"
        assert sentence in readme_text


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
UPGRADE_SCRIPT = REPO_ROOT / "scripts" / "compose_upgrade.sh"

#: The compose-upgrade job's steps that run its script unconditionally, in
#: order: one phase of scripts/compose_upgrade.sh each (the image builds sit
#: between `previous` and `images`).
UPGRADE_PHASES = (
    "previous",
    "images",
    "blocks",
    "install",
    "prepare",
    "retag",
    "upgrade",
    "restart",
    "rollback",
)

#: What run_block may pass a block: the variables the page tells the reader to
#: set (Upgrade's table, and Run it's ENV_FILE), and nothing of the job's.
BLOCK_VARIABLES = {"RELEASE", "PUBLIC_BASE_URL", "ENV_FILE"}

#: A call of the script's run_block: the block's name, then VAR=VALUE words.
RUN_BLOCK_CALL = re.compile(
    r"\brun_block ([a-z][a-z0-9-]*)((?: [A-Za-z_][A-Za-z0-9_]*=\S+)*)"
)

#: A call of the script's dc (its own `docker compose`), as a command or in $(...).
DC_CALL = re.compile(r"(?<![\w$-])dc\s")


def _function(script: str, name: str) -> str:
    """The body of shell function *name* in *script*, `name() {` to `}`."""
    found = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}$", script, re.M | re.S)
    assert found, f"compose_upgrade.sh has no {name} function"
    return found.group(1)


def _code(script: str) -> str:
    """*script* without its comment lines, which may name what it never runs."""
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="this tree has no .github/workflows")
class TestWorkflow:
    """The jobs that boot the file and rehearse its upgrade: they must not
    build where they should pull or pull where they should build, log in to a
    registry, run a block the page does not hold, or skip a check."""

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

    # -- compose-upgrade (#1807): the README's own blocks, from N-1's images --

    @pytest.fixture(scope="class")
    def jobs(self) -> Dict[str, Any]:
        return wg.load(WORKFLOW)["jobs"]

    @pytest.fixture(scope="class")
    def upgrade_job(self, jobs) -> Dict[str, Any]:
        assert "compose-upgrade" in jobs, (
            "compose-production.yml has no compose-upgrade job"
        )
        return jobs["compose-upgrade"]

    @pytest.fixture(scope="class")
    def script(self) -> str:
        assert UPGRADE_SCRIPT.is_file(), "scripts/compose_upgrade.sh does not exist"
        return UPGRADE_SCRIPT.read_text(encoding="utf-8")

    def test_no_job_or_step_may_fail_and_carry_on(self, jobs):
        """SPEC, refused: `continue-on-error` anywhere in the workflow."""
        for name, job in jobs.items():
            assert "continue-on-error" not in job, name
            for step in job.get("steps", []):
                assert "continue-on-error" not in step, f"{name}: {step.get('name')}"

    def test_nothing_logs_in_to_a_registry(self, script):
        """SPEC, refused: every pull is anonymous, as a fork's must be; and the
        header no longer says nothing pulls."""
        text = WORKFLOW.read_text(encoding="utf-8")
        for refused in ("docker login", "login-action", "packages:"):
            assert refused not in text, f"{refused!r} in compose-production.yml"
            assert refused not in script, f"{refused!r} in compose_upgrade.sh"
        assert "nothing here pulls from ghcr.io" not in text

    def test_the_upgrade_job_is_named_bounded_and_skips_only_a_push(self, upgrade_job):
        """Not on a push: on the release commit, compose.yml names core-N
        before release.yml has published it, so the page's pull would fail."""
        assert upgrade_job["name"] == "Compose Upgrade"
        assert upgrade_job["if"] == "github.event_name != 'push'"
        assert upgrade_job["timeout-minutes"] == 40
        assert upgrade_job["runs-on"] == "ubuntu-latest"
        assert "strategy" not in upgrade_job, "one profile, no matrix"
        assert "concurrency" not in upgrade_job, "the workflow's group only"

    def test_the_upgrade_job_runs_on_pull_requests_nightly_and_by_hand(self):
        on = wg.triggers(wg.load(WORKFLOW))
        assert on["schedule"] == [{"cron": "30 4 * * *"}]
        assert set(on["workflow_dispatch"]["inputs"]) == {"previous_version"}
        assert on["workflow_dispatch"]["inputs"]["previous_version"]["default"] == ""
        paths = on["pull_request"]["paths"]
        for path in (
            "deploy/compose/README.md",
            "scripts/compose_upgrade.sh",
            "scripts/guide_blocks.py",
        ):
            assert path in paths, path

    def test_the_upgrade_job_runs_its_phases_in_order(self, upgrade_job):
        steps = upgrade_job["steps"]
        assert steps[0]["uses"] == "actions/checkout@v7"
        assert steps[0]["with"] == {"fetch-depth": 0}, (
            "N-1 is a tag, and the self-hoster's clone is made from this one"
        )
        phase = re.compile(r"bash scripts/compose_upgrade\.sh ([a-z-]+)")
        ran, conditional = [], {}
        for step in steps:
            found = phase.fullmatch(str(step.get("run", "")).strip())
            if not found:
                continue
            if "if" in step:
                conditional[found.group(1)] = step["if"]
            else:
                ran.append(found.group(1))
        assert ran == list(UPGRADE_PHASES)
        assert conditional == {"diagnose": "failure()", "teardown": "always()"}

    def test_the_upgrade_job_builds_what_the_other_job_builds(self, jobs, upgrade_job):
        """The same two core builds, cache and build arguments, under local
        names: the stack must pull N-1's images, so nothing may be tagged with
        the published names before the install."""

        def builds(job):
            return [
                step["with"]
                for step in job["steps"]
                if str(step.get("uses", "")).startswith("docker/build-push-action@")
            ]

        mine, theirs = builds(upgrade_job), builds(jobs["compose-production"])
        assert [b["tags"] for b in mine] == [
            "compose-upgrade-api:core",
            "compose-upgrade-web:core",
        ]
        assert [{**b, "tags": None} for b in mine] == [
            {**b, "tags": None} for b in theirs
        ]

    def test_the_script_names_the_page_the_builds_and_the_project(
        self, script, compose
    ):
        def value(name):
            found = re.search(rf"^{name}=(\S+)$", script, re.M)
            assert found, f"compose_upgrade.sh sets no {name}"
            return found.group(1)

        assert REPO_ROOT / value("GUIDE") == README
        assert value("MARKER") == "compose-upgrade"
        assert value("PROJECT") == compose["name"]
        assert value("PROFILE") == "core"
        assert value("BUILD_API") == "compose-upgrade-api:core"
        assert value("BUILD_WEB") == "compose-upgrade-web:core"

    def test_the_upgrade_job_runs_every_block_of_the_page(self, script):
        """Every bash block of the README is marked, in the script's list, and
        run by name: an unrun block is a command on the page nothing tests."""
        text = README.read_text(encoding="utf-8")
        listed = re.search(r"^GUIDE_BLOCKS=(\S+)$", script, re.M)
        assert listed, "compose_upgrade.sh sets no GUIDE_BLOCKS"
        blocks = listed.group(1).split(",")
        markers = re.findall(
            r"^<!-- compose-upgrade: ([a-z][a-z0-9-]*) -->$", text, re.M
        )
        assert markers == blocks
        assert len(re.findall(r"^```bash$", text, re.M)) == len(blocks)
        called = {m.group(1) for m in RUN_BLOCK_CALL.finditer(_code(script))}
        assert called == set(blocks)

    def test_every_block_runs_under_env_i_with_only_the_declared_variables(
        self, script
    ):
        """C6, SPEC, refused: a block run outside run_block, or a run_block
        that does not start `env -i`. The block sees HOME, PATH and the
        variables the page declares, never the job's environment."""
        body = _function(script, "run_block")
        runs = [line for line in body.splitlines() if '"$BLOCKS/$name.sh"' in line]
        runs = [line for line in runs if "bash" in line]
        assert len(runs) == 1, runs
        command = runs[0].split("&&", 1)[-1].strip()
        assert command.startswith(
            'env -i HOME="$READER_HOME" PATH="$PATH" "$@" bash -euo pipefail '
        ), f"run_block does not run the block under env -i: {runs[0]!r}"
        outside = _code(script.replace(body, ""))
        assert "bash -euo pipefail" not in outside, "a block run outside run_block"
        assert '"$BLOCKS/' not in outside.replace('--out "$BLOCKS"', ""), (
            "a block read outside run_block"
        )

        declared = re.search(r"^DECLARED='([^']*)'$", script, re.M)
        assert declared, "compose_upgrade.sh declares no DECLARED variables"
        assert set(declared.group(1).split()) == BLOCK_VARIABLES
        assert UPGRADE_VARIABLES <= BLOCK_VARIABLES
        for call in RUN_BLOCK_CALL.finditer(_code(script)):
            passed = set(re.findall(r" ([A-Za-z_][A-Za-z0-9_]*)=", call.group(2)))
            assert passed <= BLOCK_VARIABLES, call.group(0)

    def test_each_block_is_given_the_variables_it_reads(self, script):
        """Every call of a block passes each variable the block expands: under
        `bash -u` a block without one stops at its first line, and under
        `env -i` nothing else can supply it."""
        bodies = dict(
            re.findall(
                r"^<!-- compose-upgrade: ([a-z][a-z0-9-]*) -->\n```bash\n(.*?)^```$",
                README.read_text(encoding="utf-8"),
                re.M | re.S,
            )
        )
        calls = list(RUN_BLOCK_CALL.finditer(_code(script)))
        assert calls, "compose_upgrade.sh runs no block"
        for call in calls:
            passed = set(re.findall(r" ([A-Za-z_][A-Za-z0-9_]*)=", call.group(2)))
            reads = _expanded_variables(bodies[call.group(1)])
            assert reads <= passed, (
                f"{call.group(0)!r} does not pass {sorted(reads - passed)}, "
                "which the block reads"
            )

    def test_the_upgrade_starts_from_a_clone_at_the_previous_release(self, script):
        """C5(a): the ref get-files checks out is made after the clone, so only
        the page's `git fetch --tags` can bring it."""
        install = _function(script, "do_install")
        clone = install.index(
            'git clone --quiet --branch "v$prev" "$REPO_ROOT" "$SELFHOST"'
        )
        tag = install.index('git -C "$REPO_ROOT" tag -f "$CHANGE_TAG" HEAD')
        named = install.index("run_block install-named ")
        plain = install.index("run_block install ")
        assert clone < tag < named < plain
        assert "--pull never" not in script

    def test_it_rolls_back_in_place_before_it_restores(self, script):
        """PR 1's step 7: roll back in place first (steps 3 and 5 with the old
        tag); restore the backup only after that, into a stack with no volumes."""
        rollback = _function(script, "do_rollback")
        order = [
            rollback.index(text)
            for text in (
                'run_block get-files RELEASE="v$prev"',
                "run_block pull",
                "run_block upgrade",
                "dc down -v --remove-orphans",
                "run_block restore",
            )
        ]
        assert order == sorted(order)
        assert _code(script).count("down -v") == 2, (
            "the rollback's and the teardown's only"
        )

    def test_a_check_settings_refusal_is_reported_without_compose(self, script):
        """Compose loads the whole compose.yml for every subcommand, `ps`
        included, so after check-settings refuses the file a `dc` call fails
        before the refusal is reported. The refusal branch, and the
        api_image_id it calls, find the api container by the labels Compose
        put on it; the refusal is reported whether or not its image is read;
        diagnose finds the containers the same way, and teardown falls back to
        the labels when `down` fails."""
        branch = re.search(
            r"^    if ! run_block check-settings; then\n(.*?)^    fi$",
            _function(script, "do_prepare"),
            re.M | re.S,
        )
        assert branch, "do_prepare has no `if ! run_block check-settings` branch"
        refusal = _code(branch.group(1))
        lookup = _code(_function(script, "api_image_id"))
        for name, body in (
            ("the check-settings refusal branch", refusal),
            ("api_image_id", lookup),
            ("do_diagnose", _code(_function(script, "do_diagnose"))),
        ):
            calls = [line.strip() for line in body.splitlines() if DC_CALL.search(line)]
            assert not calls, f"{name} calls dc, which loads the refused file: {calls}"
        project = '"label=com.docker.compose.project=$PROJECT"'
        assert project in lookup
        assert "--filter label=com.docker.compose.service=api" in lookup
        assert "if still=$(api_image_id); then" in refusal, (
            "the refusal is reported only when the api's image can be read"
        )
        last = refusal.strip().splitlines()[-1].strip()
        assert last.startswith("fail \"the page's check-settings block refused "), last
        teardown = _code(_function(script, "do_teardown"))
        assert "if dc down -v --remove-orphans; then" in teardown
        assert teardown.count('--filter "$label"') == 4

    def test_restart_is_waited_for_by_the_bootstrap_line(self, script):
        """C5(b): `restart` returns before the entrypoint runs again; the job
        waits, bounded, for one more bootstrap line before it reads the log."""
        restart = _function(script, "do_restart")
        assert "dc restart api" in restart
        assert "RESTART_WAIT=300" in script
        assert restart.index("dc restart api") < restart.index("wait_for_bootstrap")

    def test_the_upgrade_job_is_one_step_of_the_core_profile_on_linux(
        self, script, upgrade_job
    ):
        """What the README says CI does not rehearse: the full profile and a
        jump over more than one release."""
        assert re.search(r"^PROFILE=core$", script, re.M)
        assert upgrade_job["runs-on"] == "ubuntu-latest"
        for step in upgrade_job["steps"]:
            target = (step.get("with") or {}).get("target")
            assert target in (None, "core"), step.get("name")


def _backup_section() -> str:
    """The README's "Back up and restore" section, up to the next `## `."""
    text = README.read_text(encoding="utf-8")
    start = text.index("\n## Back up and restore\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _backup_commands() -> list:
    """Every command in the section's bash blocks, continuation lines joined."""
    blocks = re.findall(r"```bash\n(.*?)```", _backup_section(), re.S)
    return [
        line.strip()
        for block in blocks
        for line in block.replace("\\\n", " ").splitlines()
        if line.strip()
    ]


class TestBackupAndRestore:
    """The commands in the README's backup and restore section (#1500).

    They run `docker compose ... exec` against the stack's own service, with
    the project named, so each name they use is held to compose.yml here: the
    project, the service, the database user and database. A rename that left
    the README behind would back up nothing, or the wrong stack, and the
    commands would still look right.
    """

    def _compose_commands(self) -> list:
        return [c for c in _backup_commands() if c.startswith("docker compose")]

    def test_the_section_has_the_four_compose_commands(self):
        assert len(self._compose_commands()) == 4

    def test_every_compose_command_names_the_stacks_project(self, compose):
        for command in self._compose_commands():
            assert command.startswith(f"docker compose -p {compose['name']} "), command

    def test_the_project_in_the_prose_is_the_composes(self, compose):
        section = _backup_section().replace("`", "")
        assert f"the Compose project, {compose['name']}, which is the name: in" in (
            " ".join(section.split())
        )
        assert f"docker compose -p {compose['name']} stop" in section

    def test_exec_runs_in_the_postgres_service_without_a_terminal(self, services):
        execs = [c for c in self._compose_commands() if " exec " in c]
        assert len(execs) == 2
        for command in execs:
            assert " exec -T postgres " in command, command
        assert "postgres" in services

    def test_the_tools_read_the_database_user_and_name_the_service_has(self, services):
        environment = services["postgres"]["environment"]
        assert set(environment) >= {"POSTGRES_USER", "POSTGRES_DB"}
        for tool in ("pg_dump", "pg_restore"):
            (command,) = [c for c in self._compose_commands() if tool in c]
            assert '-U "$POSTGRES_USER" -d "$POSTGRES_DB"' in command
        # The names the prose gives for those defaults.
        assert environment["POSTGRES_USER"] == DATABASE_USER_BEFORE_THE_RENAME
        assert environment["POSTGRES_DB"] == "${POSTGRES_DB:-experimentation}"
        assert "(the defaults in compose.yml, unless" in " ".join(
            _backup_section().replace("`", "").split()
        )

    def test_the_restore_warns_before_the_block_about_a_running_stack(self):
        section = " ".join(_backup_section().replace("`", "").split())
        warning = (
            "Do not run the commands below in the directory of a stack that is running"
        )
        assert warning in section
        assert section.index(warning) < section.index(
            "cp ~/controlvar-backups/controlvar.env"
        )
        # The two reasons it gives are the two commands' effects.
        assert "the first replaces its .env" in section
        assert "whatever database its postgres service holds" in section

    def test_the_dump_is_custom_format_and_the_restore_reads_it(self):
        (dump,) = [c for c in self._compose_commands() if "pg_dump" in c]
        (restore,) = [c for c in self._compose_commands() if "pg_restore" in c]
        assert "--format=custom" in dump
        assert dump.rstrip().endswith("controlvar.dump")
        assert "> ~/controlvar-backups/controlvar.dump" in dump
        assert "< ~/controlvar-backups/controlvar.dump" in restore

    def test_postgres_alone_is_started_first_and_nothing_waits_on_it(
        self, compose, services
    ):
        assert (
            f"docker compose -p {compose['name']} up -d --wait postgres"
            in self._compose_commands()
        )
        assert "depends_on" not in services["postgres"]
        assert "depends_on" not in services["api"]

    def test_the_api_creates_the_schema_when_it_starts(self, services):
        assert services["api"]["environment"]["RUN_MIGRATIONS"] == "true"

    def test_the_env_file_is_restored_beside_compose_yml(self):
        assert "cp ~/controlvar-backups/controlvar.env .env" in _backup_commands()
        assert "cp .env ~/controlvar-backups/controlvar.env" in _backup_commands()

    def test_nothing_in_the_section_deletes_a_volume(self):
        section = _backup_section()
        forbidden = (
            r"\bdown\b",
            r"(^|\s)-v\b",
            r"--volumes",
            r"\bvolume\s+(rm|prune)\b",
            r"\bprune\b",
            r"\brm\b",
            r"\bkill\b",
        )
        for pattern in forbidden:
            assert not re.search(pattern, section), pattern

    def test_the_volumes_take_their_names_from_the_project(self, compose):
        """So a different `-p` starts with empty volumes, and keeps the old ones."""
        for key, spec in compose["volumes"].items():
            assert not (isinstance(spec, dict) and spec.get("name")), key

    def test_redis_is_optional_and_persists(self, services):
        environment = services["api"]["environment"]
        assert environment["REDIS_REQUIRED"] == "false"
        command = services["redis"]["command"]
        assert command[command.index("--appendonly") + 1] == "yes"
        assert services["redis"]["volumes"] == ["redis_data:/data"]

    @pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
    def test_the_generated_env_says_its_secrets_cannot_be_regenerated(self, tmp_path):
        """The README sends the reader to this note, and says a second run makes
        different keys: both are read from the file the generator writes."""
        generator = TestGenerator()
        files = [tmp_path / "one.env", tmp_path / "two.env"]
        for file in files:
            result = generator._run(file)
            assert result.returncode == 0, result.stderr
        first, second = (f.read_text(encoding="utf-8") for f in files)
        lines = first.splitlines()
        assert lines[0].startswith("# Written by generate-env.sh.")
        assert "the secrets below cannot be regenerated" in " ".join(
            line.lstrip("# ") for line in lines[:2]
        )
        for name in ("SECRET_KEY", "AUDIT_HMAC_KEY"):
            (one,) = [x for x in first.splitlines() if x.startswith(f"{name}=")]
            (two,) = [x for x in second.splitlines() if x.startswith(f"{name}=")]
            assert one != two, name


#: The README's upgrade procedure (#1807): each block exactly as the page gives
#: it, in page order. A self-hoster runs these on the stack that holds their
#: data, so a changed word in one is a reviewed change to what they run.
UPGRADE_BLOCKS = (
    (
        "check-backup",
        "docker compose -p experimently-selfhost exec -T postgres \\\n"
        "  pg_restore -f /dev/null < ~/controlvar-backups/controlvar.dump\n",
    ),
    ("get-files", 'git fetch --tags\ngit checkout "$RELEASE"\n'),
    ("check-settings", "docker compose -p experimently-selfhost config --quiet\n"),
    ("pull", "docker compose -p experimently-selfhost pull\n"),
    (
        "upgrade",
        "docker compose -p experimently-selfhost up -d --wait --wait-timeout 600\n",
    ),
    (
        "verify",
        "docker compose -p experimently-selfhost ps\n"
        'curl -fsS "$PUBLIC_BASE_URL/api/v1/modules"\n',
    ),
    (
        "check-migrations",
        "docker compose -p experimently-selfhost logs api 2>&1 \\\n"
        "  | grep -e 'running database bootstrap' -e 'Running upgrade'\n",
    ),
)

#: The shell variables the section's table asks the reader to set, and the only
#: ones its blocks may read.
UPGRADE_VARIABLES = {"RELEASE", "PUBLIC_BASE_URL"}

#: What deletes a volume (TestBackupAndRestore's list), and the generator, which
#: an upgrade never runs: the install keeps the .env it has.
UPGRADE_FORBIDDEN = (
    r"\bdown\b",
    r"(^|\s)-v\b",
    r"--volumes",
    r"\bvolume\s+(rm|prune)\b",
    r"\bprune\b",
    r"\brm\b",
    r"\bkill\b",
    r"generate-env",
)


def _upgrade_section() -> str:
    """The README's "Upgrade" section, up to the next `## `."""
    text = README.read_text(encoding="utf-8")
    assert "\n## Upgrade\n" in text, "the README has no `## Upgrade` section"
    start = text.index("\n## Upgrade\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _upgrade_step(number: int) -> str:
    """Step *number* of the Upgrade section, from its `### ` heading to the next."""
    section = _upgrade_section()
    found = re.search(rf"^### {number}\. .*?(?=^### |\Z)", section, re.M | re.S)
    assert found, f"the Upgrade section has no step {number}"
    return found.group(0)


def _prose(text: str) -> str:
    """*text* with its fences removed and its lines joined by single spaces."""
    unfenced = re.sub(r"^```.*?^```$", "", text, flags=re.M | re.S)
    return " ".join(unfenced.split())


def _refusal_bootstrap_prints() -> str:
    """The literal text of the refusal an older image gives a database a newer
    release migrated: the last `return` of `unresolvable_revisions_message`,
    read from bootstrap.py's syntax tree, each `{...}` field as `{}`."""
    source = (REPO_ROOT / "backend" / "app" / "db" / "bootstrap.py").read_text(
        encoding="utf-8"
    )
    (function,) = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "unresolvable_revisions_message"
    ]
    message = function.body[-1]
    assert isinstance(message, ast.Return), ast.dump(message)
    assert isinstance(message.value, ast.JoinedStr), ast.dump(message.value)
    return "".join(
        part.value if isinstance(part, ast.Constant) else "{}"
        for part in message.value.values
    )


def _upgrade_fences() -> list:
    """Every fence in the section, as (info string, body): all of them, so a
    block in another language, or with a tag, is seen rather than skipped."""
    fences, info, body = [], None, []
    for line in _upgrade_section().splitlines():
        opens = line.lstrip().startswith(("```", "~~~"))
        if info is None:
            if opens:
                info, body = line.lstrip()[3:].strip(), []
        elif opens:
            fences.append((info, "".join(f"{text}\n" for text in body)))
            info = None
        else:
            body.append(line)
    assert info is None, "an unclosed fence in the Upgrade section"
    return fences


def _declared_variables() -> list:
    """The first cell of each row of the section's `| Variable | Meaning |` table."""
    lines = _upgrade_section().splitlines()
    assert "| Variable | Meaning |" in lines, "the section has no variables table"
    start = lines.index("| Variable | Meaning |")
    assert lines[start + 1] == "|---|---|", lines[start + 1]
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line.split("|")[1].strip().strip("`"))
    return rows


def _expanded_variables(block: str) -> set:
    """The names a block expands, `$NAME` or `${NAME}`, outside single quotes."""
    unquoted = re.sub(r"'[^']*'", "", block)
    return set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", unquoted))


class TestUpgrade:
    """The README's upgrade procedure (#1807), read as text, with no Docker.

    A self-hoster runs its blocks in order on the stack that holds their data:
    check the backup, get the release's files, check the settings, pull,
    start, verify, and roll back in place, restoring the backup only when the
    old release refuses the database. So each block's
    text is pinned, and the properties that fail silently are held by name:
    every compose command names the stack's project, none deletes a volume or
    runs the generator, the wait is as long as the API's health check allows,
    and the blocks read only the variables the section tells the reader to set.
    """

    def _blocks(self) -> list:
        return [body for _, body in _upgrade_fences()]

    def _commands(self) -> list:
        """Every command line of the blocks, continuation lines joined."""
        return [
            line.strip()
            for block in self._blocks()
            for line in block.replace("\\\n", " ").splitlines()
            if line.strip()
        ]

    def test_the_blocks_are_the_procedure_in_order(self):
        blocks = self._blocks()
        names = [name for name, _ in UPGRADE_BLOCKS]
        assert len(blocks) == len(UPGRADE_BLOCKS), (
            f"{len(blocks)} blocks in Upgrade, expected {len(names)}: {names}"
        )
        for (name, expected), block in zip(UPGRADE_BLOCKS, blocks):
            for want, got in zip_longest(expected.splitlines(), block.splitlines()):
                assert got == want, f"{name}: {got!r}, expected {want!r}"

    def test_the_backup_is_linked_not_copied(self):
        assert "[Back up](#back-up)" in _upgrade_section()
        for block in self._blocks():
            assert "pg_dump" not in block, (
                f"a second copy of the backup command in Upgrade: {block!r}; "
                "link to Back up instead"
            )

    def test_every_compose_command_names_the_stacks_project(self, compose):
        commands = [c for c in self._commands() if "docker compose" in c]
        assert len(commands) == 6, commands
        for command in commands:
            for found in re.finditer(r"docker compose(?: -p (\S+))?", command):
                assert found.group(1) == compose["name"], command

    def test_no_block_deletes_a_volume_or_runs_the_generator(self):
        """The block bodies only: the prose may name what they never run."""
        for block in self._blocks():
            for pattern in UPGRADE_FORBIDDEN:
                assert not re.search(pattern, block, re.M), f"{pattern} in {block!r}"

    def test_the_wait_is_as_long_as_the_api_health_check(self, services):
        """`--wait-timeout` is about the time the API's health check gives a
        start, a migration included, before it reports the container
        unhealthy: the start period, then that many failed checks."""
        check = services["api"]["healthcheck"]

        def seconds(value) -> int:
            assert re.fullmatch(r"\d+s", str(value)), value
            return int(str(value)[:-1])

        window = seconds(check["start_period"]) + check["retries"] * seconds(
            check["interval"]
        )
        (upgrade,) = [block for block in self._blocks() if " up -d" in block]
        found = re.search(r" up -d --wait --wait-timeout (\d+)$", upgrade, re.M)
        assert found, f"the upgrade block has no --wait-timeout: {upgrade!r}"
        assert int(found.group(1)) == window, (
            f"--wait-timeout {found.group(1)}, but the api health check gives "
            f"{check['start_period']} + {check['retries']} x {check['interval']} "
            f"= {window} s"
        )

    def test_the_blocks_read_only_the_variables_the_section_declares(self):
        declared = set(_declared_variables())
        for name in sorted(UPGRADE_VARIABLES - declared):
            pytest.fail(f"the variables table does not declare {name}")
        for name in sorted(declared - UPGRADE_VARIABLES):
            pytest.fail(f"the variables table declares {name}, which is not pinned")
        read = set()
        for block in self._blocks():
            for name in sorted(_expanded_variables(block) - declared):
                pytest.fail(
                    f"the block starting {block.splitlines()[0]!r} reads ${name}, "
                    "which the section does not tell the reader to set"
                )
            read |= _expanded_variables(block)
        assert read == declared, f"declared, read by no block: {declared - read}"

    def test_no_block_carries_a_comment_or_a_tag(self):
        for info, body in _upgrade_fences():
            assert info == "bash", f"a fence opened as {info!r}: plain bash only"
            for line in body.splitlines():
                assert not re.search(r"(^|\s)#", line), f"a comment: {line!r}"

    def test_the_log_check_reads_the_line_every_start_writes(self, services):
        """The API runs the bootstrap on every start, and the entrypoint logs
        `running database bootstrap` to stderr first: the block reads both
        streams and finds each start by that line."""
        entrypoint = (REPO_ROOT / "backend" / "docker-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        assert "log() { printf '[entrypoint] %s\\n' \"$*\" >&2; }" in entrypoint
        assert (
            'if [ "$RUN_MIGRATIONS" = "true" ]; then\n'
            '    log "running database bootstrap (' in entrypoint
        )
        assert services["api"]["environment"]["RUN_MIGRATIONS"] == "true"
        (logs,) = [block for block in self._blocks() if " logs api " in block]
        assert " logs api 2>&1 " in logs, logs
        assert "-e 'running database bootstrap'" in logs, logs

    def test_the_rollback_restores_only_on_the_refusal_bootstrap_prints(self):
        """Step 7 rolls back in place whatever the log showed, and restores
        only when the old release then refuses the database, by the sentence
        bootstrap.py prints. A `Running upgrade` line does not decide it: a
        migration that fails is undone, and the old release starts as before,
        so a restore there would lose what was written since the backup."""
        refusal = "this database was migrated by a newer Controlvar release"
        assert refusal in _refusal_bootstrap_prints()
        rollback = _prose(_upgrade_step(7))
        named = f"shows `{refusal}`"
        assert named in rollback, f"step 7 does not name the refusal {refusal!r}"
        in_place = "whatever the log showed, roll back in place"
        assert in_place in rollback, rollback
        restore = "[Restore](#restore)"
        assert rollback.count(restore) == 1, f"step 7 links Restore twice: {rollback}"
        order = [rollback.index(text) for text in (in_place, named, restore)]
        assert order == sorted(order), (
            "step 7 must roll back in place, then name the refusal, then restore"
        )

    def test_after_a_slow_migration_a_second_up_starts_the_dashboard(self, services):
        """The dashboard waits for a healthy API, so an `up` that gives up
        while the migration still runs never starts it, and nothing starts it
        later: step 5 says to run `up` again once the API is healthy."""
        assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"
        upgrade = _prose(_upgrade_step(5))
        assert "then run the `up` command again" in upgrade, upgrade
        assert "starts the dashboard" in upgrade, upgrade

    def test_the_verify_step_counts_every_service(self, services):
        """`ps` lists only the containers that run, so a dashboard that never
        started is missing rather than unhealthy: step 6 says how many to look
        for, and each service has a health check, so each can show `healthy`."""
        words = ("no", "one", "two", "three", "four", "five", "six")
        assert len(services) < len(words), sorted(services)
        verify = _prose(_upgrade_step(6))
        counted = f"each of the {words[len(services)]} services is listed and `healthy`"
        assert counted in verify, f"step 6 does not say {counted!r}: {verify}"
        for name, service in services.items():
            assert "healthcheck" in service, f"{name} has no health check"

    def test_the_release_to_roll_back_to_is_noted_before_the_files_change(self):
        """After the checkout, the clone names the new release, and an install
        cloned from main may never have been on a tag: step 3 has the reader
        note the running release before its block, and step 7 goes back to it."""
        get_files = _upgrade_step(3)
        assert "```bash" in get_files, get_files
        before = _prose(get_files[: get_files.index("```bash")])
        assert "note the release you run now" in before, before
        assert "the `version` that step 6's `curl` prints, with a `v`" in before, before
        rollback = _prose(_upgrade_step(7))
        assert "go back to the release you noted in step 3" in rollback, rollback
        assert "`RELEASE` set to the release you noted" in rollback, rollback

    def test_the_env_file_is_not_in_git(self):
        """So checking out a release leaves the install's .env as it was."""
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".env" in ignored
        unignored = [line for line in ignored if line.startswith("!")]
        assert not [line for line in unignored if line.endswith(".env")], unignored

    def test_every_link_within_the_page_names_a_heading(self):
        """GitHub's anchors: the heading in lower case, punctuation dropped,
        spaces as hyphens. A step renamed would leave its links pointing at
        nothing; the site does not build this page, so mkdocs never sees them."""
        text = README.read_text(encoding="utf-8")
        anchors = {
            re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
            for heading in re.findall(r"^#{1,6} (.+)$", text, re.M)
        }
        links = re.findall(r"\]\(#([^)]+)\)", text)
        assert links, "the README links to none of its headings"
        for link in links:
            assert link in anchors, f"#{link} names no heading of the README"
