"""
Unit tests for backend/app/middleware/prometheus_metrics_middleware.py

Covers:
- PrometheusMetricsMiddleware records metrics on successful requests
- Status code and duration are forwarded correctly
- A route registered on the app is labelled with its template

These build a bare FastAPI app, whose routes sit on the root router. The
application's own routes sit in included routers, and
``test_metric_labels_bounded.py`` drives those.
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# PrometheusMetricsMiddleware integration-style tests (in-process)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_middleware_records_on_success(mocker):
    """record_request is called once for a successful response."""
    mock_record = mocker.patch(
        "backend.app.middleware.prometheus_metrics_middleware.record_request"
    )

    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    from backend.app.middleware.prometheus_metrics_middleware import (
        PrometheusMetricsMiddleware,
    )

    app.add_middleware(PrometheusMetricsMiddleware)
    client = TestClient(app)

    response = client.get("/ping")
    assert response.status_code == 200

    mock_record.assert_called_once()
    call_kwargs = mock_record.call_args.kwargs
    assert call_kwargs["status_code"] == 200
    assert call_kwargs["duration"] > 0
    assert call_kwargs["method"] == "GET"


@pytest.mark.asyncio
async def test_metrics_middleware_records_404(mocker):
    """record_request is called even when the route is not found (404)."""
    mock_record = mocker.patch(
        "backend.app.middleware.prometheus_metrics_middleware.record_request"
    )

    app = FastAPI()
    from backend.app.middleware.prometheus_metrics_middleware import (
        PrometheusMetricsMiddleware,
    )

    app.add_middleware(PrometheusMetricsMiddleware)
    client = TestClient(app)

    response = client.get("/nonexistent-route")
    assert response.status_code == 404

    mock_record.assert_called_once()
    call_kwargs = mock_record.call_args.kwargs
    assert call_kwargs["status_code"] == 404
    assert call_kwargs["endpoint"] == "unmatched"


@pytest.mark.asyncio
async def test_metrics_middleware_labels_uuid_path_with_route_template(mocker):
    """A request is labelled with its route's template, not the path sent."""
    mock_record = mocker.patch(
        "backend.app.middleware.prometheus_metrics_middleware.record_request"
    )

    app = FastAPI()

    @app.get("/api/v1/experiments/{experiment_id}")
    async def get_experiment(experiment_id: str):
        return {"id": experiment_id}

    from backend.app.middleware.prometheus_metrics_middleware import (
        PrometheusMetricsMiddleware,
    )

    app.add_middleware(PrometheusMetricsMiddleware)
    client = TestClient(app)

    client.get("/api/v1/experiments/550e8400-e29b-41d4-a716-446655440000")

    mock_record.assert_called_once()
    call_kwargs = mock_record.call_args.kwargs
    assert call_kwargs["endpoint"] == "/api/v1/experiments/{experiment_id}"


@pytest.mark.asyncio
async def test_metrics_middleware_records_post(mocker):
    """POST requests are tracked correctly."""
    mock_record = mocker.patch(
        "backend.app.middleware.prometheus_metrics_middleware.record_request"
    )

    app = FastAPI()

    @app.post("/api/v1/events")
    async def create_event():
        return {"created": True}

    from backend.app.middleware.prometheus_metrics_middleware import (
        PrometheusMetricsMiddleware,
    )

    app.add_middleware(PrometheusMetricsMiddleware)
    client = TestClient(app)

    response = client.post("/api/v1/events", json={})
    assert response.status_code == 200

    mock_record.assert_called_once()
    assert mock_record.call_args.kwargs["method"] == "POST"
