"""The statistics the results must show, computed from the per-user values.

Written from the docs, not from the product:

* a conversion metric is analysed as a proportion, a two-sided Fisher's exact
  test on the users who converted and those who did not, each treatment
  against the control (``docs/architecture/technical-guide.md``, "Statistical
  Methods"); its ``absolute_difference`` is the difference of the shares;
* a revenue, count, duration or custom metric is analysed as its mean per
  assigned user (``docs/getting-started/concepts.md``); its p-value is a
  permutation test of Welch's t statistic, twice the smaller tail
  (``docs/architecture/technical-guide.md``, engine 1.13.0), computed here
  with scipy's own permutation test over every assigned user's value, so it
  agrees with the server's within the Monte Carlo error of both
  (``permutation_tolerance``), never exactly; its ``absolute_difference`` is
  the difference of the means and ``absolute_difference_ci`` Welch's interval
  at the confidence level (``docs/api/endpoints.md``, "The difference from the
  control, its intervals and the impact");
* ``adjusted_p_value`` applies the experiment's correction method across the
  treatments of one metric, and ``is_significant`` is decided on it
  (``docs/statistics/fdr-correction.md``);
* the sample-ratio check is Pearson's chi-square of the assignment counts
  against the allocation (``docs/guides/user-guide.md``, "Sample Ratio Check").

A proportion's interval (Newcombe's), the Bayesian and the sequential fields
are not computed here.

Welch's interval is computed from the per-user values through their exact sums
(``math.fsum``), so the reference does not share the product's arithmetic. It
uses scipy for the distributions, as the product does: a defect in scipy would
be shared.  The mean's permutation test is scipy's: it reassigns all the users
at random, where the server draws how many non-zero values fall in each arm
and which, so the two share neither the draws nor the arithmetic.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scipy import stats

#: The reference's resamples plus one.
REFERENCE_RESAMPLES = 10_000

#: How each metric type is analysed.
ANALYSIS = {
    "conversion": "proportion",
    "revenue": "mean",
    "count": "mean",
    "duration": "mean",
    "custom": "mean",
}
CORRECTION_METHODS = ("none", "bonferroni", "benjamini_hochberg")


@dataclass(frozen=True)
class Welch:
    p_value: float | None
    #: None when an arm has no user.
    absolute_difference: float | None
    absolute_difference_ci: tuple[float, float] | None


@dataclass(frozen=True)
class Comparison:
    """One treatment of one metric against the control."""

    variant: str
    p_value: float | None
    adjusted_p_value: float | None
    absolute_difference: float | None
    #: Welch's interval for a mean; None for a proportion (not computed here).
    absolute_difference_ci: tuple[float, float] | None
    is_significant: bool | None


def fisher_p(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
) -> float:
    """The two-sided p-value of Fisher's exact test on the 2x2 table of users."""
    table = [
        [treatment_converted, treatment_users - treatment_converted],
        [control_converted, control_users - control_converted],
    ]
    if min(min(row) for row in table) < 0:
        raise ValueError("more users converted than were assigned")
    return float(stats.fisher_exact(table, alternative="two-sided")[1])


def _moments(values: Sequence[float]) -> tuple[int, float, float]:
    """n, the mean and the sample variance, from exact sums."""
    n = len(values)
    mean = math.fsum(values) / n
    variance = math.fsum((v - mean) ** 2 for v in values) / (n - 1)
    return n, mean, variance


def welch(
    control: Sequence[float], treatment: Sequence[float], confidence_level: float
) -> Welch:
    """Welch's t-test of the treatment's mean against the control's.

    The p-value is two-sided; the interval is the difference of the means plus
    or minus the t quantile at the Welch-Satterthwaite degrees of freedom times
    the standard error. With fewer than two users in an arm, or no variation
    in either, the p-value and the interval are None.
    """
    if not control or not treatment:
        return Welch(None, None, None)
    if len(control) < 2 or len(treatment) < 2:
        difference = math.fsum(treatment) / len(treatment) - math.fsum(control) / len(
            control
        )
        return Welch(None, difference, None)
    n_c, mean_c, var_c = _moments(control)
    n_t, mean_t, var_t = _moments(treatment)
    difference = mean_t - mean_c
    a, b = var_t / n_t, var_c / n_c
    if a + b == 0:
        return Welch(None, difference, None)
    p_value = float(
        stats.ttest_ind_from_stats(
            mean_t,
            math.sqrt(var_t),
            n_t,
            mean_c,
            math.sqrt(var_c),
            n_c,
            equal_var=False,
        ).pvalue
    )
    df = (a + b) ** 2 / (a * a / (n_t - 1) + b * b / (n_c - 1))
    margin = float(stats.t.ppf((1 + confidence_level) / 2, df)) * math.sqrt(a + b)
    return Welch(p_value, difference, (difference - margin, difference + margin))


def permutation_rng(seed: int, scenario: str, metric: str) -> int:
    """The int that seeds the reference's permutation test of one metric:
    sha256 of ``seed|scenario|metric``, 63 bits, so a failure reproduces from
    the run's printed seed."""
    digest = hashlib.sha256(f"{seed}|{scenario}|{metric}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def permutation_p(
    control: Sequence[float], treatment: Sequence[float], rng: int
) -> float:
    """The two-sided permutation p-value of Welch's t, treatment against
    control, over every assigned user's value (0 included): scipy's
    ``ttest_ind(equal_var=False, method=PermutationMethod(...))`` with
    ``REFERENCE_RESAMPLES - 1`` resamples, which is twice the smaller tail
    counting the observed assignment."""
    method = stats.PermutationMethod(
        n_resamples=REFERENCE_RESAMPLES - 1, batch=500, rng=rng
    )
    return float(
        stats.ttest_ind(
            list(treatment), list(control), equal_var=False, method=method
        ).pvalue
    )


def product_resamples(treatments: int, confidence_level: float) -> int:
    """The server's permutations plus one for a metric with *treatments*
    compared treatments, as ``docs/architecture/technical-guide.md`` gives the
    rule: ``max(2000, 2000 * ceil(4k / (2000 * alpha)))`` with alpha
    ``1 - confidence_level``, in integer arithmetic on
    ``round(alpha * 10**9)``."""
    a = round((1 - confidence_level) * 10**9)
    blocks = -(-(4 * treatments * 10**9) // (2000 * a))
    return max(2000, 2000 * blocks)


def permutation_tolerance(
    p_server: float,
    p_reference: float,
    server_resamples: int,
    reference_resamples: int = REFERENCE_RESAMPLES,
) -> float:
    """How far apart two Monte Carlo permutation p-values of the same data
    may be: 5 standard errors of the difference of two equal-tailed p-values
    plus the server's step.

    ``q = clip((p_server + p_reference) / 4, 1 / reference_resamples, 0.5)``
    is the smaller tail; each p-value is twice a share, so the standard error
    of the difference is ``2 * sqrt(q (1 - q) (1 / B_s + 1 / B_r))``, with
    ``B`` the resamples plus one; the server's p-value moves in steps of
    ``2 / B_s``.  At p 0.05, 2,000 and 10,000: 0.0392.
    """
    q = min(max((p_server + p_reference) / 4, 1 / reference_resamples), 0.5)
    se = 2 * math.sqrt(q * (1 - q) * (1 / server_resamples + 1 / reference_resamples))
    return 5 * se + 2 / server_resamples


def significance_comparable(
    p_server: float, p_reference: float, alpha: float, tolerance: float
) -> bool:
    """Whether ``is_significant`` can be compared exactly: both p-values are
    more than *tolerance* from alpha, so Monte Carlo error cannot put them on
    different sides of it."""
    return abs(p_server - alpha) > tolerance and abs(p_reference - alpha) > tolerance


def adjust(p_values: Sequence[float | None], method: str) -> list[float | None]:
    """The p-values adjusted by *method* across one metric's treatments.

    ``none`` adjusts nothing and returns None for each comparison, which
    mirrors ``GET /api/v1/results/{id}`` (``adjusted_p_value`` is null under
    ``none``; the decision is then made on the p-value, see ``compare``);
    ``bonferroni`` multiplies each by their number; ``benjamini_hochberg`` is
    the step-up adjustment. A None stays None and is not counted.
    """
    if method not in CORRECTION_METHODS:
        raise ValueError(
            f"correction method {method!r} is not one of {CORRECTION_METHODS}"
        )
    present = [i for i, p in enumerate(p_values) if p is not None]
    if method == "none":
        return [None] * len(p_values)
    adjusted: list[float | None] = list(p_values)
    m = len(present)
    if method == "bonferroni":
        for i in present:
            adjusted[i] = min(1.0, p_values[i] * m)
    elif method == "benjamini_hochberg":
        running = 1.0
        for rank, i in reversed(
            list(enumerate(sorted(present, key=lambda j: p_values[j]), 1))
        ):
            running = min(running, p_values[i] * m / rank)
            adjusted[i] = running
    return adjusted


def srm_p(observed: Sequence[int], allocations: Sequence[float]) -> float | None:
    """Pearson's chi-square p-value of the assignment counts against the
    allocation (normalised); None when no user is assigned."""
    if len(observed) != len(allocations) or len(observed) < 2:
        raise ValueError("one count and one allocation per variant, at least two")
    total = sum(observed)
    if total == 0:
        return None
    share = math.fsum(allocations)
    expected = [total * a / share for a in allocations]
    return float(stats.chisquare(list(observed), f_exp=expected).pvalue)


def compare(
    per_user: Mapping[str, Sequence[float]],
    analysis: str,
    correction_method: str,
    confidence_level: float,
    *,
    rng: int | None = None,
) -> list[Comparison]:
    """Each treatment of one metric against the control.

    *per_user* maps each variant, in creation order with the control first, to
    its users' values (``contract.ExperimentExpected.per_user``): 1 or 0 for a
    proportion. *analysis* is ``proportion`` or ``mean`` (``ANALYSIS``).  A
    mean's p-value is ``permutation_p`` seeded with *rng*
    (``permutation_rng``), which a mean therefore needs; its interval is
    Welch's.
    """
    if analysis not in ("proportion", "mean"):
        raise ValueError(f"analysis {analysis!r} is not proportion or mean")
    if analysis == "mean" and rng is None:
        raise ValueError("a mean's permutation p-value needs rng")
    (_, control), *treatments = per_user.items()
    if not treatments:
        raise ValueError("a comparison needs a control and a treatment")
    found = []
    for name, values in treatments:
        if analysis == "proportion":
            if not values or not control:
                found.append((name, None, None, None))
                continue
            converted_c, converted_t = (
                round(math.fsum(control)),
                round(math.fsum(values)),
            )
            p = fisher_p(len(control), converted_c, len(values), converted_t)
            difference = converted_t / len(values) - converted_c / len(control)
            found.append((name, p, difference, None))
        else:
            result = welch(control, values, confidence_level)
            # No test where Welch has none: fewer than 2 users in an arm, or
            # neither arm varies.
            p = None if result.p_value is None else permutation_p(control, values, rng)
            found.append(
                (
                    name,
                    p,
                    result.absolute_difference,
                    result.absolute_difference_ci,
                )
            )
    adjusted = adjust([p for _, p, _, _ in found], correction_method)
    alpha = 1 - confidence_level
    comparisons = []
    for (name, p, difference, ci), q in zip(found, adjusted, strict=True):
        decisive = q if q is not None else p  # under ``none``, the p-value itself
        comparisons.append(
            Comparison(
                variant=name,
                p_value=p,
                adjusted_p_value=q,
                absolute_difference=difference,
                absolute_difference_ci=ci,
                is_significant=None if decisive is None else decisive < alpha,
            )
        )
    return comparisons


@dataclass(frozen=True)
class Stratified:
    """One treatment's stratified estimate over every group (#448)."""

    absolute_difference: float
    standard_error: float
    #: None when the standard error is 0.
    absolute_difference_ci: tuple[float, float] | None
    #: In percent; None when the weighted control rate is 0.
    relative_improvement_pct: float | None
    #: Two-sided normal; None when the standard error is 0.
    p_value: float | None


def stratified(
    groups: Sequence[Mapping[str, tuple[int, int]]],
    control: str,
    treatment: str,
    confidence_level: float,
) -> Stratified:
    """The stratified difference in conversion rates of *treatment* from
    *control* (Cochran, *Sampling Techniques*, chapter 5; the method of
    ``docs/guides/balance-within-groups.md``), from each group's counts.

    *groups* holds one mapping per group, variant -> (users, converting
    users). With ``N_s`` the users of every variant in group ``s`` and ``N``
    the users of every group, ``w_s = N_s / N`` and ``p_vs`` the share of
    variant ``v``'s users in ``s`` who converted:

    * the difference is the sum over groups of ``w_s * (p_ts - p_cs)``;
    * its variance is the sum of ``w_s**2 * (u_ts / n_ts + u_cs / n_cs)``, with
      ``u = p * (1 - p) * n / (n - 1)``, the unbiased variance of a 0/1 value;
    * the relative change is the difference over ``sum w_s * p_cs``, in percent;
    * the interval is the difference plus or minus the normal quantile times
      the standard error, and the p-value the two-sided normal one.

    Every group needs 2 users in *control* and *treatment* (a smaller one is
    merged by the product, which this reference does not model); a refused
    group raises ``ValueError``. The sums are ``math.fsum``, so the order of
    the groups changes nothing, and the p-value is ``erfc``, not scipy's.
    """
    if not 0 < confidence_level < 1:
        raise ValueError("the confidence level is strictly between 0 and 1")
    total = sum(sum(users for users, _ in g.values()) for g in groups)
    if total <= 0:
        raise ValueError("no group has a user")
    differences, variances, control_rates = [], [], []
    for g in groups:
        (n_t, c_t), (n_c, c_c) = g.get(treatment, (0, 0)), g.get(control, (0, 0))
        if n_t < 2 or n_c < 2:
            raise ValueError(
                "a group has fewer than 2 users in the control or treatment"
            )
        weight = sum(users for users, _ in g.values()) / total
        p_t, p_c = c_t / n_t, c_c / n_c
        differences.append(weight * (p_t - p_c))
        variances.append(
            weight
            * weight
            * (
                p_t * (1 - p_t) / (n_t - 1)  # u_t / n_t, with u = p(1-p)n/(n-1)
                + p_c * (1 - p_c) / (n_c - 1)
            )
        )
        control_rates.append(weight * p_c)
    difference = math.fsum(differences)
    error = math.sqrt(math.fsum(variances))
    base = math.fsum(control_rates)
    z = float(stats.norm.ppf((1 + confidence_level) / 2))
    return Stratified(
        absolute_difference=difference,
        standard_error=error,
        absolute_difference_ci=(
            (difference - z * error, difference + z * error) if error > 0 else None
        ),
        relative_improvement_pct=difference / base * 100.0 if base > 0 else None,
        p_value=math.erfc(abs(difference) / error / math.sqrt(2))
        if error > 0
        else None,
    )
