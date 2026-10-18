"""The settings an AWS deployment gives the API, and what the API does with them (#1142).

The database engine is built from ``POSTGRES_SERVER`` with a fixed pool; no
setting names a reader endpoint or a pool size. The Cognito settings are read
only by the Cognito sign-in. The DynamoDB counter client takes its region from
``AWS_DEFAULT_REGION``, which the compose file sets from ``AWS_REGION`` and
which the Helm chart leaves to ``api.extraEnv``. Nothing sends events to
Kinesis or indexes them into OpenSearch.
"""

from __future__ import annotations

import ast
import pathlib

import yaml

from backend.app.core.config import Settings

REPO = pathlib.Path(__file__).resolve().parents[4]
TREES = [REPO / "backend" / "app", REPO / "modules" / "backend" / "app"]


def _sources():
    for tree in TREES:
        if tree.is_dir():
            for path in sorted(tree.rglob("*.py")):
                yield path.relative_to(REPO).as_posix(), path.read_text()


def test_one_engine_on_postgres_server_with_a_fixed_pool():
    from backend.app.db import session

    assert session.engine.url.host == session.settings.POSTGRES_SERVER
    pool = session.engine.pool
    assert (pool.size(), pool._max_overflow, pool._timeout) == (20, 20, 30)
    # No setting names a reader endpoint or changes the pool.
    fields = set(Settings.model_fields)
    assert not [f for f in fields if "READER" in f or "REPLICA" in f or "POOL" in f]
    # The API process builds one engine; the only other core builder is the
    # bootstrap, which runs before the API starts.
    builders = set()
    for path in sorted((REPO / "backend" / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "create_engine"
            ):
                builders.add(path.relative_to(REPO).as_posix())
    assert builders == {"backend/app/db/session.py", "backend/app/db/bootstrap.py"}


def test_redis_defaults_and_cognito_settings_only_for_cognito_sign_in(monkeypatch):
    for name in ("REDIS_HOST", "REDIS_PORT"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    assert (defaults.REDIS_HOST, str(defaults.REDIS_PORT)) == ("localhost", "6379")

    readers = {
        path
        for path, text in _sources()
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Constant)
        and node.value in {"COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID"}
    }
    assert readers == {"backend/app/services/auth_service.py"}
    # And the Cognito service reads AWS_REGION.
    auth = (REPO / "backend" / "app" / "services" / "auth_service.py").read_text()
    assert 'os.environ.get("AWS_REGION"' in auth


def test_the_counter_client_takes_its_region_from_aws_default_region():
    path = (
        REPO
        / "modules"
        / "backend"
        / "app"
        / "services"
        / "dynamodb_counter_service.py"
    )
    if not path.is_file():
        return  # core checkout: no counters module
    tree = ast.parse(path.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"client", "resource"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "boto3"
    ]
    assert calls, "the service builds no boto3 client"
    for call in calls:
        assert not [k for k in call.keywords if k.arg == "region_name"], ast.unparse(
            call
        )
    assert "AWS_REGION" not in path.read_text()


def test_compose_sets_aws_default_region_from_aws_region_and_helm_leaves_it_to_extra_env():
    api = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["api"]
    assert api["environment"]["AWS_DEFAULT_REGION"].startswith("${AWS_REGION")

    chart = REPO / "charts" / "experimently"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["api"]["extraEnv"] == []
    template = (chart / "templates" / "api-deployment.yaml").read_text()
    assert 'include "controlvar.extraEnv"' in template
    for path in chart.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="ignore")
            assert "AWS_DEFAULT_REGION" not in text and "AWS_REGION" not in text, path


def test_nothing_sends_events_to_kinesis_or_indexes_them_into_opensearch():
    for path, text in _sources():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"client", "resource"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant):
                        assert first.value not in {
                            "kinesis",
                            "firehose",
                            "opensearch",
                            "es",
                        }, path
                assert node.func.attr not in {"put_record", "put_records"}, path
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [
                    getattr(node, "module", "") or ""
                ]
                assert not [
                    n for n in names if "opensearch" in n or "elasticsearch" in n
                ], path


def test_the_ai_design_key_is_optional():
    from backend.app.services.ai_design_service import AIDesignService

    assert "ANTHROPIC_API_KEY" not in Settings.model_fields
    import os

    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert AIDesignService.is_ai_available() is False
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        assert AIDesignService.is_ai_available() is True
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved


def test_each_request_reaches_the_access_log_with_its_path_and_status():
    import io
    import logging

    from backend.app.core.logger import configure_logging

    entrypoint = (REPO / "backend" / "docker-entrypoint.sh").read_text()
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    for text in (entrypoint, dockerfile):
        assert "--no-access-log" not in text
        assert "--log-level" not in text
    assert "set -- uvicorn backend.app.main:app" in entrypoint

    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    lines = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    try:
        configure_logging(stream=io.StringIO())
        collector = Collect(logging.INFO)
        root.addHandler(collector)
        access = logging.getLogger("uvicorn.access")
        assert not access.disabled and access.propagate
        # The call uvicorn's protocol makes for each response.
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "10.0.0.1:52000",
            "POST",
            "/api/v1/integrations/webhooks/salesforce",
            "1.1",
            401,
        )
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
    assert any(
        '"POST /api/v1/integrations/webhooks/salesforce HTTP/1.1" 401' in line
        for line in lines
    ), lines


def test_a_version_is_deployed_by_the_deploy_workflow_with_a_release_tag():
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "deploy.yml").read_text()
    )
    # What the Actions tab lists, and what a person picks: "Deploy".
    assert workflow["name"] == "Deploy"
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    version = triggers["workflow_dispatch"]["inputs"]["version"]
    assert version["required"] is True
    preflight = yaml.dump(workflow["jobs"]["preflight"])
    assert "v[0-9]+" in preflight and "refs/tags/${VERSION}" in preflight
