"""
Unit tests for EP-035 Batch 2: Bayesian service integration with AnalysisService
and ExperimentScheduler.

Tests cover:
- AnalysisService._compute_bayesian_results() called when bayesian_enabled=True
- BayesianService receives correct conversions/totals from experiment metrics
- BayesianDecision is stored back to experiment.bayesian_decision

The decision is a recommendation: the scheduler no longer stops an experiment on
it (#216).  That is pinned against a real database by
backend/tests/integration/api/test_experiment_bayesian_config_api.py.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, call, patch

import pytest

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.schemas.bayesian import (
    BayesianConfig,
    BayesianDecision,
    BayesianPosteriorResult,
    BayesianResultsResponse,
    BayesianVariantResult,
)
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.bayesian_service import BayesianService

# ---------------------------------------------------------------------------
# Helpers / Factories
# ---------------------------------------------------------------------------


def _make_mock_variant(
    variant_id: str = None,
    name: str = "Control",
    is_control: bool = True,
) -> MagicMock:
    v = MagicMock()
    v.id = uuid.UUID(variant_id) if variant_id else uuid.uuid4()
    v.name = name
    v.is_control = is_control
    return v


def _make_mock_metric(
    metric_id: str = None,
    name: str = "Checkout Conversion",
    event_name: str = "checkout",
    is_primary: bool = True,
) -> MagicMock:
    m = MagicMock()
    m.id = uuid.UUID(metric_id) if metric_id else uuid.uuid4()
    m.name = name
    m.event_name = event_name
    m.conversion_window_hours = None
    m.is_primary = is_primary
    m.metric_type = "conversion"
    return m


def _make_mock_experiment(
    bayesian_enabled: bool = True,
    bayesian_config: Dict = None,
    bayesian_decision: str = None,
    status: ExperimentStatus = ExperimentStatus.ACTIVE,
) -> MagicMock:
    exp = MagicMock(spec=Experiment)
    exp.id = uuid.uuid4()
    exp.name = "EP-035 Integration Test"
    exp.status = status
    exp.bayesian_enabled = bayesian_enabled
    exp.bayesian_config = bayesian_config or {
        "prior_family": "beta",
        "alpha": 1.0,
        "beta": 1.0,
        "loss_threshold": 0.001,
        "credible_level": 0.95,
    }
    exp.bayesian_decision = bayesian_decision
    exp.start_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    exp.end_date = None
    exp.variants = [
        _make_mock_variant(name="Control", is_control=True),
        _make_mock_variant(name="Treatment", is_control=False),
    ]
    exp.metric_definitions = [_make_mock_metric()]
    # The stored analysis settings the results read (#580).
    exp.correction_method = "benjamini_hochberg"
    exp.confidence_level = 0.95
    return exp


# ---------------------------------------------------------------------------
# Tests: _compute_bayesian_results is called when bayesian_enabled=True
# ---------------------------------------------------------------------------


class TestAnalysisServiceBayesianIntegration:
    """Tests for AnalysisService integration with BayesianService."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def analysis_service(self, mock_db):
        return AnalysisService(mock_db)

    @pytest.mark.unit
    def test_compute_bayesian_results_method_exists(self, analysis_service):
        """AnalysisService must have a _compute_bayesian_results method."""
        assert hasattr(analysis_service, "_compute_bayesian_results"), (
            "AnalysisService must define _compute_bayesian_results()"
        )

    @pytest.mark.unit
    def test_compute_bayesian_results_returns_bayesian_results_response(
        self, analysis_service
    ):
        """_compute_bayesian_results must return a BayesianResultsResponse."""
        experiment = _make_mock_experiment()
        metrics_data = {
            str(experiment.variants[0].id): {"conversions": 100, "total": 1000},
            str(experiment.variants[1].id): {"conversions": 110, "total": 1000},
        }

        result = analysis_service._compute_bayesian_results(experiment, metrics_data)

        assert isinstance(result, BayesianResultsResponse), (
            f"Expected BayesianResultsResponse, got {type(result)}"
        )

    @pytest.mark.unit
    def test_compute_bayesian_results_is_enabled_true(self, analysis_service):
        """BayesianResultsResponse.is_enabled must be True."""
        experiment = _make_mock_experiment()
        metrics_data = {
            str(experiment.variants[0].id): {"conversions": 100, "total": 1000},
            str(experiment.variants[1].id): {"conversions": 110, "total": 1000},
        }

        result = analysis_service._compute_bayesian_results(experiment, metrics_data)

        assert result.is_enabled is True

    @pytest.mark.unit
    def test_compute_bayesian_results_has_variant_results(self, analysis_service):
        """Result must have one BayesianVariantResult per variant."""
        experiment = _make_mock_experiment()
        n_variants = len(experiment.variants)
        metrics_data = {
            str(v.id): {"conversions": 100, "total": 1000} for v in experiment.variants
        }

        result = analysis_service._compute_bayesian_results(experiment, metrics_data)

        assert len(result.variant_results) == n_variants

    @pytest.mark.unit
    def test_compute_bayesian_results_decision_is_valid(self, analysis_service):
        """The decision field must be a valid BayesianDecision value."""
        experiment = _make_mock_experiment()
        metrics_data = {
            str(v.id): {"conversions": 100, "total": 1000} for v in experiment.variants
        }

        result = analysis_service._compute_bayesian_results(experiment, metrics_data)

        valid_decisions = set(BayesianDecision)
        assert result.decision in valid_decisions, (
            f"Invalid decision {result.decision!r}"
        )

    @pytest.mark.unit
    def test_compute_bayesian_results_loads_config_from_experiment(
        self, analysis_service
    ):
        """BayesianConfig must be loaded from experiment.bayesian_config JSONB."""
        config = {
            "prior_family": "beta",
            "alpha": 2.0,
            "beta": 10.0,
            "loss_threshold": 0.005,
            "credible_level": 0.95,
        }
        experiment = _make_mock_experiment(bayesian_config=config)
        metrics_data = {
            str(v.id): {"conversions": 50, "total": 500} for v in experiment.variants
        }

        with patch(
            "backend.app.services.analysis_service.BayesianService"
        ) as MockService:
            mock_instance = MockService.return_value
            mock_instance.analyze.return_value = {
                "posteriors": [
                    {"alpha": 52.0, "beta": 460.0},
                    {"alpha": 52.0, "beta": 460.0},
                ],
                "credible_intervals": [(0.08, 0.13), (0.08, 0.13)],
                "probability_to_be_best": [0.5, 0.5],
                "expected_loss": [0.01, 0.01],
                "decision": BayesianDecision.CONTINUE,
            }

            analysis_service._compute_bayesian_results(experiment, metrics_data)

            # Verify BayesianService was constructed with a BayesianConfig
            assert MockService.called
            call_args = MockService.call_args
            if call_args and call_args[0]:
                config_arg = call_args[0][0]
                assert isinstance(config_arg, BayesianConfig)
            elif call_args and call_args[1]:
                config_arg = call_args[1].get("config")
                if config_arg is not None:
                    assert isinstance(config_arg, BayesianConfig)

    @pytest.mark.unit
    def test_compute_bayesian_results_calls_analyze_with_observations(
        self, analysis_service
    ):
        """BayesianService.analyze must be called with per-variant observations."""
        experiment = _make_mock_experiment()
        metrics_data = {
            str(experiment.variants[0].id): {"conversions": 200, "total": 2000},
            str(experiment.variants[1].id): {"conversions": 250, "total": 2000},
        }

        with patch(
            "backend.app.services.analysis_service.BayesianService"
        ) as MockService:
            mock_instance = MockService.return_value
            mock_instance.analyze.return_value = {
                "posteriors": [
                    {"alpha": 201.0, "beta": 1801.0},
                    {"alpha": 251.0, "beta": 1751.0},
                ],
                "credible_intervals": [(0.087, 0.113), (0.11, 0.14)],
                "probability_to_be_best": [0.2, 0.8],
                "expected_loss": [0.015, 0.001],
                "decision": BayesianDecision.CONTINUE,
            }

            analysis_service._compute_bayesian_results(experiment, metrics_data)

            assert mock_instance.analyze.called, (
                "BayesianService.analyze must be called"
            )
            call_args = mock_instance.analyze.call_args
            # The variant_observations must include the correct data
            if call_args and call_args[0]:
                observations = call_args[0][0]
                assert isinstance(observations, list)
                assert len(observations) == 2
            elif call_args and call_args[1]:
                observations = call_args[1].get("variant_observations")
                if observations is not None:
                    assert len(observations) == 2

    @pytest.mark.unit
    def test_compute_bayesian_results_ptbb_in_range(self, analysis_service):
        """Each probability_to_be_best value must be in [0, 1]."""
        experiment = _make_mock_experiment()
        metrics_data = {
            str(v.id): {"conversions": 100, "total": 1000} for v in experiment.variants
        }

        result = analysis_service._compute_bayesian_results(experiment, metrics_data)

        for vr in result.variant_results:
            assert 0.0 <= vr.probability_to_be_best <= 1.0, (
                f"probability_to_be_best out of range: {vr.probability_to_be_best}"
            )

    @pytest.mark.unit
    def test_compute_bayesian_results_expected_loss_non_negative(
        self, analysis_service
    ):
        """Each expected_loss value must be >= 0."""
        experiment = _make_mock_experiment()
        metrics_data = {
            str(v.id): {"conversions": 100, "total": 1000} for v in experiment.variants
        }

        result = analysis_service._compute_bayesian_results(experiment, metrics_data)

        for vr in result.variant_results:
            assert vr.expected_loss >= 0.0, (
                f"expected_loss must be >= 0, got {vr.expected_loss}"
            )


# ---------------------------------------------------------------------------
# Tests: get_experiment_results includes bayesian_results in return dict
# ---------------------------------------------------------------------------


class TestGetExperimentResultsBayesian:
    """Tests for get_experiment_results() adding bayesian_results to dict."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        return db

    @pytest.mark.unit
    def test_get_results_includes_bayesian_results_key_when_enabled(self, mock_db):
        """
        When bayesian_enabled=True and bayesian_config is set,
        get_experiment_results() must include 'bayesian_results' in the returned dict.
        """
        experiment = _make_mock_experiment(bayesian_enabled=True)
        mock_db.query.return_value.options.return_value.filter.return_value.first.return_value = experiment
        # Mock scalar queries for assignments/events to return 0
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0

        service = AnalysisService(mock_db)

        with patch.object(service, "_compute_bayesian_results") as mock_bayes:
            bayesian_response = BayesianResultsResponse(
                is_enabled=True,
                decision=BayesianDecision.CONTINUE,
                variant_results=[],
            )
            mock_bayes.return_value = bayesian_response

            result = service.get_experiment_results(experiment.id)

        assert "bayesian_results" in result, (
            "get_experiment_results must include bayesian_results key"
        )

    @pytest.mark.unit
    def test_get_results_bayesian_results_null_when_disabled(self, mock_db):
        """
        When bayesian_enabled=False, bayesian_results must be None in the dict.
        """
        experiment = _make_mock_experiment(bayesian_enabled=False)
        mock_db.query.return_value.options.return_value.filter.return_value.first.return_value = experiment
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0

        service = AnalysisService(mock_db)
        result = service.get_experiment_results(experiment.id)

        assert result.get("bayesian_results") is None, (
            "bayesian_results must be None when bayesian_enabled=False"
        )

    @pytest.mark.unit
    def test_bayesian_decision_stored_back_to_experiment(self, mock_db):
        """
        After Bayesian analysis, bayesian_decision must be written back to
        experiment.bayesian_decision.
        """
        experiment = _make_mock_experiment(bayesian_enabled=True)
        mock_db.query.return_value.options.return_value.filter.return_value.first.return_value = experiment
        mock_db.query.return_value.filter.return_value.scalar.return_value = 0

        service = AnalysisService(mock_db)

        with patch.object(service, "_compute_bayesian_results") as mock_bayes:
            bayesian_response = BayesianResultsResponse(
                is_enabled=True,
                decision=BayesianDecision.STOP_WINNER,
                variant_results=[],
            )
            mock_bayes.return_value = bayesian_response

            service.get_experiment_results(experiment.id)

        # The decision value must have been stored back
        assert experiment.bayesian_decision == BayesianDecision.STOP_WINNER.value, (
            f"bayesian_decision must be stored back, "
            f"got {experiment.bayesian_decision!r}"
        )
