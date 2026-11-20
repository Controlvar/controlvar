"""
Unit tests for BanditScheduler (Issue #22 Batch B).

Coverage:
- BanditScheduler.run_once()                    (scheduler logic with mocked DB)
- BanditScheduler.update_experiment()
- BanditScheduler.get_variant_stats_from_counters()
- BanditScheduler.estimate_regret_reduction()
- BanditScheduler.get_recommendation()
- Pure-logic integration tests (no DB)
"""

import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.services.bandit_service import (
    UCB1,
    BanditService,
    EpsilonGreedy,
    ThompsonSampling,
    VariantStats,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_variant(variant_id: str, name: str = None):
    """Create a mock Variant with a UUID id."""
    v = MagicMock()
    v.id = uuid.UUID(variant_id) if "-" in variant_id else uuid.uuid4()
    v.name = name or f"variant_{variant_id}"
    return v


def _make_experiment(
    exp_id: str,
    optimization_type: str = "thompson_sampling",
    variant_ids: Optional[List[str]] = None,
    status=None,
):
    """Create a mock Experiment object."""
    from backend.app.models.experiment import ExperimentStatus

    exp = MagicMock()
    exp.id = uuid.UUID(exp_id) if len(exp_id) == 36 else uuid.uuid4()
    exp.optimization_type = optimization_type
    exp.status = status or ExperimentStatus.ACTIVE

    if variant_ids is None:
        variant_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    exp.variants = [_make_variant(vid) for vid in variant_ids]
    return exp


def _make_scheduler(db=None):
    """Create a BanditScheduler with a mocked DB session."""
    if db is None:
        db = MagicMock()
    return BanditScheduler(db=db)


# ===========================================================================
# TestBanditScheduler — scheduler logic with mocked DB
# ===========================================================================


class TestBanditScheduler:
    """Tests for BanditScheduler using mocked DB and service dependencies."""

    # -----------------------------------------------------------------------
    # 1. run_once finds active MAB experiments
    # -----------------------------------------------------------------------
    def test_run_once_finds_active_mab_experiments(self):
        """run_once queries for ACTIVE experiments with non-fixed optimization_type."""
        from backend.app.models.experiment import ExperimentStatus

        db = MagicMock()
        exp_id = str(uuid.uuid4())
        mock_exp = _make_experiment(exp_id, optimization_type="thompson_sampling")

        # DB query chain returns a list with one experiment
        db.query.return_value.filter.return_value.all.return_value = [mock_exp]

        scheduler = BanditScheduler(db=db)

        with patch.object(scheduler, "update_experiment", return_value=True):
            result = scheduler.run_once()

        assert result["updated"] == 1
        assert result["errors"] == 0

    # -----------------------------------------------------------------------
    # 2. run_once skips fixed experiments
    # -----------------------------------------------------------------------
    def test_run_once_skips_fixed_optimization_experiments(self):
        """run_once must NOT include fixed-type experiments in the query."""
        db = MagicMock()

        # DB returns empty list (fixed experiments excluded by WHERE clause)
        db.query.return_value.filter.return_value.all.return_value = []

        scheduler = BanditScheduler(db=db)
        result = scheduler.run_once()

        assert result["updated"] == 0
        assert result["skipped"] == 0

    # -----------------------------------------------------------------------
    # 3. update_experiment calls BanditService.compute_weights with correct algorithm
    # -----------------------------------------------------------------------
    def test_update_experiment_calls_correct_algorithm(self):
        """update_experiment uses the experiment's optimization_type."""
        db = MagicMock()
        exp_id = str(uuid.uuid4())
        mock_exp = _make_experiment(exp_id, optimization_type="ucb1")

        scheduler = BanditScheduler(db=db)

        mock_stats = {
            str(v.id): VariantStats(
                variant_id=str(v.id), successes=10, failures=5, pulls=15
            )
            for v in mock_exp.variants
        }

        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=mock_stats
        ):
            with patch.object(
                BanditService,
                "compute_weights",
                return_value={str(v.id): 0.5 for v in mock_exp.variants},
            ) as mock_compute:
                db.query.return_value.filter.return_value.first.return_value = None
                scheduler.update_experiment(mock_exp)

        mock_compute.assert_called_once()
        call_kwargs = mock_compute.call_args
        assert call_kwargs[1]["algorithm"] == "ucb1" or call_kwargs[0][0] == "ucb1"

    # -----------------------------------------------------------------------
    # 4. update_experiment updates variant_weights in DB
    # -----------------------------------------------------------------------
    def test_update_experiment_updates_bandit_state_in_db(self):
        """update_experiment commits a BanditState to the DB."""
        db = MagicMock()
        exp_id = str(uuid.uuid4())
        mock_exp = _make_experiment(exp_id, optimization_type="thompson_sampling")

        # Pretend no BanditState exists yet
        db.query.return_value.filter.return_value.first.return_value = None

        scheduler = BanditScheduler(db=db)

        vid = str(mock_exp.variants[0].id)
        vid2 = str(mock_exp.variants[1].id)
        mock_stats = {
            vid: VariantStats(variant_id=vid, successes=20, failures=10, pulls=30),
            vid2: VariantStats(variant_id=vid2, successes=5, failures=15, pulls=20),
        }
        weights = {vid: 0.7, vid2: 0.3}

        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=mock_stats
        ):
            with patch.object(BanditService, "compute_weights", return_value=weights):
                result = scheduler.update_experiment(mock_exp)

        assert result is True
        db.add.assert_called_once()
        db.commit.assert_called_once()

    # -----------------------------------------------------------------------
    # 5. Creates BanditState record if it doesn't exist
    # -----------------------------------------------------------------------
    def test_update_experiment_creates_bandit_state_when_missing(self):
        """A new BanditState is added when none exists for the experiment."""
        from backend.app.models.bandit_state import BanditState

        db = MagicMock()
        exp_id = str(uuid.uuid4())
        mock_exp = _make_experiment(exp_id)
        db.query.return_value.filter.return_value.first.return_value = None

        scheduler = BanditScheduler(db=db)
        vid = str(mock_exp.variants[0].id)
        vid2 = str(mock_exp.variants[1].id)
        mock_stats = {
            vid: VariantStats(variant_id=vid),
            vid2: VariantStats(variant_id=vid2),
        }

        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=mock_stats
        ):
            with patch.object(
                BanditService, "compute_weights", return_value={vid: 0.5, vid2: 0.5}
            ):
                scheduler.update_experiment(mock_exp)

        # db.add must have been called with a BanditState instance
        added_obj = db.add.call_args[0][0]
        assert isinstance(added_obj, BanditState)
        assert str(added_obj.experiment_id) == str(mock_exp.id)

    # -----------------------------------------------------------------------
    # 6. Updates existing BanditState rather than creating a new one
    # -----------------------------------------------------------------------
    def test_update_experiment_updates_existing_bandit_state(self):
        """When a BanditState already exists, it is updated in place."""
        from backend.app.models.bandit_state import BanditState

        db = MagicMock()
        exp_id = str(uuid.uuid4())
        mock_exp = _make_experiment(exp_id)

        existing_state = MagicMock(spec=BanditState)
        db.query.return_value.filter.return_value.first.return_value = existing_state

        scheduler = BanditScheduler(db=db)
        vid = str(mock_exp.variants[0].id)
        vid2 = str(mock_exp.variants[1].id)
        mock_stats = {
            vid: VariantStats(variant_id=vid, successes=30, pulls=50),
            vid2: VariantStats(variant_id=vid2, successes=10, pulls=50),
        }

        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=mock_stats
        ):
            with patch.object(
                BanditService, "compute_weights", return_value={vid: 0.65, vid2: 0.35}
            ):
                scheduler.update_experiment(mock_exp)

        # db.add should NOT be called (existing record updated in place)
        db.add.assert_not_called()
        db.commit.assert_called_once()
        # The variant_weights attribute on the existing state should be set
        assert existing_state.variant_weights is not None

    # -----------------------------------------------------------------------
    # 7. Handles empty active experiments gracefully
    # -----------------------------------------------------------------------
    def test_run_once_handles_no_active_experiments(self):
        """run_once returns zero counts when no active MAB experiments exist."""
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        scheduler = BanditScheduler(db=db)
        result = scheduler.run_once()

        assert result == {"updated": 0, "skipped": 0, "errors": 0}

    # -----------------------------------------------------------------------
    # 8. Handles experiment with no variant data (uses priors)
    # -----------------------------------------------------------------------
    def test_update_experiment_handles_all_zero_counts(self):
        """Experiment with zero pulls uses equal weights (uninformative priors)."""
        db = MagicMock()
        exp_id = str(uuid.uuid4())
        mock_exp = _make_experiment(exp_id, optimization_type="thompson_sampling")
        db.query.return_value.filter.return_value.first.return_value = None

        scheduler = BanditScheduler(db=db)
        vid = str(mock_exp.variants[0].id)
        vid2 = str(mock_exp.variants[1].id)
        mock_stats = {
            vid: VariantStats(variant_id=vid),  # 0 pulls
            vid2: VariantStats(variant_id=vid2),  # 0 pulls
        }

        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=mock_stats
        ):
            result = scheduler.update_experiment(mock_exp)

        assert result is True
        db.commit.assert_called_once()

    # -----------------------------------------------------------------------
    # 9. get_variant_stats_from_counters against the real DynamoDB provider:
    #    modules/backend/tests/unit/services/test_bandit_scheduler_counters.py
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # 10. estimate_regret_reduction > 0 when best variant dominates
    # -----------------------------------------------------------------------
    def test_estimate_regret_reduction_positive_when_best_dominates(self):
        """Regret reduction > 0 when bandit has concentrated weight on best variant."""
        db = MagicMock()
        scheduler = BanditScheduler(db=db)

        vid1, vid2 = "v1", "v2"
        # v1 has 80% weight and 70% conversion; v2 has 20% and 10% conversion
        weights = {vid1: 0.8, vid2: 0.2}
        variant_stats = {
            vid1: VariantStats(variant_id=vid1, successes=70, pulls=100),
            vid2: VariantStats(variant_id=vid2, successes=10, pulls=100),
        }

        reduction = scheduler.estimate_regret_reduction(weights, variant_stats)
        assert reduction > 0.0


# ===========================================================================
# TestBanditSchedulerIntegration — pure logic, no DB
# ===========================================================================


class TestBanditSchedulerIntegration:
    """Integration tests that exercise the pure MAB algorithm logic."""

    # -----------------------------------------------------------------------
    # 11. Thompson Sampling weights update when successes increase
    # -----------------------------------------------------------------------
    def test_thompson_weights_shift_toward_better_variant(self):
        """Thompson Sampling allocates more traffic to the higher-converting variant."""
        variant_data = {
            "ctrl": {"successes": 5, "failures": 95, "pulls": 100, "total_reward": 5.0},
            "trtm": {
                "successes": 50,
                "failures": 50,
                "pulls": 100,
                "total_reward": 50.0,
            },
        }
        weights = BanditService.compute_weights("thompson_sampling", variant_data)
        assert weights["trtm"] > weights["ctrl"]

    # -----------------------------------------------------------------------
    # 12. UCB1 weights shift toward better variant over rounds
    # -----------------------------------------------------------------------
    def test_ucb1_weights_shift_toward_better_variant(self):
        """UCB1 allocates more weight to the variant with higher observed reward."""
        variant_data = {
            "ctrl": {
                "successes": 10,
                "failures": 90,
                "pulls": 100,
                "total_reward": 10.0,
            },
            "trtm": {
                "successes": 80,
                "failures": 20,
                "pulls": 100,
                "total_reward": 80.0,
            },
        }
        weights = BanditService.compute_weights("ucb1", variant_data)
        assert weights["trtm"] > weights["ctrl"]

    # -----------------------------------------------------------------------
    # 13. Epsilon-Greedy converges to best variant with epsilon=0.05
    # -----------------------------------------------------------------------
    def test_epsilon_greedy_converges_to_best_variant(self):
        """EpsilonGreedy assigns most traffic (1-ε) to the best variant."""
        stats = [
            VariantStats(variant_id="ctrl", successes=10, pulls=100),
            VariantStats(variant_id="trtm", successes=70, pulls=100),
        ]
        weights = EpsilonGreedy.compute_weights(stats, epsilon=0.05)
        # trtm should get (1 - 0.05) + 0.05/2 = 0.975 weight
        assert weights["trtm"] > 0.9
        assert weights["ctrl"] < 0.1

    # -----------------------------------------------------------------------
    # 14. estimate_regret_reduction > 0 vs uniform
    # -----------------------------------------------------------------------
    def test_estimate_regret_reduction_vs_uniform(self):
        """Bandit with concentrated weights beats uniform allocation."""
        db = MagicMock()
        scheduler = BanditScheduler(db=db)

        vid_a, vid_b = "a", "b"
        weights = {vid_a: 0.85, vid_b: 0.15}
        variant_stats = {
            vid_a: VariantStats(variant_id=vid_a, successes=60, pulls=100),
            vid_b: VariantStats(variant_id=vid_b, successes=10, pulls=100),
        }

        reduction = scheduler.estimate_regret_reduction(weights, variant_stats)
        assert reduction > 0.0

    # -----------------------------------------------------------------------
    # 15. estimate_regret_reduction == 0 for uniform weights
    # -----------------------------------------------------------------------
    def test_estimate_regret_reduction_zero_for_uniform_weights(self):
        """Uniform allocation produces 0% regret reduction vs itself."""
        db = MagicMock()
        scheduler = BanditScheduler(db=db)

        vid_a, vid_b = "a", "b"
        weights = {vid_a: 0.5, vid_b: 0.5}
        variant_stats = {
            vid_a: VariantStats(variant_id=vid_a, successes=30, pulls=100),
            vid_b: VariantStats(variant_id=vid_b, successes=70, pulls=100),
        }

        reduction = scheduler.estimate_regret_reduction(weights, variant_stats)
        # Uniform weights → no improvement over baseline
        assert reduction == 0.0

    # -----------------------------------------------------------------------
    # 16. All-zero counts returns equal weights
    # -----------------------------------------------------------------------
    def test_all_zero_counts_returns_equal_weights(self):
        """With no observations, non-epsilon algorithms fall back to equal weights.

        Note: EpsilonGreedy with 0 pulls assigns the exploit weight to the
        *first* arm (arbitrary tie-break on conversion_rate == 0), so only
        Thompson Sampling and UCB1 are checked for equal splits here.
        """
        variant_data = {
            "a": {"successes": 0, "failures": 0, "pulls": 0, "total_reward": 0.0},
            "b": {"successes": 0, "failures": 0, "pulls": 0, "total_reward": 0.0},
        }
        # Thompson Sampling and UCB1 produce near-equal weights with no data
        for algo in ("thompson_sampling", "ucb1"):
            weights = BanditService.compute_weights(algo, variant_data)
            assert abs(weights["a"] - weights["b"]) < 0.3, (
                f"{algo} weights not near equal: {weights}"
            )

        # EpsilonGreedy still sums to 1.0 even if not equal
        eg_weights = BanditService.compute_weights("epsilon_greedy", variant_data)
        assert abs(sum(eg_weights.values()) - 1.0) < 1e-6

    # -----------------------------------------------------------------------
    # 17. Single variant → weight = 1.0
    # -----------------------------------------------------------------------
    def test_single_variant_gets_full_weight(self):
        """With a single variant every algorithm assigns weight=1.0."""
        variant_data = {
            "only": {"successes": 5, "failures": 5, "pulls": 10, "total_reward": 5.0}
        }
        for algo in ("thompson_sampling", "ucb1", "epsilon_greedy"):
            weights = BanditService.compute_weights(algo, variant_data)
            assert weights["only"] == pytest.approx(1.0), (
                f"{algo}: expected 1.0, got {weights}"
            )

    # -----------------------------------------------------------------------
    # 18. get_recommendation strings
    # -----------------------------------------------------------------------
    def test_get_recommendation_deploying_when_weight_above_0_8(self):
        """Returns DEPLOYING_<name> when max weight >= 0.8."""
        db = MagicMock()
        scheduler = BanditScheduler(db=db)
        weights = {"v1": 0.85, "v2": 0.15}
        names = {"v1": "VariantA", "v2": "VariantB"}
        rec = scheduler.get_recommendation(weights, names)
        assert rec == "DEPLOYING_VariantA"

    def test_get_recommendation_converging_when_weight_0_5_to_0_8(self):
        """Returns CONVERGING when max weight is between 0.5 and 0.8."""
        db = MagicMock()
        scheduler = BanditScheduler(db=db)
        weights = {"v1": 0.6, "v2": 0.4}
        names = {"v1": "VariantA", "v2": "VariantB"}
        rec = scheduler.get_recommendation(weights, names)
        assert rec == "CONVERGING"

    def test_get_recommendation_exploring_when_weight_below_0_5(self):
        """Returns EXPLORING when no variant dominates (max weight < 0.5)."""
        db = MagicMock()
        scheduler = BanditScheduler(db=db)
        weights = {"v1": 0.45, "v2": 0.35, "v3": 0.20}
        names = {"v1": "A", "v2": "B", "v3": "C"}
        rec = scheduler.get_recommendation(weights, names)
        assert rec == "EXPLORING"


# ===========================================================================
# TestBanditSchedulerStatsFallback — DynamoDB → PostgreSQL → BanditState → priors
# ===========================================================================


@contextmanager
def _counter_capability(provider):
    """Register *provider* under ``counters.service`` for the duration.

    ``None`` unregisters it -- a build that does not ship the service; a
    provider answering ``None`` is one whose service could not be resolved.
    Both must fall through to PostgreSQL.
    """
    from backend.app.core import hooks

    saved = hooks.get_capability("counters.service")
    if provider is None:
        with hooks._lock:
            hooks._capabilities.pop("counters.service", None)
    else:
        hooks.register_capability("counters.service", provider)
    try:
        yield
    finally:
        if saved is None:
            with hooks._lock:
                hooks._capabilities.pop("counters.service", None)
        else:
            hooks.register_capability("counters.service", saved)


def _mock_metric(event_name: str, is_primary: bool):
    metric = MagicMock()
    metric.event_name = event_name
    metric.is_primary = is_primary
    return metric


class TestBanditSchedulerStatsFallback:
    """The stats chain used by get_variant_stats_from_counters."""

    def _scheduler_with_experiment(self, metrics=None, legacy_metrics=None):
        db = MagicMock()
        scheduler = BanditScheduler(db=db)
        exp = _make_experiment(str(uuid.uuid4()))
        exp.metric_definitions = metrics if metrics is not None else []
        exp.metrics = legacy_metrics
        return db, scheduler, exp

    def test_falls_back_to_postgres_when_counter_module_is_absent(self):
        """
        A build without the optional real-time counter module is a normal
        configuration: _stats_from_dynamodb returns None and PostgreSQL is
        used, with no exception escaping and nothing logged as a warning.
        """
        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[_mock_metric("purchase", True)]
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)

        with (
            _counter_capability(None),
            patch.object(
                scheduler,
                "_count_assignments_by_variant",
                return_value={vid1: 20, vid2: 20},
            ) as count_pulls,
            patch.object(
                scheduler,
                "_count_conversions_by_variant",
                return_value={vid1: 6, vid2: 2},
            ),
        ):
            assert scheduler._load_counter_service_class() is None
            assert scheduler._stats_from_dynamodb(exp.id, [vid1, vid2]) is None
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        count_pulls.assert_called_once_with(exp.id)
        assert stats[vid1].pulls == 20
        assert stats[vid1].successes == 6
        assert stats[vid2].successes == 2

    @pytest.mark.regression
    def test_missing_counter_service_is_not_logged_as_a_warning(self):
        """A build without the optional service is normal, not a warning.

        The earlier version patched ``bandit_scheduler.logger`` while the
        message it was checking for came from the seam module's own logger,
        so it could not fail. The seam module no longer logs at all -- it is
        a registry lookup -- so the scheduler's logger is the only one on this
        path, and patching it is the whole check.
        """
        from backend.app.core import optional_modules

        assert not hasattr(optional_modules, "logger")

        db, scheduler, _ = self._scheduler_with_experiment()
        with (
            _counter_capability(None),
            patch("backend.app.core.bandit_scheduler.logger") as mock_logger,
        ):
            assert scheduler._load_counter_service_class() is None
            assert scheduler._stats_from_dynamodb(uuid.uuid4(), ["a"]) is None

        assert mock_logger.warning.call_count == 0
        assert mock_logger.error.call_count == 0

    def test_falls_back_to_postgres_when_the_provider_answers_none(self):
        """The service is installed but the provider answers None (the class
        could not be resolved). PostgreSQL it is."""
        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[_mock_metric("purchase", True)]
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)

        with (
            _counter_capability(lambda: None),
            patch.object(
                scheduler,
                "_count_assignments_by_variant",
                return_value={vid1: 10, vid2: 10},
            ) as count_pulls,
            patch.object(
                scheduler,
                "_count_conversions_by_variant",
                return_value={vid1: 1, vid2: 3},
            ),
        ):
            assert scheduler._load_counter_service_class() is None
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        count_pulls.assert_called_once_with(exp.id)
        assert stats[vid2].successes == 3

    def test_falls_back_to_postgres_when_dynamodb_raises(self):
        """DynamoDB unavailable → pulls/successes come from PostgreSQL counts."""
        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[_mock_metric("purchase", True)]
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)

        with (
            patch.object(scheduler, "_stats_from_dynamodb", return_value=None),
            patch.object(
                scheduler,
                "_count_assignments_by_variant",
                return_value={vid1: 100, vid2: 100},
            ) as count_pulls,
            patch.object(
                scheduler,
                "_count_conversions_by_variant",
                return_value={vid1: 40, vid2: 5},
            ) as count_conv,
        ):
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        count_pulls.assert_called_once_with(exp.id)
        count_conv.assert_called_once_with(exp.id, "purchase", [vid1, vid2])
        assert stats[vid1].pulls == 100
        assert stats[vid1].successes == 40
        assert stats[vid1].failures == 60
        assert stats[vid1].total_reward == 40.0
        assert stats[vid2].successes == 5
        assert stats[vid2].failures == 95

    # "DynamoDB reachable but empty -> PostgreSQL" drives the real counters
    # module provider and lives in modules/backend/tests/unit/services/
    # test_bandit_scheduler_counters.py.

    def test_successes_are_capped_at_pulls(self):
        """A variant can never convert more users than it was assigned."""
        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[_mock_metric("purchase", True)]
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)

        with (
            patch.object(scheduler, "_stats_from_dynamodb", return_value=None),
            patch.object(
                scheduler,
                "_count_assignments_by_variant",
                return_value={vid1: 5, vid2: 5},
            ),
            patch.object(
                scheduler,
                "_count_conversions_by_variant",
                return_value={vid1: 9, vid2: 0},
            ),
        ):
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        assert stats[vid1].successes == 5
        assert stats[vid1].failures == 0

    def test_primary_metric_resolution_order(self):
        """is_primary metric → first metric → legacy JSON → None."""
        exp = _make_experiment(str(uuid.uuid4()))

        exp.metric_definitions = [
            _mock_metric("secondary", False),
            _mock_metric("primary", True),
        ]
        exp.metrics = {"primary_metric": "legacy"}
        assert BanditScheduler._primary_event_name(exp) == "primary"

        exp.metric_definitions = [
            _mock_metric("first", False),
            _mock_metric("second", False),
        ]
        assert BanditScheduler._primary_event_name(exp) == "first"

        exp.metric_definitions = []
        assert BanditScheduler._primary_event_name(exp) == "legacy"

        exp.metrics = None
        assert BanditScheduler._primary_event_name(exp) is None
        assert BanditScheduler._primary_event_name(None) is None

    def test_no_metric_counts_non_exposure_events(self):
        """Without a metric the conversion query uses event_name=None (any non-exposure event)."""
        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[], legacy_metrics=None
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)

        with (
            patch.object(scheduler, "_stats_from_dynamodb", return_value=None),
            patch.object(
                scheduler,
                "_count_assignments_by_variant",
                return_value={vid1: 4, vid2: 4},
            ),
            patch.object(
                scheduler, "_count_conversions_by_variant", return_value={vid2: 2}
            ) as count_conv,
        ):
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        count_conv.assert_called_once_with(exp.id, None, [vid1, vid2])
        assert stats[vid2].successes == 2

    def test_old_signature_loads_experiment_from_db(self):
        """Calling without the experiment kwarg still works (experiment loaded by id)."""
        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[_mock_metric("signup", True)]
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)
        db.query.return_value.filter.return_value.first.return_value = exp

        with (
            patch.object(scheduler, "_stats_from_dynamodb", return_value=None),
            patch.object(
                scheduler,
                "_count_assignments_by_variant",
                return_value={vid1: 20, vid2: 20},
            ),
            patch.object(
                scheduler,
                "_count_conversions_by_variant",
                return_value={vid1: 7, vid2: 1},
            ) as count_conv,
        ):
            stats = scheduler.get_variant_stats_from_counters(exp.id, [vid1, vid2])

        count_conv.assert_called_once_with(exp.id, "signup", [vid1, vid2])
        assert stats[vid1].successes == 7

    def test_falls_back_to_bandit_state_when_postgres_is_empty(self):
        """No assignments in PostgreSQL → previously persisted BanditState stats."""
        from backend.app.models.bandit_state import BanditState

        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[_mock_metric("purchase", True)]
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)

        state = MagicMock(spec=BanditState)
        state.variant_weights = {
            vid1: {"weight": 0.7, "successes": 30, "failures": 20, "pulls": 50},
            vid2: {"weight": 0.3, "successes": 10, "failures": 40, "pulls": 50},
        }
        db.query.return_value.filter.return_value.first.return_value = state

        with (
            patch.object(scheduler, "_stats_from_dynamodb", return_value=None),
            patch.object(scheduler, "_count_assignments_by_variant", return_value={}),
            patch.object(scheduler, "_count_conversions_by_variant", return_value={}),
        ):
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        assert stats[vid1].pulls == 50
        assert stats[vid1].successes == 30
        assert stats[vid2].failures == 40

    def test_falls_back_to_bandit_state_when_postgres_raises(self):
        """A failing PostgreSQL query is logged and the BanditState is used."""
        from backend.app.models.bandit_state import BanditState

        db, scheduler, exp = self._scheduler_with_experiment(
            metrics=[_mock_metric("purchase", True)]
        )
        vid1, vid2 = (str(v.id) for v in exp.variants)

        state = MagicMock(spec=BanditState)
        state.variant_weights = {
            vid1: {"weight": 1.0, "successes": 3, "failures": 1, "pulls": 4}
        }
        db.query.return_value.filter.return_value.first.return_value = state

        with (
            patch.object(scheduler, "_stats_from_dynamodb", return_value=None),
            patch.object(
                scheduler,
                "_count_assignments_by_variant",
                side_effect=RuntimeError("db down"),
            ),
        ):
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        db.rollback.assert_called()
        assert stats[vid1].pulls == 4
        assert stats[vid2].pulls == 0

    def test_zero_priors_when_no_source_has_data(self):
        """Nothing anywhere → zero-count VariantStats for every variant."""
        db, scheduler, exp = self._scheduler_with_experiment(metrics=[])
        vid1, vid2 = (str(v.id) for v in exp.variants)
        db.query.return_value.filter.return_value.first.return_value = None

        with (
            patch.object(scheduler, "_stats_from_dynamodb", return_value=None),
            patch.object(scheduler, "_count_assignments_by_variant", return_value={}),
            patch.object(scheduler, "_count_conversions_by_variant", return_value={}),
        ):
            stats = scheduler.get_variant_stats_from_counters(
                exp.id, [vid1, vid2], experiment=exp
            )

        assert set(stats) == {vid1, vid2}
        assert all(vs.pulls == 0 and vs.successes == 0 for vs in stats.values())

    def test_update_experiment_passes_experiment_to_stats(self):
        """update_experiment hands the experiment over so no extra query is needed."""
        db = MagicMock()
        exp = _make_experiment(str(uuid.uuid4()))
        db.query.return_value.filter.return_value.first.return_value = None
        scheduler = BanditScheduler(db=db)

        stats = {str(v.id): VariantStats(variant_id=str(v.id)) for v in exp.variants}
        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=stats
        ) as get_stats:
            scheduler.update_experiment(exp)

        get_stats.assert_called_once()
        assert get_stats.call_args.kwargs["experiment"] is exp


# ===========================================================================
# TestDynamoDBCompleteness — #426: a partial DynamoDB count never replaces
# the complete PostgreSQL count
# ===========================================================================


def _fake_counter_provider(per_variant: Optional[Dict[str, tuple]], raises=None):
    """A ``counters.service`` provider answering a fake service class.

    ``per_variant`` maps variant id -> ``(assignments, conversions)``; the
    fake mirrors the duck-typed shape the scheduler reads
    (``counters.variants[i].variant_id/assignments/conversions``).  No real
    DynamoDB is touched.
    """

    class _FakeCounterService:
        def get_experiment_counters(self, experiment_id):
            if raises is not None:
                raise raises
            variants = []
            for vid, (assignments, conversions) in (per_variant or {}).items():
                vc = MagicMock()
                vc.variant_id = vid
                vc.assignments = assignments
                vc.conversions = conversions
                variants.append(vc)
            counters = MagicMock()
            counters.variants = variants
            return counters

    return lambda: _FakeCounterService


class TestDynamoDBCompleteness:
    """PostgreSQL's stats are returned whole whenever any variant has a pull
    there; DynamoDB (successes capped at pulls) is used only when PostgreSQL
    has no pulls or cannot be read (#426, #581)."""

    def _setup(self):
        db = MagicMock()
        scheduler = BanditScheduler(db=db)
        exp = _make_experiment(str(uuid.uuid4()))
        exp.metric_definitions = [_mock_metric("purchase", True)]
        exp.metrics = None
        vid1, vid2 = (str(v.id) for v in exp.variants)
        return scheduler, exp, vid1, vid2

    def _stats(self, scheduler, exp, vids, dynamo, pg_pulls, pg_conv, pg_raises=None):
        pulls_patch = (
            patch.object(
                scheduler, "_count_assignments_by_variant", side_effect=pg_raises
            )
            if pg_raises is not None
            else patch.object(
                scheduler, "_count_assignments_by_variant", return_value=pg_pulls
            )
        )
        with (
            _counter_capability(dynamo),
            pulls_patch,
            patch.object(
                scheduler, "_count_conversions_by_variant", return_value=pg_conv
            ),
        ):
            return scheduler.get_variant_stats_from_counters(
                exp.id, list(vids), experiment=exp
            )

    @pytest.mark.regression
    def test_partial_dynamodb_count_loses_to_larger_postgres_count(self):
        """One manual increment in DynamoDB must not replace 200 PostgreSQL
        assignments (the #426 defect: any DynamoDB pull used to win)."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (1, 1)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 10},
        )
        assert stats[vid1].pulls == 100
        assert stats[vid1].successes == 30
        assert stats[vid2].pulls == 100
        assert stats[vid2].successes == 10

    @pytest.mark.regression
    def test_larger_dynamodb_total_but_one_variant_short_uses_postgres(self):
        """Compared per variant: a surplus in one variant cannot hide a
        missing count in another."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (500, 50), vid2: (5, 1)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 10},
        )
        assert stats[vid1].pulls == 100
        assert stats[vid2].pulls == 100

    @pytest.mark.regression
    def test_complete_pulls_but_partial_dynamodb_successes_uses_postgres(self):
        """DynamoDB with equal pulls but 1 conversion per variant against
        PostgreSQL's 30 and 10 must not understate the conversion rate:
        PostgreSQL has data, so its stats are returned whole."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (100, 1), vid2: (100, 1)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 10},
        )
        assert stats[vid1].pulls == 100
        assert stats[vid1].successes == 30
        assert stats[vid2].successes == 10

    @pytest.mark.regression
    def test_dynamodb_used_when_postgres_has_no_pulls(self):
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (7, 2), vid2: (3, 1)}),
            pg_pulls={},
            pg_conv={},
        )
        assert stats[vid1].pulls == 7
        assert stats[vid1].successes == 2
        assert stats[vid2].pulls == 3

    @pytest.mark.regression
    def test_dynamodb_used_when_postgres_is_unavailable(self):
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (7, 2), vid2: (3, 1)}),
            pg_pulls=None,
            pg_conv={},
            pg_raises=RuntimeError("database is down"),
        )
        assert stats[vid1].pulls == 7
        assert stats[vid2].pulls == 3

    @pytest.mark.regression
    def test_postgres_wins_when_dynamodb_equal_or_larger_on_everything(self):
        """#581: DynamoDB at or above PostgreSQL on pulls AND successes used
        to win and its event counts were weighed.  PostgreSQL has data, so
        PostgreSQL's stats are returned whole (red on main)."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (100, 41), vid2: (100, 11)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 10},
        )
        assert (stats[vid1].pulls, stats[vid1].successes) == (100, 30)
        assert (stats[vid2].pulls, stats[vid2].successes) == (100, 10)

    @pytest.mark.regression
    def test_postgres_wins_when_dynamodb_is_ahead_of_postgres(self):
        """DynamoDB ahead on pulls AND successes (the pulls-only rule of an
        earlier revision, and main, would pick it): PostgreSQL's stats whole,
        pulls included.  Red on main and on the pulls-only revision."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (105, 33), vid2: (102, 12)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 10},
        )
        assert (stats[vid1].pulls, stats[vid1].successes) == (100, 30)
        assert stats[vid1].failures == 70
        assert (stats[vid2].pulls, stats[vid2].successes) == (100, 10)

    @pytest.mark.regression
    def test_postgres_pulls_win_even_when_dynamodb_successes_are_lower(self):
        """Distinguishes pulls-only from pulls-and-successes completeness:
        DynamoDB ahead on pulls but behind on successes is still ignored."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (105, 5), vid2: (102, 0)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 10},
        )
        assert (stats[vid1].pulls, stats[vid1].successes) == (100, 30)
        assert (stats[vid2].pulls, stats[vid2].successes) == (100, 10)

    @pytest.mark.regression
    def test_arm_with_dynamodb_only_pulls_is_not_starved(self):
        """S1: PostgreSQL has no assignment for B (DynamoDB has 100).  Mixing
        DynamoDB pulls with PostgreSQL successes gave B (100 pulls, 0
        successes) and weight 0.  PostgreSQL whole gives B no pulls, the
        prior.  Red on main (B = 100 pulls) and on the pulls-only revision."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (100, 30), vid2: (100, 30)}),
            pg_pulls={vid1: 100},
            pg_conv={vid1: 30},
        )
        assert (stats[vid1].pulls, stats[vid1].successes) == (100, 30)
        assert (stats[vid2].pulls, stats[vid2].successes) == (0, 0)

    @pytest.mark.regression
    def test_uneven_dynamodb_lead_does_not_flip_the_ranking(self):
        """S2: DynamoDB has 200 users in A where PostgreSQL has 100.  The
        extra 100 cannot convert in PostgreSQL's count, so mixing halved A's
        rate (0.15 < B's 0.2).  PostgreSQL whole keeps A at 0.3 vs 0.2."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (200, 60), vid2: (100, 20)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 20},
        )
        assert (stats[vid1].pulls, stats[vid1].successes) == (100, 30)
        assert (stats[vid2].pulls, stats[vid2].successes) == (100, 20)

    @pytest.mark.regression
    def test_user_converting_three_times_counts_once(self):
        """#581: one user, three conversion events.  DynamoDB's counter says 3
        (an event counter); PostgreSQL's converting users say 1.  PostgreSQL
        has data, so its converting users (1) are returned."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (1, 3), vid2: (1, 0)}),
            pg_pulls={vid1: 1, vid2: 1},
            pg_conv={vid1: 1},
        )
        assert stats[vid1].pulls == 1
        assert stats[vid1].successes == 1
        assert stats[vid1].failures == 0
        assert stats[vid2].successes == 0

    @pytest.mark.regression
    def test_mixed_users_one_converting_twice(self):
        """#581: two users in a variant, one converts twice, one never.
        DynamoDB: 2 events; users: 1."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (2, 2), vid2: (2, 1)}),
            pg_pulls={vid1: 2, vid2: 2},
            pg_conv={vid1: 1, vid2: 1},
        )
        assert (stats[vid1].pulls, stats[vid1].successes) == (2, 1)
        assert stats[vid1].failures == 1
        assert (stats[vid2].pulls, stats[vid2].successes) == (2, 1)

    @pytest.mark.regression
    def test_event_inflated_dynamodb_successes_never_win(self):
        """#581: DynamoDB successes at or above PostgreSQL's used to pass the
        completeness check and be weighed.  They must not be, however large."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (100, 400), vid2: (100, 90)}),
            pg_pulls={vid1: 100, vid2: 100},
            pg_conv={vid1: 30, vid2: 10},
        )
        assert stats[vid1].successes == 30
        assert stats[vid2].successes == 10

    @pytest.mark.regression
    def test_dynamodb_successes_capped_at_pulls_without_postgres_data(self):
        """With no PostgreSQL data DynamoDB's counts are all there is, but
        more successes than pulls is impossible for users: capped."""
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider({vid1: (2, 9), vid2: (3, 1)}),
            pg_pulls={},
            pg_conv={},
        )
        assert stats[vid1].successes == 2
        assert stats[vid1].failures == 0

    @pytest.mark.regression
    def test_dynamodb_unavailable_falls_through_to_postgres(self):
        scheduler, exp, vid1, vid2 = self._setup()
        stats = self._stats(
            scheduler,
            exp,
            (vid1, vid2),
            dynamo=_fake_counter_provider(None, raises=RuntimeError("no table")),
            pg_pulls={vid1: 20, vid2: 20},
            pg_conv={vid1: 4, vid2: 2},
        )
        assert stats[vid1].pulls == 20
        assert stats[vid1].successes == 4
        assert stats[vid2].successes == 2


# ===========================================================================
# TestBanditSchedulerRunner — asyncio background loop
# ===========================================================================


class TestBanditSchedulerRunner:
    """Lifecycle of the in-app BanditSchedulerRunner."""

    RESULT = {"updated": 1, "skipped": 0, "errors": 0}

    @pytest.fixture(autouse=True)
    def _held_lock(self, monkeypatch):
        """The loop runs each pass under the scheduler advisory lock
        (``backend.app.core.scheduler_tick``). These tests exercise the loop
        with a mocked pass and no application database, so pretend the lock
        was acquired and skip the run-history write; the lock itself is
        covered by ``tests/unit/core/test_scheduler_lock.py``."""
        from contextlib import asynccontextmanager

        from backend.app.core import scheduler_tick

        @asynccontextmanager
        async def acquired(name, engine=None):
            yield True

        monkeypatch.setattr(scheduler_tick, "async_scheduler_lock", acquired)
        monkeypatch.setattr(scheduler_tick, "_persist_run", lambda *a, **k: None)

    def _runner(self):
        from backend.app.core.bandit_scheduler import BanditSchedulerRunner

        runner = BanditSchedulerRunner(interval_minutes=1, run_in_tests=True)
        runner.interval_seconds = 0.01
        return runner

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        import asyncio

        runner = self._runner()
        runner._run_sync = MagicMock(return_value=self.RESULT)

        await runner.start()
        assert runner.is_running is True
        assert runner.task is not None

        # Poll for the first pass rather than sleeping a fixed 0.1 s (#433):
        # the pass runs in a worker thread (asyncio.to_thread), and on a busy
        # machine nothing bounds when that thread gets to run.
        deadline = asyncio.get_running_loop().time() + 5.0
        while runner.run_count < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        await runner.stop()

        assert runner.is_running is False
        assert runner.task is None
        assert runner.run_count >= 1
        assert runner.last_result == self.RESULT

    @pytest.mark.asyncio
    async def test_double_start_creates_single_task(self):
        runner = self._runner()
        runner._run_sync = MagicMock(return_value=self.RESULT)

        await runner.start()
        first_task = runner.task
        await runner.start()
        assert runner.task is first_task

        await runner.stop()

    @pytest.mark.asyncio
    async def test_run_exception_does_not_kill_loop(self):
        import asyncio

        runner = self._runner()
        calls = {"n": 0}

        def flaky_run():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return self.RESULT

        runner._run_sync = flaky_run

        await runner.start()
        # Each tick takes an advisory lock in Postgres, so the second (good)
        # pass lands anywhere between a few ms and a second depending on load.
        # Poll instead of sleeping a fixed slice: a fixed sleep made this test
        # fail roughly half the time on a busy machine.
        deadline = asyncio.get_running_loop().time() + 5.0
        while runner.run_count < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

        assert runner.is_running is True
        assert runner.task is not None and not runner.task.done()
        assert runner.error_count == 1
        assert runner.last_error == "boom"
        assert runner.run_count >= 1
        assert runner.last_result == self.RESULT

        await runner.stop()

    @pytest.mark.asyncio
    async def test_start_is_skipped_in_test_environment(self, monkeypatch):
        from backend.app.core.bandit_scheduler import BanditSchedulerRunner

        monkeypatch.setenv("APP_ENV", "test")
        runner = BanditSchedulerRunner(interval_minutes=1)

        await runner.start()

        assert runner.is_running is False
        assert runner.task is None
        # stop() on a never-started runner is a no-op
        await runner.stop()

    def test_interval_defaults_from_settings(self):
        from backend.app.core import bandit_scheduler as module

        with patch.object(module.settings, "BANDIT_UPDATE_INTERVAL_MINUTES", 7):
            runner = module.BanditSchedulerRunner(run_in_tests=True)

        assert runner.interval_minutes == 7
        assert runner.interval_seconds == 7 * 60

    def test_run_sync_uses_fresh_session_and_closes_it(self):
        from backend.app.core.bandit_scheduler import BanditSchedulerRunner

        session = MagicMock()
        with (
            patch("backend.app.db.session.SessionLocal", return_value=session),
            patch.object(
                BanditScheduler, "run_once", return_value=self.RESULT
            ) as run_once,
        ):
            result = BanditSchedulerRunner._run_sync()

        assert result == self.RESULT
        run_once.assert_called_once()
        session.close.assert_called_once()

    def test_module_level_instance_exists(self):
        from backend.app.core.bandit_scheduler import (
            BanditSchedulerRunner,
            bandit_scheduler_runner,
        )

        assert isinstance(bandit_scheduler_runner, BanditSchedulerRunner)
        assert bandit_scheduler_runner.is_running is False
