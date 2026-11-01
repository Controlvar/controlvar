"""GD-5: the intervals on a treatment's difference from the control cover the
true difference as often as their level says (#1496).

``/results`` gives each treatment an interval on its absolute difference from
the control and one on its relative change.  A point-value oracle cannot see
an interval that is centred right but too narrow, or one that ignores the
control's own uncertainty; only coverage can.  Each battery simulates 2,000
experiments with known true rates or means, passes each one to the builder
``/results`` uses (``binomial_metric_result`` for a proportion,
``per_user_mean_metric_result`` for a mean) at the 0.95 level, and counts the
experiments whose interval contains the true value.

The band is 1,868 to 1,932 of 2,000: 95% plus or minus 3.29 standard
deviations of Binomial(2,000, 0.95), about its central 99.9%.

The settings are ones where the relative interval is never None at the seed
(a proportion's control with no conversions, or a mean's control whose limits
reach 0, has none), and each battery asserts that, so a None cannot quietly
shrink the count's denominator.  A separate low-count battery, where some
draws' controls have no conversions, holds both intervals to a one-sided
floor and pins its counts.

Measured at these seeds, with no relative interval None in any of them:

* proportion, control rate 0.10, +30% relative, 1,000 users per arm:
  absolute 1,907, relative 1,910.  A relative interval made by dividing the
  absolute one by the observed control rate, which ignores the control's
  own uncertainty, covers 1,824 here: below the band;
* proportion, control rate 0.02, +20%, 2,000 users per arm: absolute 1,908,
  relative 1,914 (the divided interval covers 1,873 at this seed, inside
  the band: the first battery is the one that catches it);
* mean, per-user values exponential with mean 10 against 11, 400 users per
  arm: absolute 1,898, relative 1,898;
* with 1.645 in place of 1.96 (a 90% interval reported as 95%), the two
  proportion batteries cover 1,806 and 1,827 (absolute), 1,790 and 1,830
  (relative), in the order above.

The draws come from one ``numpy`` Generator per battery with a committed
seed, so every count is the same on every run.
"""

from __future__ import annotations

import math
from functools import lru_cache
from types import SimpleNamespace
from typing import Dict, Tuple

import numpy as np
import pytest

from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
    per_user_mean_metric_result,
)

pytestmark = pytest.mark.unit

SIMS = 2_000
CONFIDENCE_LEVEL = 0.95
#: 95% of ``SIMS`` plus or minus 3.29 standard deviations.
MIN_COVERED = 1_868
MAX_COVERED = 1_932

CONTROL = BinomialVariant("00000000-0000-4000-8000-0000000000c0", "Control", True)
TREATMENT = BinomialVariant("00000000-0000-4000-8000-0000000000e1", "Treatment", False)

#: (control rate, relative lift, users per arm, seed) -> measured
#: (absolute covered, relative covered).
PROPORTION_BATTERIES: Dict[Tuple[float, float, int, int], Tuple[int, int]] = {
    (0.10, 0.30, 1_000, 1_496_051): (1_907, 1_910),
    (0.02, 0.20, 2_000, 1_496_052): (1_908, 1_914),
}

MEAN_SEED = 1_496_053
MEAN_USERS = 400
MEAN_CONTROL = 10.0
MEAN_TREATMENT = 11.0

#: The low-count battery: control rate 0.02, +30%, 200 users per arm.
LOW_COUNT_SEED = 1_496_054
#: Draws whose control has no conversions, the only ones with no relative
#: interval.
LOW_COUNT_RELATIVE_NONE = 36
#: (absolute covered of 2,000, relative covered, relative present).
LOW_COUNT_COVERED = (1_940, 1_909, 1_964)


def _treatment(result):
    return next(v for v in result["variants"] if not v["is_control"])


def _covers(interval, value) -> bool:
    return interval[0] <= value <= interval[1]


@lru_cache(maxsize=None)
def _proportion_battery(
    rate: float, lift: float, users: int, seed: int
) -> Tuple[int, int, int]:
    """(absolute covered, relative covered, relative None) over ``SIMS``."""
    rng = np.random.default_rng(seed)
    treatment_rate = rate * (1.0 + lift)
    control_counts = rng.binomial(users, rate, size=SIMS)
    treatment_counts = rng.binomial(users, treatment_rate, size=SIMS)
    metric = SimpleNamespace(
        id="00000000-0000-4000-8000-0000000000a0",
        name="conversion",
        metric_type="conversion",
        is_primary=True,
    )
    absolute = relative = none = 0
    for x_c, x_t in zip(control_counts, treatment_counts):
        row = _treatment(
            binomial_metric_result(
                [(CONTROL, users, int(x_c)), (TREATMENT, users, int(x_t))],
                1.0 - CONFIDENCE_LEVEL,
                "none",
                metric=metric,
            )
        )
        absolute += _covers(row["absolute_difference_ci"], treatment_rate - rate)
        if row["relative_improvement_pct_ci"] is None:
            none += 1
        else:
            relative += _covers(row["relative_improvement_pct_ci"], 100.0 * lift)
    return absolute, relative, none


@pytest.mark.parametrize("battery", sorted(PROPORTION_BATTERIES), ids=str)
def test_the_proportion_intervals_cover_the_true_difference(battery):
    absolute, relative, none = _proportion_battery(*battery)
    assert none == 0
    assert (absolute, relative) == PROPORTION_BATTERIES[battery]
    assert MIN_COVERED <= absolute <= MAX_COVERED
    assert MIN_COVERED <= relative <= MAX_COVERED


def test_the_mean_intervals_cover_the_true_difference():
    """Per-user values exponential with mean 10 (control) and 11, 400 users
    per arm: Welch's interval on the difference and Fieller's (MOVER-R) on
    the relative change.  Measured: 1,898 and 1,898."""
    rng = np.random.default_rng(MEAN_SEED)
    metric = SimpleNamespace(
        id="00000000-0000-4000-8000-0000000000a1",
        name="value per user",
        metric_type="revenue",
        is_primary=True,
    )
    absolute = relative = none = 0
    for _ in range(SIMS):
        control = rng.exponential(MEAN_CONTROL, size=MEAN_USERS)
        treatment = rng.exponential(MEAN_TREATMENT, size=MEAN_USERS)
        row = _treatment(
            per_user_mean_metric_result(
                [
                    (CONTROL, MEAN_USERS, MEAN_USERS, control.tolist()),
                    (TREATMENT, MEAN_USERS, MEAN_USERS, treatment.tolist()),
                ],
                1.0 - CONFIDENCE_LEVEL,
                "none",
                metric=metric,
            )
        )
        absolute += _covers(
            row["absolute_difference_ci"], MEAN_TREATMENT - MEAN_CONTROL
        )
        if row["relative_improvement_pct_ci"] is None:
            none += 1
        else:
            relative += _covers(
                row["relative_improvement_pct_ci"],
                100.0 * (MEAN_TREATMENT - MEAN_CONTROL) / MEAN_CONTROL,
            )
    assert none == 0
    assert (absolute, relative) == (1_898, 1_898)
    assert MIN_COVERED <= absolute <= MAX_COVERED
    assert MIN_COVERED <= relative <= MAX_COVERED


def _one_sided_floor(present: int) -> float:
    """95% of ``present`` less 3.29 standard deviations of
    Binomial(present, 0.95)."""
    return CONFIDENCE_LEVEL * present - 3.29 * math.sqrt(
        present * CONFIDENCE_LEVEL * (1.0 - CONFIDENCE_LEVEL)
    )


def test_the_low_count_intervals_cover_at_least_their_level():
    """Control rate 0.02, +30%, 200 users per arm: the control expects 4
    conversions.  A proportion's relative interval is None only when the
    control has no conversions, which happens in 36 of these 2,000 draws;
    the test counts those from the draws themselves, not from the code.

    The floor is one-sided because the correct method over-covers here: at
    so few conversions both intervals are conservative.  Measured: the
    difference's interval covers 1,940 of 2,000 and the relative change's
    1,909 of the 1,964 that have one.  1,909 is above the symmetric band's
    upper limit for 1,964 draws (1,897), so a symmetric band would fail a
    correct implementation; an interval that under-covers still fails the
    floor, and any change at all fails the exact pins."""
    rng = np.random.default_rng(LOW_COUNT_SEED)
    users, rate, lift = 200, 0.02, 0.30
    control_counts = rng.binomial(users, rate, size=SIMS)
    treatment_counts = rng.binomial(users, rate * (1.0 + lift), size=SIMS)
    metric = SimpleNamespace(
        id="00000000-0000-4000-8000-0000000000a2",
        name="conversion",
        metric_type="conversion",
        is_primary=True,
    )
    absolute = relative = present = absolute_none = relative_none = 0
    for x_c, x_t in zip(control_counts, treatment_counts):
        row = _treatment(
            binomial_metric_result(
                [(CONTROL, users, int(x_c)), (TREATMENT, users, int(x_t))],
                1.0 - CONFIDENCE_LEVEL,
                "none",
                metric=metric,
            )
        )
        absolute_none += row["absolute_difference_ci"] is None
        absolute += _covers(row["absolute_difference_ci"], rate * lift)
        if row["relative_improvement_pct_ci"] is None:
            relative_none += 1
        else:
            present += 1
            relative += _covers(row["relative_improvement_pct_ci"], 100.0 * lift)
    no_control_conversions = int((control_counts == 0).sum())
    assert absolute_none == 0
    assert relative_none == no_control_conversions == LOW_COUNT_RELATIVE_NONE
    assert (absolute, relative, present) == LOW_COUNT_COVERED
    assert absolute >= _one_sided_floor(SIMS)
    assert relative >= _one_sided_floor(present)
