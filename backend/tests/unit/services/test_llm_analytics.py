"""
Unit tests for LLMEvaluationAnalyticsService (EP-046).

Tests statistics computation, CI calculation, p-values, winner determination,
human rating submission, and LLM-as-judge mocking.
"""

import math
import statistics
import uuid
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.llm_experiment import (
    LLMEvaluation,
    LLMEvaluationMetric,
    LLMExperiment,
    LLMExperimentStatus,
    LLMProvider,
    LLMTaskType,
    LLMVariant,
)
from backend.app.schemas.llm_experiments import LLMExperimentResults, VariantStats
from backend.app.services.llm_analytics_service import (
    JUDGE_CALL_FAILED_REASONING,
    JUDGE_NO_SCORE_REASONING,
    LLMEvaluationAnalyticsService,
    _cohens_d,
    _confidence_interval_95,
    _mean,
    _std,
    _t_critical,
    _welch_t_test,
)

# ---------------------------------------------------------------------------
# Statistical helper tests
# ---------------------------------------------------------------------------


class TestStatisticalHelpers:
    def test_mean_empty(self):
        assert _mean([]) is None

    def test_mean_single(self):
        assert _mean([5.0]) == 5.0

    def test_mean_multiple(self):
        assert _mean([1.0, 2.0, 3.0]) == pytest.approx(2.0)

    def test_std_empty(self):
        assert _std([]) is None

    def test_std_single(self):
        assert _std([1.0]) is None  # Can't compute std with 1 value

    def test_std_multiple(self):
        result = _std([1.0, 2.0, 3.0])
        assert result == pytest.approx(1.0, rel=1e-4)

    def test_ci_empty(self):
        lower, upper = _confidence_interval_95([])
        assert lower is None
        assert upper is None

    def test_ci_single(self):
        lower, upper = _confidence_interval_95([5.0])
        assert lower == 5.0
        assert upper == 5.0

    def test_ci_many_values_contains_mean(self):
        values = [10.0 + i * 0.1 for i in range(100)]
        m = sum(values) / len(values)
        lower, upper = _confidence_interval_95(values)
        assert lower < m < upper

    def test_ci_width_decreases_with_more_samples(self):
        small = [5.0, 5.5, 6.0, 4.5]
        large = [5.0 + (i % 3) * 0.5 for i in range(100)]
        lo_s, hi_s = _confidence_interval_95(small)
        lo_l, hi_l = _confidence_interval_95(large)
        width_small = hi_s - lo_s
        width_large = hi_l - lo_l
        assert width_large < width_small

    def test_welch_t_test_different_means(self):
        a = [1.0, 1.1, 0.9, 1.0, 1.05]
        b = [2.0, 2.1, 1.9, 2.0, 2.05]
        p = _welch_t_test(a, b)
        assert p is not None
        assert p < 0.05  # Should be highly significant

    def test_welch_t_test_identical_constants_have_no_p_value(self):
        # Neither sample varies: no t distribution, so no p-value (#1192)
        a = [1.0, 1.0, 1.0, 1.0]
        b = [1.0, 1.0, 1.0, 1.0]
        assert _welch_t_test(a, b) is None

    def test_welch_t_test_too_few_samples(self):
        assert _welch_t_test([1.0], [2.0]) is None
        assert _welch_t_test([], [1.0, 2.0]) is None

    def test_cohens_d_large_effect(self):
        # Add slight variance so pooled std > 0
        import random

        rng = random.Random(42)
        a = [1.0 + rng.gauss(0, 0.1) for _ in range(50)]
        b = [3.0 + rng.gauss(0, 0.1) for _ in range(50)]
        d = _cohens_d(a, b)
        assert d is not None and abs(d) > 2.0  # Large effect

    def test_cohens_d_zero_effect(self):
        a = [1.0, 2.0, 3.0]
        b = [1.0, 2.0, 3.0]
        d = _cohens_d(a, b)
        # Both samples identical — Cohen's d = 0
        assert d == pytest.approx(0.0, abs=1e-9)

    def test_cohens_d_too_few_samples(self):
        assert _cohens_d([1.0], [2.0]) is None


# Fixed sample pairs for the exact Welch p-value (#1192). The comment on each
# gives the Welch t statistic and degrees of freedom it produces.
_WELCH_CASES = {
    # t = 2.64, df = 6.0
    "df_6": ([10.0, 12.0, 11.0, 13.0], [13.5, 14.0, 12.5, 15.5]),
    # t = 2.52, df = 10.0
    "df_10_t_2_5": (
        [5.1, 4.8, 5.6, 5.0, 4.7, 5.3],
        [5.5, 5.8, 5.1, 6.0, 5.3, 5.7],
    ),
    # t = 2.0004, df = 10.0: the old step table said 0.05; it is 0.073
    "df_10_t_2_0": (
        [5.1, 4.8, 5.6, 5.0, 4.7, 5.3],
        [5.4, 5.7, 5.0, 5.9, 5.2, 5.6],
    ),
    # t = 3.20, df = 2.2 (three observations against twelve)
    "df_2": (
        [3.0, 5.0, 4.0],
        [5.0, 6.0, 5.5, 6.5, 5.8, 6.2, 5.9, 6.1, 6.0, 5.7, 6.3, 5.6],
    ),
    # t = 0.55, df = 7.7 (unequal variances)
    "unequal_variances": (
        [0.62, 0.71, 0.55, 0.68, 0.74, 0.59, 0.66, 0.70, 0.63, 0.69],
        [0.81, 0.52, 0.94, 0.47, 0.88, 0.60, 0.99, 0.41],
    ),
    # t = 0.01, df = 8.0: the old step table said 0.30; it is 0.99
    "t_near_0": ([1.0, 2.0, 3.0, 4.0, 5.0], [1.2, 1.9, 3.1, 3.8, 5.05]),
    # t = 3.09, df = 46.6: the old normal approximation said 0.0020
    "df_over_30": (
        [0.40 + 0.03 * ((i * 7) % 11) for i in range(25)],
        [0.46 + 0.03 * ((i * 5) % 13) for i in range(25)],
    ),
}


@pytest.mark.regression
class TestWelchPValueIsExact:
    """#1192: below 30 degrees of freedom the p-value was one of five steps
    (0.01, 0.02, 0.05, 0.15, 0.30) and at 30 or more a normal approximation.
    It is now the exact two-tailed Welch p-value."""

    @pytest.mark.parametrize("case", sorted(_WELCH_CASES))
    def test_p_value_matches_scipy_welch(self, case):
        from scipy import stats

        a, b = _WELCH_CASES[case]
        expected = stats.ttest_ind(a, b, equal_var=False).pvalue
        p = _welch_t_test(a, b)
        assert p is not None
        assert abs(p - expected) <= 1e-9, (case, p, expected)
        # Order of the arms does not change a two-tailed p-value
        assert abs(_welch_t_test(b, a) - expected) <= 1e-9

    def test_t_near_zero_is_near_one(self):
        a, b = _WELCH_CASES["t_near_0"]
        assert _welch_t_test(a, b) > 0.99

    def test_equal_means_is_exactly_one(self):
        assert _welch_t_test([1.0, 2.0, 3.0], [0.0, 2.0, 4.0]) == 1.0

    def test_too_few_values_or_no_spread_give_no_p_value(self):
        assert _welch_t_test([1.0], [1.0, 2.0]) is None
        assert _welch_t_test([1.0, 2.0], []) is None
        # Zero variance in both arms: the standard error is 0, so there is no
        # p-value. It used to be 1.0, "no difference", for two arms that do
        # not overlap at all.
        assert _welch_t_test([2.0, 2.0, 2.0], [5.0, 5.0]) is None


# Zero spread (#1192), each case with its p-value and whether Cohen's d is
# defined: null p and d whenever neither arm varies, whatever the means; one
# arm with spread is enough for both, and the p-value is then scipy's.
_ZERO_SPREAD_CASES = {
    "unequal_constants": ([2.0, 2.0, 2.0], [5.0, 5.0], None, False),
    "equal_constants": ([2.0, 2.0, 2.0], [2.0, 2.0], None, False),
    # A coarse judge: scipy answers 6.2e-31 here from float cancellation
    "coarse_judge": ([0.8, 0.8, 0.8], [0.9, 0.9], None, False),
    "one_arm_varies": ([2.0, 2.0, 2.0], [5.0, 6.0], "scipy", True),
}


@pytest.mark.regression
class TestZeroSpread:
    """#1192: when neither arm varied, the p-value was 1.0 and Cohen's d 0.0,
    "no difference" and "no effect", even for [2, 2, 2] against [5, 5]. Both
    are now None ("not computed"), as the warehouse analysis already reports
    a comparison with no variation."""

    @pytest.mark.parametrize("case", sorted(_ZERO_SPREAD_CASES))
    def test_p_value_and_effect_size(self, case):
        from scipy import stats

        a, b, p_expected, d_defined = _ZERO_SPREAD_CASES[case]
        p = _welch_t_test(a, b)
        d = _cohens_d(a, b)
        if p_expected is None:
            assert p is None, (case, p)
        else:
            expected = stats.ttest_ind(a, b, equal_var=False).pvalue
            assert p is not None and abs(p - expected) <= 1e-9, (case, p, expected)
            assert round(p, 4) == 0.0903
        if d_defined:
            assert d is not None and math.isfinite(d) and d > 0, (case, d)
        else:
            assert d is None, (case, d)


# Fixed samples for the exact 95 % interval (#1192), by degrees of freedom
# (n - 1). The old table took the nearest listed df at or below, so df 12 used
# df 10's 2.228 and df 99 used df 60's 2.000: intervals too wide.
_CI_CASES = {
    "df_1": [4.0, 7.0],
    "df_10": [5.1, 4.8, 5.6, 5.0, 4.7, 5.3, 5.5, 5.8, 5.1, 6.0, 5.3],
    "df_12": [
        0.62,
        0.71,
        0.55,
        0.68,
        0.74,
        0.59,
        0.66,
        0.70,
        0.63,
        0.69,
        0.58,
        0.72,
        0.64,
    ],
    "df_39": [0.40 + 0.03 * ((i * 7) % 11) for i in range(40)],
    "df_99": [200.0 + 15.0 * ((i * 5) % 13) for i in range(100)],
}


@pytest.mark.regression
class TestConfidenceIntervalIsExact:
    """#1192: the 95 % interval's t critical value came from a step table that
    skipped most df, so the interval was too wide. It is now the exact t
    quantile at df = n - 1."""

    @pytest.mark.parametrize("case", sorted(_CI_CASES))
    def test_interval_matches_scipy(self, case):
        import statistics as st

        from scipy import stats

        values = _CI_CASES[case]
        n = len(values)
        lo_exp, hi_exp = stats.t.interval(
            0.95, n - 1, loc=st.mean(values), scale=stats.sem(values)
        )
        lower, upper = _confidence_interval_95(values)
        assert abs(lower - lo_exp) <= 1e-9, (case, lower, lo_exp)
        assert abs(upper - hi_exp) <= 1e-9, (case, upper, hi_exp)

    def test_t_critical_matches_published_table(self):
        # Two-tailed 0.05 critical values from a printed t table
        assert _t_critical(1) == pytest.approx(12.7062, abs=1e-4)
        assert _t_critical(12) == pytest.approx(2.1788, abs=1e-4)
        assert _t_critical(99) == pytest.approx(1.9842, abs=1e-4)

    def test_degenerate_df_keeps_the_normal_value(self):
        assert _t_critical(0) == 1.96
        assert _t_critical(-1) == 1.96


# ---------------------------------------------------------------------------
# Helpers for building mock DB / experiments
# ---------------------------------------------------------------------------


def _make_eval(
    exp_id: uuid.UUID,
    variant_id: uuid.UUID,
    latency_ms: int = 200,
    cost: float = 0.001,
    auto_eval: float = None,
    human_rating: float = None,
    bm: float = None,
) -> LLMEvaluation:
    ev = LLMEvaluation()
    ev.id = uuid.uuid4()
    ev.llm_experiment_id = exp_id
    ev.variant_id = variant_id
    ev.user_id = f"user-{uuid.uuid4().hex[:6]}"
    ev.input_variables = {}
    ev.rendered_prompt = "Hello"
    ev.model_response = "Hi there"
    ev.latency_ms = latency_ms
    ev.input_tokens = 10
    ev.output_tokens = 20
    ev.estimated_cost_usd = cost
    ev.auto_eval_score = auto_eval
    ev.human_rating = human_rating
    ev.business_metric_value = bm
    return ev


def _make_experiment_with_data(
    control_bm: List[float] = None,
    treatment_bm: List[float] = None,
    control_ae: List[float] = None,
    treatment_ae: List[float] = None,
) -> tuple:
    """
    Build a mock LLMExperiment with two variants and evaluations.
    Returns (experiment, all_evaluations).
    """
    exp = LLMExperiment()
    exp.id = uuid.uuid4()
    exp.name = "Test Exp"
    exp.status = LLMExperimentStatus.ACTIVE
    exp.task_type = LLMTaskType.CHAT_COMPLETION
    exp.evaluation_metric = LLMEvaluationMetric.BUSINESS_METRIC

    ctrl = LLMVariant()
    ctrl.id = uuid.uuid4()
    ctrl.llm_experiment_id = exp.id
    ctrl.name = "control"
    ctrl.is_control = True
    ctrl.traffic_split = 0.5
    ctrl.provider = LLMProvider.ANTHROPIC
    ctrl.model_name = "claude-3-5-sonnet-20241022"
    ctrl.system_prompt = ""
    ctrl.prompt_template = "Hello"
    ctrl.temperature = 0.7
    ctrl.max_tokens = 500
    ctrl.additional_params = {}

    trt = LLMVariant()
    trt.id = uuid.uuid4()
    trt.llm_experiment_id = exp.id
    trt.name = "treatment"
    trt.is_control = False
    trt.traffic_split = 0.5
    trt.provider = LLMProvider.OPENAI
    trt.model_name = "gpt-4o"
    trt.system_prompt = ""
    trt.prompt_template = "Hello"
    trt.temperature = 0.7
    trt.max_tokens = 500
    trt.additional_params = {}

    exp.variants = [ctrl, trt]

    evals = []
    for v in control_bm or []:
        evals.append(_make_eval(exp.id, ctrl.id, bm=v))
    for v in treatment_bm or []:
        evals.append(_make_eval(exp.id, trt.id, bm=v))
    for v in control_ae or []:
        evals.append(_make_eval(exp.id, ctrl.id, auto_eval=v))
    for v in treatment_ae or []:
        evals.append(_make_eval(exp.id, trt.id, auto_eval=v))

    return exp, evals


# ---------------------------------------------------------------------------
# Analytics service tests
# ---------------------------------------------------------------------------


class TestGetExperimentResults:
    def _make_db(self, experiment=None, evals=None):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = experiment
        db.query.return_value.filter.return_value.all.return_value = evals or []
        return db

    def test_no_evaluations_returns_zero_counts(self):
        svc = LLMEvaluationAnalyticsService()
        exp, _ = _make_experiment_with_data()
        db = self._make_db(experiment=exp, evals=[])
        results = svc.get_experiment_results(db, exp.id)
        assert results.total_evaluations == 0
        for vs in results.variant_stats:
            assert vs.n_evaluations == 0

    def test_missing_experiment_raises(self):
        svc = LLMEvaluationAnalyticsService()
        db = self._make_db(experiment=None, evals=[])
        with pytest.raises(ValueError, match="not found"):
            svc.get_experiment_results(db, uuid.uuid4())

    def test_mean_latency_calculated(self):
        svc = LLMEvaluationAnalyticsService()
        exp, _ = _make_experiment_with_data()
        evals = [
            _make_eval(exp.id, exp.variants[0].id, latency_ms=200),
            _make_eval(exp.id, exp.variants[0].id, latency_ms=400),
        ]
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        ctrl_stats = next(vs for vs in results.variant_stats if vs.is_control)
        assert ctrl_stats.mean_latency_ms == pytest.approx(300.0)

    def test_mean_cost_calculated(self):
        svc = LLMEvaluationAnalyticsService()
        exp, _ = _make_experiment_with_data()
        evals = [
            _make_eval(exp.id, exp.variants[0].id, cost=0.002),
            _make_eval(exp.id, exp.variants[0].id, cost=0.004),
        ]
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        ctrl_stats = next(vs for vs in results.variant_stats if vs.is_control)
        assert ctrl_stats.mean_cost_usd == pytest.approx(0.003)

    def test_95_ci_on_business_metric(self):
        svc = LLMEvaluationAnalyticsService()
        ctrl_bm = [0.1, 0.2, 0.15, 0.18, 0.12]
        exp, evals = _make_experiment_with_data(control_bm=ctrl_bm)
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        ctrl_stats = next(vs for vs in results.variant_stats if vs.is_control)
        assert ctrl_stats.business_metric_ci_lower is not None
        assert ctrl_stats.business_metric_ci_upper is not None
        assert ctrl_stats.business_metric_ci_lower < ctrl_stats.mean_business_metric
        assert ctrl_stats.business_metric_ci_upper > ctrl_stats.mean_business_metric

    def test_p_value_significant_difference(self):
        svc = LLMEvaluationAnalyticsService()
        import random

        rng = random.Random(99)
        # Use values with variance so Welch t-test fires properly
        ctrl_bm = [0.1 + rng.gauss(0, 0.02) for _ in range(20)]
        trt_bm = [0.5 + rng.gauss(0, 0.02) for _ in range(20)]
        exp, evals = _make_experiment_with_data(control_bm=ctrl_bm, treatment_bm=trt_bm)
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        trt_stats = next(vs for vs in results.variant_stats if not vs.is_control)
        assert trt_stats.p_value is not None
        assert trt_stats.p_value < 0.05

    def test_winner_is_variant_with_higher_business_metric(self):
        svc = LLMEvaluationAnalyticsService()
        ctrl_bm = [0.2] * 10
        trt_bm = [0.8] * 10
        exp, evals = _make_experiment_with_data(control_bm=ctrl_bm, treatment_bm=trt_bm)
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        assert results.winner_variant_name == "treatment"

    def test_winner_based_on_auto_eval_when_no_business_metric(self):
        svc = LLMEvaluationAnalyticsService()
        ctrl_ae = [0.3] * 10
        trt_ae = [0.9] * 10
        exp, evals = _make_experiment_with_data(control_ae=ctrl_ae, treatment_ae=trt_ae)
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        assert results.winner_variant_name == "treatment"

    def test_no_winner_when_no_scores(self):
        svc = LLMEvaluationAnalyticsService()
        exp, evals = _make_experiment_with_data()
        db = self._make_db(experiment=exp, evals=[])
        results = svc.get_experiment_results(db, exp.id)
        assert results.winner_variant_id is None

    def test_single_variant_control_only(self):
        """Results work when only control has evaluations."""
        svc = LLMEvaluationAnalyticsService()
        exp, _ = _make_experiment_with_data()
        evals = [_make_eval(exp.id, exp.variants[0].id, bm=0.5) for _ in range(5)]
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        ctrl_stats = next(vs for vs in results.variant_stats if vs.is_control)
        assert ctrl_stats.n_evaluations == 5

    def test_results_returns_correct_experiment_name(self):
        svc = LLMEvaluationAnalyticsService()
        exp, evals = _make_experiment_with_data()
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        assert results.experiment_name == exp.name

    def test_human_rating_aggregation(self):
        svc = LLMEvaluationAnalyticsService()
        exp, _ = _make_experiment_with_data()
        evals = [
            _make_eval(exp.id, exp.variants[0].id, human_rating=4.0),
            _make_eval(exp.id, exp.variants[0].id, human_rating=5.0),
        ]
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        ctrl_stats = next(vs for vs in results.variant_stats if vs.is_control)
        assert ctrl_stats.mean_human_rating == pytest.approx(4.5)

    def test_effect_size_calculated(self):
        svc = LLMEvaluationAnalyticsService()
        import random

        rng = random.Random(7)
        # Use values with small variance so Cohen's d is large
        ctrl_bm = [0.1 + rng.gauss(0, 0.05) for _ in range(30)]
        trt_bm = [0.9 + rng.gauss(0, 0.05) for _ in range(30)]
        exp, evals = _make_experiment_with_data(control_bm=ctrl_bm, treatment_bm=trt_bm)
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        trt_stats = next(vs for vs in results.variant_stats if not vs.is_control)
        assert trt_stats.effect_size is not None
        assert abs(trt_stats.effect_size) > 5.0  # Very large effect

    def test_total_evaluations_count(self):
        svc = LLMEvaluationAnalyticsService()
        ctrl_bm = [0.3] * 7
        trt_bm = [0.6] * 9
        exp, evals = _make_experiment_with_data(control_bm=ctrl_bm, treatment_bm=trt_bm)
        db = self._make_db(experiment=exp, evals=evals)
        results = svc.get_experiment_results(db, exp.id)
        assert results.total_evaluations == 16


# ---------------------------------------------------------------------------
# Human rating submission
# ---------------------------------------------------------------------------


class TestSubmitHumanRating:
    def test_valid_rating_stored(self):
        svc = LLMEvaluationAnalyticsService()
        ev = _make_eval(uuid.uuid4(), uuid.uuid4())
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = ev
        db.commit.return_value = None
        db.refresh.return_value = None
        result = svc.submit_human_rating(db, ev.id, 4.5)
        assert ev.human_rating == 4.5

    def test_out_of_range_rating_raises(self):
        svc = LLMEvaluationAnalyticsService()
        with pytest.raises(ValueError, match="human_rating"):
            svc.submit_human_rating(MagicMock(), uuid.uuid4(), 6.0)

    def test_below_range_rating_raises(self):
        svc = LLMEvaluationAnalyticsService()
        with pytest.raises(ValueError, match="human_rating"):
            svc.submit_human_rating(MagicMock(), uuid.uuid4(), 0.5)

    def test_missing_evaluation_raises(self):
        svc = LLMEvaluationAnalyticsService()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError, match="not found"):
            svc.submit_human_rating(db, uuid.uuid4(), 3.0)


# ---------------------------------------------------------------------------
# LLM-as-judge
# ---------------------------------------------------------------------------


class TestRunLLMAsJudge:
    @pytest.mark.asyncio
    async def test_judge_scores_evaluations(self):
        svc = LLMEvaluationAnalyticsService()
        exp_id = uuid.uuid4()
        ev = _make_eval(exp_id, uuid.uuid4())
        ev.auto_eval_score = None

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [ev]
        db.query.return_value.filter.return_value.filter.return_value.all.return_value = [
            ev
        ]
        db.commit.return_value = None
        db.add.return_value = None

        # Mock the anthropic call inside _judge_response
        with patch.object(
            svc,
            "_judge_response",
            new=AsyncMock(return_value=(0.85, "Good response", None)),
        ):
            results = await svc.run_llm_as_judge(db, exp_id, "helpfulness")

        assert len(results) == 1
        assert results[0].score == 0.85
        assert results[0].reasoning == "Good response"
        assert results[0].error is None

    @pytest.mark.asyncio
    async def test_judge_updates_auto_eval_score(self):
        svc = LLMEvaluationAnalyticsService()
        exp_id = uuid.uuid4()
        ev = _make_eval(exp_id, uuid.uuid4())
        ev.auto_eval_score = None

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [ev]
        db.commit.return_value = None
        db.add.return_value = None

        with patch.object(
            svc, "_judge_response", new=AsyncMock(return_value=(0.7, "OK", None))
        ):
            await svc.run_llm_as_judge(db, exp_id, "accuracy")

        assert ev.auto_eval_score == 0.7

    def test_parse_judge_response_valid_json(self):
        svc = LLMEvaluationAnalyticsService()
        score, reasoning, error = svc._parse_judge_response(
            '{"score": 0.75, "reasoning": "Good answer"}'
        )
        assert score == pytest.approx(0.75)
        assert reasoning == "Good answer"
        assert error is None


def _judge_db(*evaluations: LLMEvaluation) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = list(evaluations)
    db.query.return_value.filter.return_value.filter.return_value.all.return_value = (
        list(evaluations)
    )
    return db


def _anthropic_replying(*replies):
    """``anthropic.AsyncAnthropic`` replaced: each ``messages.create`` call gives
    the next reply, a text block, or raises it when it is an exception.

    Returns the patch and the ``create`` mock, so a test can show the judge
    was called rather than skipped.
    """
    answers = [
        r
        if isinstance(r, BaseException)
        else SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])
        for r in replies
    ]
    create = AsyncMock(side_effect=answers)
    client = MagicMock()
    client.messages.create = create
    return patch("anthropic.AsyncAnthropic", return_value=client), create


@pytest.mark.regression
class TestTheJudgeStoresNoInventedScore:
    """#1192: a judge call that raised scored the response 0.5, and a reply
    without a usable score was read as 0.5, as the first 0 or 1 in its text,
    or clamped into 0 to 1. Each now stores no score: ``auto_eval_score`` is
    None, replacing any earlier score, and the result says why in ``error``.

    These replace ``test_judge_fallback_on_api_error``, which patched
    ``_judge_response`` itself and so never reached the code it named.
    """

    @pytest.mark.asyncio
    async def test_a_judge_call_that_fails_leaves_the_evaluation_unscored(self):
        svc = LLMEvaluationAnalyticsService()
        exp_id = uuid.uuid4()
        ev = _make_eval(exp_id, uuid.uuid4())
        # What the Anthropic SDK raises when no key is set (measured, 1.12.1)
        failure = TypeError("Could not resolve authentication method.")
        anthropic_patch, create = _anthropic_replying(failure)
        with anthropic_patch:
            results = await svc.run_llm_as_judge(_judge_db(ev), exp_id, "safety")

        create.assert_called_once()
        assert ev.auto_eval_score is None
        assert results[0].score is None
        assert results[0].error == "call_failed"
        # A fixed text: the exception goes to the log, not the response
        assert results[0].reasoning == JUDGE_CALL_FAILED_REASONING
        assert "authentication" not in results[0].reasoning

    @pytest.mark.asyncio
    async def test_with_no_key_the_judge_calls_nothing_and_stores_no_score(
        self, monkeypatch, tmp_path
    ):
        """The real SDK with no key, token or profile set, and its network replaced
        by a transport that records: nothing is sent, nothing is scored."""
        import anthropic
        import httpx2

        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_PROFILE",
            "ANTHROPIC_IDENTITY_TOKEN",
            "ANTHROPIC_IDENTITY_TOKEN_FILE",
            "ANTHROPIC_FEDERATION_RULE_ID",
            "ANTHROPIC_BASE_URL",
        ):
            monkeypatch.delenv(name, raising=False)
        # An empty profile directory, so no profile on this machine is found
        monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))
        sent = []

        def refuse(request):
            sent.append(request)
            return httpx2.Response(401, json={"error": "unexpected request"})

        real = anthropic.AsyncAnthropic

        class Recorded(real):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs):
                kwargs.setdefault(
                    "http_client",
                    httpx2.AsyncClient(transport=httpx2.MockTransport(refuse)),
                )
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(anthropic, "AsyncAnthropic", Recorded)
        svc = LLMEvaluationAnalyticsService()
        exp_id = uuid.uuid4()
        ev = _make_eval(exp_id, uuid.uuid4(), auto_eval=0.5)
        results = await svc.run_llm_as_judge(_judge_db(ev), exp_id)

        assert sent == []
        assert ev.auto_eval_score is None
        assert (results[0].score, results[0].error) == (None, "call_failed")

    @pytest.mark.asyncio
    async def test_a_rejudge_replaces_an_earlier_score_or_clears_it(self):
        svc = LLMEvaluationAnalyticsService()
        exp_id = uuid.uuid4()
        # 0.5 is what an earlier version stored for a failed call
        ev = _make_eval(exp_id, uuid.uuid4(), auto_eval=0.5)

        anthropic_patch, create = _anthropic_replying(
            '{"score": 0.8, "reasoning": "clear"}'
        )
        with anthropic_patch:
            await svc.run_llm_as_judge(_judge_db(ev), exp_id)
        create.assert_called_once()
        assert ev.auto_eval_score == 0.8

        anthropic_patch, create = _anthropic_replying(RuntimeError("API down"))
        with anthropic_patch:
            results = await svc.run_llm_as_judge(_judge_db(ev), exp_id)
        create.assert_called_once()
        assert ev.auto_eval_score is None
        assert results[0].error == "call_failed"

    def test_a_score_stored_before_the_upgrade_is_kept_until_the_judge_runs_again(
        self,
    ):
        """Reading the results changes no stored score: an earlier version's
        0.5 still counts as a score, because nothing tells it from a real 0.5;
        running the judge again (the test above) is what re-scores it."""
        svc = LLMEvaluationAnalyticsService()
        exp, evals = _make_experiment_with_data(
            control_ae=[0.5, 0.5, 0.5], treatment_ae=[0.7, 0.6]
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp
        db.query.return_value.filter.return_value.all.return_value = evals
        results = svc.get_experiment_results(db, exp.id)
        control = next(vs for vs in results.variant_stats if vs.is_control)
        assert control.mean_auto_eval_score == 0.5
        assert control.n_auto_eval_unscored == 0
        assert [e.auto_eval_score for e in evals[:3]] == [0.5, 0.5, 0.5]


# Replies with no usable score, each by the way the old parser read it.
_UNSCORED_REPLIES = {
    "no_score_key": '{"reasoning": "fine"}',  # was 0.5
    "null": '{"score": null, "reasoning": "x"}',  # was 0.5
    "not_a_number": '{"score": "high"}',  # was 0.5
    "boolean": '{"score": true}',  # was 1.0
    "nan": '{"score": NaN}',  # was 1.0
    "infinity": '{"score": Infinity}',  # was 1.0
    "minus_infinity": '{"score": -Infinity}',  # was 0.0
    "above_one": '{"score": 1.7}',  # was clamped to 1.0
    "out_of_five": '{"score": 7}',  # was clamped to 1.0
    "below_zero": '{"score": -0.2}',  # was clamped to 0.0
    "nan_string": '{"score": "nan"}',  # was 1.0
    "a_list": "[0.9]",  # was 0.9, from the text
    "a_bare_number": "0.8",  # was 0.8
    "free_text": "The score is 0.8 because it was helpful.",  # was 0.8
    "out_of_five_in_text": "I give it 1 out of 5.",  # was 1.0
    "scale_in_text": "On a scale of 0 to 1, I give it 0.9.",  # was 0.0
    "steps_in_text": "Step 1: read it. Score 0.2",  # was 1.0
    "no_number": "no useful content",  # was 0.5
    "empty": "",  # was 0.5
    "broken_json": '{"score": 0.9, "reasoning": ',  # was 0.9, from the text
}

# Replies that do carry a score, with the score each must give.
_SCORED_REPLIES = {
    "bare_object": ('{"score": 0.75, "reasoning": "Good answer"}', 0.75),
    "fenced": ('```json\n{"score": 0.8, "reasoning": "x"}\n```', 0.8),
    "in_prose": ('Here it is: {"score": 0.3, "reasoning": "terse"} Thanks.', 0.3),
    "numeric_string": ('{"score": "0.85"}', 0.85),
    "zero": ('{"score": 0}', 0.0),
    "one": ('{"score": 1}', 1.0),
}


@pytest.mark.regression
class TestParseJudgeResponse:
    """#1192: only a JSON object's ``score``, finite and from 0 to 1, is a
    score. Nothing is read from free text, nothing is clamped, and nothing is
    assumed for a missing score."""

    @pytest.mark.parametrize("case", sorted(_UNSCORED_REPLIES))
    def test_a_reply_without_a_usable_score_is_unscored(self, case):
        raw = _UNSCORED_REPLIES[case]
        score, reasoning, error = LLMEvaluationAnalyticsService._parse_judge_response(
            raw
        )
        assert score is None, (case, score)
        assert error == "no_score"
        assert reasoning == JUDGE_NO_SCORE_REASONING + raw[:200]

    @pytest.mark.parametrize("case", sorted(_SCORED_REPLIES))
    def test_a_json_score_from_0_to_1_is_read_bare_fenced_or_in_prose(self, case):
        raw, expected = _SCORED_REPLIES[case]
        score, _, error = LLMEvaluationAnalyticsService._parse_judge_response(raw)
        assert (score, error) == (expected, None)

    def test_the_reply_in_the_reasoning_is_cut_at_200_characters(self):
        raw = "x" * 5000
        _, reasoning, _ = LLMEvaluationAnalyticsService._parse_judge_response(raw)
        assert reasoning == JUDGE_NO_SCORE_REASONING + "x" * 200


@pytest.mark.regression
class TestUnscoredCount:
    """#1192: the results say how many evaluations have no judge score, per
    variant and for the experiment, and leave them out of the statistics."""

    def test_unscored_evaluations_are_counted_and_left_out(self):
        svc = LLMEvaluationAnalyticsService()
        exp, evals = _make_experiment_with_data(
            control_ae=[0.6, 0.7, 0.65], treatment_ae=[0.8, 0.9, 0.85, 0.7]
        )
        control, treatment = exp.variants
        # Two never judged (or the judge failed) in treatment, one in control
        evals += [_make_eval(exp.id, treatment.id) for _ in range(2)]
        evals += [_make_eval(exp.id, control.id)]
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = exp
        db.query.return_value.filter.return_value.all.return_value = evals
        results = svc.get_experiment_results(db, exp.id)
        by_name = {vs.variant_name: vs for vs in results.variant_stats}

        assert by_name["control"].n_auto_eval_unscored == 1
        assert by_name["treatment"].n_auto_eval_unscored == 2
        assert results.total_auto_eval_unscored == 3
        assert results.total_auto_eval_unscored == sum(
            vs.n_auto_eval_unscored for vs in results.variant_stats
        )
        assert by_name["treatment"].n_evaluations == 6
        assert by_name["treatment"].mean_auto_eval_score == statistics.mean(
            [0.8, 0.9, 0.85, 0.7]
        )
