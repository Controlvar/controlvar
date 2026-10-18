"""The development stack's services, ports, profiles and settings (#1142).

``docker-compose.yml`` at the repository root is the development stack. Each
service's image or build, its default host port and its profile are pinned
here from the file itself, with the settings the stack passes to the API and
the ones it leaves out. Read as YAML, with no Docker, so this runs in the unit
job on every pull request; ``test_dev_stack_loopback.py`` holds the
``127.0.0.1`` binding of the same ports.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"

#: Each service: (its image, or the Dockerfile it is built from), its default
#: host port (None: it publishes none), its profile (None: started by default).
SERVICES: Dict[str, Tuple[str, Optional[int], Optional[str]]] = {
    "postgres": ("postgres:16-alpine", 5432, None),
    "redis": ("redis:7-alpine", 6379, None),
    "api": ("backend/Dockerfile", 8000, None),
    "frontend": ("frontend/Dockerfile", 3000, None),
    "shoplab": ("demo/shoplab/Dockerfile", 3200, "demo"),
    "shoplab-simulator": ("python:3.11-slim", None, "demo"),
    "streampulse": ("demo/streampulse/Dockerfile", 3300, "demo"),
    "streampulse-simulator": ("python:3.11-slim", None, "demo"),
    "pgadmin": ("dpage/pgadmin4", 5050, "tools"),
    "localstack": ("localstack/localstack:3", 4566, "aws"),
}

#: A published port as the file writes it: 127.0.0.1:${NAME_HOST_PORT:-N}:C.
_PORT = re.compile(
    r"^127\.0\.0\.1:\$\{(?P<name>[A-Z_]+_HOST_PORT):-(?P<port>\d+)\}:\d+$"
)


@pytest.fixture(scope="module")
def services() -> Dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def image_of(service: Dict[str, Any]) -> str:
    build = service.get("build")
    if build:
        return build["dockerfile"]
    return service["image"].split(":latest")[0]


def host_port(service: Dict[str, Any]) -> Optional[int]:
    ports = service.get("ports") or []
    assert len(ports) <= 1, ports
    if not ports:
        return None
    match = _PORT.fullmatch(ports[0])
    assert match, f"{ports[0]!r} is not 127.0.0.1:${{NAME_HOST_PORT:-N}}:C"
    return int(match.group("port"))


def profile_of(service: Dict[str, Any]) -> Optional[str]:
    profiles = service.get("profiles")
    if profiles is None:
        return None
    assert len(profiles) == 1, profiles
    return profiles[0]


def test_the_file_has_exactly_these_services(services):
    assert set(services) == set(SERVICES)


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_each_service_has_its_image_host_port_and_profile(services, name):
    image, port, profile = SERVICES[name]
    service = services[name]
    assert image_of(service) == image
    assert host_port(service) == port
    assert profile_of(service) == profile


def test_the_profile_picks_the_api_build_target(services):
    """`EXPERIMENTLY_PROFILE=full` builds the full target; the image is tagged core
    by default."""
    api = services["api"]
    assert api["build"]["target"] == "${EXPERIMENTLY_PROFILE:-core}"
    assert (
        api["image"]
        == "experimently-api:${EXPERIMENTLY_IMAGE_TAG:-${EXPERIMENTLY_PROFILE:-core}}"
    )
    frontend = services["frontend"]
    assert (
        frontend["build"]["args"]["EXPERIMENTLY_PROFILE"]
        == "${EXPERIMENTLY_PROFILE:-core}"
    )
    assert (
        frontend["image"]
        == "experimently-web:${EXPERIMENTLY_IMAGE_TAG:-${EXPERIMENTLY_PROFILE:-core}}"
    )


def test_the_dashboard_proxies_the_api(services):
    assert services["frontend"]["environment"]["API_UPSTREAM"] == "http://api:8000"


def test_the_database_is_the_postgres_data_volume(services):
    """`docker compose down -v` removes the named volumes, and with them the database."""
    assert "postgres_data:/var/lib/postgresql/data" in services["postgres"]["volumes"]
    top = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["volumes"]
    assert "postgres_data" in top


def test_the_api_and_the_dashboard_run_built_images_with_no_source_mounted(services):
    """Nothing reloads an edit: the code is in the image, and the server runs
    without --reload."""
    for name in ("api", "frontend"):
        service = services[name]
        assert service.get("build"), name
        mounts = [str(m) for m in service.get("volumes") or []]
        assert not [m for m in mounts if m.startswith((".", "/"))], (name, mounts)
    assert "--reload" not in ENTRYPOINT.read_text(encoding="utf-8")


def test_redis_has_no_password(services):
    redis = services["redis"]
    assert "requirepass" not in str(redis.get("command") or "")
    assert "REDIS_PASSWORD" not in services["api"]["environment"]


def test_the_host_ports_can_be_moved(services):
    names = {
        name: _PORT.fullmatch(services[name]["ports"][0]).group("name")
        for name in ("postgres", "redis", "api", "frontend")
    }
    assert names == {
        "postgres": "POSTGRES_HOST_PORT",
        "redis": "REDIS_HOST_PORT",
        "api": "API_HOST_PORT",
        "frontend": "FRONTEND_HOST_PORT",
    }


def test_the_api_reads_the_settings_the_page_names(services):
    environment = services["api"]["environment"]
    for name in (
        "SEED",
        "LOG_FORMAT",
        "METRICS_TOKEN",
        "WEB_CONCURRENCY",
        "AUTH_PROVIDER",
    ):
        assert name in environment, name
    assert environment["AUTH_PROVIDER"] == "${AUTH_PROVIDER:-local}"
    assert environment["DEV_AUTH_BYPASS"] == "${DEV_AUTH_BYPASS:-false}"
    assert environment["SEED"] == "${SEED-demo}"


def test_the_api_is_not_given_cognito_or_aws_keys(services):
    """AUTH_PROVIDER=cognito needs them added to the api service by hand."""
    environment = services["api"]["environment"]
    passed = [
        name
        for name in environment
        if name.startswith("COGNITO_")
        or name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    ]
    assert passed == []


@pytest.mark.parametrize("app", ["shoplab", "streampulse"])
def test_a_demo_simulator_exits_without_its_seed(services, app):
    """The default SEED is `demo` alone, and a simulator stops at start when
    the app's seed has not written its API key."""
    command = " ".join(services[f"{app}-simulator"]["command"])
    assert f"test -f /data/demo/{app}/.api_key" in command
    assert re.search(r"\|\| \{ [^}]*; exit 1; \}", command), command
    assert services["api"]["environment"]["SEED"] == "${SEED-demo}"


@pytest.mark.parametrize("app", ["shoplab", "streampulse"])
def test_a_demo_app_is_built_with_the_key_its_seed_creates(services, app):
    """The app's image and the api's seed take one key, from one variable.

    So running the seed after the app was built (the demo command on a stack
    that started without it) makes the key in the image valid, with no rebuild.
    """
    variable = f"{app.upper()}_API_KEY"
    baked = services[app]["build"]["args"]["NEXT_PUBLIC_CONTROLVAR_API_KEY"]
    assert baked.startswith("${" + variable + ":-eptk_demo_"), baked
    assert services["api"]["environment"][variable] == baked
    seed = (REPO_ROOT / "backend" / "scripts" / f"seed_{app}.py").read_text(
        encoding="utf-8"
    )
    assert f'os.environ.get("{variable}")' in seed


def test_the_profiles_add_what_the_guide_says(services):
    by_profile: Dict[Optional[str], set] = {}
    for name, service in services.items():
        by_profile.setdefault(profile_of(service), set()).add(name)
    assert by_profile == {
        None: {"postgres", "redis", "api", "frontend"},
        "tools": {"pgadmin"},
        "aws": {"localstack"},
        "demo": {
            "shoplab",
            "shoplab-simulator",
            "streampulse",
            "streampulse-simulator",
        },
    }


def test_the_project_name_follows_the_checkout_directory():
    """No `name:`: compose takes the project, and so the volume names, from the
    directory of the checkout. The upgrade note says what that means for a clone
    made under the new name."""
    assert "name" not in yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
