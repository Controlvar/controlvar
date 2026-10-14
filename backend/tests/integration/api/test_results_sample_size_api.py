"""GET /api/v1/results/{id}/sample-size plans from the experiment's own data (#666).

Before the fix the route planned every experiment from a fixed 10% baseline
(57,763 per variant at the defaults), called the total divided by two the
"smallest variant", and checked the experiment existed by running the whole
analysis while swallowing any error it raised.

Now, with no query parameters, the baseline is the control variant's
conversion rate on the primary metric, counted the way ``/results`` counts it
(converting users over assigned users), progress is the smallest variant, and
every input comes back with where it came from. With nothing to plan from the
answer is 200 with nulls and ``unavailable_reason``.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

import math
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pytest
from scipy.stats import norm

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.power_calculator_service import (
    SAMPLE_SIZE_NOT_FINITE_MESSAGE,
    TREATMENT_RATE_CEILING_MESSAGE,
    compute_power,
    sample_size_two_proportions,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _reference(p1: float, mde: float, alpha: float, power: float) -> int:
    """Fleiss, two-sided, written out independently of the service."""
    p2 = p1 * (1 + mde)
    z_a = norm.ppf(1 - alpha / 2)
    z_b = norm.ppf(power)
    p_bar = (p1 + p2) / 2
    return math.ceil(
        (
            z_a * math.sqrt(2 * p_bar * (1 - p_bar))
            + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
        )
        ** 2
        / (p2 - p1) ** 2
    )


@pytest.fixture
def seed(db_session, make_experiment):
    """
    Build an experiment: ``arms`` is a list of (name, is_control, allocation,
    assigned users, converting users). Every converting user buys twice, so a
    count of events instead of users would show.
    """
    made = []

    def _seed(
        arms: List[Tuple[str, bool, int, int, int]],
        with_metric: bool = True,
        **overrides,
    ):
        suffix = uuid.uuid4().hex[:8]
        exp = make_experiment(
            name=f"Sample size {suffix}",
            key=f"sample-size-{suffix}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            **overrides,
        )
        made.append(exp)
        rows = [
            Variant(
                experiment_id=exp.id,
                name=name,
                is_control=is_control,
                traffic_allocation=allocation,
            )
            for name, is_control, allocation, _, _ in arms
        ]
        if with_metric:
            rows.append(
                Metric(
                    experiment_id=exp.id,
                    name="Purchase",
                    event_name="purchase",
                    metric_type=MetricType.CONVERSION,
                    is_primary=True,
                )
            )
        db_session.add_all(rows)
        db_session.commit()
        db_session.refresh(exp)
        by_name = {v.name: v for v in exp.variants}
        data = []
        now = datetime.now(timezone.utc).isoformat()
        for name, _, _, assigned, converting in arms:
            variant = by_name[name]
            for i in range(assigned):
                user_id = f"ss-{suffix}-{name}-{i}"
                data.append(
                    Assignment(
                        experiment_id=exp.id, variant_id=variant.id, user_id=user_id
                    )
                )
                if i < converting:
                    data.extend(
                        Event(
                            event_type="purchase",
                            event_name="purchase",
                            user_id=user_id,
                            experiment_id=exp.id,
                            variant_id=variant.id,
                            value=1.0,
                            created_at=now,
                        )
                        for _ in range(2)
                    )
        db_session.add_all(data)
        db_session.commit()
        return exp

    yield _seed
    db_session.rollback()
    for exp in made:
        for model in (Event, Assignment):
            db_session.query(model).filter(model.experiment_id == exp.id).delete(
                synchronize_session=False
            )
    db_session.commit()


def _get(client, exp_id, expect: int = 200, **params) -> Dict:
    response = client.get(f"/api/v1/results/{exp_id}/sample-size", params=params)
    assert response.status_code == expect, response.text
    return response.json()


# --- row 3: the experiment's own control rate, and the smallest arm -------------


@pytest.mark.regression
def test_plans_from_the_observed_control_rate_and_counts_the_smallest_arm(client, seed):
    """Three variants with no options: the experiment's stored correction,
    Benjamini-Hochberg by default (#580), plans each of the two comparisons
    at 0.05 / 2."""
    exp = seed(
        [
            ("control", True, 34, 50, 6),  # 6 / 50 = 0.12
            ("treatment_a", False, 33, 30, 3),
            ("treatment_b", False, 33, 20, 1),
        ]
    )
    data = _get(client, exp.id)

    # One comparison, so a failure prints all three.
    assert (
        data["required_sample_size_per_variant"],
        data["baseline_rate"],
        data["current_sample_size_per_variant"],
    ) == (_reference(0.12, 0.05, 0.025, 0.8), 0.12, 20)
    # The worked example in docs/statistics/power-analysis.md.
    assert data["required_sample_size_per_variant"] == 56961
    assert data["baseline_source"] == "observed"
    assert data["baseline_users"] == 50
    assert data["achieved_power"] == compute_power(20, 0.12, 0.126, 0.025, True)
    assert data["is_adequate"] is False
    assert data["unavailable_reason"] is None
    assert data["mde"] == 0.05
    assert data["mde_absolute"] == pytest.approx(0.006)
    assert data["confidence_level"] == 0.95
    assert data["power_target"] == 0.8
    assert data["alpha"] == pytest.approx(0.025)
    assert data["comparisons"] == 2
    assert data["correction_method"] == "benjamini_hochberg"
    assert data["analysed_as"] == "conversion"
    assert data["metric_name"] == "Purchase"
    assert data["metric_type"] == "conversion"
    assert data["metric_id"] == str(exp.metric_definitions[0].id)


def test_the_days_to_significance_and_the_completion_date_are_always_null(client, seed):
    """Neither is computed: a plan with traffic and one without both carry null."""
    planned = seed([("control", True, 50, 40, 7), ("treatment", False, 50, 40, 9)])
    empty = seed([("control", True, 50, 0, 0), ("treatment", False, 50, 0, 0)])
    for exp in (planned, empty):
        data = _get(client, exp.id)
        assert data["days_to_significance"] is None
        assert data["projected_completion_date"] is None
    assert _get(client, planned.id)["required_sample_size_per_variant"] is not None


def test_the_observed_rate_is_the_control_rate_the_results_show(client, seed):
    exp = seed([("control", True, 50, 40, 7), ("treatment", False, 50, 40, 9)])
    sample = _get(client, exp.id)
    response = client.get(f"/api/v1/results/{exp.id}", params={"use_cache": "false"})
    assert response.status_code == 200, response.text
    primary = next(m for m in response.json()["metrics"] if m["is_primary"])
    control = next(v for v in primary["variants"] if v["is_control"])
    assert sample["baseline_rate"] == control["mean"] == 7 / 40
    assert sample["baseline_users"] == control["sample_size"] == 40


def test_a_requested_baseline_wins_over_the_observed_one(client, seed):
    exp = seed([("control", True, 50, 40, 7), ("treatment", False, 50, 40, 9)])
    data = _get(client, exp.id, baseline_conversion_rate=0.12, mde=0.05)
    assert data["baseline_rate"] == 0.12
    assert data["baseline_source"] == "request"
    assert data["baseline_users"] is None
    assert data["required_sample_size_per_variant"] == 47036


@pytest.mark.parametrize("method", ["bonferroni", "benjamini_hochberg"])
def test_a_correction_plans_each_comparison_at_alpha_over_k_minus_1(
    client, seed, method
):
    exp = seed(
        [
            ("control", True, 34, 50, 6),
            ("treatment_a", False, 33, 50, 6),
            ("treatment_b", False, 33, 50, 6),
        ]
    )
    data = _get(client, exp.id, correction_method=method)
    assert data["alpha"] == pytest.approx(0.025)
    assert data["correction_method"] == method
    assert data["required_sample_size_per_variant"] == _reference(
        0.12, 0.05, 0.025, 0.8
    )
    plain = _get(client, exp.id, correction_method="none")
    assert (
        plain["required_sample_size_per_variant"]
        < data["required_sample_size_per_variant"]
    )


def test_every_input_is_honoured(client, seed):
    exp = seed([("control", True, 50, 40, 8), ("treatment", False, 50, 40, 9)])
    data = _get(client, exp.id, mde=0.137, confidence_level=0.9, power_target=0.95)
    assert data["required_sample_size_per_variant"] == _reference(0.2, 0.137, 0.1, 0.95)
    assert (data["mde"], data["confidence_level"], data["power_target"]) == (
        0.137,
        0.9,
        0.95,
    )


def test_reached_when_the_smallest_arm_has_the_planned_size(client, seed):
    # A 50% baseline and a 90% lift needs only a handful per arm.
    exp = seed([("control", True, 50, 20, 10), ("treatment", False, 50, 20, 10)])
    required = _reference(0.5, 0.9, 0.05, 0.8)
    data = _get(client, exp.id, mde=0.9, baseline_conversion_rate=0.5)
    assert data["required_sample_size_per_variant"] == required <= 20
    assert data["is_adequate"] is True
    assert data["achieved_power"] >= 0.8


# --- row 5: edge cases, each with its exact status and reason ------------------


def _assert_unavailable(data: Dict, reason: str, current: int) -> None:
    assert data["unavailable_reason"] == reason
    assert data["required_sample_size_per_variant"] is None
    assert data["achieved_power"] is None
    assert data["is_adequate"] is False
    assert data["current_sample_size_per_variant"] == current


@pytest.mark.regression
def test_no_assignments_answers_200_with_nulls(client, seed):
    """A new experiment's normal state: 200 and null, never a 500 (PE C6)."""
    exp = seed([("control", True, 50, 0, 0), ("treatment", False, 50, 0, 0)])
    data = _get(client, exp.id)
    _assert_unavailable(data, "no_control_data", 0)
    assert data["baseline_rate"] is None
    assert data["baseline_source"] is None
    assert data["mde_absolute"] is None


def test_no_control_conversions(client, seed):
    exp = seed([("control", True, 50, 10, 0), ("treatment", False, 50, 8, 2)])
    data = _get(client, exp.id)
    _assert_unavailable(data, "no_control_conversions", 8)
    assert data["baseline_users"] == 10
    assert data["baseline_rate"] is None


def test_every_control_user_converted(client, seed):
    exp = seed([("control", True, 50, 10, 10), ("treatment", False, 50, 10, 4)])
    _assert_unavailable(_get(client, exp.id), "rate_at_boundary", 10)


def test_observed_rate_raised_by_the_effect_reaches_100_percent(client, seed):
    exp = seed([("control", True, 50, 25, 24), ("treatment", False, 50, 25, 20)])
    data = _get(client, exp.id)
    _assert_unavailable(data, "effect_out_of_range", 25)
    assert data["baseline_rate"] == 0.96
    assert data["baseline_source"] == "observed"


def test_no_metric(client, seed):
    exp = seed(
        [("control", True, 50, 10, 0), ("treatment", False, 50, 10, 0)],
        with_metric=False,
    )
    data = _get(client, exp.id)
    _assert_unavailable(data, "no_metric", 10)
    assert data["metric_id"] is None and data["metric_name"] is None


def test_no_control_variant(client, seed):
    exp = seed([("a", False, 50, 10, 2), ("b", False, 50, 10, 2)])
    _assert_unavailable(_get(client, exp.id), "no_control_data", 10)


def test_a_requested_baseline_plans_without_any_traffic(client, seed):
    """With no users the power is 0.0, not null (PE C9)."""
    exp = seed([("control", True, 50, 0, 0), ("treatment", False, 50, 0, 0)])
    data = _get(client, exp.id, baseline_conversion_rate=0.12)
    assert data["required_sample_size_per_variant"] == 47036
    assert data["achieved_power"] == 0.0
    assert data["current_sample_size_per_variant"] == 0
    assert data["unavailable_reason"] is None


def test_one_variant(client, seed):
    exp = seed([("control", True, 100, 40, 8)])
    data = _get(client, exp.id)
    assert data["comparisons"] == 1
    assert data["current_sample_size_per_variant"] == 40
    assert data["required_sample_size_per_variant"] == _reference(0.2, 0.05, 0.05, 0.8)


@pytest.mark.regression
def test_a_requested_baseline_the_effect_raises_past_100_percent_is_refused(
    client, seed
):
    """On main: 200, with the treatment rate silently clamped below 1."""
    exp = seed([("control", True, 50, 0, 0), ("treatment", False, 50, 0, 0)])
    data = _get(client, exp.id, expect=422, baseline_conversion_rate=0.6, mde=0.8)
    assert data == {
        "detail": (
            "This baseline raised by this effect reaches 100% or more. "
            "Lower the baseline rate or the effect."
        )
    }
    # The one sentence the wizard and /utils answer with too.
    assert data["detail"] == TREATMENT_RATE_CEILING_MESSAGE


# --- #694: a size that is not a finite number ---------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    ("baseline", "mde"),
    [
        (1e-300, 0.05),  # (p2 - p1) ** 2 underflows to zero
        (0.12, 1e-17),  # the treatment rate rounds to the baseline
    ],
)
def test_a_requested_baseline_with_no_finite_size_is_refused(
    client, seed, baseline, mde
):
    """On main: 500 (OverflowError, or ValueError from the helper)."""
    exp = seed([("control", True, 50, 0, 0), ("treatment", False, 50, 0, 0)])
    data = _get(client, exp.id, expect=422, baseline_conversion_rate=baseline, mde=mde)
    assert data == {"detail": SAMPLE_SIZE_NOT_FINITE_MESSAGE}
    # The one sentence the wizard and /utils answer with too.
    assert data["detail"] == (
        "This baseline raised by this effect changes too little to estimate a "
        "sample size. Raise the baseline rate or the effect."
    )


@pytest.mark.regression
def test_an_observed_rate_with_no_finite_size_is_a_reason_not_an_error(client, seed):
    """On main: 500. The observed rate is not the caller's input, so this is a
    200 with nulls, like effect_out_of_range, rather than a 422."""
    exp = seed([("control", True, 50, 50, 6), ("treatment", False, 50, 40, 4)])
    data = _get(client, exp.id, mde=1e-17)
    _assert_unavailable(data, "effect_too_small", 40)
    assert data["baseline_rate"] == 0.12
    assert data["baseline_source"] == "observed"


def test_a_requested_baseline_just_inside_the_limit_is_still_planned(client, seed):
    exp = seed([("control", True, 50, 0, 0), ("treatment", False, 50, 0, 0)])
    data = _get(client, exp.id, baseline_conversion_rate=1e-160)
    assert data["unavailable_reason"] is None
    assert data["required_sample_size_per_variant"] == sample_size_two_proportions(
        1e-160, 1e-160 * 1.05, 1.0 - 0.95, 0.8, True
    )


# --- row 2: the guided setup and the results tab give the same number ---------


@pytest.mark.regression
@pytest.mark.parametrize("alpha", (0.01, 0.05, 0.10))
@pytest.mark.parametrize("power", (0.80, 0.90, 0.95))
@pytest.mark.parametrize("variants", (2, 3))
def test_the_tab_and_the_guided_setup_agree(client, seed, alpha, power, variants):
    """
    Every significance and power the guided setup offers, at two and three
    variants, with ``==``. The guided setup plans without a correction
    (#820), so the tab is asked for ``none``; its default is the
    experiment's stored correction (#580).
    test_sample_size_one_formula.py pins the guided setup and /utils to the
    same helper; this adds the results tab, which needs a database.
    """
    arms = [("control", True, 50, 0, 0)] + [
        (f"treatment_{i}", False, 50, 0, 0) for i in range(variants - 1)
    ]
    exp = seed(arms)
    tab = _get(
        client,
        exp.id,
        baseline_conversion_rate=0.12,
        mde=0.05,
        confidence_level=round(1 - alpha, 2),
        power_target=power,
        correction_method="none",
    )
    response = client.get(
        "/api/v1/experiments/analysis/sample-size",
        params={
            "baseline_rate": 0.12,
            "minimum_detectable_effect": 0.05,
            "significance_level": alpha,
            "statistical_power": power,
            "variant_count": variants,
        },
    )
    assert response.status_code == 200, response.text
    wizard = response.json()["samples_per_variant"]
    assert tab["required_sample_size_per_variant"] == wizard
    assert tab["comparisons"] == variants - 1
    if (alpha, power) == (0.05, 0.80):
        assert wizard == 47036


@pytest.mark.parametrize(
    "params",
    [
        {"mde": 1.0},
        {"mde": 0},
        {"baseline_conversion_rate": 0},
        {"baseline_conversion_rate": 1},
        {"correction_method": "holm"},
        {"confidence_level": 0.5},
        {"power_target": 0.3},
    ],
)
def test_out_of_range_inputs_answer_422(client, seed, params):
    exp = seed([("control", True, 50, 0, 0), ("treatment", False, 50, 0, 0)])
    _get(client, exp.id, expect=422, **params)


def test_unknown_experiment_is_404(client):
    data = _get(client, uuid.uuid4(), expect=404)
    assert data == {"detail": "Experiment not found"}


# --- SILENT: the route no longer runs the whole analysis -----------------------


def test_never_runs_the_full_analysis(client, seed, monkeypatch):
    """The existence check used to call it and swallow whatever it raised."""
    exp = seed([("control", True, 50, 40, 8), ("treatment", False, 50, 40, 9)])

    def boom(*args, **kwargs):
        raise AssertionError("get_experiment_results must not be called")

    monkeypatch.setattr(AnalysisService, "get_experiment_results", boom)
    assert _get(client, exp.id)["required_sample_size_per_variant"] is not None


# --- when a fixed sample size is only a guide ----------------------------------


@pytest.mark.parametrize(
    "overrides,allocations,expected",
    [
        ({}, (50, 50), []),
        ({}, (70, 30), ["unequal_allocation"]),
        ({"optimization_type": "thompson_sampling"}, (50, 50), ["adaptive_allocation"]),
        ({"sequential_testing_enabled": True}, (50, 50), ["sequential_testing"]),
        ({"bayesian_enabled": True}, (50, 50), ["bayesian"]),
    ],
)
def test_guide_only_reasons(
    client, seed, overrides, allocations, expected: Optional[List[str]]
):
    exp = seed(
        [
            ("control", True, allocations[0], 10, 2),
            ("treatment", False, allocations[1], 10, 2),
        ],
        **overrides,
    )
    assert _get(client, exp.id)["guide_only_reasons"] == expected
