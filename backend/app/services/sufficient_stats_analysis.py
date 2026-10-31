"""
Metric results from per-variant sufficient statistics.

For a proportion metric, everything ``/results`` reports about a variant
follows from two numbers: the units in it and the units that converted.  This
module turns those counts into results; it reads no database.  ``/results``
counts assignments and converting users and passes the counts here, and a
caller that has the counts from somewhere else (a customer's warehouse, #312)
gets the same numbers for the same counts.

``binomial_metric_result`` was extracted, without numeric change, from
``AnalysisService.calculate_metric_results`` and ``_to_metric_result`` (#404).
``test_binomial_metric_result_characterisation.py`` pins the ``/results``
output it has to reproduce, and ``test_sufficient_stats_fingerprint.py`` pins
its own output per ``ENGINE_VERSION``.

``mean_metric_result`` does the same for a mean metric, from each variant's
unit count and its sums of ``y - k`` and ``(y - k) ** 2`` centred on the grand
mean ``k``.  ``test_mean_metric_result.py`` holds it to an exact reference.
``per_user_mean_metric_result`` gives ``/results`` a revenue, count, duration
or custom metric's mean per assigned user from each user's value, through
``mean_metric_result``.

``binomial_metric_result`` and ``per_user_mean_metric_result`` give each
treatment its difference from the control with an interval, the relative
change's interval and the incremental impact (#1496);
``test_difference_intervals.py`` holds them to independent oracles.
``mean_metric_result``, which warehouse mean runs use, does not carry them.
"""

import logging
import math
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from scipy import stats

logger = logging.getLogger(__name__)


class BinomialVariant(NamedTuple):
    """The identity of a variant; any object with these attributes will do
    (an ORM ``Variant`` included)."""

    id: Any
    name: str
    is_control: bool


#: ``(variant, n, n_converted)``: units in the variant and units that converted.
BinomialCounts = Tuple[Any, int, int]


#: The correction methods ``adjusted_p_values`` knows, as stored and as sent.
CORRECTION_METHODS = ("none", "bonferroni", "benjamini_hochberg")


class UnknownCorrectionMethodError(Exception):
    """A correction method that is not one of ``CORRECTION_METHODS``.

    Deliberately not a ``ValueError``: the results route answers a
    ``ValueError`` with 404 and the export reads one as "no results", and
    either would hide a method spelt wrongly somewhere in the code (#580).
    Raised, it reaches the route's ``unexpected_failure`` (500).
    """

    def __init__(self, method: Any) -> None:
        self.method = method
        super().__init__(
            f"Unknown correction method {method!r}; expected one of "
            f"{', '.join(CORRECTION_METHODS)}"
        )


def adjusted_p_values(
    p_values: List[Optional[float]], method: str
) -> List[Optional[float]]:
    """Multiple-comparison correction across the treatment variants of one metric.

    Raises:
        UnknownCorrectionMethodError: ``method`` is not one of
            ``CORRECTION_METHODS``, whatever the p-values are. An unknown
            spelling used to mean "no correction", silently.
    """
    if method not in CORRECTION_METHODS:
        raise UnknownCorrectionMethodError(method)
    valid = [(i, p) for i, p in enumerate(p_values) if p is not None]
    adjusted: List[Optional[float]] = [None] * len(p_values)
    if not valid or method == "none":
        return adjusted
    k = len(valid)
    if method == "bonferroni":
        for i, p in valid:
            adjusted[i] = min(1.0, p * k)
        return adjusted
    if method == "benjamini_hochberg":
        ordered = sorted(valid, key=lambda ip: ip[1])
        running = 1.0
        for rank in range(k, 0, -1):
            i, p = ordered[rank - 1]
            running = min(running, p * k / rank)
            adjusted[i] = min(1.0, running)
        return adjusted
    raise AssertionError(f"unreachable: {method!r}")  # pragma: no cover


def metric_lower_is_better(metric: Any) -> bool:
    """Whether a decrease is the good direction for ``metric``.

    Reads the metric definition's ``lower_is_better``.  Only ``True`` means
    lower is better: a metric without the attribute, a stored NULL (the
    column is nullable) and anything else that is not ``True`` read as False,
    higher is better.
    """
    return getattr(metric, "lower_is_better", False) is True


def effect_size_label(abs_effect: float) -> str:
    """Map absolute effect size to a human-readable label (EP-016 thresholds)."""
    if abs_effect < 0.2:
        return "negligible"
    elif abs_effect < 0.5:
        return "small"
    elif abs_effect < 0.8:
        return "medium"
    else:
        return "large"


def two_sided_z(confidence_level: float) -> float:
    """The two-sided normal critical value for ``confidence_level``.

    ``norm.ppf(1 - (1 - level) / 2)``: 1.6449 at 0.90, 1.9600 at 0.95,
    2.5758 at 0.99.

    Raises:
        ValueError: unless ``0 < confidence_level < 1``.
    """
    if not 0.0 < confidence_level < 1.0:
        raise ValueError(
            f"confidence_level must be between 0 and 1, got {confidence_level!r}"
        )
    return float(stats.norm.ppf(1.0 - (1.0 - confidence_level) / 2.0))


# ---------------------------------------------------------------------------
# A treatment's difference from the control: intervals and impact (#1496)
# ---------------------------------------------------------------------------
#
# One construction covers both analyses.  Each arm has a point estimate and
# two-sided limits at the response's level: Wilson score limits for a
# proportion, ``mean +/- c * se`` with Welch's critical value ``c`` for a mean.
# The interval of the difference is the MOVER difference of those limits
# (Newcombe's hybrid score interval for a proportion; for a mean it is
# Welch's own interval), and the interval of the ratio of the means is the
# MOVER ratio of the same limits (Fieller's interval for a mean).  The ratio's
# bound equals 1 exactly where the difference's bound equals 0, so the
# absolute interval, the relative one and the impact's agree on whether 0 is
# inside.  None of these intervals is adjusted for multiple comparisons.


class ArmLimits(NamedTuple):
    """An arm's point estimate and its two-sided limits at one level."""

    mean: float
    lower: float
    upper: float


def wilson_limits(n_converted: int, n: int, z: float) -> ArmLimits:
    """``n_converted / n`` and its Wilson score interval at critical value ``z``.

    The lower limit is exactly 0 when nothing converted and the upper exactly
    1 when everything did, as the interval's closed form gives in exact
    arithmetic.  ``n`` must be at least 1.
    """
    p = n_converted / n
    z2 = z * z
    scale = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / scale
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / scale
    lower = 0.0 if n_converted == 0 else centre - half
    upper = 1.0 if n_converted == n else centre + half
    return ArmLimits(p, lower, upper)


def newcombe_interval(treatment: ArmLimits, control: ArmLimits) -> Tuple[float, float]:
    """The MOVER interval of ``treatment.mean - control.mean`` from each arm's
    limits: Newcombe's hybrid score interval when the limits are Wilson's.

    ``d - sqrt((m_t - l_t)^2 + (u_c - m_c)^2)`` to
    ``d + sqrt((u_t - m_t)^2 + (m_c - l_c)^2)``, with ``d = m_t - m_c``.
    """
    d = treatment.mean - control.mean
    return (
        d - math.hypot(treatment.mean - treatment.lower, control.upper - control.mean),
        d + math.hypot(treatment.upper - treatment.mean, control.mean - control.lower),
    )


def _mover_ratio(
    treatment: ArmLimits, control: ArmLimits
) -> Optional[Tuple[float, float]]:
    """The MOVER-R interval of ``m_t / m_c`` for a control whose mean is above
    0, or None when the control's limits do not keep its mean away from 0.

    The lower bound ``L <= m_t / m_c`` solves
    ``(m_t - L m_c)^2 = (m_t - l_t)^2 + L^2 (u_c - m_c)^2`` and the upper bound
    ``U >= m_t / m_c`` solves ``(U m_c - m_t)^2 = (u_t - m_t)^2 + U^2 (m_c - l_c)^2``.
    Each is the root of a quadratic whose leading coefficient is
    ``u_c (2 m_c - u_c)`` (``l_c (2 m_c - l_c)`` for the upper bound); when
    ``l_c <= 0`` or ``u_c >= 2 m_c`` one of them is not above 0, the set of
    ratios the data allow is not an interval, and the answer is None.

    The discriminants are written as sums of non-negative terms (the same
    numbers as ``b^2 - a c``), and each root is taken in the form that does
    not subtract two nearly equal numbers.
    """
    m_t, l_t, u_t = treatment
    m_c, l_c, u_c = control
    if not (l_c > 0.0 and u_c < 2.0 * m_c):
        return None
    a_low = u_c * (2.0 * m_c - u_c)
    a_high = l_c * (2.0 * m_c - l_c)
    b = m_t * m_c
    root_low = math.sqrt((m_t - l_t) ** 2 * a_low + m_t * m_t * (u_c - m_c) ** 2)
    root_high = math.sqrt((u_t - m_t) ** 2 * a_high + m_t * m_t * (m_c - l_c) ** 2)
    if b > 0.0:
        low = l_t * (2.0 * m_t - l_t) / (b + root_low)
    else:
        low = (b - root_low) / a_low
    if b < 0.0:
        high = u_t * (2.0 * m_t - u_t) / (b - root_high)
    else:
        high = (b + root_high) / a_high
    return low, high


def relative_change_interval(
    treatment: ArmLimits, control: ArmLimits
) -> Optional[Tuple[float, float]]:
    """The interval of ``100 * (m_t - m_c) / |m_c|``, the relative change in
    percent, from each arm's limits; None when it has no finite bounds.

    For a control whose mean is above 0 it is ``100 * (ratio - 1)`` over the
    MOVER-R interval of ``m_t / m_c``.  For one below 0 both arms are negated
    (their ratio is unchanged and the control's mean is then above 0) and the
    change is ``100 * (1 - ratio)``, so the interval is reflected:
    ``[100 * (1 - U), 100 * (1 - L)]``.

    None when the control's mean is 0, and when the control's limits, so
    oriented, do not keep its mean away from 0 (the lower limit at or below 0,
    or the upper limit at or above twice the mean): the change is then
    unbounded at this level, as for a control with 2 conversions of 200 users
    at 0.95.  Neither of these is unexpected, and neither is logged.  A
    limit or a bound that is not a finite number is unexpected: it is logged
    as a warning, and gives None.
    """
    numbers = (*treatment, *control)
    if not all(math.isfinite(value) for value in numbers):
        logger.warning(
            "The relative change's interval was not computed: an arm's limits "
            "are not finite"
        )
        return None
    if control.mean == 0.0:
        return None
    if control.mean > 0.0:
        ratio = _mover_ratio(treatment, control)
        if ratio is None:
            return None
        low, high = ratio
        interval = (100.0 * (low - 1.0), 100.0 * (high - 1.0))
    else:
        ratio = _mover_ratio(
            ArmLimits(-treatment.mean, -treatment.upper, -treatment.lower),
            ArmLimits(-control.mean, -control.upper, -control.lower),
        )
        if ratio is None:
            return None
        low, high = ratio
        interval = (100.0 * (1.0 - high), 100.0 * (1.0 - low))
    if not all(math.isfinite(value) for value in interval):
        logger.warning(
            "The relative change's interval was not computed: a bound is not finite"
        )
        return None
    return interval


def _impact(
    difference: Optional[float],
    interval: Optional[Tuple[float, float]],
    sample_size: int,
) -> Dict[str, Any]:
    """The incremental impact: the difference, and its interval, times the
    treatment's own assigned users.  Each is None when what it scales is."""
    return {
        "incremental_impact": (
            difference * sample_size if difference is not None else None
        ),
        "incremental_impact_ci": (
            (interval[0] * sample_size, interval[1] * sample_size)
            if interval is not None
            else None
        ),
    }


#: The keys every variant of ``binomial_metric_result`` and
#: ``per_user_mean_metric_result`` carries for the difference from the
#: control, all None on the control (#1496).
DIFFERENCE_FIELDS = (
    "absolute_difference",
    "absolute_difference_ci",
    "relative_improvement_pct_ci",
    "incremental_impact",
    "incremental_impact_ci",
)


def _no_difference() -> Dict[str, Any]:
    return dict.fromkeys(DIFFERENCE_FIELDS)


def _proportion_difference(
    n_t: int, x_t: int, n_c: int, x_c: int, z: float
) -> Dict[str, Any]:
    """A proportion treatment's difference from the control and its intervals.

    ``absolute_difference`` is ``x_t / n_t - x_c / n_c`` from the counts; its
    interval is Newcombe's, from each arm's Wilson limits at ``z``, and the
    relative change's interval is the MOVER-R interval on the same limits.
    Everything is None when either arm has no users.
    """
    if n_t <= 0 or n_c <= 0:
        return _no_difference()
    treatment = wilson_limits(x_t, n_t, z)
    control = wilson_limits(x_c, n_c, z)
    difference = x_t / n_t - x_c / n_c
    interval = newcombe_interval(treatment, control)
    return {
        "absolute_difference": difference,
        "absolute_difference_ci": interval,
        "relative_improvement_pct_ci": relative_change_interval(treatment, control),
        **_impact(difference, interval, n_t),
    }


def binomial_variant_results(
    variants: Sequence[BinomialCounts],
    confidence_level: float = 0.95,
) -> List[Dict[str, Any]]:
    """Per-variant results on the legacy percent scale.

    These are the ``variant_results`` of ``/results``' ``metrics_results``:
    conversion rate and a normal-approximation interval at
    ``confidence_level`` in percent, and, for each treatment, Fisher's exact
    p-value against the control, its significance at
    ``1 - confidence_level`` and the relative improvement in percent.  The
    interval and the significance decision use the same level.  The first
    variant flagged ``is_control`` is the control.

    Raises:
        ValueError: when no variant is the control, or the level is not
            strictly between 0 and 1.
    """
    z = two_sided_z(confidence_level)
    alpha = 1.0 - confidence_level
    control = next((v for v, _, _ in variants if v.is_control), None)
    if control is None:
        raise ValueError("No variant is the control")
    control_id = str(control.id)

    assignments: Dict[str, int] = {}
    conversions: Dict[str, int] = {}
    for variant, n, n_converted in variants:
        assignments[str(variant.id)] = n
        conversions[str(variant.id)] = n_converted

    rates: Dict[str, float] = {}
    for variant_id, n in assignments.items():
        if n > 0:
            rate = (conversions[variant_id] / n) * 100
        else:
            rate = 0
        rates[variant_id] = rate

    results: List[Dict[str, Any]] = []
    control_conversions = conversions[control_id]
    control_non_conversions = assignments[control_id] - control_conversions

    for variant, _, _ in variants:
        variant_id = str(variant.id)
        # The literal 0s below stay ints, as they were in /results' output.
        p_value: Optional[float]
        relative_improvement: Optional[float]

        if variant.is_control:
            p_value = 1.0
            is_significant = False
            relative_improvement = 0
        else:
            variant_conversions = conversions[variant_id]
            variant_non_conversions = assignments[variant_id] - variant_conversions

            contingency_table = [
                [variant_conversions, variant_non_conversions],
                [control_conversions, control_non_conversions],
            ]

            try:
                odds_ratio, p_value = stats.fisher_exact(contingency_table)
                is_significant = p_value < alpha

                if rates[control_id] > 0:
                    relative_improvement = (
                        (rates[variant_id] - rates[control_id]) / rates[control_id]
                    ) * 100
                else:
                    relative_improvement = float("inf") if rates[variant_id] > 0 else 0
            except Exception as e:
                logger.error(f"Error calculating statistics: {e!s}")
                p_value = None
                is_significant = False
                relative_improvement = None

        # Confidence interval using the normal approximation
        if assignments[variant_id] > 0:
            proportion = rates[variant_id] / 100  # Convert percentage to proportion
            se = math.sqrt((proportion * (1 - proportion)) / assignments[variant_id])

            ci_lower = max(0, (proportion - z * se) * 100)
            ci_upper = min(100, (proportion + z * se) * 100)
        else:
            ci_lower = 0
            ci_upper = 0

        results.append(
            {
                "variant_id": variant_id,
                "variant_name": variant.name,
                "is_control": variant.is_control,
                "sample_size": assignments[variant_id],
                "conversions": conversions[variant_id],
                "conversion_rate": rates[variant_id],
                "confidence_interval": [ci_lower, ci_upper],
                "p_value": p_value,
                "is_significant": is_significant,
                "relative_improvement": relative_improvement,
            }
        )

    return results


def binomial_metric_result(
    variants: Sequence[BinomialCounts],
    alpha: float,
    correction_method: str,
    *,
    metric: Any,
) -> Dict[str, Any]:
    """One proportion metric's result from per-variant counts.

    Args:
        variants: ``(variant, n, n_converted)`` per variant, in report order.
            ``variant`` has ``id``, ``name`` and ``is_control``
            (``BinomialVariant`` or an ORM ``Variant``); exactly one should be
            the control.
        alpha: significance level for ``is_significant`` and observed power;
            the interval is at ``1 - alpha``.
        correction_method: ``none``, ``bonferroni`` or ``benjamini_hochberg``,
            applied across the treatments of this metric.
        metric: the metric's identity: ``id``, ``name``, ``metric_type`` and
            ``is_primary``, which the result carries, and optionally
            ``lower_is_better`` (absent or None reads as False).  When it is
            true the winner is the significant treatment with the LOWEST
            rate below the control's; otherwise the highest above it.
            ``relative_improvement_pct`` stays the signed change either way.

    Each treatment also carries its difference from the control (#1496), at
    the level ``1 - alpha`` and not adjusted for multiple comparisons:
    ``absolute_difference`` (``x_t / n_t - x_c / n_c``, from the counts) with
    Newcombe's interval from each arm's Wilson limits,
    ``relative_improvement_pct_ci`` (MOVER-R on the same limits; None when
    the control's limits do not keep its rate away from 0), and
    ``incremental_impact`` and ``incremental_impact_ci``, the difference and
    its interval times the treatment's ``sample_size``.  All five are None on
    the control, and on a treatment when either arm has no users.

    Returns:
        A dict in the shape of ``schemas.results.MetricResult``, exactly what
        ``/results`` returns in ``metrics`` for the same counts.

    Raises:
        ValueError: when no variant is the control, or ``alpha`` is not
            strictly between 0 and 1.
    """
    variant_results = binomial_variant_results(variants, 1.0 - alpha)
    z = two_sided_z(1.0 - alpha)

    control = next((v for v in variant_results if v["is_control"]), None)
    control_rate = (control["conversion_rate"] / 100.0) if control else 0.0
    treatments = [v for v in variant_results if not v["is_control"]]
    adjusted = adjusted_p_values(
        [v.get("p_value") for v in treatments], correction_method
    )
    adjusted_by_id = {v["variant_id"]: a for v, a in zip(treatments, adjusted)}

    results: List[Dict[str, Any]] = []
    for v in variant_results:
        rate = v["conversion_rate"] / 100.0
        n = v["sample_size"]
        std_dev = math.sqrt(rate * (1 - rate)) if n > 0 else None
        ci_low, ci_high = v["confidence_interval"]
        entry: Dict[str, Any] = {
            "variant_id": v["variant_id"],
            "variant_name": v["variant_name"],
            "is_control": v["is_control"],
            "sample_size": n,
            "conversions": v["conversions"],
            "mean": rate,
            "std_dev": std_dev,
            "confidence_interval": (ci_low / 100.0, ci_high / 100.0),
            "p_value": None,
            "adjusted_p_value": None,
            "is_significant": False,
            "effect_size": None,
            "effect_size_label": None,
            "relative_improvement_pct": None,
            "power": None,
            "statistical_test_used": None,
            **_no_difference(),
        }
        if not v["is_control"]:
            if control is not None:
                entry.update(
                    _proportion_difference(
                        n,
                        v["conversions"],
                        control["sample_size"],
                        control["conversions"],
                        z,
                    )
                )
            p_value = v.get("p_value")
            adj = adjusted_by_id.get(v["variant_id"])
            decisive = adj if adj is not None else p_value
            improvement = v.get("relative_improvement")
            if improvement is not None and not math.isfinite(improvement):
                improvement = None
            effect = None
            if n > 0 and control and control["sample_size"] > 0:
                # Cohen's h for two proportions
                effect = 2 * math.asin(math.sqrt(rate)) - 2 * math.asin(
                    math.sqrt(control_rate)
                )
            power = None
            if effect is not None and control and control["sample_size"] > 0 and n > 0:
                try:
                    from statsmodels.stats.power import NormalIndPower

                    power = float(
                        NormalIndPower().power(
                            effect_size=abs(effect),
                            nobs1=n,
                            alpha=alpha,
                            ratio=control["sample_size"] / n,
                        )
                    )
                except Exception:
                    power = None
            entry.update(
                p_value=p_value,
                adjusted_p_value=adj,
                is_significant=bool(decisive is not None and decisive < alpha),
                effect_size=effect,
                effect_size_label=effect_size_label(abs(effect))
                if effect is not None
                else None,
                relative_improvement_pct=improvement,
                power=power,
                statistical_test_used="fisher_exact" if p_value is not None else None,
            )
        results.append(entry)

    lower_is_better = metric_lower_is_better(metric)
    if lower_is_better:
        winners = [
            v
            for v in results
            if not v["is_control"]
            and v["is_significant"]
            and (v["relative_improvement_pct"] or 0) < 0
        ]
        winner = min(winners, key=lambda v: v["mean"]) if winners else None
    else:
        winners = [
            v
            for v in results
            if not v["is_control"]
            and v["is_significant"]
            and (v["relative_improvement_pct"] or 0) > 0
        ]
        winner = max(winners, key=lambda v: v["mean"]) if winners else None
    metric_type = (
        metric.metric_type.value
        if hasattr(metric.metric_type, "value")
        else str(metric.metric_type)
    )
    return {
        "metric_id": str(metric.id),
        "metric_name": metric.name,
        "metric_type": metric_type,
        "is_primary": bool(metric.is_primary),
        "lower_is_better": lower_is_better,
        "variants": results,
        "has_significant_result": any(v["is_significant"] for v in results),
        "winning_variant_id": winner["variant_id"] if winner else None,
    }


# ---------------------------------------------------------------------------
# Mean metrics from centred sums
# ---------------------------------------------------------------------------

#: ``(variant, n, sum_d, sum_d2)``: the units in the variant and, over their
#: values ``y``, ``sum(y - k)`` and ``sum((y - k) ** 2)`` for the grand mean
#: ``k`` the caller centred on.
MeanSums = Tuple[Any, int, float, float]

FEWER_THAN_2_UNITS = "fewer_than_2_units"
NO_VARIATION = "Not computed: no variation"


class SufficientStatsRefused(ValueError):
    """Sums that no sample could produce (a negative variance, a non-finite
    sum): the metric is refused, never computed.  ``code`` is
    ``result_invalid``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "result_invalid"


class SufficientStatsNotComputed(ValueError):
    """Valid sums, but too few units to compute the metric from.

    ``code`` is ``fewer_than_2_units``; ``message`` is the text a reader sees.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _MeanArm(NamedTuple):
    variant: Any
    n: int
    c: float  # the centred mean, sum_d / n
    var: float  # the sample variance, ddof 1


def _mean_arm(variant: Any, n: Any, sum_d: Any, sum_d2: Any) -> _MeanArm:
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise SufficientStatsRefused("n is not a non-negative integer")
    sum_d = float(sum_d)
    sum_d2 = float(sum_d2)
    if not (math.isfinite(sum_d) and math.isfinite(sum_d2)):
        raise SufficientStatsRefused("a sum is not finite")
    if n < 2:
        raise SufficientStatsNotComputed(
            FEWER_THAN_2_UNITS, "Not computed: fewer than 2 units"
        )
    c = sum_d / n
    ss = sum_d2 - sum_d * sum_d / n
    # Rounding can leave a tiny negative where the true value is 0: an arm
    # whose values are all equal (no buyer at all, say) centred on a k it does
    # not share.  A negative within 1e-9 of sum_d2 is read as 0.  The bound is
    # relative only, so it holds at every scale: no sample gives a sum of
    # squares further below 0 than that, and one is refused.
    if not math.isfinite(ss) or ss < -1e-9 * abs(sum_d2):
        raise SufficientStatsRefused("the sums give a negative variance")
    var = max(ss, 0.0) / (n - 1)
    return _MeanArm(variant, n, c, var)


def _mean_comparison(
    arm: _MeanArm, control: _MeanArm, k: float, alpha: float
) -> Dict[str, Any]:
    """One treatment against the control: p-value, effect size, power and
    relative improvement, or None for each that cannot be computed."""
    from statsmodels.stats.power import NormalIndPower

    diff = arm.c - control.c
    control_mean = k + control.c
    improvement = diff / abs(control_mean) * 100 if control_mean != 0 else None
    if arm.var == 0 and control.var == 0:
        return {
            "p_value": None,
            "effect_size": None,
            "power": None,
            "relative_improvement_pct": improvement,
            "note": NO_VARIATION,
        }
    # The centred means go in, never k + c: at a large k the reconstructed
    # means round away most of the difference.
    p_value = float(
        stats.ttest_ind_from_stats(
            arm.c,
            math.sqrt(arm.var),
            arm.n,
            control.c,
            math.sqrt(control.var),
            control.n,
            equal_var=False,
        ).pvalue
    )
    effect = diff / math.sqrt((control.var + arm.var) / 2.0)
    power: Optional[float]
    try:
        power = float(
            NormalIndPower().power(
                effect_size=abs(effect),
                nobs1=arm.n,
                alpha=alpha,
                ratio=control.n / arm.n,
            )
        )
    except Exception:
        power = None
    return {
        "p_value": p_value,
        "effect_size": effect,
        "power": power,
        "relative_improvement_pct": improvement,
        "note": None,
    }


def mean_metric_result(
    k: float,
    variants: Sequence[MeanSums],
    alpha: float,
    correction_method: str,
    *,
    metric: Any,
) -> Dict[str, Any]:
    """One mean metric's result from per-variant sums centred on ``k``.

    The sums are centred so that a large offset (order values in the
    thousands, say) does not cancel the variance away: ``k`` is the grand mean
    over every unit, ``sum_d = sum(y - k)`` and ``sum_d2 = sum((y - k) ** 2)``.
    Everything compared across variants uses the centred means ``sum_d / n``,
    in which ``k`` cancels; ``k`` is added back only to the reported ``mean``
    and interval.

    Per variant: ``mean = k + sum_d / n``; the sample variance (ddof 1) and
    its square root as ``std_dev``; and ``mean +/- t(1 - alpha/2, n - 1) *
    sqrt(var / n)`` as the interval.  Per treatment, against the control:
    Welch's t-test p-value, Cohen's d as ``AnalysisService.cohens_d`` defines
    it, observed power at ``alpha`` (``NormalIndPower``, the convention of the
    proportion results) and the relative improvement in percent, which is None
    when the control's mean is 0.  When neither the treatment nor the control
    varies, the p-value, effect size and power are None and the treatment's
    ``note`` says so.

    Args:
        k: the grand mean the sums are centred on.
        variants: ``(variant, n, sum_d, sum_d2)`` per variant, in report
            order; ``variant`` has ``id``, ``name`` and ``is_control``, and
            exactly one should be the control.
        alpha: significance level for the interval, ``is_significant`` and
            observed power.
        correction_method: ``none``, ``bonferroni`` or ``benjamini_hochberg``,
            applied across the treatments of this metric.
        metric: the metric's identity: ``id``, ``name``, ``metric_type`` and
            ``is_primary``, which the result carries, and optionally
            ``lower_is_better`` (absent or None reads as False).  When it is
            true the winner is the significant treatment with the LOWEST mean
            below the control's; otherwise the highest above it.

    Returns:
        A dict in the shape of ``schemas.results.MetricResult``.  Each variant
        also carries ``note``: None, or why a comparison was not computed.

    Raises:
        SufficientStatsRefused: a count or sum is malformed or not finite, or
            the sums give a negative variance beyond rounding (a rounding
            residue is read as 0).
        SufficientStatsNotComputed: a variant has fewer than 2 units.
        ValueError: when no variant is the control.
    """
    k = float(k)
    if not math.isfinite(k):
        raise SufficientStatsRefused("k is not finite")
    arms = [_mean_arm(*row) for row in variants]
    control = next((a for a in arms if a.variant.is_control), None)
    if control is None:
        raise ValueError("No variant is the control")

    comparisons = [
        _mean_comparison(arm, control, k, alpha)
        for arm in arms
        if not arm.variant.is_control
    ]
    adjusted = adjusted_p_values(
        [comparison["p_value"] for comparison in comparisons], correction_method
    )
    pending = list(zip(comparisons, adjusted))

    results: List[Dict[str, Any]] = []
    for arm in arms:
        mean = k + arm.c
        half_width = float(stats.t.ppf(1 - alpha / 2, arm.n - 1)) * math.sqrt(
            arm.var / arm.n
        )
        entry: Dict[str, Any] = {
            "variant_id": str(arm.variant.id),
            "variant_name": arm.variant.name,
            "is_control": arm.variant.is_control,
            "sample_size": arm.n,
            "conversions": None,
            "mean": mean,
            "std_dev": math.sqrt(arm.var),
            "confidence_interval": (mean - half_width, mean + half_width),
            "p_value": None,
            "adjusted_p_value": None,
            "is_significant": False,
            "effect_size": None,
            "effect_size_label": None,
            "relative_improvement_pct": None,
            "power": None,
            "statistical_test_used": None,
            "note": None,
        }
        if not arm.variant.is_control:
            # Treatments come out of `pending` in the order they went in.
            comparison, adj = pending.pop(0)
            p_value = comparison["p_value"]
            decisive = adj if adj is not None else p_value
            effect = comparison["effect_size"]
            entry.update(
                p_value=p_value,
                adjusted_p_value=adj,
                is_significant=bool(decisive is not None and decisive < alpha),
                effect_size=effect,
                effect_size_label=effect_size_label(abs(effect))
                if effect is not None
                else None,
                relative_improvement_pct=comparison["relative_improvement_pct"],
                power=comparison["power"],
                statistical_test_used="welch_t_test" if p_value is not None else None,
                note=comparison["note"],
            )
        results.append(entry)

    # A winner is significant and on the good side of the control (above it,
    # or below it when lower is better), compared on the centred means (a
    # relative improvement is None when the control's mean is 0).
    lower_is_better = metric_lower_is_better(metric)
    if lower_is_better:
        winners = [
            (entry, arm)
            for entry, arm in zip(results, arms)
            if not entry["is_control"] and entry["is_significant"] and arm.c < control.c
        ]
        winner = min(winners, key=lambda pair: pair[1].c)[0] if winners else None
    else:
        winners = [
            (entry, arm)
            for entry, arm in zip(results, arms)
            if not entry["is_control"] and entry["is_significant"] and arm.c > control.c
        ]
        winner = max(winners, key=lambda pair: pair[1].c)[0] if winners else None
    metric_type = (
        metric.metric_type.value
        if hasattr(metric.metric_type, "value")
        else str(metric.metric_type)
    )
    return {
        "metric_id": str(metric.id),
        "metric_name": metric.name,
        "metric_type": metric_type,
        "is_primary": bool(metric.is_primary),
        "lower_is_better": lower_is_better,
        "variants": results,
        "has_significant_result": any(v["is_significant"] for v in results),
        "winning_variant_id": winner["variant_id"] if winner else None,
    }


def welch_difference_intervals(
    variants: Sequence[MeanSums], alpha: float
) -> Dict[str, Tuple[Optional[float], Optional[Tuple[float, float]]]]:
    """Each treatment's difference from the control, and its interval.

    ``variants`` are ``(variant, n, sum_d, sum_d2)`` as ``mean_metric_result``
    takes them, centred on one common ``k`` (it cancels in a difference).  For
    each treatment, keyed by ``str(variant.id)``: the difference of the means,
    treatment minus control, and its Welch-Satterthwaite interval at
    ``1 - alpha``::

        d +/- t(1 - alpha / 2, df) * se,    se ** 2 = v_t / n_t + v_c / n_c,
        df = se ** 4 / ((v_t / n_t) ** 2 / (n_t - 1) + (v_c / n_c) ** 2 / (n_c - 1))

    where ``v`` is an arm's sample variance (ddof 1).  It is the interval
    ``scipy.stats.ttest_ind(t, c, equal_var=False).confidence_interval`` gives,
    and it is not adjusted for multiple comparisons.  ``df`` is computed from
    each arm's share of ``se ** 2``, so it neither overflows nor divides by 0
    while ``se`` is above 0.

    The difference and the interval are both None when either arm has fewer
    than 2 units.  The interval alone is None when neither arm varies: ``se``
    is 0, and ``df`` would be 0 / 0.

    Raises:
        SufficientStatsRefused: a count or sum is malformed or not finite, or
            the sums give a negative variance beyond rounding.
        ValueError: when no variant is the control.
    """
    return {
        variant_id: (welch.difference, welch.interval)
        for variant_id, welch in _welch_differences(variants, alpha).items()
    }


class _WelchDifference(NamedTuple):
    """One treatment's Welch difference from the control, with the pieces the
    relative change's interval is built from."""

    difference: Optional[float]
    interval: Optional[Tuple[float, float]]
    #: ``t(1 - alpha / 2, df)`` at the Welch-Satterthwaite df; None with
    #: ``interval``.
    critical: Optional[float]
    #: Each arm's standard error of its mean, ``sqrt(var / n)``.
    se_treatment: Optional[float]
    se_control: Optional[float]


def _welch_differences(
    variants: Sequence[MeanSums], alpha: float
) -> Dict[str, _WelchDifference]:
    """``welch_difference_intervals``' numbers, keyed the same way, with the
    critical value and the arms' standard errors it used."""
    arms: List[Tuple[Any, Optional[_MeanArm]]] = []
    for row in variants:
        try:
            arms.append((row[0], _mean_arm(*row)))
        except SufficientStatsNotComputed:
            arms.append((row[0], None))
    controls = [arm for variant, arm in arms if variant.is_control]
    if not controls:
        raise ValueError("No variant is the control")
    control = controls[0]

    differences: Dict[str, _WelchDifference] = {}
    for variant, arm in arms:
        if variant.is_control:
            continue
        if arm is None or control is None:
            differences[str(variant.id)] = _WelchDifference(
                None, None, None, None, None
            )
            continue
        diff = arm.c - control.c
        share_t = arm.var / arm.n
        share_c = control.var / control.n
        se2 = share_t + share_c
        if not se2 > 0.0:
            differences[str(variant.id)] = _WelchDifference(
                diff, None, None, None, None
            )
            continue
        df = 1.0 / (
            (share_t / se2) ** 2 / (arm.n - 1) + (share_c / se2) ** 2 / (control.n - 1)
        )
        critical = float(stats.t.ppf(1 - alpha / 2, df))
        half_width = critical * math.sqrt(se2)
        differences[str(variant.id)] = _WelchDifference(
            diff,
            (diff - half_width, diff + half_width),
            critical,
            math.sqrt(share_t),
            math.sqrt(share_c),
        )
    return differences


def _mean_relative_change_interval(
    welch: _WelchDifference, treatment_mean: float, control_mean: float
) -> Optional[Tuple[float, float]]:
    """A mean treatment's relative change interval: MOVER-R on each arm's
    ``mean +/- c * se`` with Welch's ``c``, which is Fieller's interval at
    that critical value.  None when the difference's interval is None."""
    if (
        welch.interval is None
        or welch.critical is None
        or welch.se_treatment is None
        or welch.se_control is None
    ):
        return None
    margin_t = welch.critical * welch.se_treatment
    margin_c = welch.critical * welch.se_control
    return relative_change_interval(
        ArmLimits(treatment_mean, treatment_mean - margin_t, treatment_mean + margin_t),
        ArmLimits(control_mean, control_mean - margin_c, control_mean + margin_c),
    )


# ---------------------------------------------------------------------------
# A metric's mean per assigned user (#439)
# ---------------------------------------------------------------------------

#: The metric types ``/results`` analyses as a mean per assigned user
#: (``per_user_mean_metric_result``): every type but ``conversion``, which is
#: analysed as a proportion (``binomial_metric_result``).  A new
#: ``MetricType`` member fails ``test_value_metrics_per_user.py`` until it is
#: classified here or as a proportion.
MEAN_METRIC_TYPES = ("revenue", "count", "duration", "custom")

#: The mean types whose per-user value is the number of the metric's events
#: the user sent, whatever their ``value`` (``AnalysisService.event_counts``).
#: Every other mean type adds up the events' ``value``
#: (``AnalysisService.value_totals``).
EVENT_COUNT_METRIC_TYPES = ("count",)

#: ``(variant, n, n_converted, totals)``: the users assigned to the variant,
#: how many of them converted, and the value of each assigned user who has
#: one: their events' value total, or for a count metric their number of
#: events.  Every other assigned user's value is 0.
PerUserTotals = Tuple[Any, int, int, Sequence[float]]


def metric_type_name(metric: Any) -> str:
    """``metric.metric_type`` as text, whether it is an enum or a string."""
    metric_type = metric.metric_type
    return metric_type.value if hasattr(metric_type, "value") else str(metric_type)


def _descriptive_mean_entry(
    variant: Any,
    n: int,
    n_converted: int,
    mean: float,
    arm: Optional[_MeanArm],
    alpha: float,
) -> Dict[str, Any]:
    """An arm's mean, spread and interval, with no comparison."""
    std_dev: Optional[float] = None
    interval = (mean, mean)
    if arm is not None:
        std_dev = math.sqrt(arm.var)
        half_width = float(stats.t.ppf(1 - alpha / 2, arm.n - 1)) * math.sqrt(
            arm.var / arm.n
        )
        interval = (mean - half_width, mean + half_width)
    return {
        "variant_id": str(variant.id),
        "variant_name": variant.name,
        "is_control": variant.is_control,
        "sample_size": n,
        "conversions": n_converted,
        "mean": mean,
        "std_dev": std_dev,
        "confidence_interval": interval,
        "p_value": None,
        "adjusted_p_value": None,
        "is_significant": False,
        "effect_size": None,
        "effect_size_label": None,
        "relative_improvement_pct": None,
        "power": None,
        "statistical_test_used": None,
        **_no_difference(),
    }


def per_user_mean_metric_result(
    variants: Sequence[PerUserTotals],
    alpha: float,
    correction_method: str,
    *,
    metric: Any,
) -> Dict[str, Any]:
    """One metric's result as its mean per assigned user (``MEAN_METRIC_TYPES``).

    Every user assigned to a variant is one unit, and their value is what the
    caller read for them (the total of their counted values, or for a count
    metric their number of events): 0 for a user with none.  The metric's type
    is only echoed here; nothing below reads it.  The arms are compared
    with ``mean_metric_result`` on sums centred on the grand mean ``k``, the
    total of every value over every assigned user.

    Per variant: ``sample_size`` is ``n`` (at least the number of totals);
    ``conversions`` is ``n_converted``, the users who converted, as for a
    proportion metric; ``mean`` is the total of the values over ``n``;
    ``std_dev`` is over the ``n`` per-user values (ddof 1); and the interval
    is ``mean +/- t(1 - alpha / 2, n - 1) * std_dev / sqrt(n)``.  Per
    treatment: Welch's t-test against the control and the rest of
    ``mean_metric_result``'s comparison, the winner in the metric's direction,
    ``relative_improvement_pct`` computed on the means (None when the
    control's mean is 0), and ``absolute_difference`` with its interval from
    ``welch_difference_intervals``.  From the same Welch numbers (#1496):
    ``relative_improvement_pct_ci``, MOVER-R on each arm's ``mean +/- c * se``
    with ``c`` Welch's critical value (Fieller's interval; None when the
    control's ``mean +/- c * se`` reaches 0), and ``incremental_impact`` and
    ``incremental_impact_ci``, the difference and its interval times the
    treatment's ``sample_size``.  None of the intervals is adjusted for
    multiple comparisons.

    Nothing here raises for well-formed input:

    * an arm with fewer than 2 users has its mean (0.0 with none), no
      ``std_dev``, the interval ``(mean, mean)`` and no comparison; with a
      control of fewer than 2 users no treatment is compared;
    * when neither arm varies (every user's value is 0 before the first
      purchase) the p-value and the difference's interval are None;
    * sums ``mean_metric_result`` refuses leave every arm uncompared, and are
      logged.

    Returns:
        A dict in the shape of ``schemas.results.MetricResult``, with
        ``analysis`` ``"mean"``.

    Raises:
        SufficientStatsRefused: a count is not a non-negative integer, or a
            total is not finite.
        ValueError: when no variant is the control.
    """
    arms: List[Tuple[Any, int, int, List[float]]] = []
    for variant, n, n_converted, totals in variants:
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise SufficientStatsRefused("n is not a non-negative integer")
        values = [float(total) for total in totals]
        if not all(math.isfinite(value) for value in values):
            raise SufficientStatsRefused("a total is not finite")
        # A user whose value was read is one of the arm's users, even if the
        # count was read a moment before they were assigned.
        arms.append((variant, max(n, len(values)), n_converted, values))
    if not any(variant.is_control for variant, _, _, _ in arms):
        raise ValueError("No variant is the control")

    users = sum(n for _, n, _, _ in arms)
    k = (
        math.fsum(value for _, _, _, values in arms for value in values) / users
        if users
        else 0.0
    )
    sums: List[MeanSums] = []
    means: Dict[str, float] = {}
    for variant, n, _, values in arms:
        zeros = n - len(values)
        mean = math.fsum(values) / n if n else 0.0
        means[str(variant.id)] = mean
        sum_d = math.fsum([value - k for value in values] + [-k * zeros])
        # sum((y - k) ** 2) is the arm's own sum of squares plus
        # sum_d * sum_d / n.  Adding that term to the own sum of squares, in
        # the expression _mean_arm subtracts, gives the own sum back exactly
        # when it is 0: an arm whose users all have the same value (every arm
        # before its first purchase) has variance 0, not a rounding residue
        # of its distance from k.
        own = math.fsum(
            [(value - mean) ** 2 for value in values] + [mean * mean * zeros]
        )
        sums.append((variant, n, sum_d, own + sum_d * sum_d / n if n else 0.0))

    compared = [row for row in sums if row[1] >= 2]
    result: Optional[Dict[str, Any]] = None
    welch: Dict[str, _WelchDifference] = {}
    if any(row[0].is_control for row in compared):
        try:
            result = mean_metric_result(
                k, compared, alpha, correction_method, metric=metric
            )
            welch = _welch_differences(compared, alpha)
        except SufficientStatsRefused as exc:
            logger.warning(
                "Metric %s: the per-user sums were refused (%s); no variant is "
                "compared",
                getattr(metric, "id", None),
                exc,
            )
            result = None
    by_id = {v["variant_id"]: v for v in result["variants"]} if result else {}

    control_id = next(
        str(variant.id) for variant, _, _, _ in arms if variant.is_control
    )
    entries: List[Dict[str, Any]] = []
    for (variant, n, n_converted, _), row in zip(arms, sums):
        variant_id = str(variant.id)
        mean = means[variant_id]
        computed = by_id.get(variant_id)
        if computed is None:
            try:
                arm = _mean_arm(*row) if n >= 2 else None
            except SufficientStatsRefused:
                arm = None
            entries.append(
                _descriptive_mean_entry(variant, n, n_converted, mean, arm, alpha)
            )
            continue
        entry = dict(computed)
        entry.pop("note", None)
        # The mean is the total over n itself, so an arm with no value
        # reads 0.0 exactly; k + sum_d / n can be a rounding residue away.
        shift = mean - entry["mean"]
        low, high = entry["confidence_interval"]
        entry.update(
            conversions=n_converted,
            mean=mean,
            confidence_interval=(low + shift, high + shift),
            **_no_difference(),
        )
        if not variant.is_control:
            pieces = welch.get(
                variant_id, _WelchDifference(None, None, None, None, None)
            )
            difference, interval = pieces.difference, pieces.interval
            control_mean = means[control_id]
            relative = (
                difference / abs(control_mean) * 100
                if difference is not None and control_mean != 0
                else None
            )
            entry.update(
                absolute_difference=difference,
                absolute_difference_ci=interval,
                relative_improvement_pct=relative,
                relative_improvement_pct_ci=(
                    _mean_relative_change_interval(pieces, mean, control_mean)
                    if relative is not None
                    else None
                ),
                **_impact(difference, interval, n),
            )
        entries.append(entry)

    return {
        "metric_id": str(metric.id),
        "metric_name": metric.name,
        "metric_type": metric_type_name(metric),
        "is_primary": bool(metric.is_primary),
        "lower_is_better": metric_lower_is_better(metric),
        "analysis": "mean",
        "variants": entries,
        "has_significant_result": any(v["is_significant"] for v in entries),
        "winning_variant_id": result["winning_variant_id"] if result else None,
    }


# ---------------------------------------------------------------------------
# CUPED from per-arm sums (#217)
# ---------------------------------------------------------------------------

#: ``(variant, n, sum_y, sum_x, sum_y2, sum_x2, sum_xy)``: the units in an arm
#: and, over their outcome ``y`` and covariate ``x``, the five sums.
CupedSums = Tuple[Any, int, float, float, float, float, float]

#: Reason a treatment row carries when neither arm's outcome varies.
NO_VARIATION_REASON = "no_variation"

#: Reason a treatment row carries when the covariate leaves no residual
#: variation in either arm: the adjusted standard error would be 0 (or a
#: rounding residue), and a p-value from it would claim certainty the data
#: cannot give.
NO_RESIDUAL_VARIATION_REASON = "no_residual_variation"


class _CupedArm(NamedTuple):
    variant: Any
    n: int
    sum_y: float
    sum_x: float
    syy: float  # within-arm sum of squares of y about the arm's mean
    sxx: float
    sxy: float


def _cuped_arm(
    variant: Any,
    n: Any,
    sum_y: Any,
    sum_x: Any,
    sum_y2: Any,
    sum_x2: Any,
    sum_xy: Any,
) -> _CupedArm:
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise SufficientStatsRefused("n is not a non-negative integer")
    sums = [float(v) for v in (sum_y, sum_x, sum_y2, sum_x2, sum_xy)]
    if not all(math.isfinite(v) for v in sums):
        raise SufficientStatsRefused("a sum is not finite")
    sum_y, sum_x, sum_y2, sum_x2, sum_xy = sums
    if n == 0:
        if any(sums):
            raise SufficientStatsRefused("an arm with no units has non-zero sums")
        return _CupedArm(variant, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    syy = sum_y2 - sum_y * sum_y / n
    sxx = sum_x2 - sum_x * sum_x / n
    sxy = sum_xy - sum_x * sum_y / n
    # Rounding can leave a tiny negative where the true value is 0; a clearly
    # negative sum of squares no sample could produce.
    for value, total in ((syy, sum_y2), (sxx, sum_x2)):
        if value < -1e-9 * max(1.0, abs(total)):
            raise SufficientStatsRefused("the sums give a negative variance")
    syy = max(syy, 0.0)
    sxx = max(sxx, 0.0)
    if sxy * sxy > syy * sxx * (1 + 1e-9) + 1e-9:
        raise SufficientStatsRefused("the sums give a correlation above 1")
    return _CupedArm(variant, n, sum_y, sum_x, syy, sxx, sxy)


def cuped_metric_result(
    arms: Sequence[CupedSums],
    confidence_level: float,
    correction_method: str,
    *,
    metric: Any,
    adjust: bool = True,
) -> List[Dict[str, Any]]:
    """One metric's CUPED comparisons, every treatment against the control.

    The estimate is the regression ``y ~ C(arm) + x`` fitted by ordinary least
    squares, computed from sums alone, so a caller with the sums from
    somewhere else (a warehouse) gets the same numbers:

    * ``theta`` is the pooled within-arm slope ``sum_k Sxy_k / sum_k Sxx_k``
      over **every** arm, the ``x`` coefficient of that regression; 0 when
      ``adjust`` is false or when ``x`` does not vary within any arm (for a
      0/1 covariate, coverage of exactly 0 or 100%);
    * each arm's adjusted mean is ``ybar_k - theta * (xbar_k - xbar)``, centred
      on the grand mean ``xbar`` so it stays on the outcome's scale;
    * its variance is ``(Syy_k - 2 theta Sxy_k + theta^2 Sxx_k) / (n_k - 1)
      / n_k``, the residual variance of the arm over its size;
    * the effect is the treatment's adjusted mean minus the control's, with a
      two-sided z-test and an interval at ``two_sided_z(confidence_level)``;
    * ``variance_reduction_pct`` is ``100 * (1 - Var(adjusted effect) /
      Var(unadjusted effect))``.  It can be negative: the pooled slope is
      fitted over every arm, and an arm whose own slope differs can end up
      with a larger variance than without adjustment.  For the same reason,
      adding an arm changes the other arms' adjusted effects;
    * ``corrected_p_value`` applies ``correction_method`` across the
      treatments of this metric (``adjusted_p_values``).

    A treatment with fewer than 2 units gets ``unavailable_reason``
    ``fewer_than_2_units`` and no numbers.  It is still part of the pooled
    fit, where it contributes nothing: an arm of one unit has no within-arm
    variation.  A treatment whose outcome, and the control's, does not vary
    at all gets ``no_variation``; one where the covariate leaves no residual
    variation in either arm (the adjusted standard error would be 0) gets
    ``no_residual_variation``.  Neither carries a p-value.

    Args:
        arms: ``(variant, n, sum_y, sum_x, sum_y2, sum_x2, sum_xy)`` per arm,
            in report order; ``variant`` has ``id``, ``name`` and
            ``is_control``, and exactly one should be the control.
        confidence_level: the level of the interval and of ``is_significant``.
        correction_method: ``none``, ``bonferroni`` or ``benjamini_hochberg``.
        metric: the metric's identity (``id`` and ``name``).
        adjust: false for the method ``none``: theta is 0 and the adjusted
            numbers are the unadjusted ones.

    Returns:
        One dict per treatment, in report order.

    Raises:
        SufficientStatsRefused: a count or sum is malformed or not finite, or
            the sums are ones no sample could produce.
        SufficientStatsNotComputed: the control has fewer than 2 units.
        UnknownCorrectionMethodError: ``correction_method`` is not one of
            ``CORRECTION_METHODS``.
        ValueError: when no variant is the control, or the level is not
            strictly between 0 and 1.
    """
    if correction_method not in CORRECTION_METHODS:
        raise UnknownCorrectionMethodError(correction_method)
    z_crit = two_sided_z(confidence_level)
    alpha = 1.0 - confidence_level
    parsed = [_cuped_arm(*row) for row in arms]
    control = next((a for a in parsed if a.variant.is_control), None)
    if control is None:
        raise ValueError("No variant is the control")
    if control.n < 2:
        raise SufficientStatsNotComputed(
            FEWER_THAN_2_UNITS, "Not computed: fewer than 2 units"
        )

    pooled_sxx = sum(a.sxx for a in parsed)
    pooled_sxy = sum(a.sxy for a in parsed)
    scale = max(1.0, sum(abs(a.sum_x) for a in parsed))
    theta = pooled_sxy / pooled_sxx if adjust and pooled_sxx > 1e-12 * scale else 0.0
    total_n = sum(a.n for a in parsed)
    x_grand = sum(a.sum_x for a in parsed) / total_n

    def _stats(arm: _CupedArm) -> Tuple[float, float, float, float]:
        """Adjusted mean, its variance, unadjusted mean, its variance."""
        y_mean = arm.sum_y / arm.n
        x_mean = arm.sum_x / arm.n
        adjusted_mean = y_mean - theta * (x_mean - x_grand)
        residual = arm.syy - 2.0 * theta * arm.sxy + theta * theta * arm.sxx
        residual = max(residual, 0.0)
        return (
            adjusted_mean,
            residual / (arm.n - 1) / arm.n,
            y_mean,
            arm.syy / (arm.n - 1) / arm.n,
        )

    c_adj, c_var, c_mean, c_uvar = _stats(control)
    rows: List[Dict[str, Any]] = []
    for arm in parsed:
        if arm.variant.is_control:
            continue
        row: Dict[str, Any] = {
            "metric_id": str(metric.id),
            "metric_name": metric.name,
            "variant_id": str(arm.variant.id),
            "variant_name": arm.variant.name,
            "control_variant_id": str(control.variant.id),
            "control_sample_size": control.n,
            "treatment_sample_size": arm.n,
            "adjusted_control_mean": None,
            "adjusted_treatment_mean": None,
            "adjusted_effect": None,
            "adjusted_se": None,
            "adjusted_p_value": None,
            "adjusted_ci_lower": None,
            "adjusted_ci_upper": None,
            "unadjusted_effect": None,
            "unadjusted_se": None,
            "corrected_p_value": None,
            "is_significant": False,
            "variance_reduction_pct": None,
            "theta": None,
            "unavailable_reason": None,
        }
        if arm.n < 2:
            row["unavailable_reason"] = FEWER_THAN_2_UNITS
            rows.append(row)
            continue
        t_adj, t_var, t_mean, t_uvar = _stats(arm)
        unadjusted_var = t_uvar + c_uvar
        if unadjusted_var <= 0.0:
            row["unavailable_reason"] = NO_VARIATION_REASON
            rows.append(row)
            continue
        adjusted_var = t_var + c_var
        # Relative, so the residue of an exact fit (1e-17, not 0) is caught too.
        if adjusted_var <= 1e-12 * unadjusted_var:
            row["unavailable_reason"] = NO_RESIDUAL_VARIATION_REASON
            rows.append(row)
            continue
        effect = t_adj - c_adj
        se = math.sqrt(adjusted_var)
        p_value = float(2.0 * stats.norm.sf(abs(effect) / se))
        row.update(
            adjusted_control_mean=c_adj,
            adjusted_treatment_mean=t_adj,
            adjusted_effect=effect,
            adjusted_se=se,
            adjusted_p_value=p_value,
            adjusted_ci_lower=effect - z_crit * se,
            adjusted_ci_upper=effect + z_crit * se,
            unadjusted_effect=t_mean - c_mean,
            unadjusted_se=math.sqrt(unadjusted_var),
            variance_reduction_pct=100.0 * (1.0 - adjusted_var / unadjusted_var),
            theta=theta,
        )
        rows.append(row)

    corrected = adjusted_p_values(
        [row["adjusted_p_value"] for row in rows], correction_method
    )
    for row, adj in zip(rows, corrected):
        row["corrected_p_value"] = adj
        decisive = adj if adj is not None else row["adjusted_p_value"]
        row["is_significant"] = bool(decisive is not None and decisive < alpha)
    return rows
