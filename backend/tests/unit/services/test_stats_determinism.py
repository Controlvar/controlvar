"""
Determinism of every Monte Carlo path (P0 statistical credibility).

Covers ``backend.app.core.stats_engine`` (seed derivation), the Bayesian
service (PtBB, expected loss, ROPE stopping, ``analyze``), Thompson sampling
in the bandit service, the seed threading through ``AnalysisService`` and
``BanditScheduler``, and the provenance fields on the response schemas.
"""

import uuid
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.core.stats_engine import (
    ENGINE_VERSION,
    as_of_bucket,
    derive_seed,
    make_rng,
)
from backend.app.schemas.bandit import BanditStatusResponse
from backend.app.schemas.bayesian import (
    BayesianConfig,
    BayesianDecision,
    BayesianResultsResponse,
)
from backend.app.schemas.variance_reduction import (
    CupedResultsResponse,
    VarianceReductionMethod,
)
from backend.app.services import bayesian_service
from backend.app.services.analysis_service import BAYESIAN_N_SAMPLES, AnalysisService
from backend.app.services.bandit_service import (
    BanditService,
    ThompsonSampling,
    VariantStats,
)
from backend.app.services.bayesian_service import (
    BayesianService,
    compute_expected_loss,
    compute_probability_to_be_best,
    should_stop,
)

EXP_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
POSTERIORS = [
    {"alpha": 101.0, "beta": 901.0},
    {"alpha": 116.0, "beta": 886.0},
    {"alpha": 95.0, "beta": 905.0},
]


# ---------------------------------------------------------------------------
# stats_engine
# ---------------------------------------------------------------------------


class TestStatsEngine:
    def test_engine_version_is_semver_string(self):
        # That it is the last frozen version is pinned by
        # test_engine_versions_frozen.py, not by a literal here.
        assert len(ENGINE_VERSION.split(".")) == 3

    def test_derive_seed_is_stable_and_63_bit(self):
        seed = derive_seed(EXP_ID, "2026-09-11", 100_000)
        assert isinstance(seed, int)
        assert 0 <= seed < 2**63
        # Pinned value: changing the derivation is a breaking change for
        # persisted snapshots and must bump ENGINE_VERSION.
        assert seed == derive_seed(str(EXP_ID), "2026-09-11", 100_000)
        assert seed == 5953050452947335919

    def test_derive_seed_changes_with_each_input(self):
        base = derive_seed(EXP_ID, "2026-09-11", 100_000)
        assert derive_seed(uuid.uuid4(), "2026-09-11", 100_000) != base
        assert derive_seed(EXP_ID, "2026-09-12", 100_000) != base
        assert derive_seed(EXP_ID, "2026-09-11", 10_000) != base

    def test_as_of_bucket_is_utc_calendar_day(self):
        aware = datetime(2026, 9, 11, 23, 30, tzinfo=timezone.utc)
        assert as_of_bucket(aware) == "2026-09-11"
        # Naive datetimes are treated as UTC; aware ones are converted.
        assert as_of_bucket(datetime(2026, 9, 11, 0, 5)) == "2026-09-11"
        from datetime import timedelta, tzinfo

        plus_two = timezone(timedelta(hours=2))
        assert (
            as_of_bucket(datetime(2026, 9, 12, 1, 0, tzinfo=plus_two)) == "2026-09-11"
        )
        assert as_of_bucket(date(2026, 9, 11)) == "2026-09-11"
        assert as_of_bucket() == datetime.now(timezone.utc).date().isoformat()

    def test_make_rng_reproduces_byte_identical_draws(self):
        seed = derive_seed(EXP_ID, "2026-09-11", 1000)
        a = make_rng(seed).beta(2.0, 5.0, 1000)
        b = make_rng(seed).beta(2.0, 5.0, 1000)
        assert a.tobytes() == b.tobytes()
        assert isinstance(make_rng(seed), np.random.Generator)


# ---------------------------------------------------------------------------
# Bayesian service
# ---------------------------------------------------------------------------


class TestBayesianDeterminism:
    def test_same_seed_gives_identical_arrays(self):
        seed = derive_seed(EXP_ID, "2026-09-11", 50_000)
        p1 = compute_probability_to_be_best(POSTERIORS, n_samples=50_000, seed=seed)
        p2 = compute_probability_to_be_best(POSTERIORS, n_samples=50_000, seed=seed)
        l1 = compute_expected_loss(POSTERIORS, n_samples=50_000, seed=seed)
        l2 = compute_expected_loss(POSTERIORS, n_samples=50_000, seed=seed)
        assert p1 == p2  # exact, not approx
        assert l1 == l2

    def test_different_as_of_gives_different_draws(self):
        s1 = derive_seed(EXP_ID, "2026-09-11", 50_000)
        s2 = derive_seed(EXP_ID, "2026-09-12", 50_000)
        assert s1 != s2
        samples_1 = bayesian_service._draw_posterior_samples(POSTERIORS, 50_000, s1)
        samples_2 = bayesian_service._draw_posterior_samples(POSTERIORS, 50_000, s2)
        assert samples_1.tobytes() != samples_2.tobytes()

    def test_draws_are_default_rng_not_global_state(self):
        """The global ``np.random`` seed must have no effect on the output."""
        seed = derive_seed(EXP_ID, "2026-09-11", 20_000)
        np.random.seed(1)
        first = compute_probability_to_be_best(POSTERIORS, n_samples=20_000, seed=seed)
        np.random.seed(2)
        second = compute_probability_to_be_best(POSTERIORS, n_samples=20_000, seed=seed)
        assert first == second

    def test_seed_none_is_deterministic_fallback(self):
        a = compute_probability_to_be_best(POSTERIORS, n_samples=20_000)
        b = compute_probability_to_be_best(POSTERIORS, n_samples=20_000)
        assert a == b
        # The seedless fallback is keyed on the posteriors being analysed,
        # not on a single platform-wide stream.
        assert bayesian_service.resolve_seed(None, 20_000, POSTERIORS) == derive_seed(
            "bayesian_service",
            bayesian_service.posterior_fingerprint(POSTERIORS),
            20_000,
        )
        assert bayesian_service.resolve_seed(42, 20_000, POSTERIORS) == 42

    def test_seedless_fallback_differs_per_posterior_set(self):
        other = [
            {"alpha": 101.0, "beta": 901.0},
            {"alpha": 117.0, "beta": 885.0},  # one variant differs
            {"alpha": 95.0, "beta": 905.0},
        ]
        seed_a = bayesian_service.resolve_seed(None, 20_000, POSTERIORS)
        seed_b = bayesian_service.resolve_seed(None, 20_000, other)
        assert seed_a != seed_b
        # ...and the same data is still reproducible.
        assert seed_a == bayesian_service.resolve_seed(None, 20_000, list(POSTERIORS))
        assert compute_expected_loss(
            POSTERIORS, n_samples=20_000
        ) == compute_expected_loss(POSTERIORS, n_samples=20_000)
        assert compute_expected_loss(
            POSTERIORS, n_samples=20_000
        ) != compute_expected_loss(other, n_samples=20_000)

    def test_bandit_seedless_fallback_differs_per_arm_set(self):
        seed_a = ThompsonSampling.resolve_seed(None, 10_000, [5.0, 15.0], [15.0, 5.0])
        seed_b = ThompsonSampling.resolve_seed(None, 10_000, [6.0, 15.0], [15.0, 5.0])
        assert seed_a != seed_b
        assert seed_a == ThompsonSampling.resolve_seed(
            None, 10_000, [5.0, 15.0], [15.0, 5.0]
        )
        assert ThompsonSampling.resolve_seed(7, 10_000, [5.0], [15.0]) == 7
        # Reproducible per arm set, different across arm sets.
        assert ThompsonSampling.sample(
            [5.0, 15.0], [15.0, 5.0], 10_000
        ) == ThompsonSampling.sample([5.0, 15.0], [15.0, 5.0], 10_000)

    def test_should_stop_rope_path_is_seeded(self):
        config = BayesianConfig(loss_threshold=1e-9, rope=[-0.05, 0.05])
        equal = [{"alpha": 100.0, "beta": 900.0}, {"alpha": 100.0, "beta": 900.0}]
        seed = derive_seed(EXP_ID, "2026-09-11", 20_000)
        with patch.object(
            bayesian_service,
            "_draw_posterior_samples",
            wraps=bayesian_service._draw_posterior_samples,
        ) as draw:
            decision = should_stop(equal, config, n_samples=20_000, seed=seed)
        assert decision == BayesianDecision.STOP_EQUIVALENT
        # One draw with the caller's seed feeds both the expected loss and
        # the ROPE comparison (they shared the same matrix before, too).
        assert draw.call_count == 1
        assert draw.call_args.args[2] == seed

    def test_analyze_draws_the_posterior_matrix_once(self):
        """3 variants × 100k used to mean ~1.2M Beta variates per request."""
        config = BayesianConfig(loss_threshold=1e-9, rope=[-0.05, 0.05])
        service = BayesianService(config)
        observations = [
            {"conversions": 100, "total": 1000},
            {"conversions": 115, "total": 1000},
            {"conversions": 95, "total": 1000},
        ]
        seed = derive_seed(EXP_ID, "2026-09-11", 20_000)

        with patch.object(
            bayesian_service,
            "_draw_posterior_samples",
            wraps=bayesian_service._draw_posterior_samples,
        ) as draw:
            result = service.analyze(observations, n_samples=20_000, seed=seed)

        assert draw.call_count == 1
        assert draw.call_args.args[2] == seed

        # Same numbers as drawing separately per statistic with that seed.
        posteriors = result["posteriors"]
        assert result["probability_to_be_best"] == compute_probability_to_be_best(
            posteriors, n_samples=20_000, seed=seed
        )
        assert result["expected_loss"] == compute_expected_loss(
            posteriors, n_samples=20_000, seed=seed
        )

    def test_analyze_is_byte_identical_across_calls_without_a_seed(self):
        service = BayesianService(BayesianConfig())
        observations = [
            {"conversions": 100, "total": 1000},
            {"conversions": 115, "total": 1000},
        ]
        first = service.analyze(observations, n_samples=20_000)
        second = service.analyze(observations, n_samples=20_000)
        assert first["seed"] == second["seed"]
        assert first["probability_to_be_best"] == second["probability_to_be_best"]
        assert first["expected_loss"] == second["expected_loss"]

        # Different data → a different stream.
        other = service.analyze(
            [{"conversions": 100, "total": 1000}, {"conversions": 116, "total": 1000}],
            n_samples=20_000,
        )
        assert other["seed"] != first["seed"]

    def test_analyze_echoes_seed_n_samples_engine_version(self):
        service = BayesianService(BayesianConfig())
        observations = [
            {"conversions": 100, "total": 1000},
            {"conversions": 115, "total": 1000},
        ]
        seed = derive_seed(EXP_ID, "2026-09-11", 30_000)
        first = service.analyze(observations, n_samples=30_000, seed=seed)
        second = service.analyze(observations, n_samples=30_000, seed=seed)

        assert first["seed"] == seed
        assert first["n_samples"] == 30_000
        assert first["engine_version"] == ENGINE_VERSION
        assert first["probability_to_be_best"] == second["probability_to_be_best"]
        assert first["expected_loss"] == second["expected_loss"]
        assert first["decision"] == second["decision"]


# ---------------------------------------------------------------------------
# AnalysisService threading
# ---------------------------------------------------------------------------


def _mock_experiment():
    exp = MagicMock()
    exp.id = EXP_ID
    exp.name = "Seeded"
    exp.bayesian_enabled = True
    exp.bayesian_config = {"alpha": 1.0, "beta": 1.0, "loss_threshold": 0.001}
    variants = []
    for name, is_control in (("Control", True), ("Treatment", False)):
        v = MagicMock()
        v.id = uuid.uuid4()
        v.name = name
        v.is_control = is_control
        variants.append(v)
    exp.variants = variants
    metric = MagicMock()
    metric.is_primary = True
    metric.event_name = "purchase"
    exp.metric_definitions = [metric]
    return exp


class TestAnalysisServiceSeed:
    def test_compute_bayesian_results_uses_experiment_day_seed(self):
        exp = _mock_experiment()
        metrics_data = {
            str(exp.variants[0].id): {"conversions": 100, "total": 1000},
            str(exp.variants[1].id): {"conversions": 115, "total": 1000},
        }
        as_of = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)
        expected_seed = derive_seed(EXP_ID, "2026-09-11", BAYESIAN_N_SAMPLES)

        service = AnalysisService(MagicMock())
        first = service._compute_bayesian_results(exp, metrics_data, as_of=as_of)
        second = service._compute_bayesian_results(
            exp, metrics_data, as_of=datetime(2026, 9, 11, 23, 59, tzinfo=timezone.utc)
        )

        assert isinstance(first, BayesianResultsResponse)
        assert first.seed == expected_seed
        assert first.n_samples == BAYESIAN_N_SAMPLES
        assert first.engine_version == ENGINE_VERSION
        # Same day → byte-identical posterior summaries.
        assert first.model_dump() == second.model_dump()

        next_day = service._compute_bayesian_results(
            exp, metrics_data, as_of=datetime(2026, 9, 12, 0, 1, tzinfo=timezone.utc)
        )
        assert next_day.seed != first.seed

    def test_compute_bayesian_results_passes_seed_to_analyze(self):
        exp = _mock_experiment()
        metrics_data = {
            str(v.id): {"conversions": 10, "total": 100} for v in exp.variants
        }
        with patch(
            "backend.app.services.analysis_service.BayesianService"
        ) as MockService:
            MockService.return_value.analyze.return_value = {
                "posteriors": [{"alpha": 11.0, "beta": 91.0}] * 2,
                "credible_intervals": [(0.05, 0.18)] * 2,
                "probability_to_be_best": [0.5, 0.5],
                "expected_loss": [0.01, 0.01],
                "decision": BayesianDecision.CONTINUE,
            }
            AnalysisService(MagicMock())._compute_bayesian_results(
                exp, metrics_data, as_of=datetime(2026, 9, 11, tzinfo=timezone.utc)
            )
        kwargs = MockService.return_value.analyze.call_args.kwargs
        assert kwargs["seed"] == derive_seed(EXP_ID, "2026-09-11", BAYESIAN_N_SAMPLES)
        assert kwargs["n_samples"] == BAYESIAN_N_SAMPLES


# ---------------------------------------------------------------------------
# Bandit service / scheduler
# ---------------------------------------------------------------------------


class TestBanditDeterminism:
    def test_thompson_sample_same_seed_identical(self):
        seed = derive_seed(EXP_ID, "2026-09-11", ThompsonSampling.N_SAMPLES)
        a = ThompsonSampling.sample([5.0, 15.0], [15.0, 5.0], 10_000, seed=seed)
        b = ThompsonSampling.sample([5.0, 15.0], [15.0, 5.0], 10_000, seed=seed)
        assert a == b

    def test_thompson_sample_different_bucket_differs(self):
        s1 = derive_seed(EXP_ID, "2026-09-11", 10_000)
        s2 = derive_seed(EXP_ID, "2026-09-12", 10_000)
        a = ThompsonSampling.sample([5.0, 15.0], [15.0, 5.0], 10_000, seed=s1)
        b = ThompsonSampling.sample([5.0, 15.0], [15.0, 5.0], 10_000, seed=s2)
        assert a != b

    def test_thompson_ignores_global_numpy_state(self):
        seed = derive_seed(EXP_ID, "2026-09-11", 10_000)
        np.random.seed(7)
        a = ThompsonSampling.sample([2.0, 3.0], [3.0, 2.0], 10_000, seed=seed)
        np.random.seed(8)
        b = ThompsonSampling.sample([2.0, 3.0], [3.0, 2.0], 10_000, seed=seed)
        assert a == b

    def test_bandit_service_forwards_seed_for_thompson_only(self):
        stats = {
            "a": {"successes": 30, "failures": 70, "pulls": 100, "total_reward": 30.0},
            "b": {"successes": 60, "failures": 40, "pulls": 100, "total_reward": 60.0},
        }
        seed = derive_seed(EXP_ID, "2026-09-11", ThompsonSampling.N_SAMPLES)
        w1 = BanditService.compute_weights("thompson_sampling", stats, seed=seed)
        w2 = BanditService.compute_weights("thompson_sampling", stats, seed=seed)
        assert w1 == w2
        assert BanditService.is_stochastic("thompson_sampling")
        assert not BanditService.is_stochastic("ucb1")
        # Deterministic algorithms accept (and ignore) the seed.
        assert BanditService.compute_weights(
            "ucb1", stats, seed=seed
        ) == BanditService.compute_weights("ucb1", stats)

    def test_scheduler_derives_seed_from_tick_day(self):
        exp = MagicMock()
        exp.id = EXP_ID
        exp.optimization_type = "thompson_sampling"
        v1, v2 = MagicMock(), MagicMock()
        v1.id, v2.id = uuid.uuid4(), uuid.uuid4()
        exp.variants = [v1, v2]

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        scheduler = BanditScheduler(db=db)
        stats = {
            str(v1.id): VariantStats(
                variant_id=str(v1.id), successes=10, failures=90, pulls=100
            ),
            str(v2.id): VariantStats(
                variant_id=str(v2.id), successes=30, failures=70, pulls=100
            ),
        }
        tick = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)

        with (
            patch.object(
                scheduler, "get_variant_stats_from_counters", return_value=stats
            ),
            patch("backend.app.core.bandit_scheduler.datetime") as mock_dt,
            patch.object(
                BanditService, "compute_weights", wraps=BanditService.compute_weights
            ) as spy,
        ):
            mock_dt.now.return_value = tick
            assert scheduler.update_experiment(exp) is True

        expected_seed = derive_seed(EXP_ID, "2026-09-11", ThompsonSampling.N_SAMPLES)
        assert spy.call_args.kwargs["seed"] == expected_seed

        state = db.add.call_args[0][0]
        assert state.seed == expected_seed
        assert state.n_samples == ThompsonSampling.N_SAMPLES
        assert state.engine_version == ENGINE_VERSION

        # History rows + snapshot are staged with add_all inside a savepoint.
        db.begin_nested.assert_called_once()
        staged = db.add_all.call_args[0][0]
        history = [r for r in staged if type(r).__name__ == "BanditStateHistory"]
        snapshots = [r for r in staged if type(r).__name__ == "AnalysisSnapshot"]
        assert len(history) == 2
        assert {r.variant_id for r in history} == {v1.id, v2.id}
        assert all(r.seed == expected_seed and r.tick_at == tick for r in history)
        assert history[0].alpha == 11.0 and history[0].beta == 91.0
        assert len(snapshots) == 1
        assert snapshots[0].kind == "bandit" and snapshots[0].seed == expected_seed

    def test_scheduler_records_null_seed_for_deterministic_algorithm(self):
        exp = MagicMock()
        exp.id = EXP_ID
        exp.optimization_type = "ucb1"
        v1, v2 = MagicMock(), MagicMock()
        v1.id, v2.id = uuid.uuid4(), uuid.uuid4()
        exp.variants = [v1, v2]
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        scheduler = BanditScheduler(db=db)
        stats = {
            str(v1.id): VariantStats(
                variant_id=str(v1.id),
                successes=10,
                failures=90,
                pulls=100,
                total_reward=10.0,
            ),
            str(v2.id): VariantStats(
                variant_id=str(v2.id),
                successes=30,
                failures=70,
                pulls=100,
                total_reward=30.0,
            ),
        }
        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=stats
        ):
            scheduler.update_experiment(exp)
        state = db.add.call_args[0][0]
        assert state.seed is None and state.n_samples is None
        assert state.engine_version == ENGINE_VERSION

    def test_history_failure_does_not_block_state_update(self):
        exp = MagicMock()
        exp.id = EXP_ID
        exp.optimization_type = "thompson_sampling"
        v1, v2 = MagicMock(), MagicMock()
        v1.id, v2.id = uuid.uuid4(), uuid.uuid4()
        exp.variants = [v1, v2]
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        db.begin_nested.return_value.__enter__.side_effect = RuntimeError(
            "savepoint boom"
        )
        scheduler = BanditScheduler(db=db)
        stats = {str(v.id): VariantStats(variant_id=str(v.id)) for v in exp.variants}
        with patch.object(
            scheduler, "get_variant_stats_from_counters", return_value=stats
        ):
            assert scheduler.update_experiment(exp) is True
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Response schemas carry provenance
# ---------------------------------------------------------------------------


class TestProvenanceSchemas:
    def test_bayesian_response_defaults(self):
        r = BayesianResultsResponse(is_enabled=True)
        assert r.seed is None and r.n_samples is None
        assert r.engine_version == ENGINE_VERSION
        dumped = r.model_dump()
        assert {"seed", "n_samples", "engine_version"} <= set(dumped)

    def test_bandit_response_defaults(self):
        r = BanditStatusResponse(
            experiment_id=str(EXP_ID),
            algorithm="thompson_sampling",
            current_weights=[],
            total_pulls=0,
            recommendation="EXPLORING",
        )
        assert r.seed is None and r.n_samples is None
        assert r.engine_version == ENGINE_VERSION
        seeded = r.model_copy(update={"seed": 123, "n_samples": 10_000})
        assert seeded.seed == 123

    def test_cuped_response_has_engine_version_and_null_seed(self):
        r = CupedResultsResponse(
            experiment_id=str(EXP_ID),
            method=VarianceReductionMethod.CUPED,
            metrics=[],
            computed_at="2026-09-11T00:00:00+00:00",
        )
        assert r.engine_version == ENGINE_VERSION
        assert r.seed is None and r.n_samples is None
        assert set(r.model_dump()) >= {"seed", "n_samples", "engine_version"}
