"""The balanced-groups estimate's error rate and coverage, by simulation (#448).

``balanced_groups_estimate.balanced_overall`` gives an experiment that
balances its split within groups one size-weighted estimate per treatment.
Two batteries hold it to what its interval and p-value promise:

* **A/A.** 2,000 simulated experiments in which both variants convert at the
  same rate in every group, each group split exactly in half (as balancing
  makes it). The group rates are drawn from 1% to 30% for every experiment.
  The count reported significant at 0.05 must lie in 68 to 132 of 2,000 (3.4%
  to 6.6%, about the central 99.9% of Binomial(2,000, 0.05)). The band is
  two-sided, because the defect it exists for, a variance that is too large,
  reports too *few*: an upper bound alone would pass it. Two shapes run: 8
  groups of 30 users and 4 groups of 200. A third, 8 groups of 30 at 1% to 3%,
  makes experiments in which every group's rate is 0 or 1 in both variants,
  so the standard error is 0: those count in the 2,000 and have no p-value
  (never a p-value of 0.0).
* **Planted effects.** Three groups of 600, 400 and 200 users with treatment
  effects of +5, 0 and -10 percentage points. Each group's own estimate
  recovers its effect, and the overall 95% interval covers the size-weighted
  effect in 1,868 to 1,932 of 2,000 experiments (the central 99.9% of
  Binomial(2,000, 0.95)).

The draws come from ``numpy`` Generators with committed seeds, so every count
is the same on every run. Measured at these seeds: 108 (8 x 30), 109 (4 x 200)
and 95 (8 x 30 at 1% to 3%, 22 of them with a standard error of 0) of 2,000
significant; coverage 1,892 of 2,000; group means +0.0511, -0.0004 and
-0.0994. At nine other seeds: 87 to 115, 75 to 110 and 1,874 to 1,911. A
variance with ``w_s`` in place of ``w_s ** 2`` reports 0 of 2,000 significant.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Tuple

import numpy as np
import pytest
from scipy.stats import binom

from backend.app.services.balanced_groups_estimate import (
    Arm,
    GroupCounts,
    balanced_overall,
)

pytestmark = pytest.mark.unit

SIMS = 2_000
ALPHA = 0.05
CONFIDENCE_LEVEL = 1 - ALPHA
#: One treatment: Benjamini-Hochberg leaves its p-value as it is.
CORRECTION_METHOD = "benjamini_hochberg"

#: 3.4% and 6.6% of ``SIMS``.
MIN_SIGNIFICANT = 68
MAX_SIGNIFICANT = 132
#: The central 99.9% of Binomial(``SIMS``, 0.95).
MIN_COVERED = 1_868
MAX_COVERED = 1_932

CONTROL = Arm("00000000-0000-4000-8000-0000000000c0", "Control", True)
TREATMENT = Arm("00000000-0000-4000-8000-0000000000e1", "Treatment", False)
ARMS = (CONTROL, TREATMENT)

#: (name, groups, users per group, lowest rate, highest rate, seed)
AA_SHAPES = {
    "8x30-rates-1-30pct": (8, 30, 0.01, 0.30, 448_011),
    "4x200-rates-1-30pct": (4, 200, 0.01, 0.30, 448_012),
    "8x30-rates-1-3pct": (8, 30, 0.01, 0.03, 448_013),
}

#: Planted: (users per group, control rate, treatment effect).
PLANTED = ((600, 0.20, 0.05), (400, 0.30, 0.0), (200, 0.40, -0.10))
PLANTED_SEED = 448_021


def _group(name: str, users: int, control: int, treatment: int) -> GroupCounts:
    half = users // 2
    return GroupCounts(
        name,
        {CONTROL.variant_id: (half, control), TREATMENT.variant_id: (half, treatment)},
    )


@lru_cache(maxsize=None)
def aa_outcomes(shape: str) -> Tuple[int, int, int]:
    """(significant, standard error 0, p-value None) over ``SIMS`` A/A runs."""
    groups, users, low, high, seed = AA_SHAPES[shape]
    rng = np.random.default_rng(seed)
    significant = zero_se = no_p = 0
    for _ in range(SIMS):
        rates = rng.uniform(low, high, size=groups)
        converted = rng.binomial(users // 2, np.repeat(rates, 2)).reshape(groups, 2)
        counts = [
            _group(f"g{i}", users, int(c), int(t)) for i, (c, t) in enumerate(converted)
        ]
        result = balanced_overall(
            counts,
            ARMS,
            confidence_level=CONFIDENCE_LEVEL,
            correction_method=CORRECTION_METHOD,
        )
        entry = result["overall"][0]
        significant += bool(entry["is_significant"])
        zero_se += entry["standard_error"] == 0.0
        no_p += entry["p_value"] is None
    return significant, zero_se, no_p


def test_the_bands_are_the_central_99_9_percent():
    assert abs(binom.ppf(0.0005, SIMS, ALPHA) - MIN_SIGNIFICANT) <= 1
    assert abs(binom.ppf(0.9995, SIMS, ALPHA) - MAX_SIGNIFICANT) <= 1
    assert abs(binom.ppf(0.0005, SIMS, 0.95) - MIN_COVERED) <= 1
    assert abs(binom.ppf(0.9995, SIMS, 0.95) - MAX_COVERED) <= 1


@pytest.mark.parametrize("shape", sorted(AA_SHAPES))
def test_aa_significance_rate_is_within_the_band(shape):
    significant, zero_se, no_p = aa_outcomes(shape)
    assert MIN_SIGNIFICANT <= significant <= MAX_SIGNIFICANT, (
        f"{shape}: {significant} of {SIMS} A/A experiments significant; the band "
        f"is {MIN_SIGNIFICANT} to {MAX_SIGNIFICANT}"
    )
    assert no_p == zero_se, (
        f"{shape}: {zero_se} runs with a standard error of 0 but {no_p} with no p-value"
    )


def test_a_standard_error_of_zero_occurs_and_has_no_p_value():
    """At 1% to 3% some runs have every group's rate at 0 or 1 in both
    variants: they are counted, as not significant, with no p-value."""
    _, zero_se, no_p = aa_outcomes("8x30-rates-1-3pct")
    assert zero_se > 0
    assert no_p == zero_se


@lru_cache(maxsize=None)
def planted_outcomes() -> Tuple[int, List[float], float]:
    """(runs covering the weighted effect, each group's mean estimate, the
    overall mean estimate) over ``SIMS`` runs."""
    rng = np.random.default_rng(PLANTED_SEED)
    total = sum(users for users, _, _ in PLANTED)
    truth = sum(users / total * effect for users, _, effect in PLANTED)
    covered = 0
    per_group = np.zeros(len(PLANTED))
    overall = 0.0
    for _ in range(SIMS):
        counts = []
        for i, (users, rate, effect) in enumerate(PLANTED):
            half = users // 2
            c = int(rng.binomial(half, rate))
            t = int(rng.binomial(half, rate + effect))
            counts.append(_group(f"g{i}", users, c, t))
        result = balanced_overall(
            counts,
            ARMS,
            confidence_level=CONFIDENCE_LEVEL,
            correction_method=CORRECTION_METHOD,
        )
        entry = result["overall"][0]
        low, high = entry["absolute_difference_ci"]
        covered += low <= truth <= high
        overall += entry["absolute_difference"]
        for i, group in enumerate(counts):
            alone = balanced_overall(
                [group],
                ARMS,
                confidence_level=CONFIDENCE_LEVEL,
                correction_method=CORRECTION_METHOD,
            )
            per_group[i] += alone["overall"][0]["absolute_difference"]
    return covered, list(per_group / SIMS), overall / SIMS


def test_each_groups_planted_effect_is_recovered():
    _, per_group, _ = planted_outcomes()
    for (users, rate, effect), mean in zip(PLANTED, per_group):
        half = users // 2
        sd = np.sqrt((rate * (1 - rate) + (rate + effect) * (1 - rate - effect)) / half)
        # Four Monte Carlo standard errors of the mean.
        assert abs(mean - effect) <= 4 * sd / np.sqrt(SIMS), (
            f"group of {users}: mean estimate {mean:.4f} != planted {effect:+.2f}"
        )


def test_the_overall_interval_covers_the_weighted_effect():
    covered, _, _ = planted_outcomes()
    assert MIN_COVERED <= covered <= MAX_COVERED, (
        f"coverage {covered} of {SIMS}; the band is {MIN_COVERED} to {MAX_COVERED}"
    )


def test_the_overall_estimate_is_the_weighted_effect_on_average():
    _, _, mean = planted_outcomes()
    total = sum(users for users, _, _ in PLANTED)
    truth = sum(users / total * effect for users, _, effect in PLANTED)
    assert abs(mean - truth) <= 0.002, f"mean estimate {mean:.4f} != {truth:.4f}"
