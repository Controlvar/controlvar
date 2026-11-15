"""
Unit tests for dimensional analysis API (Issue #28).

Tests for GET /api/v1/results/{experiment_id}?breakdown=<dimension>.

TDD: tests written alongside the feature implementation.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api.deps import get_current_user, get_db
from backend.app.main import app
from backend.app.models.user import User
from backend.app.schemas.dimensional import (
    DimensionalBreakdownResponse,
    SegmentBreakdown,
    SegmentVariantResult,
)
from backend.app.services.analysis_service import AnalysisService

# ---------------------------------------------------------------------------
# Shared UUIDs
# ---------------------------------------------------------------------------

EXPERIMENT_UUID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTROL_UUID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TREATMENT_UUID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
METRIC_UUID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
USER_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
UNKNOWN_UUID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

# Module path for patching the helper in the endpoint module
_ENDPOINT_MODULE = "backend.app.api.v1.endpoints.results"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_breakdown_response(
    dimension: str,
    segment_values: List[str],
    has_hte: bool = False,
    adjusted_alpha: float = 0.05,
) -> DimensionalBreakdownResponse:
    """Build a DimensionalBreakdownResponse for mocking."""
    segments = []
    for sv in segment_values:
        ctrl = SegmentVariantResult(
            variant_id=str(CONTROL_UUID),
            variant_name="Control",
            is_control=True,
            sample_size=500,
            conversions=50,
            mean=0.10,
            confidence_interval=(0.075, 0.130),
            p_value=None,
            is_significant=False,
        )
        treat = SegmentVariantResult(
            variant_id=str(TREATMENT_UUID),
            variant_name="Treatment",
            is_control=False,
            sample_size=500,
            conversions=60,
            mean=0.12,
            confidence_interval=(0.093, 0.151),
            p_value=0.18,
            is_significant=False,
        )
        segments.append(
            SegmentBreakdown(
                segment_value=sv,
                sample_size=1000,
                variants=[ctrl, treat],
            )
        )

    hte_warning = (
        (
            "The first treatment's difference from the control varies between "
            f"'{dimension}' segments by more than chance usually produces. "
            "Treat this as a lead to confirm with a follow-up experiment, "
            "not as a result."
        )
        if has_hte
        else None
    )

    return DimensionalBreakdownResponse(
        dimension=dimension,
        is_exploratory=True,
        adjusted_alpha=adjusted_alpha,
        has_heterogeneous_effects=has_hte,
        hte_warning=hte_warning,
        segments=segments,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = USER_UUID
    user.email = "analyst@example.com"
    user.is_active = True
    user.is_superuser = True
    user.role = "ADMIN"
    return user


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
def client(mock_db, mock_user):
    def override_get_db():
        try:
            yield mock_db
        finally:
            pass

    async def override_get_current_user():
        return mock_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides = {}


@pytest.fixture
def mock_experiment_results() -> Dict[str, Any]:
    """Minimal valid AnalysisService result dict."""
    return {
        "experiment_id": str(EXPERIMENT_UUID),
        "experiment_name": "Platform Segmentation Test",
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
                "metric_name": "Conversion Rate",
                "metric_type": "conversion",
                "is_primary": True,
                "has_significant_result": False,
                "winning_variant_id": None,
                "variants": [
                    {
                        "variant_id": str(CONTROL_UUID),
                        "variant_name": "Control",
                        "is_control": True,
                        "sample_size": 5000,
                        "conversions": 500,
                        "mean": 0.10,
                        "confidence_interval": [0.0917, 0.1083],
                        "p_value": None,
                        "adjusted_p_value": None,
                        "is_significant": False,
                        "relative_improvement_pct": None,
                        "effect_size": None,
                        "effect_size_label": None,
                        "statistical_test_used": None,
                    },
                    {
                        "variant_id": str(TREATMENT_UUID),
                        "variant_name": "Treatment",
                        "is_control": False,
                        "sample_size": 5000,
                        "conversions": 520,
                        "mean": 0.104,
                        "confidence_interval": [0.0957, 0.1123],
                        "p_value": 0.32,
                        "adjusted_p_value": None,
                        "is_significant": False,
                        "relative_improvement_pct": 4.0,
                        "effect_size": 0.013,
                        "effect_size_label": "negligible",
                        "statistical_test_used": "z_test_proportions",
                    },
                ],
            }
        ],
        "summary": {
            "total_users": 10000,
            "total_events": 1020,
            "total_conversions": 1020,
            "duration_days": 14,
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "CONTINUE_TESTING",
            "recommendation_reason": "No significant winner yet.",
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBreakdownQueryParam:
    """Tests for ?breakdown=<dimension> extension to GET /results/{id}."""

    @pytest.mark.unit
    def test_breakdown_platform_returns_200(self, client, mock_experiment_results):
        """GET /results/{id}?breakdown=platform returns 200."""
        mock_bd = _make_breakdown_response("platform", ["ios", "android", "web"])

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "platform", "use_cache": "false"},
                )

        assert response.status_code == 200

    @pytest.mark.unit
    def test_breakdown_country_returns_200(self, client, mock_experiment_results):
        """GET /results/{id}?breakdown=country returns 200."""
        mock_bd = _make_breakdown_response("country", ["US", "UK", "DE"])

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "country", "use_cache": "false"},
                )

        assert response.status_code == 200

    @pytest.mark.unit
    def test_no_breakdown_still_works(self, client, mock_experiment_results):
        """GET /results/{id} with no breakdown param is backward compatible."""
        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            response = client.get(
                f"/api/v1/results/{EXPERIMENT_UUID}",
                params={"use_cache": "false"},
            )

        assert response.status_code == 200
        data = response.json()
        # breakdown field should be absent or None when not requested
        assert data.get("breakdown") is None

    @pytest.mark.unit
    def test_breakdown_response_contains_segments(
        self, client, mock_experiment_results
    ):
        """Response breakdown.segments lists one entry per distinct segment value."""
        mock_bd = _make_breakdown_response("platform", ["ios", "android", "web"])

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "platform", "use_cache": "false"},
                )

        data = response.json()
        assert "breakdown" in data
        assert data["breakdown"] is not None
        assert len(data["breakdown"]["segments"]) == 3

    @pytest.mark.unit
    def test_breakdown_is_exploratory_true(self, client, mock_experiment_results):
        """breakdown.is_exploratory must always be True."""
        mock_bd = _make_breakdown_response("platform", ["ios", "android"])

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "platform", "use_cache": "false"},
                )

        data = response.json()
        assert data["breakdown"]["is_exploratory"] is True

    @pytest.mark.unit
    def test_breakdown_adjusted_alpha_is_bonferroni_corrected(
        self, client, mock_experiment_results
    ):
        """breakdown.adjusted_alpha == 0.05 / num_segments."""
        expected_alpha = 0.05 / 3
        mock_bd = _make_breakdown_response(
            "platform", ["ios", "android", "web"], adjusted_alpha=expected_alpha
        )

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "platform", "use_cache": "false"},
                )

        data = response.json()
        assert data["breakdown"]["adjusted_alpha"] == pytest.approx(
            expected_alpha, rel=1e-3
        )

    @pytest.mark.unit
    def test_unknown_dimension_returns_empty_segments(
        self, client, mock_experiment_results
    ):
        """GET with ?breakdown=unknown_dim returns 200 with empty segments list."""
        mock_bd = _make_breakdown_response("unknown_dim", [])

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "unknown_dim", "use_cache": "false"},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["breakdown"]["segments"] == []

    @pytest.mark.unit
    def test_unknown_experiment_returns_404(self, client):
        """GET /results/{unknown_id}?breakdown=platform returns 404."""
        with patch.object(
            AnalysisService,
            "get_experiment_results",
            side_effect=ValueError(f"Experiment {UNKNOWN_UUID} not found"),
        ):
            response = client.get(
                f"/api/v1/results/{UNKNOWN_UUID}",
                params={"breakdown": "platform", "use_cache": "false"},
            )

        assert response.status_code == 404

    @pytest.mark.unit
    def test_hte_detected_sets_flag_and_warning(self, client, mock_experiment_results):
        """When HTE is detected, has_heterogeneous_effects=True and hte_warning is set."""
        mock_bd = _make_breakdown_response("platform", ["ios", "android"], has_hte=True)

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "platform", "use_cache": "false"},
                )

        data = response.json()
        assert data["breakdown"]["has_heterogeneous_effects"] is True
        assert data["breakdown"]["hte_warning"] is not None
        assert len(data["breakdown"]["hte_warning"]) > 0

    @pytest.mark.unit
    def test_no_hte_sets_flag_false_and_warning_none(
        self, client, mock_experiment_results
    ):
        """When no HTE, has_heterogeneous_effects=False and hte_warning=None."""
        mock_bd = _make_breakdown_response(
            "platform", ["ios", "android"], has_hte=False
        )

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "platform", "use_cache": "false"},
                )

        data = response.json()
        assert data["breakdown"]["has_heterogeneous_effects"] is False
        assert data["breakdown"]["hte_warning"] is None

    @pytest.mark.unit
    def test_breakdown_dimension_echoed_in_response(
        self, client, mock_experiment_results
    ):
        """breakdown.dimension in response must equal the requested dimension."""
        mock_bd = _make_breakdown_response("country", ["US", "UK"])

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "country", "use_cache": "false"},
                )

        data = response.json()
        assert data["breakdown"]["dimension"] == "country"

    @pytest.mark.unit
    def test_breakdown_segments_contain_variants(self, client, mock_experiment_results):
        """Each segment in the response must contain a non-empty variants list."""
        mock_bd = _make_breakdown_response("platform", ["ios"])

        with patch.object(
            AnalysisService,
            "get_experiment_results",
            return_value=mock_experiment_results,
        ):
            with patch(
                f"{_ENDPOINT_MODULE}._compute_dimensional_breakdown",
                return_value=mock_bd,
            ):
                response = client.get(
                    f"/api/v1/results/{EXPERIMENT_UUID}",
                    params={"breakdown": "platform", "use_cache": "false"},
                )

        data = response.json()
        for segment in data["breakdown"]["segments"]:
            assert len(segment["variants"]) > 0
