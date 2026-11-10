"""The size-weighted estimate of an experiment that balances within groups (#448).

``balanced_groups_estimate`` gives one estimate per treatment from each
group's per-variant counts. These tests hold it to an independent reference:
one least-squares regression over every variant, with the group indicators
centred on their shares and interacted with every treatment, and its HC2
standard error (statsmodels; Lin, 2013). Each count is turned back into one
0/1 row per user, so the reference sees users, not the formula.

They also pin the constants worked out by hand for a three-group example
(groups of 40, 25 and 15 users, the last split 9/6), the rule for groups too
small to estimate, a standard error of 0, the correction across treatments and
the cases that give no estimate.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
import pytest
import statsmodels.api as sm
import statsmodels.formula.api as smf
from scipy import stats

from backend.app.services import balanced_groups_estimate as bge
from backend.app.services.balanced_groups_estimate import Arm, GroupCounts
from backend.app.services.sufficient_stats_analysis import adjusted_p_values

pytestmark = pytest.mark.unit

CONTROL = Arm("c0000000-0000-4000-8000-000000000000", "Control", True)
T1 = Arm("t1000000-0000-4000-8000-000000000000", "Treatment", False)
T2 = Arm("t2000000-0000-4000-8000-000000000000", "Treatment B", False)
TWO_ARMS = (CONTROL, T1)
THREE_ARMS = (CONTROL, T1, T2)

#: The hand-worked example: three groups of 40, 25 and 15 users; ``c`` is
#: split 9 treatment / 6 control. (users, converting users) per variant.
E5 = [
    GroupCounts("a", {T1.variant_id: (20, 5), CONTROL.variant_id: (20, 4)}),
    GroupCounts("b", {T1.variant_id: (12, 4), CONTROL.variant_id: (13, 3)}),
    GroupCounts("c", {T1.variant_id: (9, 3), CONTROL.variant_id: (6, 5)}),
]
#: sum_s w_s * (p_ts - p_cs), and its Neyman standard error, by hand.
E5_ESTIMATE = -0.036698717949
E5_STANDARD_ERROR = 0.099714422423
#: ``y ~ t * C(s)``'s coefficient on t: group a's effect alone.
E5_INTERACTED_COEFFICIENT = 0.050000000000
#: The treatment's rate minus the control's, ignoring the groups.
E5_NAIVE = -0.015009380863

TOLERANCE = 1e-9


def _overall(groups, arms=TWO_ARMS, level=0.95, method="benjamini_hochberg"):
    return bge.balanced_overall(
        groups, arms, confidence_level=level, correction_method=method
    )


def _entry(result, arm):
    return next(e for e in result["overall"] if e["variant_id"] == arm.variant_id)


def _rows(groups: Sequence[GroupCounts], arms: Sequence[Arm]) -> pd.DataFrame:
    """One row per user: its group's index, its variant's index, and y."""
    records: List[Tuple[int, int, float]] = []
    for g_index, group in enumerate(groups):
        for a_index, arm in enumerate(arms):
            users, converted = group.counts.get(arm.variant_id, (0, 0))
            records += [(g_index, a_index, 1.0)] * converted
            records += [(g_index, a_index, 0.0)] * (users - converted)
    return pd.DataFrame(records, columns=["s", "arm", "y"])


def lin_hc2(
    groups: Sequence[GroupCounts], arms: Sequence[Arm]
) -> Dict[str, Tuple[float, float]]:
    """Each treatment's coefficient and HC2 standard error in ONE regression.

    ``y ~ 1 + T_t + (D_s - w_s) + T_t:(D_s - w_s)`` over every user of every
    variant, where ``D_s`` indicates group ``s`` (the first group dropped) and
    ``w_s`` is its share of all the users; ``arms[0]`` must be the control.
    """
    assert arms[0].is_control
    data = _rows(groups, arms)
    columns = {"const": np.ones(len(data))}
    treatments = list(enumerate(arms))[1:]
    for a_index, arm in treatments:
        columns[f"T{a_index}"] = (data["arm"] == a_index).astype(float).to_numpy()
    for s in range(1, len(groups)):
        indicator = (data["s"] == s).astype(float).to_numpy()
        centred = indicator - indicator.mean()
        columns[f"D{s}"] = centred
        for a_index, _ in treatments:
            columns[f"T{a_index}D{s}"] = columns[f"T{a_index}"] * centred
    design = pd.DataFrame(columns)
    fit = sm.OLS(data["y"].to_numpy(), design).fit(cov_type="HC2")
    return {
        arm.variant_id: (
            float(fit.params[f"T{a_index}"]),
            float(fit.bse[f"T{a_index}"]),
        )
        for a_index, arm in treatments
    }


def _check_against_the_regression(groups, arms):
    result = _overall(groups, arms)
    assert result["reason"] is None and result["merged_groups"] == []
    reference = lin_hc2(groups, arms)
    for arm in arms[1:]:
        entry = _entry(result, arm)
        coefficient, hc2 = reference[arm.variant_id]
        assert abs(entry["absolute_difference"] - coefficient) <= TOLERANCE, (
            f"{arm.variant_name}: estimate {entry['absolute_difference']:.4f} "
            f"!= {coefficient:.4f}"
        )
        assert abs(entry["standard_error"] - hc2) <= TOLERANCE, (
            f"{arm.variant_name}: SE mismatch, {entry['standard_error']!r} "
            f"!= HC2 {hc2!r}"
        )


def _seeded_groups(seed: int, arms: Sequence[Arm], sizes, rates) -> List[GroupCounts]:
    rng = np.random.default_rng(seed)
    groups = []
    for index, (per_arm, rate) in enumerate(zip(sizes, rates)):
        counts = {}
        for arm, users in zip(arms, per_arm):
            counts[arm.variant_id] = (users, int(rng.binomial(users, rate)))
        groups.append(GroupCounts(f"g{index}", counts))
    return groups


# ---------------------------------------------------------------------------
# The estimate is the regression's coefficient, its SE the regression's HC2
# ---------------------------------------------------------------------------


def test_the_hand_worked_example_gives_its_constants():
    entry = _entry(_overall(E5), T1)
    assert abs(entry["absolute_difference"] - E5_ESTIMATE) <= 1e-12, (
        f"estimate {entry['absolute_difference']:.4f} != {E5_ESTIMATE:.4f}"
    )
    assert abs(entry["standard_error"] - E5_STANDARD_ERROR) <= 1e-12, (
        f"SE mismatch, {entry['standard_error']!r} != {E5_STANDARD_ERROR!r}"
    )


def test_the_hand_worked_example_equals_the_regression():
    reference = lin_hc2(E5, TWO_ARMS)[T1.variant_id]
    assert abs(reference[0] - E5_ESTIMATE) <= 1e-12
    assert abs(reference[1] - E5_STANDARD_ERROR) <= 1e-12
    _check_against_the_regression(E5, TWO_ARMS)


def test_the_estimate_is_not_the_interacted_models_treatment_coefficient():
    """``y ~ t * C(s)``'s coefficient on t is group a's effect, 0.05; the naive
    difference ignores the groups. The estimate is neither."""
    data = _rows(E5, TWO_ARMS)
    data["t"] = data["arm"]
    fit = smf.ols("y ~ t * C(s)", data=data).fit()
    assert abs(fit.params["t"] - E5_INTERACTED_COEFFICIENT) <= 1e-12
    naive = data.loc[data["t"] == 1, "y"].mean() - data.loc[data["t"] == 0, "y"].mean()
    assert abs(naive - E5_NAIVE) <= 1e-12
    estimate = _entry(_overall(E5), T1)["absolute_difference"]
    assert abs(estimate - E5_INTERACTED_COEFFICIENT) > 0.05
    assert abs(estimate - E5_NAIVE) > 0.01


@pytest.mark.parametrize("seed", range(10))
def test_two_variants_equal_the_regression(seed):
    sizes = [(40, 38), (25, 23), (9, 6), (60, 61)]
    rates = [0.10, 0.25, 0.40, 0.05]
    _check_against_the_regression(
        _seeded_groups(seed, TWO_ARMS, sizes, rates), TWO_ARMS
    )


@pytest.mark.parametrize("seed", range(10))
def test_three_variants_equal_one_regression_over_all_of_them(seed):
    """The weights count all three variants' users, so the estimate is the
    three-variant regression's, not one fitted to a single pair."""
    sizes = [(30, 28, 33), (12, 9, 15), (9, 6, 7), (50, 47, 52)]
    rates = [0.12, 0.30, 0.45, 0.08]
    groups = _seeded_groups(1000 + seed, THREE_ARMS, sizes, rates)
    _check_against_the_regression(groups, THREE_ARMS)


def test_a_pair_fitted_alone_is_not_the_estimate():
    """Why the reference is one regression: a regression on the control and
    one treatment alone weights the groups by that pair's users only."""
    sizes = [(30, 10, 50), (12, 30, 4), (9, 6, 7)]
    rates = [0.12, 0.30, 0.45]
    groups = _seeded_groups(7, THREE_ARMS, sizes, rates)
    pair_only = [
        GroupCounts(g.group, {k: v for k, v in g.counts.items() if k != T2.variant_id})
        for g in groups
    ]
    pair = lin_hc2(pair_only, TWO_ARMS)[T1.variant_id][0]
    estimate = _entry(_overall(groups, THREE_ARMS), T1)["absolute_difference"]
    assert abs(estimate - pair) > 1e-3


# ---------------------------------------------------------------------------
# Exact balance: the estimate is the headline's difference
# ---------------------------------------------------------------------------


def _pooled(groups, arm):
    users = sum(g.counts[arm.variant_id][0] for g in groups)
    converted = sum(g.counts[arm.variant_id][1] for g in groups)
    return converted / users


@pytest.mark.parametrize("seed", range(5))
def test_with_every_group_split_exactly_the_estimate_is_the_headline_difference(seed):
    sizes = [(50, 50), (20, 20), (7, 7), (120, 120), (3, 3)]
    rates = [0.10, 0.35, 0.60, 0.02, 0.90]
    groups = _seeded_groups(2000 + seed, TWO_ARMS, sizes, rates)
    headline = _pooled(groups, T1) - _pooled(groups, CONTROL)
    estimate = _entry(_overall(groups), T1)["absolute_difference"]
    assert abs(estimate - headline) <= 1e-12, (
        f"estimate {estimate!r} != headline difference {headline!r}"
    )


def test_three_variants_split_exactly_give_each_headline_difference():
    sizes = [(30, 30, 30), (8, 8, 8), (64, 64, 64)]
    rates = [0.2, 0.5, 0.05]
    groups = _seeded_groups(31, THREE_ARMS, sizes, rates)
    result = _overall(groups, THREE_ARMS)
    for arm in (T1, T2):
        headline = _pooled(groups, arm) - _pooled(groups, CONTROL)
        estimate = _entry(result, arm)["absolute_difference"]
        assert abs(estimate - headline) <= 1e-12, (
            f"{arm.variant_name}: estimate {estimate!r} != headline {headline!r}"
        )


# ---------------------------------------------------------------------------
# The rest of an entry: interval, p-value, relative change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", [0.90, 0.95, 0.99])
def test_the_interval_and_p_value_are_the_normal_ones_at_the_level(level):
    """The constants carry 12 decimals, so the comparison is to 1e-9."""
    entry = _entry(_overall(E5, level=level), T1)
    z = stats.norm.ppf(1 - (1 - level) / 2)
    low, high = entry["absolute_difference_ci"]
    assert abs(low - (E5_ESTIMATE - z * E5_STANDARD_ERROR)) <= TOLERANCE
    assert abs(high - (E5_ESTIMATE + z * E5_STANDARD_ERROR)) <= TOLERANCE
    p = 2 * stats.norm.sf(abs(E5_ESTIMATE) / E5_STANDARD_ERROR)
    assert abs(entry["p_value"] - p) <= TOLERANCE


def test_the_relative_change_is_over_the_weighted_control_rate():
    weights = [40 / 80, 25 / 80, 15 / 80]
    control_rates = [4 / 20, 3 / 13, 5 / 6]
    control = sum(w * r for w, r in zip(weights, control_rates))
    entry = _entry(_overall(E5), T1)
    assert abs(entry["relative_improvement_pct"] - E5_ESTIMATE / control * 100) <= 1e-9


def test_a_control_that_never_converts_has_no_relative_change():
    groups = [
        GroupCounts("a", {T1.variant_id: (10, 3), CONTROL.variant_id: (10, 0)}),
        GroupCounts("b", {T1.variant_id: (8, 1), CONTROL.variant_id: (9, 0)}),
    ]
    entry = _entry(_overall(groups), T1)
    assert entry["relative_improvement_pct"] is None
    assert entry["absolute_difference"] > 0


def test_a_standard_error_of_zero_gives_no_p_value_and_no_interval():
    """Every group's rate 0 or 1 in both variants: the SE is 0, the p-value is
    None (never 0.0) and nothing is significant."""
    groups = [
        GroupCounts("a", {T1.variant_id: (10, 0), CONTROL.variant_id: (10, 0)}),
        GroupCounts("b", {T1.variant_id: (5, 5), CONTROL.variant_id: (6, 0)}),
    ]
    entry = _entry(_overall(groups), T1)
    assert entry["standard_error"] == 0.0
    assert entry["p_value"] is None
    assert entry["adjusted_p_value"] is None
    assert entry["absolute_difference_ci"] is None
    assert entry["is_significant"] is False
    # Only group b differs (1 - 0), weighted by its 11 of 31 users.
    assert entry["absolute_difference"] == pytest.approx(11 / 31)


# ---------------------------------------------------------------------------
# Small groups
# ---------------------------------------------------------------------------


def test_a_group_with_one_user_in_a_variant_is_merged_into_the_largest():
    big = GroupCounts("ios", {T1.variant_id: (50, 9), CONTROL.variant_id: (51, 6)})
    mid = GroupCounts("web", {T1.variant_id: (30, 4), CONTROL.variant_id: (29, 5)})
    tiny = GroupCounts("tv", {T1.variant_id: (3, 1), CONTROL.variant_id: (1, 1)})
    result = _overall([big, mid, tiny])
    assert result["merged_groups"] == [{"group": "tv", "into": "ios"}]
    together = GroupCounts(
        "ios", {T1.variant_id: (53, 10), CONTROL.variant_id: (52, 7)}
    )
    expected = _entry(_overall([together, mid]), T1)
    assert _entry(result, T1) == expected


def test_a_group_with_no_user_in_a_variant_is_merged():
    big = GroupCounts("a", {T1.variant_id: (20, 2), CONTROL.variant_id: (20, 3)})
    empty_arm = GroupCounts("b", {T1.variant_id: (4, 1)})
    result = _overall([big, empty_arm])
    assert result["merged_groups"] == [{"group": "b", "into": "a"}]
    assert result["reason"] is None


def test_of_equally_large_groups_the_first_listed_takes_the_merge():
    first = GroupCounts("x", {T1.variant_id: (10, 2), CONTROL.variant_id: (10, 1)})
    second = GroupCounts("y", {T1.variant_id: (11, 2), CONTROL.variant_id: (9, 1)})
    tiny = GroupCounts("z", {T1.variant_id: (1, 0), CONTROL.variant_id: (4, 1)})
    assert _overall([first, second, tiny])["merged_groups"] == [
        {"group": "z", "into": "x"}
    ]
    assert _overall([second, first, tiny])["merged_groups"] == [
        {"group": "z", "into": "y"}
    ]


def test_a_large_group_with_one_user_in_a_variant_is_merged_too():
    """The largest group itself can be the small one: it merges into the
    largest group that has two users in every variant."""
    lopsided = GroupCounts("a", {T1.variant_id: (90, 9), CONTROL.variant_id: (1, 0)})
    ok = GroupCounts("b", {T1.variant_id: (5, 1), CONTROL.variant_id: (5, 2)})
    result = _overall([lopsided, ok])
    assert result["merged_groups"] == [{"group": "a", "into": "b"}]


def test_no_group_with_two_users_in_every_variant_gives_too_few_users():
    groups = [
        GroupCounts("a", {T1.variant_id: (1, 0), CONTROL.variant_id: (5, 1)}),
        GroupCounts("b", {T1.variant_id: (3, 1), CONTROL.variant_id: (1, 1)}),
    ]
    assert _overall(groups) == {
        "overall": None,
        "reason": bge.REASON_TOO_FEW_USERS,
        "merged_groups": [],
    }


def test_no_users_gives_too_few_users():
    assert _overall([])["reason"] == bge.REASON_TOO_FEW_USERS
    no_control = [GroupCounts("a", {T1.variant_id: (10, 2)})]
    assert _overall(no_control)["reason"] == bge.REASON_TOO_FEW_USERS


@pytest.mark.parametrize("arms", [TWO_ARMS, THREE_ARMS], ids=["2-arms", "3-arms"])
def test_only_the_control_having_users_gives_too_few_users(arms):
    """The first users of a block can all be the control's: a 20/80 split
    (control 80) puts 4 of each 5 places in the control. With no treatment
    user there is nothing to compare, whatever the control's counts."""
    groups = [
        GroupCounts("gold", {CONTROL.variant_id: (3, 1)}),
        GroupCounts("silver", {CONTROL.variant_id: (2, 0)}),
    ]
    result = _overall(groups, arms)
    entries = [
        (e["variant_name"].lower(), e["absolute_difference"])
        for e in result["overall"] or []
    ]
    assert result == {
        "overall": None,
        "reason": bge.REASON_TOO_FEW_USERS,
        "merged_groups": [],
    }, f"reason={result['reason']} overall={entries}"


def test_a_variant_with_no_users_has_an_empty_estimate_and_merges_nothing():
    """A variant with a 0% share gets no users: it compares with nothing, so it
    takes no part in the small-group rule and the others are estimated."""
    groups = [
        GroupCounts("a", {T1.variant_id: (20, 4), CONTROL.variant_id: (20, 2)}),
        GroupCounts("b", {T1.variant_id: (15, 1), CONTROL.variant_id: (14, 3)}),
    ]
    result = _overall(groups, THREE_ARMS)
    assert result["merged_groups"] == []
    assert _entry(result, T1) == _entry(_overall(groups), T1)
    empty = _entry(result, T2)
    assert empty["absolute_difference"] is None and empty["p_value"] is None
    assert empty["is_significant"] is False


# ---------------------------------------------------------------------------
# The correction across treatments
# ---------------------------------------------------------------------------


def _three_arm_groups():
    sizes = [(200, 200, 200), (150, 150, 150)]
    return [
        GroupCounts(
            f"g{i}",
            {
                CONTROL.variant_id: (n_c, int(n_c * 0.10)),
                T1.variant_id: (n_1, int(n_1 * 0.16)),
                T2.variant_id: (n_2, int(n_2 * 0.15)),
            },
        )
        for i, (n_c, n_1, n_2) in enumerate(sizes)
    ]


@pytest.mark.parametrize("method", ["benjamini_hochberg", "bonferroni"])
def test_three_variants_are_corrected_together_with_the_experiments_method(method):
    result = _overall(_three_arm_groups(), THREE_ARMS, method=method)
    entries = [_entry(result, T1), _entry(result, T2)]
    expected = adjusted_p_values([e["p_value"] for e in entries], method)
    assert [e["adjusted_p_value"] for e in entries] == expected
    for entry in entries:
        assert entry["is_significant"] == (entry["adjusted_p_value"] < 0.05)


def test_with_no_correction_significance_follows_the_p_value():
    result = _overall(_three_arm_groups(), THREE_ARMS, method="none")
    for arm in (T1, T2):
        entry = _entry(result, arm)
        assert entry["adjusted_p_value"] is None
        assert entry["is_significant"] == (entry["p_value"] < 0.05)


def test_the_significance_level_is_one_minus_the_confidence_level():
    groups = _three_arm_groups()
    p = _entry(_overall(groups, THREE_ARMS, method="none"), T2)["p_value"]
    assert 0.01 < p < 0.10
    at_90 = _entry(_overall(groups, THREE_ARMS, level=0.90, method="none"), T2)
    at_99 = _entry(_overall(groups, THREE_ARMS, level=0.99, method="none"), T2)
    assert at_90["is_significant"] == (p < 0.10)
    assert at_99["is_significant"] == (p < 0.01)


def test_an_unknown_correction_method_is_refused():
    from backend.app.services.sufficient_stats_analysis import (
        UnknownCorrectionMethodError,
    )

    with pytest.raises(UnknownCorrectionMethodError):
        _overall(E5, method="holm")


def test_no_control_is_refused():
    with pytest.raises(ValueError):
        _overall(E5, arms=(T1, T2))


# ---------------------------------------------------------------------------
# No estimate: a mean metric, sequential testing
# ---------------------------------------------------------------------------


def _estimate(**kwargs):
    arguments = {
        "confidence_level": 0.95,
        "correction_method": "benjamini_hochberg",
        "is_proportion": True,
        "sequential_testing": False,
        **kwargs,
    }
    return bge.overall_estimate(E5, TWO_ARMS, **arguments)


def test_a_mean_metric_has_no_estimate():
    assert _estimate(is_proportion=False) == {
        "overall": None,
        "reason": bge.REASON_MEAN_METRIC,
        "merged_groups": [],
    }
    assert _estimate(is_proportion=False, sequential_testing=True)["reason"] == (
        bge.REASON_MEAN_METRIC
    )


def test_sequential_testing_has_no_estimate():
    assert _estimate(sequential_testing=True) == {
        "overall": None,
        "reason": bge.REASON_SEQUENTIAL_TESTING,
        "merged_groups": [],
    }


def test_otherwise_the_estimate_is_the_balanced_overall():
    assert _estimate() == _overall(E5)


def test_the_reasons_are_the_documented_words():
    assert bge.REASON_MEAN_METRIC == "mean_metric"
    assert bge.REASON_SEQUENTIAL_TESTING == "sequential_testing"
    assert bge.REASON_TOO_FEW_USERS == "too_few_users"
    assert bge.MIN_USERS_PER_ARM == 2
    assert math.isclose(E5_ESTIMATE, -0.036698717949)
