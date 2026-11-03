"""F1r: the reference statistics equal independent computations of them.

* Fisher's p-value and the sample-ratio p-value equal the docs runner's
  oracles (``docs_runner.oracles.fisher_exact_p`` and ``sample_ratio_p``);
* Welch's p-value and interval, computed from exact per-user sums, equal
  ``scipy.stats.ttest_ind(equal_var=False)`` on the same arrays, at a 30/70
  allocation: with arms of equal size the pooled and Welch t statistics are
  the same and only the degrees of freedom differ, so ``equal_var=True``
  could pass unseen there (on these arrays at 30/70 it moves the revenue
  p-value from 0.00022 to 0.00494);
* the corrections equal statsmodels' and the docs' worked example.

The arrays are a generated plan replayed through a fake server and counted
by the contract: what C2 will compare the live results with.
"""

from __future__ import annotations

import math
import random

import pytest
from scipy import stats
from statsmodels.stats.multitest import multipletests

from backend.tests.unit.ground_truth.library import (
    contract,
    generator,
    oracles,
    records,
    reference,
    replay,
)

pytestmark = pytest.mark.unit

g = generator
REL = 1e-9

SKEWED = g.Scenario(
    name="f1r",
    experiment_key="gt-f1r",
    arms=(
        g.Arm("control", 30, 0.05, repeat=0.4, values=g.Values(3.0, 1.0)),
        g.Arm("treatment", 70, 0.08, repeat=0.4, values=g.Values(3.0, 1.3)),
    ),
    users=6000,
    event_name="purchase",
    metrics=(
        g.Metric("buyers", "purchase", "conversion"),
        g.Metric("revenue", "purchase", "revenue"),
        g.Metric("purchases", "purchase", "count"),
    ),
)


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


@pytest.fixture(scope="module")
def skewed() -> contract.ExperimentExpected:
    plan = g.simulate(2026, SKEWED)
    found = contract.valid_events(records(replay(plan, declared={"event_dedup": True})))
    return found.experiments[SKEWED.experiment_key]


def test_the_arrays_are_skewed_enough_for_the_pooled_test_to_differ(skewed):
    """The precondition: on these arrays ``equal_var=True`` moves the p-value."""
    c = skewed.per_user("control", "purchase", "revenue")
    t = skewed.per_user("treatment", "purchase", "revenue")
    assert 0.25 < len(c) / (len(c) + len(t)) < 0.35
    welch = stats.ttest_ind(t, c, equal_var=False).pvalue
    pooled = stats.ttest_ind(t, c, equal_var=True).pvalue
    assert _rel(welch, pooled) > 1e-3, (welch, pooled)


@pytest.mark.parametrize("metric_type", ["revenue", "count"])
def test_welch_equals_scipy_on_the_same_arrays(skewed, metric_type):
    c = skewed.per_user("control", "purchase", metric_type)
    t = skewed.per_user("treatment", "purchase", metric_type)
    mine = reference.welch(c, t, 0.95)
    theirs = stats.ttest_ind(t, c, equal_var=False)
    interval = theirs.confidence_interval(0.95)
    assert _rel(mine.p_value, theirs.pvalue) <= REL, (
        f"p {mine.p_value:.5f} != {theirs.pvalue:.5f}"
        f" (rel {_rel(mine.p_value, theirs.pvalue):.1e})"
    )
    assert _rel(mine.absolute_difference_ci[0], interval.low) <= REL
    assert _rel(mine.absolute_difference_ci[1], interval.high) <= REL
    difference = math.fsum(t) / len(t) - math.fsum(c) / len(c)
    assert mine.absolute_difference == difference


@pytest.mark.parametrize("level", [0.80, 0.90, 0.99])
def test_welchs_interval_follows_the_confidence_level(skewed, level):
    c = skewed.per_user("control", "purchase", "revenue")
    t = skewed.per_user("treatment", "purchase", "revenue")
    interval = stats.ttest_ind(t, c, equal_var=False).confidence_interval(level)
    low, high = reference.welch(c, t, level).absolute_difference_ci
    assert _rel(low, interval.low) <= REL and _rel(high, interval.high) <= REL


_TABLES = [
    (1000, 100, 1000, 128),
    (50, 3, 100, 18),
    (200, 2, 200, 6),
    (10, 0, 10, 0),
    (10, 10, 12, 0),
    (4000, 400, 4000, 401),
    (1, 1, 1, 0),
]


@pytest.mark.parametrize("table", _TABLES)
def test_fisher_equals_the_docs_runner_oracle(table):
    assert _rel(reference.fisher_p(*table), oracles.fisher_exact_p(*table)) <= 1e-12


def test_fisher_on_the_replayed_arrays_equals_the_docs_runner_oracle(skewed):
    found = reference.compare(
        {v: skewed.per_user(v, "purchase", "conversion") for v in skewed.variants},
        "proportion",
        "none",
        0.95,
    )
    (treatment,) = found
    counts = (
        skewed.sample_size("control"),
        skewed.converting_users("control", "purchase"),
        skewed.sample_size("treatment"),
        skewed.converting_users("treatment", "purchase"),
    )
    assert _rel(treatment.p_value, oracles.fisher_exact_p(*counts)) <= 1e-12
    assert (
        treatment.absolute_difference == counts[3] / counts[2] - counts[1] / counts[0]
    )
    assert treatment.absolute_difference_ci is None


@pytest.mark.parametrize(
    "control, treatment, allocation",
    [
        (500, 500, 50),
        (480, 520, 50),
        (1800, 4200, 30),
        (1750, 4250, 30),
        (900, 4100, 30),
        (1, 0, 50),
    ],
)
def test_the_sample_ratio_check_equals_the_docs_runner_oracle(
    control, treatment, allocation
):
    mine = reference.srm_p([control, treatment], [allocation, 100 - allocation])
    theirs = oracles.sample_ratio_p(control, treatment, allocation, 100 - allocation)
    assert _rel(mine, theirs) <= 1e-12


def test_the_sample_ratio_check_on_the_replayed_counts(skewed):
    observed = [skewed.sample_size(v) for v in skewed.variants]
    mine = reference.srm_p(observed, list(skewed.allocations))
    assert _rel(mine, oracles.sample_ratio_p(*observed, *skewed.allocations)) <= 1e-12


def test_the_sample_ratio_check_over_three_variants_is_pearsons():
    observed, allocations = [2040, 1980, 1980], [34, 33, 33]
    total = sum(observed)
    expected = stats.chisquare(observed, [total * a / 100 for a in allocations]).pvalue
    assert _rel(reference.srm_p(observed, allocations), expected) <= 1e-12
    assert reference.srm_p([0, 0], [50, 50]) is None


@pytest.mark.parametrize(
    "method, theirs", [("benjamini_hochberg", "fdr_bh"), ("bonferroni", "bonferroni")]
)
def test_the_corrections_equal_statsmodels(method, theirs):
    draw = random.Random(4).random
    for size in range(1, 7):
        for _ in range(50):
            p_values = [draw() ** 3 for _ in range(size)]
            expected = multipletests(p_values, method=theirs)[1]
            found = reference.adjust(p_values, method)
            assert all(
                _rel(a, b) <= 1e-12 for a, b in zip(found, expected, strict=True)
            )


def test_the_docs_worked_example_and_the_decision_on_the_adjusted_p_value():
    """The data export page's example: p 0.035 adjusted to 0.071, not significant."""
    assert reference.adjust([0.0355, 0.5], "benjamini_hochberg") == [0.071, 0.5]
    assert reference.adjust([0.0355, 0.5], "none") == [0.0355, 0.5]
    assert reference.adjust([0.03, None], "bonferroni") == [0.03, None]
    control = [0.0] * 900 + [1.0] * 100
    near = [0.0] * 870 + [1.0] * 130  # p about 0.04 against the control
    far = [0.0] * 900 + [1.0] * 100
    (one, _) = reference.compare(
        {"control": control, "near": near, "far": far},
        "proportion",
        "benjamini_hochberg",
        0.95,
    )
    assert one.p_value < 0.05 and one.adjusted_p_value > 0.05
    assert one.is_significant is False
    (alone,) = reference.compare(
        {"control": control, "near": near}, "proportion", "benjamini_hochberg", 0.95
    )
    assert alone.adjusted_p_value == alone.p_value and alone.is_significant is True


def test_welch_with_no_variation_or_one_user_has_no_test():
    assert reference.welch([1.0, 1.0], [2.0, 2.0], 0.95) == reference.Welch(
        None, 1.0, None
    )
    assert reference.welch([1.0], [2.0, 3.0], 0.95) == reference.Welch(None, 1.5, None)
    assert reference.welch([], [2.0], 0.95) == reference.Welch(None, None, None)


def test_every_metric_type_has_its_analysis():
    assert set(reference.ANALYSIS) == set(contract.METRIC_TYPES) == set(g.METRIC_TYPES)
    assert reference.ANALYSIS["conversion"] == "proportion"
    assert {
        reference.ANALYSIS[t] for t in ("revenue", "count", "duration", "custom")
    } == {"mean"}
