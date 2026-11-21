"""
A mean metric's p-value from a permutation test of Welch's t (#2041).

``/results`` analyses a revenue, count, duration or custom metric as its mean
per assigned user (``per_user_mean_metric_result``).  Each treatment's p-value
against the control comes from this module: the users of the two arms are
reassigned at random ``B`` times, each arm keeping its size, Welch's t is
computed for every reassignment, and the p-value is twice the smaller tail,
counting the observed assignment.  On a zero-inflated, right-skewed metric at
a lopsided split, Welch's own p-value names a variant that is no better about
three times as often as alpha allows; this one holds alpha when both variants
have the same distribution, at every split measured
(``backend/tests/unit/stats_validation/test_false_winner_unequal_split.py``).

The procedure is a contract: it decides every mean metric's p-value, so a
change to any step below changes p-values and bumps ``ENGINE_VERSION``.

1. **Canonical values.**  Each arm's per-user values that do not count as 0,
   sorted ascending: ``vc`` (``K_c`` values) and ``vt`` (``K_t``).  Users not
   listed have value 0.  ``n_c`` and ``n_t`` are the arm sizes
   ``per_user_mean_metric_result`` uses (``max(n, len(values))``).  A per-user
   total ``v`` counts as 0 (unlisted) when ``|v| <= 1e-9 * max(1, M)``, where
   ``M`` is the largest ``|v|`` over every arm of the metric
   (:func:`listed_values`).  The same rule decides ``K`` and the values in
   ``T`` and ``T*``, so a total that is 0 in one summation order and a
   rounding residue such as ``-1.78e-15`` in another gives the same p-value.
   When a metric's largest per-user total M is large, totals at or below
   1e-9·M count as 0 in the test only: at M = 1e9 that is every total of 1 or
   less, at M = 1e12 every total of 1,000 or less. The reported mean, standard
   deviation, conversions and intervals use every total.  A net-zero user
   whose individual events are much larger than M (about a million times or
   more) can still count as listed or not, depending on summation order.
   Exact summation would close this, and is not done.

2. **Centring.**  Every sum is centred on the grand mean ``kbar`` over all
   ``n_c + n_t`` users (``math.fsum`` of the listed values over
   ``n_c + n_t``): a listed value becomes ``v - kbar`` and an unlisted user
   ``-kbar``, so a large offset does not cancel.

3. **Seed.**  ``derive_test_seed(f"perm-t|{metric_id}|{control_id}|"
   f"{treatment_id}|{n_c}|{n_t}|{K_c}|{K_t}|{B}")`` (blake2b, 8 bytes,
   big-endian, top bit cleared; ``backend.app.core.stats_engine``), then
   ``rng = numpy.random.default_rng(seed)``.  ``metric_id`` is
   ``str(metric.id)`` and ``control_id`` and ``treatment_id`` are
   ``str(variant_id)``, each the canonical lowercase hyphenated UUID
   (``str(uuid.UUID(x))``); a metric or variant without one raises
   :class:`ComparisonIdentityError`, with no fallback (:func:`comparison_seed`).
   The values are not in the key (``Event.value`` is a float, and a ``SUM``
   under another query plan can move a total's last bit), and neither is the
   day: the same rows give the same p-value in every request, process and day.

4. **Draws.**  The smaller arm by ``n`` is the treatment when ``n_t <= n_c``,
   otherwise the control.  The ``K = K_c + K_t`` pooled listed values are
   ``vc`` and ``vt`` concatenated and sorted once, ascending (float64); equal
   values are interchangeable.  One Generator per comparison:
   ``m = rng.hypergeometric(n_small, n_large, K, size=B)`` as one call, then
   for ``b = 0 .. B - 1`` in order
   ``rng.choice(K, size=int(m[b]), replace=False, shuffle=False)``, the
   indices into the pooled array of the listed values that fall in the
   smaller arm in permutation ``b``.  Nothing else draws from it.  ``m[b]`` is
   how many listed values the smaller arm gets, which has exactly the
   distribution of a full permutation of the ``n_c + n_t`` users; the cost is
   ``O(B * m)`` and the memory ``O(K)``.

5. **Statistic.**  ``T`` for the observed assignment and ``T*`` for each
   permutation, computed by one function from the centred sums
   (:func:`welch_t_from_sums`): ``d / sqrt(v_t / n_t + v_c / n_c)``, with
   ``d`` the treatment's mean minus the control's and ddof-1 variances, each
   ``max(0, computed)`` as ``sufficient_stats_analysis._mean_arm`` clamps.  A
   zero standard error gives ``+inf`` or ``-inf`` by the sign of ``d``, or 0
   when ``d`` is 0.

6. **p-value.**  A ``T*`` within ``1e-9 * max(1, |T|)`` of ``T`` is a tie and
   counts in both tails:
   ``p = min(1, 2 * min(1 + #{T* >= T}, 1 + #{T* <= T}) / (B + 1))``.
   It is a multiple of ``2 / (B + 1)``, so at least 0.001 at ``B = 1,999``.

7. **B.**  ``k`` is the number of the metric's treatments that receive a
   p-value: exactly the family ``adjusted_p_values`` adjusts.  It is the same
   whatever the correction method, so changing the method never changes a
   p-value.  In integer arithmetic, ``a = round(alpha * 10**9)`` and
   ``B + 1 = max(2000, 2000 * ceil(4 * k * 10**9 / (2000 * a)))`` with
   ``ceil(x / y)`` as ``-(-x // y)`` (:func:`permutations_for`), so the
   smallest Bonferroni- or BH-adjusted p-value, ``k * 2 / (B + 1)``, stays at
   or below ``alpha / 2``.  A float formula on ``alpha = 1 - confidence``
   would give 3,999 where 1,999 is right at (50, 1 - 0.9).

Pure: numpy and math only, no database.  ``numpy`` is pinned
(``backend/requirements.txt``), and NEP 19 does not promise a Generator's
stream across numpy versions: a numpy upgrade that moves the stream fails the
``per_user_mean_metric_result`` fingerprint and bumps ``ENGINE_VERSION``.
"""

from __future__ import annotations

import math
import uuid
from typing import Any, List, Sequence

import numpy as np

from backend.app.core.stats_engine import derive_test_seed

#: ``B + 1`` is at least this, and a multiple of it.
RESAMPLE_BLOCK = 2000

#: A per-user total within this share of ``max(1, M)`` of 0 counts as 0.
ZERO_TOLERANCE = 1e-9

#: A permuted statistic within this share of ``max(1, |T|)`` of ``T`` is a tie.
TIE_TOLERANCE = 1e-9


class ComparisonIdentityError(Exception):
    """A metric or variant whose id is missing or not a UUID.

    The id is part of the comparison's seed, so a comparison without one has
    no defined p-value.  Deliberately not a ``ValueError``: the results route
    answers a ``ValueError`` with 404, which would hide the defect; raised, it
    reaches the route's ``unexpected_failure`` (500).
    """


def canonical_id(value: Any, what: str) -> str:
    """``value`` as the canonical lowercase hyphenated UUID string.

    Raises:
        ComparisonIdentityError: ``value`` is None or not a UUID.
    """
    if value is None:
        raise ComparisonIdentityError(f"the {what} has no id")
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise ComparisonIdentityError(
            f"the {what}'s id {value!r} is not a UUID"
        ) from exc


def permutations_for(treatments: int, alpha: float) -> int:
    """``B``, the number of permutations for a metric with ``treatments``
    compared treatments at significance level ``alpha`` (step 7).

    Raises:
        ValueError: ``treatments`` is not a positive integer, or ``alpha`` is
            not between 0 and 1.
    """
    if isinstance(treatments, bool) or not isinstance(treatments, int):
        raise ValueError(f"treatments must be an integer, got {treatments!r}")
    if treatments < 1:
        raise ValueError(f"treatments must be at least 1, got {treatments}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be between 0 and 1, got {alpha!r}")
    a = round(alpha * 10**9)
    blocks = -(-(4 * treatments * 10**9) // (RESAMPLE_BLOCK * a))
    return max(RESAMPLE_BLOCK, RESAMPLE_BLOCK * blocks) - 1


def listed_values(arms: Sequence[Sequence[float]]) -> List[np.ndarray]:
    """Each arm's listed values (step 1): the per-user totals that do not
    count as 0, sorted ascending, as float64 arrays.

    ``arms`` is every arm of the metric: the tolerance is ``1e-9 * max(1, M)``
    with ``M`` the largest ``|v|`` over all of them.
    """
    arrays = [np.asarray(values, dtype=np.float64).reshape(-1) for values in arms]
    largest = max((float(np.max(np.abs(a))) for a in arrays if a.size), default=0.0)
    threshold = ZERO_TOLERANCE * max(1.0, largest)
    return [np.sort(a[np.abs(a) > threshold]) for a in arrays]


def comparison_seed(
    metric_id: Any,
    control_id: Any,
    treatment_id: Any,
    n_control: int,
    n_treatment: int,
    listed_control: int,
    listed_treatment: int,
    permutations: int,
) -> int:
    """The seed of one treatment's comparison with the control (step 3).

    Raises:
        ComparisonIdentityError: an id is missing or not a UUID.
    """
    key = (
        f"perm-t|{canonical_id(metric_id, 'metric')}"
        f"|{canonical_id(control_id, 'control variant')}"
        f"|{canonical_id(treatment_id, 'treatment variant')}"
        f"|{int(n_control)}|{int(n_treatment)}"
        f"|{int(listed_control)}|{int(listed_treatment)}|{int(permutations)}"
    )
    return derive_test_seed(key)


def welch_t_from_sums(
    sum_t: Any,
    sq_t: Any,
    n_t: int,
    sum_c: Any,
    sq_c: Any,
    n_c: int,
) -> np.ndarray:
    """Welch's t from each arm's centred sums (step 5), elementwise.

    ``sum_*`` is an arm's sum of centred values and ``sq_*`` its sum of their
    squares, over its ``n_*`` users (at least 2 each).  ``T`` and every
    ``T*`` go through this one function, so they share their arithmetic.
    """
    sum_t = np.asarray(sum_t, dtype=np.float64)
    sum_c = np.asarray(sum_c, dtype=np.float64)
    sq_t = np.asarray(sq_t, dtype=np.float64)
    sq_c = np.asarray(sq_c, dtype=np.float64)
    var_t = np.maximum(sq_t - sum_t * sum_t / n_t, 0.0) / (n_t - 1)
    var_c = np.maximum(sq_c - sum_c * sum_c / n_c, 0.0) / (n_c - 1)
    d = sum_t / n_t - sum_c / n_c
    se = np.sqrt(var_t / n_t + var_c / n_c)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = d / se
    zero_se = np.where(d > 0.0, np.inf, np.where(d < 0.0, -np.inf, 0.0))
    return np.where(se > 0.0, t, zero_se)


def permutation_t_p_value(
    control_values: Sequence[float],
    n_control: int,
    treatment_values: Sequence[float],
    n_treatment: int,
    *,
    seed: int,
    permutations: int,
) -> float:
    """The equal-tailed permutation p-value of Welch's t (steps 2 and 4-6).

    Args:
        control_values: the control's listed values (:func:`listed_values`):
            the users whose total does not count as 0.  Every other user of
            the arm is 0.
        n_control: the control's users, at least 2 and at least the number
            of listed values.
        treatment_values: the treatment's listed values.
        n_treatment: the treatment's users, likewise.
        seed: :func:`comparison_seed` for this comparison.
        permutations: ``B``, from :func:`permutations_for`.

    Returns:
        ``min(1, 2 * min(1 + #{T* >= T}, 1 + #{T* <= T}) / (B + 1))``.

    Raises:
        ValueError: an arm has fewer than 2 users or more listed values than
            users, a value is not finite, or ``permutations`` is not a
            positive integer.
    """
    for n in (n_control, n_treatment, permutations):
        if isinstance(n, bool) or not isinstance(n, (int, np.integer)):
            raise ValueError(f"expected an integer, got {n!r}")
    n_c, n_t, b_count = int(n_control), int(n_treatment), int(permutations)
    if n_c < 2 or n_t < 2:
        raise ValueError("each arm needs at least 2 users")
    if b_count < 1:
        raise ValueError("permutations must be at least 1")
    vc = np.sort(np.asarray(control_values, dtype=np.float64).reshape(-1))
    vt = np.sort(np.asarray(treatment_values, dtype=np.float64).reshape(-1))
    if vc.size > n_c or vt.size > n_t:
        raise ValueError("an arm has more listed values than users")
    if not (np.all(np.isfinite(vc)) and np.all(np.isfinite(vt))):
        raise ValueError("a value is not finite")

    n_users = n_c + n_t
    pooled = np.sort(np.concatenate([vc, vt]))
    listed = int(pooled.size)
    kbar = math.fsum(pooled.tolist()) / n_users
    centred = pooled - kbar
    centred_sq = centred * centred
    total = centred.sum()
    total_sq = centred_sq.sum()

    small_is_treatment = n_t <= n_c
    n_small, n_large = (n_t, n_c) if small_is_treatment else (n_c, n_t)
    observed_small = vt if small_is_treatment else vc

    def statistic(sum_small: Any, sq_small: Any, m: Any) -> np.ndarray:
        # The smaller arm holds m listed values and n_small - m zeros; the
        # larger arm the other listed values and the rest of the zeros.
        m = np.asarray(m, dtype=np.float64)
        zeros_small = n_small - m
        zeros_large = n_large - (listed - m)
        sum_s = sum_small - zeros_small * kbar
        sq_s = sq_small + zeros_small * (kbar * kbar)
        sum_l = (total - sum_small) - zeros_large * kbar
        sq_l = (total_sq - sq_small) + zeros_large * (kbar * kbar)
        if small_is_treatment:
            return welch_t_from_sums(sum_s, sq_s, n_small, sum_l, sq_l, n_large)
        return welch_t_from_sums(sum_l, sq_l, n_large, sum_s, sq_s, n_small)

    centred_small = observed_small - kbar
    observed = float(
        statistic(
            np.array([centred_small.sum()]),
            np.array([(centred_small * centred_small).sum()]),
            np.array([observed_small.size]),
        )[0]
    )

    rng = np.random.default_rng(int(seed))
    m_draws = rng.hypergeometric(n_small, n_large, listed, size=b_count)
    sums = np.empty(b_count, dtype=np.float64)
    squares = np.empty(b_count, dtype=np.float64)
    for b in range(b_count):
        chosen = rng.choice(listed, size=int(m_draws[b]), replace=False, shuffle=False)
        sums[b] = centred[chosen].sum()
        squares[b] = centred_sq[chosen].sum()
    permuted = statistic(sums, squares, m_draws)

    if math.isfinite(observed):
        tolerance = TIE_TOLERANCE * max(1.0, abs(observed))
        tie = (permuted == observed) | (np.abs(permuted - observed) <= tolerance)
    else:
        tie = permuted == observed
    at_least = int(np.count_nonzero((permuted > observed) | tie))
    at_most = int(np.count_nonzero((permuted < observed) | tie))
    return min(1.0, 2.0 * min(1 + at_least, 1 + at_most) / (b_count + 1))
