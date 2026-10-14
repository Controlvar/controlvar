"""One uvicorn worker per API container, and how many containers there are.

The image, both compose files and the Helm chart default ``WEB_CONCURRENCY``
to 1; the AWS stacks do not set it, so an AWS task takes the image's default.
The number of API containers is the AWS service's task count, whose floor is
``API_DESIRED_COUNT`` in ``infrastructure/cdk/app.py``, and the Helm chart's
``api.replicaCount``. Reads files only.
"""

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]


def _text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_the_image_compose_files_and_chart_default_to_one_worker():
    assert "    WEB_CONCURRENCY=1 \\\n" in _text("backend/Dockerfile")
    assert "WEB_CONCURRENCY: ${WEB_CONCURRENCY:-1}" in _text(
        "deploy/compose/compose.yml"
    )
    assert "WEB_CONCURRENCY: ${WEB_CONCURRENCY:-1}" in _text("docker-compose.yml")
    assert 'WEB_CONCURRENCY: "1"' in _text("charts/experimently/templates/config.yaml")


def test_the_aws_stacks_do_not_set_it():
    stacks = [
        path
        for root in ("infrastructure/cdk", "modules/infrastructure")
        if (REPO_ROOT / root).is_dir()
        for path in (REPO_ROOT / root).rglob("*.py")
    ]
    assert stacks, "no CDK source found"
    assert not [p for p in stacks if "WEB_CONCURRENCY" in p.read_text(encoding="utf-8")]


def test_the_api_task_count_and_its_floor_are_api_desired_count():
    app = _text("infrastructure/cdk/app.py")
    assert "api_desired_count=API_DESIRED_COUNT[env_name]" in app
    stack = _text("infrastructure/cdk/stacks/fargate_service_stack.py")
    assert "desired_count=api_desired_count" in stack
    assert "min_capacity=api_desired_count" in stack


def test_the_chart_runs_api_replica_count_api_pods():
    deployment = _text("charts/experimently/templates/api-deployment.yaml")
    assert "replicas: {{ .Values.api.replicaCount }}" in deployment
