"""The database settings the two compose files give the API, and their Postgres.

Read from the files compose reads: ``docker-compose.yml`` (the Quick Start
stack) and ``deploy/compose/compose.yml`` (the production compose file). A
value written ``${NAME:-default}`` is taken at its default, which is what a
stack started with nothing exported gets; ``${NAME:?message}`` has none and
compose refuses to start without it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
QUICK_START = REPO_ROOT / "docker-compose.yml"
PRODUCTION = REPO_ROOT / "deploy" / "compose" / "compose.yml"
CHART = REPO_ROOT / "charts" / "experimently"

_DEFAULT = re.compile(r"^\$\{(?P<name>[A-Z_]+)(?P<op>:-|-|:\?)(?P<rest>.*)\}$")


def _services(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["services"]


def _value(raw: object) -> tuple[str, str]:
    """(how, value): ``literal`` and the text, ``default`` and the default, or
    ``required`` and the message."""
    text = str(raw)
    match = _DEFAULT.match(text)
    if not match:
        return "literal", text
    if match.group("op") == ":?":
        return "required", match.group("rest")
    return "default", match.group("rest")


def _api_settings(path: Path) -> dict[str, tuple[str, str]]:
    return {k: _value(v) for k, v in _services(path)["api"]["environment"].items()}


@pytest.mark.regression
def test_the_quick_start_api_reaches_the_compose_postgres_with_these_defaults():
    services = _services(QUICK_START)
    api = _api_settings(QUICK_START)
    assert api["POSTGRES_SERVER"] == ("literal", "postgres")
    assert "postgres" in services, "POSTGRES_SERVER names no compose service"
    assert api["POSTGRES_PORT"] == ("literal", "5432")
    assert api["POSTGRES_USER"] == ("default", "postgres")
    assert api["POSTGRES_PASSWORD"] == ("default", "postgres")
    assert api["POSTGRES_DB"] == ("default", "experimentation")
    assert api["POSTGRES_SCHEMA"] == ("default", "experimentation")
    database = {k: _value(v) for k, v in services["postgres"]["environment"].items()}
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert database[name] == api[name], name
    # From the host it is localhost: the port is published on the loopback.
    (published,) = services["postgres"]["ports"]
    assert re.fullmatch(r"127\.0\.0\.1:\$\{POSTGRES_HOST_PORT:-5432\}:5432", published)


@pytest.mark.regression
def test_the_production_compose_file_names_its_user_and_requires_a_password():
    services = _services(PRODUCTION)
    api = _api_settings(PRODUCTION)
    database = {k: _value(v) for k, v in services["postgres"]["environment"].items()}
    assert (
        api["POSTGRES_USER"] == database["POSTGRES_USER"] == ("default", "experimently")
    )
    for settings in (api, database):
        how, _ = settings["POSTGRES_PASSWORD"]
        assert how == "required", settings["POSTGRES_PASSWORD"]


@pytest.mark.regression
def test_compose_sets_environment_to_development_unless_it_was_set():
    assert _api_settings(QUICK_START)["ENVIRONMENT"] == ("default", "development")


@pytest.mark.regression
@pytest.mark.parametrize(
    "path, expected",
    [(QUICK_START, ("default", "true")), (PRODUCTION, ("literal", "true"))],
    ids=["quick-start", "production"],
)
def test_compose_runs_one_api_container_and_keeps_run_migrations_true(path, expected):
    services = _services(path)
    api = services["api"]
    assert _value(api["environment"]["RUN_MIGRATIONS"]) == expected
    assert "replicas" not in (api.get("deploy") or {}), api.get("deploy")
    assert "scale" not in api
    image = str(api["image"]).split(":")[0]
    same_image = sorted(
        name
        for name, s in services.items()
        if str(s.get("image", "")).split(":")[0] == image
    )
    assert same_image == ["api"], same_image
    assert not [
        name
        for name, s in services.items()
        if name != "api"
        and "backend.app.db.bootstrap"
        in str(s.get("command", "")) + str(s.get("entrypoint", ""))
    ]


#: Ways a Postgres container's time zone is set other than the image default.
_TIME_ZONE = re.compile(r"\b(?:TZ|PGTZ)\b|timezone\s*=|-c\s*timezone", re.IGNORECASE)


@pytest.mark.regression
def test_the_bundled_postgres_keeps_the_official_images_utc_default():
    """The official image's server TimeZone is UTC; none of the three bundled
    Postgres containers sets another."""
    for path in (QUICK_START, PRODUCTION):
        postgres = _services(path)["postgres"]
        assert postgres["image"] == "postgres:16-alpine", path
        assert not _TIME_ZONE.search(yaml.safe_dump(postgres)), path
    if not CHART.is_dir():
        pytest.skip("this tree has no chart")
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["postgresql"]["image"] == "postgres:16-alpine"
    template = (CHART / "templates" / "postgres.yaml").read_text(encoding="utf-8")
    assert "image: {{ .Values.postgresql.image }}" in template
    assert not _TIME_ZONE.search(template)
