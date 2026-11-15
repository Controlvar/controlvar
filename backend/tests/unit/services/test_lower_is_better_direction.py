"""
A metric's ``lower_is_better`` decides which way is "better" (#1377).

Every place that names a winner or recommends reads it: the proportion and
mean winners (``binomial_metric_result``, ``mean_metric_result``), the
summary's recommendation (``AnalysisService._summarise_decision``), the legacy
``best_variant`` (``AnalysisService._find_best_variant``) and the Bayesian
decision (``BayesianService.analyze``).  Before this, all of them took "up" as
better, so an error-rate or uninstall metric that rose was shipped and one
that fell was kept at control.

``relative_improvement_pct`` stays the raw signed change: the direction is
carried by ``lower_is_better`` alone.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import numpy as np
import pytest

from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.schemas.bayesian import BayesianConfig, BayesianDecision
from backend.app.schemas.results import MetricResult
from backend.app.services import analysis_service as analysis_module
from backend.app.services import bayesian_service as bayesian_module
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.bayesian_service import (
    BayesianService,
    _expected_loss_from_samples,
    _ptbb_from_samples,
)
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
    mean_metric_result,
)
from backend.tests.unit.services import (
    test_sufficient_stats_fingerprint as sufficient_stats,
)
from backend.tests.unit.services.test_binomial_metric_result_characterisation import (
    _FakeSession,
    _Fixture,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

_CONTROL = "ffffffff-0000-4000-8000-000000000002"
_T1 = "00000000-0000-4000-8000-000000000001"
_T2 = "77777777-0000-4000-8000-000000000003"

CONTROL = BinomialVariant(_CONTROL, "control", True)
T1 = BinomialVariant(_T1, "t1", False)
T2 = BinomialVariant(_T2, "t2", False)

#: Leave the attribute off the metric altogether.
ABSENT = object()


def _metric(
    lower_is_better: Any = ABSENT,
    name: str = "Uninstall",
    metric_type: str = "conversion",
    is_primary: bool = True,
) -> SimpleNamespace:
    fields: Dict[str, Any] = {
        "id": "33333333-0000-4000-8000-000000000003",
        "name": name,
        "metric_type": metric_type,
        "is_primary": is_primary,
    }
    if lower_is_better is not ABSENT:
        fields["lower_is_better"] = lower_is_better
    return SimpleNamespace(**fields)


# Treatment 1 falls from 10% to 6.25% of 2000 users (p = 1.7e-05).
DROP = [(CONTROL, 2000, 200), (T1, 2000, 125)]
# The mirror: treatment 1 rises from 6.25% to 10%.
RISE = [(CONTROL, 2000, 125), (T1, 2000, 200)]
# Both treatments fall significantly: 7.5% (p = 0.006) and 5% (p = 2e-09).
TWO_DROPS = [(CONTROL, 2000, 200), (T1, 2000, 150), (T2, 2000, 100)]
# One falls (5%), one rises (15%), both significantly.
MIXED = [(CONTROL, 2000, 200), (T1, 2000, 100), (T2, 2000, 300)]


def _proportion(counts, lower_is_better: Any = True, name: str = "Uninstall"):
    return binomial_metric_result(
        counts, 0.05, "none", metric=_metric(lower_is_better, name=name)
    )


def _decide(metric_result: Dict[str, Any], adequate: bool = True) -> Dict[str, Any]:
    return AnalysisService._summarise_decision(None, [metric_result], adequate)


def _variant(result: Dict[str, Any], variant_id: str) -> Dict[str, Any]:
    return next(v for v in result["variants"] if v["variant_id"] == variant_id)


class TestProportionDirection:
    def test_a_significant_rise_is_not_a_win_when_lower_is_better(self):
        result = _proportion(RISE)
        assert _variant(result, _T1)["is_significant"] is True
        assert result["winning_variant_id"] is None
        assert _decide(result) == {
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "KEEP_CONTROL",
            "recommendation_reason": (
                "Every treatment is significantly worse than control on "
                "Uninstall, where lower is better."
            ),
        }

    def test_a_significant_drop_wins_when_lower_is_better(self):
        result = _proportion(DROP)
        assert result["winning_variant_id"] == _T1
        # The raw signed change, not negated for the direction.
        assert _variant(result, _T1)["relative_improvement_pct"] == -37.5
        decision = _decide(result)
        assert decision == {
            "has_winner": True,
            "winning_variant_id": _T1,
            "recommendation": "SHIP_VARIANT",
            "recommendation_reason": (
                "t1 shows 37.5% improvement on Uninstall (p=0.0000): it fell "
                "by 37.5%, and lower is better for this metric."
            ),
        }
        assert "-37.5% improvement" not in decision["recommendation_reason"]

    def test_before_the_minimum_sample_the_drop_is_reported_and_testing_continues(
        self,
    ):
        decision = _decide(_proportion(DROP), adequate=False)
        assert decision["has_winner"] is True
        assert decision["winning_variant_id"] == _T1
        assert decision["recommendation"] == "CONTINUE_TESTING"
        assert decision["recommendation_reason"] == (
            "t1 shows 37.5% improvement on Uninstall (p=0.0000): it fell by "
            "37.5%, and lower is better for this metric. Not every variant has "
            "reached the minimum sample size yet, so keep the experiment "
            "running before shipping."
        )

    def test_the_lowest_of_several_significant_drops_wins(self):
        result = _proportion(TWO_DROPS)
        assert _variant(result, _T1)["is_significant"] is True
        assert _variant(result, _T2)["is_significant"] is True
        assert result["winning_variant_id"] == _T2
        assert _decide(result)["recommendation"] == "SHIP_VARIANT"

    def test_with_one_better_and_one_worse_the_better_one_wins(self):
        result = _proportion(MIXED)
        assert result["winning_variant_id"] == _T1
        decision = _decide(result)
        assert decision["recommendation"] == "SHIP_VARIANT"
        assert decision["winning_variant_id"] == _T1

    def test_higher_is_better_is_unchanged(self):
        """The mirror cases with lower_is_better False: the sentences are
        byte-identical to the ones before the direction was read."""
        rise = _proportion(RISE, lower_is_better=False, name="Purchase")
        assert rise["winning_variant_id"] == _T1
        assert _decide(rise)["recommendation_reason"] == (
            "t1 shows 60.0% improvement on Purchase (p=0.0000)."
        )
        drop = _proportion(DROP, lower_is_better=False, name="Purchase")
        assert drop["winning_variant_id"] is None
        assert _decide(drop) == {
            "has_winner": False,
            "winning_variant_id": None,
            "recommendation": "KEEP_CONTROL",
            "recommendation_reason": (
                "Every treatment is significantly worse than control on Purchase."
            ),
        }
        mixed = _proportion(MIXED, lower_is_better=False)
        assert mixed["winning_variant_id"] == _T2

    @pytest.mark.parametrize("value", [ABSENT, False, None])
    def test_absent_false_and_null_read_as_higher_is_better(self, value):
        """A stored NULL (the column is nullable) is False, as is a metric
        object with no such attribute."""
        result = _proportion(MIXED, lower_is_better=value)
        assert result["lower_is_better"] is False
        assert result == _proportion(MIXED, lower_is_better=False)
        assert MetricResult.model_validate(result).lower_is_better is False

    def test_a_value_that_is_not_true_reads_as_higher_is_better(self):
        """Only True means lower is better: a mock's auto-attribute (truthy)
        must not flip the direction of a test that never set it."""
        from unittest.mock import MagicMock

        result = _proportion(MIXED, lower_is_better=MagicMock())
        assert result["lower_is_better"] is False
        assert result["winning_variant_id"] == _T2

    def test_the_result_carries_the_direction(self):
        result = _proportion(DROP)
        assert result["lower_is_better"] is True
        parsed = MetricResult.model_validate(result)
        assert parsed.lower_is_better is True
        assert str(parsed.winning_variant_id) == _T1

    def test_a_result_without_the_field_reads_as_higher_is_better(self):
        """The field is additive: a result dict made before it (an older
        snapshot) still validates, as higher is better."""
        result = _proportion(DROP, lower_is_better=False)
        del result["lower_is_better"]
        assert MetricResult.model_validate(result).lower_is_better is False


#: (k, [(variant, n, sum_d, sum_d2), ...]): means 2.2 (control), 1.7 (t1,
#: p = 4e-08) and 2.08 (t2, not significant).
MEAN_DROP = (
    2.0,
    [(CONTROL, 500, 100.0, 1000.0), (T1, 500, -150.0, 1100.0), (T2, 500, 40.0, 950.0)],
)
#: The mirror around k: t1's mean 2.3, above the control's 1.8.
MEAN_RISE = (
    2.0,
    [(CONTROL, 500, -100.0, 1000.0), (T1, 500, 150.0, 1100.0), (T2, 500, -40.0, 950.0)],
)
#: Both treatments significantly below the control's 2.2: t1 at 1.9, t2 at 1.7.
#: The lower one is listed second, so neither "the first" nor "the highest"
#: picks it by accident.
MEAN_TWO_DROPS = (
    2.0,
    [
        (CONTROL, 500, 100.0, 1000.0),
        (T1, 500, -50.0, 1000.0),
        (T2, 500, -150.0, 1100.0),
    ],
)


def _mean(sums, lower_is_better: Any = True) -> Dict[str, Any]:
    k, rows = sums
    return mean_metric_result(
        k,
        rows,
        0.05,
        "none",
        metric=_metric(lower_is_better, name="Checkout errors", metric_type="custom"),
    )


class TestMeanDirection:
    def test_the_significantly_lower_mean_wins_when_lower_is_better(self):
        result = _mean(MEAN_DROP)
        assert result["lower_is_better"] is True
        assert _variant(result, _T1)["is_significant"] is True
        assert _variant(result, _T2)["is_significant"] is False
        assert result["winning_variant_id"] == _T1
        assert _decide(result)["recommendation_reason"] == (
            "t1 shows 22.7% improvement on Checkout errors (p=0.0000): it fell "
            "by 22.7%, and lower is better for this metric."
        )

    def test_a_significantly_higher_mean_is_not_a_win_when_lower_is_better(self):
        result = _mean(MEAN_RISE)
        assert _variant(result, _T1)["is_significant"] is True
        assert result["winning_variant_id"] is None

    def test_the_lowest_of_several_significantly_lower_means_wins(self):
        """The mean twin of the proportion case with two significant drops."""
        result = _mean(MEAN_TWO_DROPS)
        assert _variant(result, _T1)["is_significant"] is True
        assert _variant(result, _T2)["is_significant"] is True
        assert result["winning_variant_id"] == _T2
        decision = _decide(result)
        assert decision["recommendation"] == "SHIP_VARIANT"
        assert decision["winning_variant_id"] == _T2

    def test_higher_is_better_is_unchanged(self):
        assert _mean(MEAN_DROP, lower_is_better=False)["winning_variant_id"] is None
        assert _mean(MEAN_RISE, lower_is_better=False)["winning_variant_id"] == _T1
        assert _mean(MEAN_RISE, lower_is_better=None) == _mean(
            MEAN_RISE, lower_is_better=False
        )


class TestRecommendationWords:
    def _winner_without_a_relative_change(self, lower_is_better: bool):
        """A winner whose relative change is None (the control's mean is 0)."""
        return {
            "is_primary": True,
            "metric_name": "Refund value",
            "lower_is_better": lower_is_better,
            "winning_variant_id": _T1,
            "variants": [
                {
                    "variant_id": _CONTROL,
                    "variant_name": "control",
                    "is_control": True,
                    "sample_size": 500,
                    "is_significant": False,
                    "relative_improvement_pct": None,
                    "p_value": None,
                    "adjusted_p_value": None,
                },
                {
                    "variant_id": _T1,
                    "variant_name": "t1",
                    "is_control": False,
                    "sample_size": 500,
                    "is_significant": True,
                    "relative_improvement_pct": None,
                    "p_value": 0.001,
                    "adjusted_p_value": None,
                },
            ],
        }

    def test_with_no_relative_change_the_sentence_names_the_direction(self):
        decision = _decide(self._winner_without_a_relative_change(True))
        assert decision["recommendation_reason"] == (
            "t1 is significantly better on Refund value, where lower is better."
        )

    def test_with_no_relative_change_higher_is_better_is_unchanged(self):
        decision = _decide(self._winner_without_a_relative_change(False))
        assert decision["recommendation_reason"] == (
            "t1 is significantly better on Refund value."
        )


def _legacy_rows(counts) -> List[Dict[str, Any]]:
    from backend.app.services.sufficient_stats_analysis import (
        binomial_variant_results,
    )

    return binomial_variant_results(counts, 0.95)


class TestLegacyBestVariant:
    def test_the_lowest_significant_rate_is_best_when_lower_is_better(self):
        best = AnalysisService._find_best_variant(
            None, _legacy_rows(MIXED), lower_is_better=True
        )
        assert best["variant_id"] == _T1

    def test_the_lowest_of_several_significant_drops_is_best(self):
        best = AnalysisService._find_best_variant(
            None, _legacy_rows(TWO_DROPS), lower_is_better=True
        )
        assert best["variant_id"] == _T2

    def test_a_rise_is_not_best_when_lower_is_better(self):
        assert (
            AnalysisService._find_best_variant(
                None, _legacy_rows(RISE), lower_is_better=True
            )
            is None
        )

    def test_higher_is_better_is_unchanged(self):
        assert (
            AnalysisService._find_best_variant(None, _legacy_rows(MIXED))["variant_id"]
            == _T2
        )
        assert AnalysisService._find_best_variant(None, _legacy_rows(DROP)) is None


def _experiment(primary_lower: Any, secondary_lower: Any) -> SimpleNamespace:
    def metric(id_, name, event, is_primary, lower):
        fields = {
            "id": id_,
            "name": name,
            "event_name": event,
            "metric_type": "conversion",
            "is_primary": is_primary,
            "minimum_sample_size": 100,
        }
        if lower is not ABSENT:
            fields["lower_is_better"] = lower
        return SimpleNamespace(**fields)

    return SimpleNamespace(
        id="99999999-0000-4000-8000-000000000009",
        name="Onboarding",
        status="active",
        start_date=None,
        end_date=None,
        variants=[
            SimpleNamespace(id=_T1, name="t1", is_control=False),
            SimpleNamespace(id=_CONTROL, name="control", is_control=True),
            SimpleNamespace(id=_T2, name="t2", is_control=False),
        ],
        metric_definitions=[
            metric(
                "11111111-0000-4000-8000-000000000001",
                "Uninstall",
                "uninstall",
                True,
                primary_lower,
            ),
            metric(
                "22222222-0000-4000-8000-000000000002",
                "Crash",
                "crash",
                False,
                secondary_lower,
            ),
        ],
        bayesian_enabled=False,
        bayesian_config=None,
    )


#: Uninstalls: t1 falls to 5%, t2 rises to 15%.  Crashes: t2 falls to 2%,
#: t1 rises to 6% (control 4%).
_RESULTS_FIXTURE = _Fixture(
    units={_T1: 2000, _CONTROL: 2000, _T2: 2000},
    converted={
        ("uninstall", _T1): 100,
        ("uninstall", _CONTROL): 200,
        ("uninstall", _T2): 300,
        ("crash", _T1): 120,
        ("crash", _CONTROL): 80,
        ("crash", _T2): 40,
    },
)


def _results(monkeypatch, primary_lower: Any, secondary_lower: Any) -> Dict[str, Any]:
    """``get_experiment_results`` through the real entry point, the database
    replaced as in the characterisation test."""
    fixture = _RESULTS_FIXTURE
    experiment = _experiment(primary_lower, secondary_lower)

    def converting_units(_db, _experiment_id, variant_id, event):
        return fixture.converted[(event, str(variant_id))]

    monkeypatch.setattr(analysis_module, "count_converting_users", converting_units)
    monkeypatch.setattr(
        AnalysisService,
        "calculate_experiment_summary",
        lambda self, experiment: {
            "total_users": sum(fixture.units.values()),
            "total_events": 0,
            "total_conversions": 0,
            "duration_days": None,
        },
    )
    return AnalysisService(_FakeSession(experiment, fixture)).get_experiment_results(
        experiment.id,
        confidence_level=0.95,
        correction_method="none",
        include_bayesian=False,
    )


class TestThroughResults:
    def test_results_follow_each_metrics_direction(self, monkeypatch):
        output = _results(monkeypatch, primary_lower=True, secondary_lower=True)
        uninstall, crash = output["metrics"]
        assert (uninstall["metric_name"], crash["metric_name"]) == (
            "Uninstall",
            "Crash",
        )
        assert uninstall["lower_is_better"] is True
        assert uninstall["winning_variant_id"] == _T1
        # A secondary metric's own winner follows its own direction.
        assert crash["lower_is_better"] is True
        assert crash["winning_variant_id"] == _T2
        assert output["summary"]["recommendation"] == "SHIP_VARIANT"
        assert output["summary"]["winning_variant_id"] == _T1
        legacy = {
            m["metric_name"]: m["best_variant"] for m in output["metrics_results"]
        }
        assert legacy["Uninstall"]["variant_id"] == _T1
        assert legacy["Crash"]["variant_id"] == _T2
        for metric in output["metrics"]:
            MetricResult.model_validate(metric)

    def test_results_of_a_metric_that_does_not_set_it_are_higher_is_better(
        self, monkeypatch
    ):
        output = _results(monkeypatch, primary_lower=None, secondary_lower=ABSENT)
        uninstall, crash = output["metrics"]
        assert uninstall["lower_is_better"] is False
        assert crash["lower_is_better"] is False
        assert uninstall["winning_variant_id"] == _T2
        assert crash["winning_variant_id"] == _T1


# Uninstalls: control 10%, treatment 5% of 2000 users.
_BAYES_OBS = [{"conversions": 200, "total": 2000}, {"conversions": 100, "total": 2000}]


class TestBayesianDirection:
    N = 20_000
    SEED = 1234

    def test_stop_winner_names_the_lowest_rate_when_lower_is_better(self):
        lower = BayesianService().analyze(
            _BAYES_OBS, n_samples=self.N, seed=self.SEED, lower_is_better=True
        )
        assert lower["decision"] == BayesianDecision.STOP_WINNER
        assert lower["probability_to_be_best"][1] >= 0.975
        higher = BayesianService().analyze(_BAYES_OBS, n_samples=self.N, seed=self.SEED)
        assert higher["probability_to_be_best"][0] >= 0.975
        # The losses are the same magnitudes, the leader swapped.
        assert lower["expected_loss"] == higher["expected_loss"][::-1]
        assert min(lower["expected_loss"]) >= 0
        # The posteriors and credible intervals are the rates, unchanged.
        assert lower["posteriors"] == higher["posteriors"]
        assert lower["credible_intervals"] == higher["credible_intervals"]

    def test_the_experiment_decision_follows_the_primary_metrics_direction(self):
        """The decision /results reports and stores on the experiment."""
        experiment = SimpleNamespace(
            id="99999999-0000-4000-8000-000000000009",
            bayesian_config={"alpha": 1.0, "beta": 1.0},
            variants=[
                SimpleNamespace(id=_CONTROL, name="control", is_control=True),
                SimpleNamespace(id=_T1, name="t1", is_control=False),
            ],
            metric_definitions=[_metric(True)],
        )
        metrics_data = {
            _CONTROL: {"conversions": 200, "total": 2000},
            _T1: {"conversions": 100, "total": 2000},
        }
        response = AnalysisService(None)._compute_bayesian_results(
            experiment, metrics_data, n_samples=self.N
        )
        assert response.decision == BayesianDecision.STOP_WINNER
        best = {
            v.variant_key: v.probability_to_be_best for v in response.variant_results
        }
        assert best["t1"] >= 0.975, best

    def test_the_decision_reads_the_primary_when_a_secondary_comes_first(
        self, monkeypatch
    ):
        """The counts and the direction both come from the primary metric,
        not from the first one listed.  Here a higher-is-better secondary
        comes first and does not differ between the arms, so reading it gives
        no winner in either direction; reading the primary's counts in the
        higher direction would name the control."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        def metric(name, event, is_primary, lower_is_better):
            return SimpleNamespace(
                id=f"{event}-0000",
                name=name,
                event_name=event,
                metric_type="conversion",
                is_primary=is_primary,
                lower_is_better=lower_is_better,
            )

        experiment = SimpleNamespace(
            id="99999999-0000-4000-8000-000000000009",
            bayesian_config={"alpha": 1.0, "beta": 1.0},
            variants=[
                SimpleNamespace(id=_CONTROL, name="control", is_control=True),
                SimpleNamespace(id=_T1, name="t1", is_control=False),
            ],
            metric_definitions=[
                metric("Purchase", "purchase", False, False),
                metric("Uninstall", "uninstall", True, True),
            ],
        )
        converted = {
            ("purchase", _CONTROL): 150,
            ("purchase", _T1): 150,
            ("uninstall", _CONTROL): 200,
            ("uninstall", _T1): 100,
        }
        monkeypatch.setattr(
            analysis_module,
            "count_converting_users",
            lambda _db, _experiment_id, variant_id, event: converted[
                (event, str(variant_id))
            ],
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 2000
        response = AnalysisService(db).compute_bayesian_results(
            experiment, as_of=datetime(2026, 10, 27, tzinfo=timezone.utc)
        )
        best = {
            v.variant_key: v.probability_to_be_best for v in response.variant_results
        }
        assert response.decision == BayesianDecision.STOP_WINNER, best
        assert best["t1"] >= 0.975, best
        assert experiment.bayesian_decision == BayesianDecision.STOP_WINNER.value

    def test_the_random_stream_is_the_higher_is_better_one(self, monkeypatch):
        """Negated after the seeded draw: the same matrix is drawn, and every
        statistic comes from its negation."""
        drawn: List[Any] = []
        real_draw = bayesian_module._draw_posterior_samples

        def spy(posteriors, n_samples, seed):
            samples = real_draw(posteriors, n_samples, seed)
            drawn.append((seed, samples.copy()))
            return samples

        monkeypatch.setattr(bayesian_module, "_draw_posterior_samples", spy)
        lower = BayesianService().analyze(
            _BAYES_OBS, n_samples=self.N, seed=self.SEED, lower_is_better=True
        )
        BayesianService().analyze(_BAYES_OBS, n_samples=self.N, seed=self.SEED)
        (seed_lower, matrix_lower), (seed_higher, matrix_higher) = drawn
        assert seed_lower == seed_higher == self.SEED
        assert np.array_equal(matrix_lower, matrix_higher)
        assert lower["probability_to_be_best"] == _ptbb_from_samples(-matrix_lower)
        assert lower["expected_loss"] == _expected_loss_from_samples(-matrix_lower)

    @pytest.mark.parametrize(
        "rope, decision",
        [
            ((0.0, 0.02), BayesianDecision.STOP_EQUIVALENT),
            ((-0.02, 0.0), BayesianDecision.CONTINUE),
        ],
    )
    def test_the_rope_is_compared_with_the_advantage_in_the_good_direction(
        self, rope, decision
    ):
        """Control 10%, treatment 9.5% of 100,000, lower is better.  The ROPE
        is compared with the best variant's advantage in the good direction
        (control minus treatment, +0.005), not with the raw treatment minus
        control (-0.005): an asymmetric ROPE keeps its meaning."""
        config = BayesianConfig(rope=list(rope), loss_threshold=1e-9)
        observations = [
            {"conversions": 10_000, "total": 100_000},
            {"conversions": 9_500, "total": 100_000},
        ]
        result = BayesianService(config).analyze(
            observations, n_samples=self.N, seed=self.SEED, lower_is_better=True
        )
        assert result["probability_to_be_best"][1] > 0.99
        assert result["decision"] == decision


#: The last engine whose bandit did not read the primary metric's
#: ``lower_is_better``; it is frozen (#1385).
BEFORE_BANDIT_DIRECTION = "1.5.0"


class TestTheBanditReadsTheDirection:
    """The bandit moves traffic toward the better arm in the metric's
    direction (#1385).  For a lower-is-better primary (uninstalls, errors) a
    user who did NOT convert is the success, so the arm with fewer converting
    users earns the traffic, for every algorithm and every stats source.  A
    higher-is-better primary keeps the old behaviour."""

    ALGORITHMS = ["thompson_sampling", "ucb1", "epsilon_greedy"]

    @staticmethod
    def _run(
        algorithm: str, lower_is_better: Any, source: str, db: Any = None
    ) -> Dict[str, float]:
        from unittest.mock import MagicMock, patch
        from uuid import UUID

        from backend.app.core.bandit_scheduler import BanditScheduler
        from backend.app.services.bandit_service import BanditService, VariantStats

        if db is None:
            db = MagicMock()
            db.query.return_value.filter.return_value.first.return_value = None
        experiment = MagicMock()
        experiment.id = UUID("99999999-0000-4000-8000-000000000009")
        experiment.optimization_type = algorithm
        experiment.variants = [
            SimpleNamespace(id=UUID(_CONTROL), name="control", is_control=True),
            SimpleNamespace(id=UUID(_T1), name="t1", is_control=False),
        ]
        experiment.metric_definitions = [_metric(lower_is_better)]
        # Converting users: control 10%, treatment 5%.
        raw = {
            _CONTROL: VariantStats(
                _CONTROL, successes=200, failures=1800, pulls=2000, total_reward=200.0
            ),
            _T1: VariantStats(
                _T1, successes=100, failures=1900, pulls=2000, total_reward=100.0
            ),
        }
        computed: Dict[str, float] = {}
        real_compute = BanditService.compute_weights

        def capture(*args, **kwargs):
            computed.update(real_compute(*args, **kwargs))
            return dict(computed)

        scheduler = BanditScheduler(db=db)
        patches = [patch.object(BanditService, "compute_weights", side_effect=capture)]
        if source == "state":
            patches.append(
                patch.object(scheduler, "_stats_from_dynamodb", return_value=None)
            )
            patches.append(
                patch.object(scheduler, "_stats_from_postgres", return_value=None)
            )
        elif source == "postgres":
            patches.append(
                patch.object(scheduler, "_stats_from_dynamodb", return_value=None)
            )
            patches.append(
                patch.object(scheduler, "_stats_from_postgres", return_value=raw)
            )
        else:
            patches.append(
                patch.object(scheduler, "_stats_from_dynamodb", return_value=raw)
            )
            patches.append(
                patch.object(scheduler, "_stats_from_postgres", return_value=None)
            )
        for p in patches:
            p.start()
        try:
            assert scheduler.update_experiment(experiment) is True
        finally:
            for p in patches:
                p.stop()
        return computed

    @staticmethod
    def _state_db(lower_is_better_unused: Any = None) -> Any:
        """A db whose stored BanditState holds RAW converting-user counts
        (control 200 of 2000, treatment 100 of 2000)."""
        from unittest.mock import MagicMock

        state = SimpleNamespace(
            variant_weights={
                _CONTROL: {"successes": 200, "failures": 1800, "pulls": 2000},
                _T1: {"successes": 100, "failures": 1900, "pulls": 2000},
            }
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = state
        return db

    @pytest.mark.parametrize("source", ["postgres", "dynamodb", "state"])
    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_editing_the_direction_changes_the_next_ticks_weights(
        self, algorithm, source
    ):
        """The direction is read on every tick, from every source including
        the BanditState fallback, so editing it mid-run flips the weights."""
        db_a = self._state_db() if source == "state" else None
        db_b = self._state_db() if source == "state" else None
        higher = self._run(algorithm, False, source, db=db_a)
        lower = self._run(algorithm, True, source, db=db_b)
        assert higher[_CONTROL] > higher[_T1]
        assert lower[_T1] > lower[_CONTROL]

    def test_stored_and_reported_counts_stay_raw_for_a_lower_is_better_metric(
        self,
    ):
        """variant_weights, history rows and the snapshot keep successes =
        converting users; only the Beta alpha/beta carry the reward counts."""
        from unittest.mock import MagicMock

        from backend.app.models.bandit_state import BanditState, BanditStateHistory
        from backend.app.services.bandit_service import ThompsonSampling

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        self._run("thompson_sampling", True, "postgres", db=db)

        states = [
            c.args[0]
            for c in db.add.call_args_list
            if isinstance(c.args[0], BanditState)
        ]
        assert len(states) == 1
        # The regret estimate is in reward counts too: weights that favour
        # the arm with fewer converting users beat a uniform split.  On raw
        # counts the same weights do worse, and the stored value is null.
        assert states[0].regret_reduction_pct is not None
        assert states[0].regret_reduction_pct > 0
        vw = states[0].variant_weights
        assert vw[_CONTROL]["successes"] == 200
        assert vw[_CONTROL]["failures"] == 1800
        assert vw[_T1]["successes"] == 100
        history = {
            str(r.variant_id): r
            for call in db.add_all.call_args_list
            for r in call.args[0]
            if isinstance(r, BanditStateHistory)
        }
        control = history[_CONTROL]
        assert (control.successes, control.failures) == (200, 1800)
        assert control.alpha == ThompsonSampling.PRIOR_ALPHA + 1800
        assert control.beta == ThompsonSampling.PRIOR_BETA + 200

    @pytest.mark.parametrize("source", ["postgres", "dynamodb"])
    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_traffic_moves_to_fewer_converting_users_when_lower_is_better(
        self, algorithm, source
    ):
        weights = self._run(algorithm, True, source)
        assert weights[_T1] > weights[_CONTROL]
        if algorithm != "ucb1":
            assert weights[_T1] > 0.85

    @pytest.mark.parametrize("source", ["postgres", "dynamodb"])
    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_traffic_moves_to_more_converting_users_when_higher_is_better(
        self, algorithm, source
    ):
        weights = self._run(algorithm, False, source)
        assert weights[_CONTROL] > weights[_T1]
        if algorithm != "ucb1":
            assert weights[_CONTROL] > 0.85


def test_the_engine_whose_bandit_reads_the_direction_is_not_a_frozen_one():
    """1.5.0 is frozen with a bandit that ignores ``lower_is_better``: its
    weights, regret and history alpha/beta favour more conversions whatever
    the direction.  The engine whose bandit reads the direction must
    therefore be a later version (#1385)."""
    assert BEFORE_BANDIT_DIRECTION in sufficient_stats.RELEASED_ENGINE_VERSIONS
    assert sufficient_stats._key(ENGINE_VERSION) > sufficient_stats._key(
        BEFORE_BANDIT_DIRECTION
    ), (
        f"ENGINE_VERSION {ENGINE_VERSION} is the frozen "
        f"{BEFORE_BANDIT_DIRECTION} or older, but the bandit's output for a "
        "lower-is-better primary changed (#1385): bump ENGINE_VERSION"
    )
