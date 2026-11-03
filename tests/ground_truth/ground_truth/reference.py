"""The statistics the results must show, computed from the per-user values.

Written from the docs, not from the product:

* a conversion metric is analysed as a proportion, a two-sided Fisher's exact
  test on the users who converted and those who did not, each treatment
  against the control (``docs/architecture/technical-guide.md``, "Statistical
  Methods"); its ``absolute_difference`` is the difference of the shares;
* a revenue, count, duration or custom metric is analysed as its mean per
  assigned user, Welch's t-test (``docs/getting-started/concepts.md``); its
  ``absolute_difference`` is the difference of the means and
  ``absolute_difference_ci`` Welch's interval at the confidence level
  (``docs/api/endpoints.md``, "The difference from the control, its intervals
  and the impact");
* ``adjusted_p_value`` applies the experiment's correction method across the
  treatments of one metric, and ``is_significant`` is decided on it
  (``docs/statistics/fdr-correction.md``);
* the sample-ratio check is Pearson's chi-square of the assignment counts
  against the allocation (``docs/guides/user-guide.md``, "Sample Ratio Check").

A proportion's interval (Newcombe's), the Bayesian and the sequential fields
are not computed here.

Welch's test is computed from the per-user values through their exact sums
(``math.fsum``), so the reference does not share the product's arithmetic. It
uses scipy for the distributions, as the product does: a defect in scipy would
be shared.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scipy import stats

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


def adjust(p_values: Sequence[float | None], method: str) -> list[float | None]:
    """The p-values adjusted by *method* across one metric's treatments.

    ``none`` leaves them; ``bonferroni`` multiplies each by their number;
    ``benjamini_hochberg`` is the step-up adjustment. A None stays None and is
    not counted.
    """
    if method not in CORRECTION_METHODS:
        raise ValueError(
            f"correction method {method!r} is not one of {CORRECTION_METHODS}"
        )
    present = [i for i, p in enumerate(p_values) if p is not None]
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
) -> list[Comparison]:
    """Each treatment of one metric against the control.

    *per_user* maps each variant, in creation order with the control first, to
    its users' values (``contract.ExperimentExpected.per_user``): 1 or 0 for a
    proportion. *analysis* is ``proportion`` or ``mean`` (``ANALYSIS``).
    """
    if analysis not in ("proportion", "mean"):
        raise ValueError(f"analysis {analysis!r} is not proportion or mean")
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
            found.append(
                (
                    name,
                    result.p_value,
                    result.absolute_difference,
                    result.absolute_difference_ci,
                )
            )
    adjusted = adjust([p for _, p, _, _ in found], correction_method)
    alpha = 1 - confidence_level
    return [
        Comparison(
            variant=name,
            p_value=p,
            adjusted_p_value=q,
            absolute_difference=difference,
            absolute_difference_ci=ci,
            is_significant=None if q is None else q < alpha,
        )
        for (name, p, difference, ci), q in zip(found, adjusted, strict=True)
    ]
