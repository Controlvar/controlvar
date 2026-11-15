"""
A revenue metric in ``/results`` is its mean value per assigned user (#439).

``per_user_mean_metric_result`` turns each variant's assigned users, its
converting users and the value totals of the users who have one into the
metric's result: every assigned user is one unit, worth 0 without a value.
``welch_difference_intervals`` gives each treatment's difference from the
control with its Welch-Satterthwaite interval.  Both are held to scipy on the
per-user arrays, zeros included.

The dataset is fixed, 10 users per arm:

* control: 20 and 35.5, a purchase with no value (that user converted and
  adds 0), and seven users who bought nothing;
* treatment: a repeat buyer (20 + 40), then 60, 35.5, 12.25, and six users
  who bought nothing.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy import stats

from backend.app.services import sufficient_stats_analysis as module
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    SufficientStatsRefused,
    per_user_mean_metric_result,
    welch_difference_intervals,
)

pytestmark = [pytest.mark.unit]

CONTROL = BinomialVariant("c", "control", True)
TREATMENT = BinomialVariant("t", "treatment", False)
SECOND = BinomialVariant("u", "second", False)

#: Value totals of the users who have one; everyone else in the arm has 0.
CONTROL_TOTALS = [20.0, 35.5]
TREATMENT_TOTALS = [20.0 + 40.0, 60.0, 35.5, 12.25]
USERS = 10
#: Users who sent the purchase event: the control's no-value buyer included.
CONTROL_BUYERS = 3
TREATMENT_BUYERS = 4


def _metric(lower_is_better=None, metric_type="revenue"):
    metric = SimpleNamespace(
        id="m", name="Order value", metric_type=metric_type, is_primary=True
    )
    if lower_is_better is not None:
        metric.lower_is_better = lower_is_better
    return metric


def _per_user(totals, users):
    return np.array(list(totals) + [0.0] * (users - len(totals)))


def _result(alpha=0.05, correction="none", metric=None):
    return per_user_mean_metric_result(
        [
            (CONTROL, USERS, CONTROL_BUYERS, CONTROL_TOTALS),
            (TREATMENT, USERS, TREATMENT_BUYERS, TREATMENT_TOTALS),
        ],
        alpha,
        correction,
        metric=metric or _metric(),
    )


def _variants(result):
    by_name = {v["variant_name"]: v for v in result["variants"]}
    return by_name["control"], by_name["treatment"]


def _finite_json(result):
    """The result as the API serialises it: NaN or an infinity would raise."""
    return json.dumps(result, allow_nan=False)


# ---------------------------------------------------------------------------
# The oracle: scipy on the per-user arrays
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.95, 0.90])
def test_revenue_per_user_matches_scipy_on_the_per_user_values(confidence):
    control_y = _per_user(CONTROL_TOTALS, USERS)
    treatment_y = _per_user(TREATMENT_TOTALS, USERS)
    welch = stats.ttest_ind(treatment_y, control_y, equal_var=False)

    result = _result(alpha=1 - confidence)
    control, treatment = _variants(result)

    assert result["analysis"] == "mean"
    assert result["metric_type"] == "revenue"
    # Every assigned user counts, non-buyers as 0: not 27.75 and 41.9375.
    assert control["mean"] == pytest.approx(5.55, rel=1e-12)
    assert treatment["mean"] == pytest.approx(16.775, rel=1e-12)
    # The spread is over per-user totals, so the repeat buyer is one 60.
    assert control["std_dev"] == pytest.approx(np.std(control_y, ddof=1), rel=1e-12)
    assert treatment["std_dev"] == pytest.approx(np.std(treatment_y, ddof=1), rel=1e-12)
    for variant, y in ((control, control_y), (treatment, treatment_y)):
        half = stats.t.ppf(1 - (1 - confidence) / 2, len(y) - 1) * stats.sem(y)
        assert variant["confidence_interval"] == pytest.approx(
            (y.mean() - half, y.mean() + half), rel=1e-12
        )
    # Welch's test, not a test on the share of buyers.
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["absolute_difference"] == pytest.approx(11.225, rel=1e-12)
    assert treatment["absolute_difference_ci"] == pytest.approx(
        tuple(welch.confidence_interval(confidence)), rel=1e-9
    )
    assert treatment["relative_improvement_pct"] == pytest.approx(
        (16.775 - 5.55) / 5.55 * 100, rel=1e-12
    )
    # The buyer count is the users who sent the event, as for a proportion.
    assert (control["conversions"], treatment["conversions"]) == (3, 4)
    assert (control["sample_size"], treatment["sample_size"]) == (10, 10)
    for comparison in (
        "p_value",
        "absolute_difference",
        "absolute_difference_ci",
        "relative_improvement_pct",
        "statistical_test_used",
    ):
        assert control[comparison] is None, comparison


def test_the_numbers_from_the_scipy_run_are_the_documented_ones():
    """The figures of the dataset above, so a change in scipy is visible."""
    control, treatment = _variants(_result())
    assert treatment["p_value"] == pytest.approx(0.230022, abs=5e-7)
    low, high = treatment["absolute_difference_ci"]
    assert (low, high) == pytest.approx((-8.031377, 30.481377), abs=5e-7)
    assert treatment["relative_improvement_pct"] == pytest.approx(202.2523, abs=5e-5)


def test_the_difference_interval_is_welch_not_pooled():
    control_y = _per_user(CONTROL_TOTALS, USERS)
    treatment_y = _per_user(TREATMENT_TOTALS, USERS)
    pooled = stats.ttest_ind(treatment_y, control_y, equal_var=True)
    _, treatment = _variants(_result())
    assert treatment["absolute_difference_ci"] != pytest.approx(
        tuple(pooled.confidence_interval(0.95)), rel=1e-6
    )


def test_a_correction_adjusts_the_welch_p_values_and_not_the_difference():
    result = per_user_mean_metric_result(
        [
            (CONTROL, USERS, CONTROL_BUYERS, CONTROL_TOTALS),
            (TREATMENT, USERS, TREATMENT_BUYERS, TREATMENT_TOTALS),
            (SECOND, USERS, 1, [5.0]),
        ],
        0.05,
        "bonferroni",
        metric=_metric(),
    )
    rows = {v["variant_name"]: v for v in result["variants"]}
    for name in ("treatment", "second"):
        row = rows[name]
        assert row["adjusted_p_value"] == pytest.approx(min(1.0, 2 * row["p_value"]))
    unadjusted = _variants(_result())[1]
    assert rows["treatment"]["absolute_difference_ci"] == pytest.approx(
        unadjusted["absolute_difference_ci"], rel=1e-12
    )


# ---------------------------------------------------------------------------
# The winner, in the metric's direction
# ---------------------------------------------------------------------------


def _clear_win():
    """200 users per arm; the treatment's buyers spend more, often enough."""
    control = [50.0 + (i % 7) for i in range(40)]
    treatment = [80.0 + (i % 11) for i in range(80)]
    return [
        (CONTROL, 200, len(control), control),
        (TREATMENT, 200, len(treatment), treatment),
    ]


def test_a_significant_rise_is_the_winner_when_higher_is_better():
    result = per_user_mean_metric_result(
        _clear_win(), 0.05, "none", metric=_metric(lower_is_better=False)
    )
    _, treatment = _variants(result)
    assert treatment["is_significant"] is True
    assert result["winning_variant_id"] == "t"
    assert result["has_significant_result"] is True


def test_a_significant_rise_is_no_winner_when_lower_is_better():
    result = per_user_mean_metric_result(
        _clear_win(), 0.05, "none", metric=_metric(lower_is_better=True)
    )
    _, treatment = _variants(result)
    assert treatment["is_significant"] is True
    assert result["winning_variant_id"] is None
    assert result["lower_is_better"] is True


# ---------------------------------------------------------------------------
# Never raises for well-formed input
# ---------------------------------------------------------------------------


def test_before_the_first_purchase_every_arm_is_zero_and_nothing_raises():
    result = per_user_mean_metric_result(
        [(CONTROL, 50, 0, []), (TREATMENT, 50, 0, [])], 0.05, "none", metric=_metric()
    )
    control, treatment = _variants(result)
    _finite_json(result)
    assert control["mean"] == 0.0 and treatment["mean"] == 0.0
    assert treatment["p_value"] is None
    assert treatment["statistical_test_used"] is None
    assert treatment["absolute_difference"] == 0.0
    assert treatment["absolute_difference_ci"] is None
    assert treatment["relative_improvement_pct"] is None
    assert result["winning_variant_id"] is None


def test_both_arms_all_zero_give_a_null_interval_not_a_division_by_zero():
    intervals = welch_difference_intervals(
        [(CONTROL, 3, 0.0, 0.0), (TREATMENT, 3, 0.0, 0.0)], 0.05
    )
    assert intervals == {"t": (0.0, None)}


def test_a_difference_needs_2_units_in_both_arms_and_a_control():
    rows = [(CONTROL, 1, 0.0, 0.0), (TREATMENT, 5, 1.0, 4.0), (SECOND, 0, 0.0, 0.0)]
    assert welch_difference_intervals(rows, 0.05) == {
        "t": (None, None),
        "u": (None, None),
    }
    with pytest.raises(ValueError, match="control"):
        welch_difference_intervals(rows[1:], 0.05)


def test_an_arm_with_no_users_has_mean_0_and_no_nan_anywhere():
    result = per_user_mean_metric_result(
        [
            (CONTROL, USERS, CONTROL_BUYERS, CONTROL_TOTALS),
            (TREATMENT, USERS, TREATMENT_BUYERS, TREATMENT_TOTALS),
            (SECOND, 0, 0, []),
        ],
        0.05,
        "none",
        metric=_metric(),
    )
    second = next(v for v in result["variants"] if v["variant_name"] == "second")
    _finite_json(result)
    assert second["sample_size"] == 0
    assert second["mean"] == 0.0
    assert second["std_dev"] is None
    assert second["confidence_interval"] == (0.0, 0.0)
    assert second["p_value"] is None
    assert second["absolute_difference"] is None
    # The other treatment is still compared.
    _, treatment = _variants(result)
    assert treatment["p_value"] == pytest.approx(0.230022, abs=5e-7)


def test_an_arm_with_one_user_has_its_mean_and_no_comparison():
    result = per_user_mean_metric_result(
        [(CONTROL, USERS, CONTROL_BUYERS, CONTROL_TOTALS), (TREATMENT, 1, 1, [42.0])],
        0.05,
        "none",
        metric=_metric(),
    )
    _, treatment = _variants(result)
    _finite_json(result)
    assert treatment["mean"] == 42.0
    assert treatment["std_dev"] is None
    assert treatment["confidence_interval"] == (42.0, 42.0)
    assert treatment["p_value"] is None
    assert treatment["absolute_difference"] is None
    assert treatment["relative_improvement_pct"] is None


def test_a_control_with_one_user_leaves_every_treatment_uncompared():
    result = per_user_mean_metric_result(
        [
            (CONTROL, 1, 1, [10.0]),
            (TREATMENT, USERS, TREATMENT_BUYERS, TREATMENT_TOTALS),
        ],
        0.05,
        "none",
        metric=_metric(),
    )
    control, treatment = _variants(result)
    _finite_json(result)
    assert control["std_dev"] is None
    # The treatment still has its own spread and interval.
    y = _per_user(TREATMENT_TOTALS, USERS)
    assert treatment["std_dev"] == pytest.approx(np.std(y, ddof=1), rel=1e-12)
    assert treatment["p_value"] is None
    assert treatment["absolute_difference"] is None
    assert result["winning_variant_id"] is None


@pytest.mark.regression
def test_a_control_with_no_buyer_has_mean_0_and_no_relative_change():
    """A no-buyer control's mean is 0 exactly, never k + sum_d / n.

    With 3 control users and buyers of 85 and 35.5 in the treatment, the
    centred mean of the control rounds to -3.6e-15 instead of 0; read as the
    control's mean it gives a relative change of about 10**18 %.
    """
    k = (85.0 + 35.5) / 5
    assert k + (-k * 3) / 3 != 0.0  # the rounding this test is about
    result = per_user_mean_metric_result(
        [(CONTROL, 3, 0, []), (TREATMENT, 2, 2, [85.0, 35.5])],
        0.05,
        "none",
        metric=_metric(),
    )
    control, treatment = _variants(result)
    _finite_json(result)
    assert control["mean"] == 0.0
    assert control["confidence_interval"] == (0.0, 0.0)
    assert treatment["mean"] == 60.25
    assert treatment["relative_improvement_pct"] is None
    assert treatment["absolute_difference"] == pytest.approx(60.25, rel=1e-12)


@pytest.mark.regression
def test_an_arm_whose_users_all_have_one_value_has_no_spread():
    """An all-zero arm far from the grand mean has standard deviation 0.

    sum((y - k) ** 2) over six zeros, with k about 8.3e10, rounds to a sum of
    squares that is not sum_d ** 2 / n; read back as a variance it gave this
    arm a standard deviation of about 1295.
    """
    result = per_user_mean_metric_result(
        [(CONTROL, 6, 6, []), (TREATMENT, 6, 2, [1e12, 10.0])],
        0.05,
        "none",
        metric=_metric(),
    )
    control, treatment = _variants(result)
    assert control["std_dev"] == 0.0
    assert control["confidence_interval"] == (0.0, 0.0)
    y = _per_user([1e12, 10.0], 6)
    assert treatment["std_dev"] == pytest.approx(np.std(y, ddof=1), rel=1e-12)
    welch = stats.ttest_ind(y, np.zeros(6), equal_var=False)
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)


def test_sums_the_estimator_refuses_leave_the_metric_uncompared(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise SufficientStatsRefused("the sums give a negative variance")

    monkeypatch.setattr(module, "mean_metric_result", refuse)
    result = _result()
    control, treatment = _variants(result)
    _finite_json(result)
    assert (control["mean"], treatment["mean"]) == pytest.approx((5.55, 16.775))
    assert treatment["p_value"] is None
    assert treatment["absolute_difference"] is None
    assert result["winning_variant_id"] is None


def test_values_offset_by_1e9_keep_their_precision():
    control = [1e9 + 3.0, 1e9 + 1.0, 1e9 + 2.0]
    treatment = [1e9 + 6.0, 1e9 + 4.0, 1e9 + 5.0, 1e9 + 4.5]
    result = per_user_mean_metric_result(
        [(CONTROL, 3, 3, control), (TREATMENT, 4, 4, treatment)],
        0.05,
        "none",
        metric=_metric(),
    )
    welch = stats.ttest_ind(
        np.array(treatment) - 1e9, np.array(control) - 1e9, equal_var=False
    )
    _, row = _variants(result)
    assert row["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert row["absolute_difference"] == pytest.approx(2.875, rel=1e-9)
    assert row["absolute_difference_ci"] == pytest.approx(
        tuple(welch.confidence_interval(0.95)), rel=1e-9
    )


# ---------------------------------------------------------------------------
# Refused input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_total_that_is_not_finite_is_refused(bad):
    with pytest.raises(SufficientStatsRefused, match="not finite"):
        per_user_mean_metric_result(
            [(CONTROL, 3, 1, [bad]), (TREATMENT, 3, 0, [])],
            0.05,
            "none",
            metric=_metric(),
        )


def test_no_control_is_a_value_error():
    with pytest.raises(ValueError, match="control"):
        per_user_mean_metric_result(
            [(TREATMENT, 3, 0, [])], 0.05, "none", metric=_metric()
        )


def test_a_user_read_with_a_total_is_one_of_the_arms_users():
    """A user assigned between the two reads counts in n, never as -1 zeros."""
    result = per_user_mean_metric_result(
        [(CONTROL, 1, 2, [10.0, 20.0]), (TREATMENT, 2, 0, [])],
        0.05,
        "none",
        metric=_metric(),
    )
    control, _ = _variants(result)
    assert control["sample_size"] == 2
    assert control["mean"] == 15.0
