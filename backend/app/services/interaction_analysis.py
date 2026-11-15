"""
Does one experiment's treatment effect differ across another experiment's arms (#219)?

Pure statistics, no database access.  For one treatment of experiment E and
the arms of experiment O that share users with E, the counts form a 2 x J
table: row 0 is E's control, row 1 the treatment, one column per arm of O.

The test asks whether the treatment's lift **in percentage points** is the
same in every column.  Under that hypothesis -- the additive model -- the
conversion rates are ``p0j = bj`` and ``p1j = bj + d``: one baseline per arm
of O and one common difference ``d``.  The model is fitted by binomial maximum
likelihood, and the statistic is Pearson's X^2 of the counts against the
fitted rates, on J - 1 degrees of freedom.

A table is tested only when every cell has at least ``MIN_USERS_PER_CELL``
users and at least ``MIN_EXPECTED_PER_CELL`` expected converters and
non-converters at the table's pooled rate (``floor_reason``).  Below that
floor the chi-squared approximation is not calibrated on skewed allocations.

The segment breakdown's heterogeneity flag (#1969,
``dimensional_analysis_service.detect_hte``) runs ``interaction_test`` on a
2 x J table of segments, with a lower floor of its own:
``HTE_MIN_EXPECTED_PER_CELL`` (10) expected converters and non-converters in
each arm of a segment, at the pooled rate, and at least one user per arm.  A
breakdown's segments are far smaller than an experiment's arms; under this
module's floor the reported case would never be tested.  Its seeded
simulations (``backend/tests/unit/stats_validation/
test_golden_hte_false_positive_rate.py``) pin the share flagged at alpha .05:
108 of 2,000 tables on the reported case (4 segments of 90 users per arm,
the same lift in each).  The test is mildly liberal in one corner, many equal
small segments at a high base rate: 22 segments of 50 users per arm at 50%
flag 121 of 2,000, and 21 of 50 users at 30% -> 38% flag 139 of 2,000.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from scipy import stats

#: Fewer users than this in any cell of the 2 x J table: not tested.
MIN_USERS_PER_CELL = 100

#: Fewer expected converters (or non-converters) than this in any cell, at
#: the table's pooled rate: not tested.
MIN_EXPECTED_PER_CELL = 25

#: Every ``unavailable_reason`` of the pair analysis, in the order they are
#: checked: the first that applies wins.  The first five apply to a whole
#: experiment, the last two to one treatment.
MUTUAL_EXCLUSION_GROUP = "mutual_exclusion_group"
NO_SHARED_USERS = "no_shared_users"
NO_METRIC = "no_metric"
NOT_A_PROPORTION_METRIC = "not_a_proportion_metric"
NO_CONTROL_VARIANT = "no_control_variant"
TOO_FEW_SHARED_USERS = "too_few_shared_users"
TOO_FEW_CONVERSIONS = "too_few_conversions"

UNAVAILABLE_REASONS = (
    MUTUAL_EXCLUSION_GROUP,
    NO_SHARED_USERS,
    NO_METRIC,
    NOT_A_PROPORTION_METRIC,
    NO_CONTROL_VARIANT,
    TOO_FEW_SHARED_USERS,
    TOO_FEW_CONVERSIONS,
)


class InteractionFitError(Exception):
    """The fit did not reach the maximum-likelihood estimate.

    Never caught: it reaches the route's ``unexpected_failure`` (a 500).  The
    fit below cannot produce it on a table that passes the floor; it is an
    assertion, kept so that a defect is a 500 and never a wrong answer.
    """


@dataclass(frozen=True)
class InteractionTest:
    """The result of ``interaction_test``."""

    statistic: float
    degrees_of_freedom: int
    p_value: float


def _as_tables(n: Sequence, x: Sequence):
    n_arr = np.asarray(n, dtype=float)
    x_arr = np.asarray(x, dtype=float)
    if n_arr.shape != x_arr.shape or n_arr.ndim != 2 or n_arr.shape[0] != 2:
        raise ValueError("n and x must both have shape (2, J)")
    return n_arr, x_arr


def floor_reason(n: Sequence, x: Sequence) -> Optional[str]:
    """Why this 2 x J table is not tested, or None when it is.

    ``too_few_shared_users``: fewer than two columns, or a cell with fewer
    than ``MIN_USERS_PER_CELL`` users.  ``too_few_conversions``: a cell whose
    expected converters ``n * pbar`` or non-converters ``n * (1 - pbar)`` is
    below ``MIN_EXPECTED_PER_CELL``, where ``pbar`` is the table's pooled rate.
    The first is checked first.
    """
    n_arr, x_arr = _as_tables(n, x)
    if n_arr.shape[1] < 2 or (n_arr < MIN_USERS_PER_CELL).any():
        return TOO_FEW_SHARED_USERS
    pooled = x_arr.sum() / n_arr.sum()
    if (n_arr * pooled < MIN_EXPECTED_PER_CELL).any() or (
        n_arr * (1.0 - pooled) < MIN_EXPECTED_PER_CELL
    ).any():
        return TOO_FEW_CONVERSIONS
    return None


#: A bracket stops when its width is at most this many ulps of its upper end...
_RELATIVE_WIDTH = 4 * np.finfo(float).eps
#: ...or at most this, for values near 0.
_ABSOLUTE_WIDTH = 1e-18
#: Halvings allowed per bisection.  The width after k halvings is at most
#: 2 ** (1 - k), so the stopping rule is met within 61; reaching this raises.
_MAX_HALVINGS = 200
#: A cell within this of 0 or 1, whose count is there too, adds 0 to X^2.
_EXACT_EDGE = 1e-12


def _ratio(count: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    """count / denominator, with 0 where the count is 0 (0 log 0 = 0)."""
    out = np.zeros(np.broadcast(count, denominator).shape)
    np.divide(count, denominator, out=out, where=(count != 0))
    return out


def _scores(x0, m0, x1, m1, b, d):
    """The control and treatment cells' scores at baseline b and difference d."""
    t = b + d
    return (
        _ratio(x0, b) - _ratio(m0, 1.0 - b),
        _ratio(x1, t) - _ratio(m1, 1.0 - t),
    )


def _narrow(lo: np.ndarray, hi: np.ndarray) -> bool:
    return bool(
        np.all(hi - lo <= np.maximum(_ABSOLUTE_WIDTH, _RELATIVE_WIDTH * np.abs(hi)))
    )


def _baselines(x0, m0, x1, m1, d):
    """For each column, the baseline that maximises the likelihood at d.

    The column's log-likelihood is strictly concave in b on
    [max(0, -d), min(1, 1 - d)], so its derivative (the two cells' scores
    added) falls as b rises: bisection on its sign.
    """
    lo = np.maximum(0.0, -d) + np.zeros_like(x0)
    hi = np.minimum(1.0, 1.0 - d) + np.zeros_like(x0)
    for _ in range(_MAX_HALVINGS):
        mid = 0.5 * (lo + hi)
        s0, s1 = _scores(x0, m0, x1, m1, mid, d)
        rising = (s0 + s1) > 0
        lo = np.where(rising, mid, lo)
        hi = np.where(rising, hi, mid)
        if _narrow(lo, hi):
            return 0.5 * (lo + hi)
    raise InteractionFitError("the baseline bisection did not narrow")


def _profile_slope(x0, m0, x1, m1, d):
    """The derivative in d of the profile log-likelihood.

    In the interior the control cell's score is minus the treatment cell's.
    When the baseline sits on a bound, the derivative is the score of the
    cell that is not on its boundary: the one farther from 0 and 1.
    """
    b = _baselines(x0, m0, x1, m1, d)
    s0, s1 = _scores(x0, m0, x1, m1, b, d)
    t = b + d
    control_inside = np.minimum(b, 1.0 - b) >= np.minimum(t, 1.0 - t)
    return np.where(control_inside, -s0, s1).sum(axis=-1)


def fit_additive_batch(n: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Maximum-likelihood rates of the additive model for each table.

    ``n`` and ``x`` have shape ``(B, 2, J)``; the result has the same shape.
    The log-likelihood is concave, so its profile in the common difference
    ``d`` is too: ``d`` is found by bisection on the sign of the profile's
    derivative over (-1, 1), and each baseline by an inner bisection.  Every
    bisection keeps a bracket around the maximiser and halves it, so nothing
    depends on a start value or a step size.

    Raises:
        InteractionFitError: a bracket did not narrow within the cap (it
            cannot, by construction; kept as an assertion).
    """
    n = np.asarray(n, dtype=float)
    x = np.asarray(x, dtype=float)
    x0, x1 = x[:, 0, :], x[:, 1, :]
    m0, m1 = n[:, 0, :] - x0, n[:, 1, :] - x1
    lo = np.full((n.shape[0], 1), -1.0)
    hi = np.full((n.shape[0], 1), 1.0)
    for _ in range(_MAX_HALVINGS):
        mid = 0.5 * (lo + hi)
        rising = (_profile_slope(x0, m0, x1, m1, mid) > 0)[:, None]
        lo = np.where(rising, mid, lo)
        hi = np.where(rising, hi, mid)
        if _narrow(lo, hi):
            break
    else:
        raise InteractionFitError("the difference bisection did not narrow")
    d = 0.5 * (lo + hi)
    b = _baselines(x0, m0, x1, m1, d)
    return np.clip(np.stack([b, b + d], axis=1), 0.0, 1.0)


def pearson_statistic_batch(
    n: np.ndarray, x: np.ndarray, fitted: np.ndarray
) -> np.ndarray:
    """Pearson's X^2 of each table against its fitted rates.

    A cell whose fitted rate is on the boundary of [0, 1] where its count is
    too (no converter at a fitted 0, every user converting at a fitted 1)
    contributes 0: the model fits it exactly.
    """
    n = np.asarray(n, dtype=float)
    x = np.asarray(x, dtype=float)
    exact = ((x == 0) & (fitted <= _EXACT_EDGE)) | (
        (x == n) & (fitted >= 1.0 - _EXACT_EDGE)
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        term = (x - n * fitted) ** 2 / (n * fitted * (1.0 - fitted))
    return np.where(exact, 0.0, term).sum(axis=(1, 2))


def interaction_test(n: Sequence, x: Sequence) -> InteractionTest:
    """Pearson's X^2 test of the additive model on one 2 x J table.

    Call it only on a table ``floor_reason`` accepts.

    Raises:
        InteractionFitError: the fit failed (a defect; never caught).
    """
    n_arr, x_arr = _as_tables(n, x)
    fitted = fit_additive_batch(n_arr[None], x_arr[None])
    statistic = float(pearson_statistic_batch(n_arr[None], x_arr[None], fitted)[0])
    if not np.isfinite(statistic):
        raise InteractionFitError("the Pearson statistic is not finite")
    df = n_arr.shape[1] - 1
    return InteractionTest(
        statistic=statistic,
        degrees_of_freedom=df,
        p_value=float(stats.chi2.sf(statistic, df)),
    )
