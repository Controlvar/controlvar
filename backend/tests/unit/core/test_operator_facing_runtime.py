"""What the API does that an operator relies on during an incident (#1142,
D73 TR4).

* ``/health`` runs ``SELECT 1`` and nothing else, so it answers 200 on a
  database that has no schema yet (and says nothing about which cluster it
  reached), while a request that needs a table answers 500 and logs an error.
* The application pool tests each connection before use.
* In staging and production every log line is one JSON object with a
  lower-case ``level``; dev and demo write console lines. Each runs the API's
  own task environment, read from the CDK stack, in a clean subprocess.
* Redis holds only cache and rate-limit state: the code that talks to it is a
  closed list, each entry classified.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from backend.app.api import deps
from backend.app.core import health

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
MISSING = 'relation "experimentation.experiments" does not exist'


class SchemaLessSession:
    """A session on a database with no schema: ``SELECT 1`` answers, anything
    else raises what PostgreSQL raises for a missing relation."""

    def __init__(self, seen: list[str]):
        self.seen = seen

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.seen.append(sql)
        if sql.strip().upper() == "SELECT 1":
            return SimpleNamespace(scalar=lambda: 1)
        raise ProgrammingError(sql, {}, Exception(MISSING))

    def query(self, *args, **kwargs):
        self.seen.append(f"query {args!r}")
        raise ProgrammingError("SELECT ...", {}, Exception(MISSING))

    def close(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def seen(monkeypatch) -> list[str]:
    statements: list[str] = []
    import backend.app.db.session as session

    monkeypatch.setattr(session, "SessionLocal", lambda: SchemaLessSession(statements))
    return statements


def test_the_database_check_runs_select_1_and_nothing_else(seen):
    result = health.check_database()
    assert result["status"] == "healthy", result
    assert seen == ["SELECT 1"]


def test_health_answers_200_on_a_database_without_the_schema(seen, monkeypatch):
    monkeypatch.setattr(health, "check_redis", lambda: {"status": "healthy"})
    monkeypatch.setattr(health, "check_disk", lambda path="/": {"status": "healthy"})
    from backend.app.main import app

    response = TestClient(app, raise_server_exceptions=False).get("/health")
    assert response.status_code == 200, response.text
    assert response.json()["checks"]["database"]["status"] == "healthy"
    assert seen == ["SELECT 1"]


def test_a_request_that_needs_a_table_answers_500_and_logs_an_error(caplog):
    from backend.app.main import app

    seen: list[str] = []

    def schema_less_db():
        yield SchemaLessSession(seen)

    user = SimpleNamespace(
        id=1, is_active=True, is_superuser=True, role="admin", email="a@example.com"
    )
    app.dependency_overrides[deps.get_db] = schema_less_db
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_cache_control] = lambda: SimpleNamespace(
        enabled=False, redis=None
    )
    try:
        with caplog.at_level("ERROR"):
            response = TestClient(app, raise_server_exceptions=False).get(
                "/api/v1/experiments/"
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500, response.text
    assert seen, "the route never reached the database"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("ProgrammingError" in r.getMessage() for r in errors), [
        r.getMessage() for r in errors
    ]


def test_the_application_pool_tests_each_connection_before_use():
    from backend.app.db.session import engine

    assert engine.pool._pre_ping is True


# --- the log format per environment ------------------------------------------------------


_PROBE = """
import logging
import backend.app.main  # noqa: F401
logging.getLogger("probe").error("probe error line")
logging.getLogger("probe").critical("probe critical line")
"""


def _probe_lines(app_env: str) -> list[str]:
    from backend.tests.unit.infrastructure.test_deployment_secrets import (
        STACKS,
        _run,
        container_environment,
    )

    # The image's profile: full when this tree has modules/, core otherwise.
    profile = "full" if (REPO_ROOT / "modules" / "backend").is_dir() else "core"
    environment = container_environment(STACKS["fargate"], profile=profile)
    environment["APP_ENV"] = app_env
    result = _run(environment, _PROBE)
    assert result.returncode == 0, result.stderr[-2000:]
    return [line for line in result.stdout.splitlines() if "probe " in line]


@pytest.mark.parametrize("app_env", ["staging", "prod"])
def test_staging_and_prod_write_each_line_as_json_with_a_lower_case_level(app_env):
    lines = _probe_lines(app_env)
    assert len(lines) == 2, lines
    records = [json.loads(line) for line in lines]
    assert [r["level"] for r in records] == ["error", "critical"]
    assert [r["event"] for r in records] == ["probe error line", "probe critical line"]


@pytest.mark.parametrize("app_env", ["dev", "demo"])
def test_dev_and_demo_write_console_lines(app_env):
    lines = _probe_lines(app_env)
    assert len(lines) == 2, lines
    for line in lines:
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)


def test_the_stack_sets_no_log_format_so_the_environment_decides():
    from backend.tests.unit.infrastructure.test_deployment_secrets import (
        STACKS,
        container_environment,
    )

    for name in STACKS:
        assert "LOG_FORMAT" not in container_environment(STACKS[name], profile="full")


# --- what Redis holds ----------------------------------------------------------------------

#: Every application file that talks to Redis, and what it keeps there. A new
#: one fails until it is classified here: cache (derived, rebuilt from the
#: database), rate limit (per-IP counters), health (a PING), or the client
#: factory and the settings it reads.
REDIS_USERS = {
    "backend/app/core/redis_client.py": "the client factory",
    "backend/app/core/config.py": "settings",
    "backend/app/core/health.py": "health: PING",
    "backend/app/middleware/rate_limiter.py": "rate limit counters",
    "backend/app/api/deps.py": "cache: the cache_control dependency",
    "backend/app/api/experiment_cache.py": "cache: dropping an experiment's detail",
    "backend/app/api/v1/endpoints/experiments.py": "cache: experiment reads",
    "backend/app/api/v1/endpoints/admin.py": "cache: admin reads",
    "backend/app/api/v1/endpoints/results.py": "cache: results",
    "backend/app/services/cache.py": "cache: the results cache service",
}

_HELPERS = {"create_redis_client", "create_async_redis_client", "get_redis_pool"}


def _talks_to_redis(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            module = getattr(node, "module", None) or ""
            if module.split(".")[0] == "redis" or any(
                n.split(".")[0] == "redis" for n in names
            ):
                return True
            if module == "backend.app.core.redis_client" or _HELPERS & set(names):
                return True
        if isinstance(node, ast.Attribute) and (
            node.attr == "redis" or node.attr in _HELPERS
        ):
            return True
        if isinstance(node, ast.Name) and node.id in _HELPERS:
            return True
        if isinstance(node, ast.FunctionDef) and node.name in _HELPERS:
            return True
        if isinstance(node, ast.Constant) and node.value in ("REDIS_HOST", "REDIS_URL"):
            return True
    return False


def test_only_cache_rate_limit_and_health_code_talks_to_redis():
    found = set()
    for root in ("backend/app", "modules/backend/app"):
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if _talks_to_redis(tree):
                found.add(path.relative_to(REPO_ROOT).as_posix())
    assert found == set(REDIS_USERS), (
        f"unclassified: {sorted(found - set(REDIS_USERS))}; "
        f"gone: {sorted(set(REDIS_USERS) - found)}"
    )


def test_the_redis_scan_sees_each_shape():
    shapes = [
        "import redis",
        "from redis.asyncio import Redis",
        "from backend.app.core.redis_client import create_redis_client",
        "client = cache_control.redis",
        "pool = await deps.get_redis_pool()",
    ]
    for shape in shapes:
        assert _talks_to_redis(ast.parse(shape)), shape
    assert not _talks_to_redis(ast.parse("x = cache.get('k')"))


# --- whether a task migrates when it starts -------------------------------------------------


@pytest.mark.parametrize(
    "setting, migrates",
    [
        ({"RUN_MIGRATIONS": "false"}, False),
        ({}, True),
        ({"RUN_MIGRATIONS": "true"}, True),
    ],
    ids=["false", "unset", "true"],
)
def test_a_task_migrates_on_start_unless_its_revision_sets_run_migrations_false(
    tmp_path, setting, migrates
):
    from backend.tests.unit.scripts import (
        test_entrypoint_seed_force_and_workers as entry,
    )

    if entry.BASH is None or not entry.ENTRYPOINT.is_file():
        pytest.skip("needs bash and backend/docker-entrypoint.sh")
    environment = dict(setting)
    if "RUN_MIGRATIONS" not in setting:
        environment["RUN_MIGRATIONS"] = ""  # unset: the entrypoint's default applies
    code, stderr, calls = entry.run(tmp_path, "uvicorn", "app", **environment)
    assert code == 0, stderr
    assert ("python -m backend.app.db.bootstrap" in calls) is migrates, calls
    # Either way the server then starts.
    assert "uvicorn app" in calls
