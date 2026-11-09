"""
Unit tests for the CUPED variance-reduced results API endpoint — Issue #21.

Tests cover:
- GET /results/{id}/cuped returns 200 with CupedResultsResponse
- Returns 404 for unknown experiment
- Default method (none) when no variance_reduction_config
- CUPED method returns CUPED-adjusted results
- WINSORIZATION method lists a conversion metric with a reason (no view applies it)
- Response includes variance_reduction_pct field
- Validates schema structure (all fields present)
"""

import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1.endpoints.results import ExperimentNotFound
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.schemas.variance_reduction import (
    CupedMetricResult,
    CupedResultsResponse,
    VarianceReductionMethod,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_EXPERIMENT_ID = str(uuid.uuid4())
TEST_USER_ID = str(uuid.uuid4())


@pytest.fixture
def mock_user():
    """A minimal mock user for auth override."""
    user = MagicMock(spec=User)
    user.id = uuid.UUID(TEST_USER_ID)
    user.is_active = True
    user.is_superuser = False
    user.role = UserRole.DEVELOPER
    return user


@pytest.fixture
def client(mock_user):
    """TestClient with auth dependency overridden."""
    from backend.app.api.deps import get_current_active_user, get_db

    def override_get_current_active_user():
        return mock_user

    def override_get_db():
        db = MagicMock()
        return db

    app.dependency_overrides[get_current_active_user] = override_get_current_active_user
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_cuped_response(
    experiment_id: str = TEST_EXPERIMENT_ID,
    method: str = "cuped",
    variance_reduction_pct: float = 25.0,
) -> CupedResultsResponse:
    """Build a minimal CupedResultsResponse for mocking."""
    metric = CupedMetricResult(
        metric_id="metric-001",
        metric_name="Conversion Rate",
        adjusted_control_mean=0.10,
        adjusted_treatment_mean=0.12,
        adjusted_effect=0.02,
        adjusted_se=0.005,
        adjusted_p_value=0.03,
        adjusted_ci_lower=0.01,
        adjusted_ci_upper=0.03,
        variance_reduction_pct=variance_reduction_pct,
        theta=0.75,
        method=method,
    )
    return CupedResultsResponse(
        experiment_id=experiment_id,
        method=method,
        metrics=[metric],
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCupedApiEndpoint:
    """Tests for GET /results/{experiment_id}/cuped."""

    def test_cuped_endpoint_returns_200(self, client):
        """GET /results/{id}/cuped returns 200 for a valid experiment."""
        mock_response = _make_cuped_response()
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200

    def test_cuped_endpoint_returns_cuped_results_response(self, client):
        """GET /results/{id}/cuped returns a CupedResultsResponse schema."""
        mock_response = _make_cuped_response()
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        assert "experiment_id" in data
        assert "method" in data
        assert "metrics" in data
        assert "computed_at" in data

    def test_cuped_endpoint_returns_404_for_unknown_experiment(self, client):
        """GET /results/{id}/cuped returns 404 for an unknown experiment."""
        unknown_id = str(uuid.uuid4())
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            side_effect=ExperimentNotFound(),
        ):
            response = client.get(f"/api/v1/results/{unknown_id}/cuped")
        assert response.status_code == 404
        assert response.json()["detail"] == "Experiment not found"

    def test_a_value_error_is_a_500_not_a_404(self, client):
        """#217: only the dedicated not-found exception answers 404; a
        ValueError from the statistics (fromisoformat, a refusal) is a 500
        with the fixed text, never a 404 that repeats its message."""
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            side_effect=ValueError("Invalid isoformat string: 'client text'"),
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 500
        assert "client text" not in response.text

    def test_cuped_endpoint_default_method_none(self, client):
        """When experiment has no variance_reduction_config, method defaults to none."""
        mock_response = _make_cuped_response(method="none", variance_reduction_pct=0.0)
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        assert data["method"] == "none"

    def test_cuped_endpoint_cuped_method_returns_adjusted_results(self, client):
        """When method is CUPED, response contains CUPED-adjusted results."""
        mock_response = _make_cuped_response(
            method="cuped", variance_reduction_pct=30.0
        )
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        assert data["method"] == "cuped"
        assert len(data["metrics"]) == 1
        assert data["metrics"][0]["variance_reduction_pct"] == 30.0

    def test_cuped_endpoint_winsorization_method(self, client):
        """When method is WINSORIZATION, response reflects that method."""
        mock_response = _make_cuped_response(
            method="winsorization", variance_reduction_pct=5.0
        )
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        assert data["method"] == "winsorization"

    def test_cuped_response_includes_variance_reduction_pct(self, client):
        """Response includes variance_reduction_pct in each metric result."""
        mock_response = _make_cuped_response(variance_reduction_pct=42.5)
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        metric = data["metrics"][0]
        assert "variance_reduction_pct" in metric
        assert metric["variance_reduction_pct"] == 42.5

    def test_cuped_metric_result_all_fields_present(self, client):
        """Each metric in response has all required CupedMetricResult fields."""
        mock_response = _make_cuped_response()
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        metric = data["metrics"][0]
        required_fields = [
            "metric_id",
            "metric_name",
            "adjusted_control_mean",
            "adjusted_treatment_mean",
            "adjusted_effect",
            "adjusted_se",
            "adjusted_p_value",
            "adjusted_ci_lower",
            "adjusted_ci_upper",
            "variance_reduction_pct",
            "theta",
            "method",
        ]
        for field in required_fields:
            assert field in metric, f"Missing field: {field}"

    def test_cuped_endpoint_experiment_id_in_response(self, client):
        """Response experiment_id matches the requested experiment id."""
        mock_response = _make_cuped_response(experiment_id=TEST_EXPERIMENT_ID)
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=mock_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        assert data["experiment_id"] == TEST_EXPERIMENT_ID

    def test_cuped_endpoint_requires_authentication(self):
        """GET /results/{id}/cuped requires authentication (no override)."""
        # Use a raw client without auth override
        with TestClient(app) as raw_client:
            response = raw_client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        # Without auth, should get 401 or 403
        assert response.status_code in (401, 403, 422)

    def test_cuped_endpoint_multiple_metrics(self, client):
        """Response can contain multiple metric results."""
        metric1 = CupedMetricResult(
            metric_id="m1",
            metric_name="Conversion Rate",
            adjusted_control_mean=0.10,
            adjusted_treatment_mean=0.12,
            adjusted_effect=0.02,
            adjusted_se=0.005,
            adjusted_p_value=0.03,
            adjusted_ci_lower=0.01,
            adjusted_ci_upper=0.03,
            variance_reduction_pct=25.0,
            theta=0.75,
            method="cuped",
        )
        metric2 = CupedMetricResult(
            metric_id="m2",
            metric_name="Revenue",
            adjusted_control_mean=10.0,
            adjusted_treatment_mean=11.0,
            adjusted_effect=1.0,
            adjusted_se=0.2,
            adjusted_p_value=0.01,
            adjusted_ci_lower=0.6,
            adjusted_ci_upper=1.4,
            variance_reduction_pct=30.0,
            theta=0.5,
            method="cuped",
        )
        multi_response = CupedResultsResponse(
            experiment_id=TEST_EXPERIMENT_ID,
            method="cuped",
            metrics=[metric1, metric2],
            computed_at=datetime.now(timezone.utc).isoformat(),
        )
        with patch(
            "backend.app.api.v1.endpoints.results.get_cuped_results_data",
            return_value=multi_response,
        ):
            response = client.get(f"/api/v1/results/{TEST_EXPERIMENT_ID}/cuped")
        assert response.status_code == 200
        data = response.json()
        assert len(data["metrics"]) == 2
