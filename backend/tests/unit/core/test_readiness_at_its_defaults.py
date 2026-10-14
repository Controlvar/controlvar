"""Readiness (``/health``, the load balancer's health check) as it is deployed.

``test_health_endpoints.py`` sets ``REDIS_REQUIRED`` in every case. Here the
setting is left at the default ``Settings`` gives it, which is what an AWS
task runs with (the API task definition does not set it:
``infrastructure/tests/test_stack_graph.py``), so a change of the default
fails here:

* a Redis that does not answer leaves ``/health`` at 200, reported
  ``unhealthy`` under ``checks``: it does not fail a deployment by itself;
* free disk of 1 GB or less (as reported, to 0.01 GB) is ``low``, more is
  ``healthy``, and ``low`` does not fail the probe either.

And ``/metrics`` exports what this process's default registry holds: no
multiprocess collector merges the workers of a container.
"""

from __future__ import annotations

import shutil
import uuid
from collections import namedtuple
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.core import health
from backend.app.core.config import Settings
from backend.app.main import app

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
GB = 1024**3
Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def deployed(monkeypatch):
    """The two switches at the values ``Settings`` defaults them to."""
    for name in ("REDIS_REQUIRED", "METRICS_ENABLED"):
        monkeypatch.setattr(health.settings, name, Settings.model_fields[name].default)
    monkeypatch.setattr(health.settings, "METRICS_TOKEN", None)
    monkeypatch.setattr(health.settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(
        health, "check_database", lambda: {"status": "healthy", "latency_ms": 0.1}
    )


def test_a_redis_that_does_not_answer_leaves_health_at_200_by_default(
    deployed, monkeypatch
):
    assert Settings.model_fields["REDIS_REQUIRED"].default is False
    monkeypatch.setattr(
        health,
        "check_redis",
        lambda: {"status": "unhealthy", "error": "ConnectionError"},
    )
    monkeypatch.setattr(health, "check_disk", lambda path="/": {"status": "healthy"})
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["checks"]["redis"]["status"] == "unhealthy"


@pytest.mark.parametrize(
    ("free_gb", "status"),
    [(0.5, "low"), (0.99, "low"), (1.0, "low"), (1.01, "healthy"), (40.0, "healthy")],
)
def test_free_disk_of_1_gb_or_less_is_low_and_does_not_fail_the_probe(
    deployed, monkeypatch, free_gb, status
):
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: Usage(100 * GB, 0, int(free_gb * GB))
    )
    assert health.check_disk()["status"] == status
    monkeypatch.setattr(health, "check_redis", lambda: {"status": "healthy"})
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["checks"]["disk"]["status"] == status


def test_metrics_are_this_processs_own(deployed):
    from prometheus_client import Counter

    name = f"probe_{uuid.uuid4().hex}"
    counter = Counter(name, "a counter only this process increments")
    counter.inc(3)
    body = TestClient(app).get("/metrics").text
    assert f"{name}_total 3.0" in body
    # Nothing aggregates the workers of a container.
    for root in ("backend/app", "modules/backend/app"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            text = path.read_text()
            for word in (
                "multiprocess",
                "MultiProcessCollector",
                "PROMETHEUS_MULTIPROC",
            ):
                assert word not in text, (path, word)
