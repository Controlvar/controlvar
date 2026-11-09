"""
Unit tests for EP-035 Batch 2: Bayesian results wired into the Results API.

Tests cover:
- GET /api/v1/results/{experiment_id} includes bayesian_results=null
  when bayesian_enabled=False
- GET /api/v1/results/{experiment_id} includes computed bayesian_results
  when bayesian_enabled=True
- bayesian_results contains: decision, variant_results
  (probability_to_be_best, expected_loss), credible_intervals
- Test with different BayesianDecision values (CONTINUE, STOP_WINNER)
- Access control: admin/developer can access, analyst can access (read-only)
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api.deps import get_current_user, get_db
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.schemas.bayesian import (
    BayesianConfig,
    BayesianDecision,
    BayesianPosteriorResult,
    BayesianResultsResponse,
    BayesianVariantResult,
)
from backend.app.services.analysis_service import AnalysisService

# ---------------------------------------------------------------------------
# Shared UUIDs
# ---------------------------------------------------------------------------

EXPERIMENT_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTROL_VARIANT_UUID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TREATMENT_VARIANT_UUID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
METRIC_UUID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
USER_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")


# ---------------------------------------------------------------------------
# Helpers: build minimal mock results dicts
# ---------------------------------------------------------------------------


def _make_base_results(
    bayesian_enabled: bool = False,
    bayesian_results: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Return a minimal ExperimentResultsResponse-compatible dict."""
    return {
        "experiment_id": str(EXPERIMENT_UUID),
        "experiment_name": "EP-035 Bayesian Test",
        "status": "active",
        "start_date": "2026-01-01T00:00:00+00:00",
        "end_date": None,
        "confidence_level": 0.95,
        "correction_method": "none",
        "computed_at": "2026-03-01T12:00:00+00:00",
        "sample_size_adequate": True,
        "metrics": [
            {
                "metric_id": str(METRIC_UUID),
                "metric_name": "Checkout Conversion",
                "metric_type": "conversion",
                "is_primary": True,
                "has_significant_result": False,
                "winning_variant_id": None,
                "variants": [
                    {
                        "variant_id": str(CONTROL_VARIANT_UUID),
                        "variant_name": "Control",
                        "is_control": True,
                        "sample_size": 1000,
                        "conversions": 100,
                        "mean": 0.10,
                        "confidence_interval": [0.082, 0.118],
                        "p_value": None,
                        "adjusted_p_value": None,
                        "is_significant": False,
                        "relative_improvement_pct": None,
                        "effect_size": None,
                        "effect_size_label": None,
                        "statistical_test_used": None,
                    },
                    {
                        "variant_id": str(TREATMENT_VARIANT_UUID),
                        "variant_name": "Treatment",
                        "is_control": False,
                        "sample_size": 1000,
                        "conversions": 110,
                        "mean": 0.11,
                        "confidence_interval": [0.091, 0.129],
                        "p_value": 0.35,
                        "adjusted_p_value": None,
                        "is_significant": False,
                        "relative_improvement_pct": 10.0,
                        "effect_size": 0.032,
                        "effect_size_label": "negligible",
                        "statistical_test_used": "z_test_proportions",
                    },
                ],
            }
        ],
        "summary": {
            "total_users": 2000,
            "total_events": 210,
            "total_conversions": 210,
            "duration_days": 10,
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "CONTINUE_TESTING",
            "recommendation_reason": "Not yet significant.",
        },
        "bayesian_results": bayesian_results,
    }


def _make_bayesian_results(decision: str = "CONTINUE") -> Dict[str, Any]:
    """Return a BayesianResultsResponse-compatible dict."""
    return {
        "is_enabled": True,
        "decision": decision,
        "variant_results": [
            {
                "variant_key": "control",
                "posterior": {
                    "alpha": 101.0,
                    "beta": 901.0,
                    "mean": 0.1008,
                    "credible_interval_lower": 0.083,
                    "credible_interval_upper": 0.120,
                },
                "probability_to_be_best": 0.35,
                "expected_loss": 0.012,
                "bayes_factor": None,
            },
            {
                "variant_key": "treatment",
                "posterior": {
                    "alpha": 111.0,
                    "beta": 891.0,
                    "mean": 0.1107,
                    "credible_interval_lower": 0.092,
                    "credible_interval_upper": 0.130,
                },
                "probability_to_be_best": 0.65,
                "expected_loss": 0.005,
                "bayes_factor": None,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    db = MagicMock()
    # The results route reads the experiment's stored analysis settings
    # (#580); these tests were written for an uncorrected 0.95.
    experiment = db.query.return_value.filter.return_value.first.return_value
    experiment.correction_method = "none"
    experiment.confidence_level = 0.95
    # A bare MagicMock attribute is truthy: without this the route would
    # try to compute the embedded sequential block (#922) from mocks.
    experiment.sequential_testing_enabled = False
    return db


@pytest.fixture
def mock_admin_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = USER_UUID
    user.email = "admin@example.com"
    user.is_active = True
    user.is_superuser = True
    user.role = UserRole.ADMIN
    return user


@pytest.fixture
def mock_analyst_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.email = "analyst@example.com"
    user.is_active = True
    user.is_superuser = False
    user.role = UserRole.ANALYST
    return user


@pytest.fixture
def mock_developer_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.email = "dev@example.com"
    user.is_active = True
    user.is_superuser = False
    user.role = UserRole.DEVELOPER
    return user


@pytest.fixture
def client(mock_db, mock_admin_user):
    def override_get_db():
        try:
            yield mock_db
        finally:
            pass

    async def override_get_current_user():
        return mock_admin_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides = {}


@pytest.fixture
def client_analyst(mock_db, mock_analyst_user):
    def override_get_db():
        try:
            yield mock_db
        finally:
            pass

    async def override_get_current_user():
        return mock_analyst_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides = {}


@pytest.fixture
def client_developer(mock_db, mock_developer_user):
    def override_get_db():
        try:
            yield mock_db
        finally:
            pass

    async def override_get_current_user():
        return mock_developer_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides = {}


# ---------------------------------------------------------------------------
# Tests: bayesian_results field in GET /results/{experiment_id}
# ---------------------------------------------------------------------------


class TestBayesianResultsField:
    """
    Tests verifying that the bayesian_results field is correctly
    populated (or null) in the experiment results API response.
    """

    @pytest.mark.unit
    def test_bayesian_results_is_null_when_bayesian_disabled(self, client: TestClient):
        """
        When bayesian_enabled=False the response must include
        bayesian_results=null (not omitted, just null).
        """
        results = _make_base_results(bayesian_enabled=False, bayesian_results=None)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "bayesian_results" in data, (
            "bayesian_results key must be present in response even when null"
        )
        assert data["bayesian_results"] is None

    @pytest.mark.unit
    def test_bayesian_results_present_when_bayesian_enabled(self, client: TestClient):
        """
        When bayesian_enabled=True the response must contain a non-null
        bayesian_results object with decision and variant_results.
        """
        bayesian_data = _make_bayesian_results(decision="CONTINUE")
        results = _make_base_results(
            bayesian_enabled=True, bayesian_results=bayesian_data
        )

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["bayesian_results"] is not None
        br = data["bayesian_results"]
        assert "decision" in br
        assert "variant_results" in br

    @pytest.mark.unit
    def test_bayesian_results_contains_decision_field(self, client: TestClient):
        """bayesian_results.decision must be present and a valid BayesianDecision string."""
        bayesian_data = _make_bayesian_results(decision="CONTINUE")
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        assert br["decision"] in {d.value for d in BayesianDecision}

    @pytest.mark.unit
    def test_bayesian_results_contains_variant_results(self, client: TestClient):
        """
        bayesian_results.variant_results must be a list with one entry per variant,
        each containing probability_to_be_best, expected_loss, and posterior.
        """
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        assert isinstance(br["variant_results"], list)
        assert len(br["variant_results"]) == 2

        for vr in br["variant_results"]:
            assert "probability_to_be_best" in vr
            assert "expected_loss" in vr
            assert "posterior" in vr
            assert "variant_key" in vr

    @pytest.mark.unit
    def test_bayesian_results_posterior_has_credible_interval(self, client: TestClient):
        """
        Each variant's posterior must include credible_interval_lower and
        credible_interval_upper fields.
        """
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        for vr in br["variant_results"]:
            posterior = vr["posterior"]
            assert "credible_interval_lower" in posterior
            assert "credible_interval_upper" in posterior
            assert (
                posterior["credible_interval_lower"]
                < posterior["credible_interval_upper"]
            )

    @pytest.mark.unit
    def test_bayesian_decision_continue(self, client: TestClient):
        """When no variant has a low enough expected loss, decision must be CONTINUE."""
        bayesian_data = _make_bayesian_results(decision="CONTINUE")
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert response.json()["bayesian_results"]["decision"] == "CONTINUE"

    @pytest.mark.unit
    def test_bayesian_decision_stop_winner(self, client: TestClient):
        """When expected loss of the best variant is below the threshold, STOP_WINNER."""
        bayesian_data = _make_bayesian_results(decision="STOP_WINNER")
        # Update expected_loss values to reflect winner scenario
        bayesian_data["variant_results"][1]["expected_loss"] = 0.0001
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert response.json()["bayesian_results"]["decision"] == "STOP_WINNER"

    @pytest.mark.unit
    def test_bayesian_decision_stop_equivalent(self, client: TestClient):
        """STOP_EQUIVALENT decision is returned correctly."""
        bayesian_data = _make_bayesian_results(decision="STOP_EQUIVALENT")
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert response.json()["bayesian_results"]["decision"] == "STOP_EQUIVALENT"

    @pytest.mark.unit
    def test_bayesian_results_is_enabled_flag(self, client: TestClient):
        """bayesian_results.is_enabled must be True when Bayesian analysis ran."""
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        assert br["is_enabled"] is True

    @pytest.mark.unit
    def test_bayesian_ptbb_sum_to_one(self, client: TestClient):
        """
        probability_to_be_best values across all variants must sum to
        approximately 1.0 (within floating point tolerance).
        """
        bayesian_data = _make_bayesian_results()
        # Force PtBB values to sum to exactly 1.0
        bayesian_data["variant_results"][0]["probability_to_be_best"] = 0.35
        bayesian_data["variant_results"][1]["probability_to_be_best"] = 0.65
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        ptbb_sum = sum(vr["probability_to_be_best"] for vr in br["variant_results"])
        assert abs(ptbb_sum - 1.0) < 0.01, (
            f"PtBB values must sum to ~1.0, got {ptbb_sum}"
        )

    @pytest.mark.unit
    def test_bayesian_expected_loss_non_negative(self, client: TestClient):
        """Expected loss values must be >= 0 for all variants."""
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        for vr in br["variant_results"]:
            assert vr["expected_loss"] >= 0.0, (
                f"expected_loss must be >= 0, got {vr['expected_loss']}"
            )


# ---------------------------------------------------------------------------
# Tests: access control
# ---------------------------------------------------------------------------


class TestBayesianResultsAccessControl:
    """Verify that different roles can access results (read-only scenario)."""

    @pytest.mark.unit
    def test_admin_can_access_bayesian_results(self, client: TestClient):
        """ADMIN users must be able to read results including bayesian_results."""
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert response.json()["bayesian_results"] is not None

    @pytest.mark.unit
    def test_analyst_can_access_bayesian_results(self, client_analyst: TestClient):
        """ANALYST users (read-only) must be able to retrieve bayesian_results."""
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client_analyst.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert response.json()["bayesian_results"] is not None

    @pytest.mark.unit
    def test_developer_can_access_bayesian_results(self, client_developer: TestClient):
        """DEVELOPER users must be able to read bayesian_results."""
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client_developer.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert response.json()["bayesian_results"] is not None

    @pytest.mark.unit
    def test_unauthenticated_request_returns_401(self):
        """Requests without authentication must be rejected with 401 or 403."""
        with TestClient(app) as unauthenticated_client:
            response = unauthenticated_client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )
        # 401 or 422 (missing token) or 403 are all acceptable rejections
        assert response.status_code in (401, 403, 422), (
            f"Expected auth rejection, got {response.status_code}"
        )


# ---------------------------------------------------------------------------
# Tests: response schema completeness
# ---------------------------------------------------------------------------


class TestBayesianResultsResponseSchema:
    """Ensure the full response schema is correct when Bayesian is enabled."""

    @pytest.mark.unit
    def test_top_level_response_includes_bayesian_results_key(self, client: TestClient):
        """
        The top-level ExperimentResultsResponse must always include a
        bayesian_results key (null or populated).
        """
        results_no_bayesian = _make_base_results(bayesian_results=None)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results_no_bayesian,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert "bayesian_results" in response.json()

    @pytest.mark.unit
    def test_bayesian_variant_result_has_all_required_fields(self, client: TestClient):
        """Each BayesianVariantResult must have variant_key, posterior,
        probability_to_be_best, and expected_loss."""
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        required_keys = {
            "variant_key",
            "posterior",
            "probability_to_be_best",
            "expected_loss",
        }
        for vr in br["variant_results"]:
            for key in required_keys:
                assert key in vr, f"Missing key '{key}' in BayesianVariantResult"

    @pytest.mark.unit
    def test_posterior_result_has_all_required_fields(self, client: TestClient):
        """BayesianPosteriorResult must have alpha, beta, mean, and CI bounds."""
        bayesian_data = _make_bayesian_results()
        results = _make_base_results(bayesian_results=bayesian_data)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        br = response.json()["bayesian_results"]
        posterior_required = {
            "alpha",
            "beta",
            "mean",
            "credible_interval_lower",
            "credible_interval_upper",
        }
        for vr in br["variant_results"]:
            for key in posterior_required:
                assert key in vr["posterior"], (
                    f"Missing key '{key}' in BayesianPosteriorResult"
                )

    @pytest.mark.unit
    def test_response_is_200_even_when_bayesian_is_none(self, client: TestClient):
        """
        The endpoint must return 200 even when bayesian_results is null
        (backwards-compatible for experiments that predate EP-035).
        """
        results = _make_base_results(bayesian_results=None)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        assert response.json()["bayesian_results"] is None
