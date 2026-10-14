"""The two consequences of CUPED's one pooled slope (``cuped_metric_result``).

``theta`` is fitted over every arm at once, so:

* an arm whose history relates to its outcome differently from the others' can
  end up with a larger adjusted variance than unadjusted one:
  ``variance_reduction_pct`` is then negative;
* adding an arm moves ``theta``, and with it the other arms' adjusted effects;
  their unadjusted effects do not move.

The data are seeded 0/1 pairs (``_binary_pair`` in test_cuped_metric_result.py):
a control and a large treatment whose history goes with the outcome, and a
small treatment whose history goes against it.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.app.services.sufficient_stats_analysis import cuped_metric_result
from backend.tests.unit.services.test_cuped_metric_result import (
    _METRIC,
    CONTROL,
    T1,
    T2,
    _binary_pair,
    _sums,
)

pytestmark = pytest.mark.unit


def _arms():
    rng = np.random.default_rng(1142)
    control = _binary_pair(rng, 4000, 0.3, 0.4, 0.6)
    against = _binary_pair(rng, 300, 0.3, 0.4, -0.4)
    large = _binary_pair(rng, 6000, 0.3, 0.4, 0.6)
    return control, against, large


@pytest.mark.regression
def test_variance_reduction_can_be_negative():
    control, against, large = _arms()
    rows = cuped_metric_result(
        [_sums(CONTROL, *control), _sums(T1, *against), _sums(T2, *large)],
        0.95,
        "none",
        metric=_METRIC,
    )
    by_arm = {row["variant_id"]: row for row in rows}
    assert by_arm["t1"]["variance_reduction_pct"] < -50.0
    assert by_arm["t2"]["variance_reduction_pct"] > 30.0
    # It is the ratio of the variances, recomputed from the rows.
    theta = by_arm["t1"]["theta"]
    (yc, xc), (yt, xt) = control, against
    adjusted = np.var(yc - theta * xc, ddof=1) / len(yc) + np.var(
        yt - theta * xt, ddof=1
    ) / len(yt)
    raw = np.var(yc, ddof=1) / len(yc) + np.var(yt, ddof=1) / len(yt)
    assert by_arm["t1"]["variance_reduction_pct"] == pytest.approx(
        100.0 * (1 - adjusted / raw), abs=1e-9
    )


@pytest.mark.regression
def test_adding_an_arm_moves_the_other_arms_adjusted_effects_only():
    control, against, large = _arms()
    (two,) = cuped_metric_result(
        [_sums(CONTROL, *control), _sums(T1, *against)], 0.95, "none", metric=_METRIC
    )
    three = cuped_metric_result(
        [_sums(CONTROL, *control), _sums(T1, *against), _sums(T2, *large)],
        0.95,
        "none",
        metric=_METRIC,
    )
    with_third = next(row for row in three if row["variant_id"] == "t1")
    assert with_third["theta"] != pytest.approx(two["theta"], rel=1e-3)
    assert with_third["adjusted_effect"] != pytest.approx(
        two["adjusted_effect"], rel=1e-3
    )
    assert with_third["adjusted_se"] != pytest.approx(two["adjusted_se"], rel=1e-3)
    assert with_third["unadjusted_effect"] == two["unadjusted_effect"]
    assert with_third["unadjusted_se"] == two["unadjusted_se"]
