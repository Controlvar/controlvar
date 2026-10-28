"""The oracles a journey's expectations may name.

An oracle computes, independently of the product, a number a screen or an API
answer must show: ``ORACLES[name](**args)``. The loader refuses a journey naming
one that is not here. ``expect.number`` and ``expect.cells`` compare what a
screen shows with one, at the precision the screen shows it (``checks.agrees``);
``expect.computed`` compares a number in an api step's answer with one, within a
relative tolerance the step gives.

Each oracle works from the counts or the inputs a journey chose, with the
numerics pinned in ``tests/acceptance/requirements.txt`` (the versions the API
computes with), and imports nothing from the product.
"""

from __future__ import annotations

import math
import sys
from typing import Callable, Dict, Tuple

from scipy import integrate, stats
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import samplesize_proportions_2indep_onetail


def fisher_exact_p(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
) -> float:
    """The two-sided p-value of Fisher's exact test on a 2x2 table of users.

    Rows are the two variants, columns the users who converted and those who
    did not. This is the test ``GET /api/v1/results/{experiment_id}`` reports
    for a conversion metric (``statistical_test_used: fisher_exact``), on
    converting users, not events: a user who converted twice counts once.
    """
    table = [
        [treatment_converted, treatment_users - treatment_converted],
        [control_converted, control_users - control_converted],
    ]
    if min(min(row) for row in table) < 0:
        raise ValueError("more users converted than were assigned")
    return float(stats.fisher_exact(table, alternative="two-sided")[1])


def sample_ratio_p(
    control_users: int,
    treatment_users: int,
    control_allocation: float = 50,
    treatment_allocation: float = 50,
) -> float:
    """The p-value of the sample-ratio check on two variants' assignment counts.

    ``guides/user-guide.md`` ("Sample Ratio Check"): the results compare how
    many users each variant received with its traffic allocation by a
    chi-square test of the assignment counts, and a mismatch is p < 0.001.
    That is Pearson's chi-square goodness of fit, one degree of freedom for
    two variants, the expected counts the total split by the allocations
    (in any unit; they are normalised). ``GET /api/v1/results/{experiment_id}``
    reports it as ``srm.p_value``. scipy's ``chisquare`` computes it,
    independently of the product.
    """
    observed = [control_users, treatment_users]
    if min(observed) < 0 or sum(observed) <= 0:
        raise ValueError("the check needs at least one user and no negative count")
    shares = [float(control_allocation), float(treatment_allocation)]
    if min(shares) <= 0:
        raise ValueError("each variant needs a positive allocation")
    total = sum(observed)
    expected = [total * share / sum(shares) for share in shares]
    return float(stats.chisquare(observed, f_exp=expected).pvalue)


def _per_comparison_alpha(alpha: float, n_variants: int) -> float:
    """Bonferroni over the ``n_variants - 1`` comparisons with the control."""
    if n_variants < 2:
        raise ValueError("an experiment has at least two variants")
    return alpha / (n_variants - 1)


def sample_size_proportions(
    baseline_rate: float,
    minimum_detectable_effect: float,
    alpha: float,
    power: float,
    n_variants: int = 2,
) -> float:
    """Users per variant for a conversion rate, by the formula the guide states.

    ``statistics/power-analysis.md`` ("The Formula") gives the two-proportions
    z-test sample size of Fleiss (2003): the null's variance pooled at
    ``p_bar``, the alternative's unpooled, two-sided, with
    ``p2 = baseline + baseline * MDE`` and, for more than two variants, alpha
    divided by the number of comparisons with the control (Bonferroni).
    statsmodels' ``samplesize_proportions_2indep_onetail`` computes exactly
    that (``alternative="two-sided"`` halves alpha), independently of the
    product. A plan needs whole users, at least as many as the formula says,
    so the oracle rounds up.
    """
    corrected = _per_comparison_alpha(alpha, n_variants)
    p1 = baseline_rate
    p2 = baseline_rate + baseline_rate * minimum_detectable_effect
    if not 0 < p1 < p2 < 1:
        raise ValueError("the rates must satisfy 0 < baseline < treatment < 1")
    n = samplesize_proportions_2indep_onetail(
        diff=p2 - p1,
        prop2=p1,
        power=power,
        ratio=1,
        alpha=corrected,
        alternative="two-sided",
    )
    return float(math.ceil(float(n)))


def sample_size_means(
    baseline_rate: float,
    minimum_detectable_effect: float,
    baseline_std: float,
    alpha: float,
    power: float,
    n_variants: int = 2,
) -> float:
    """Users per variant for a mean (``metric_type: mean``), rounded up.

    The guide states no formula for a mean. This is the z-test on two means
    with a known standard deviation, two-sided by its near tail only, as the
    conversion formula is: statsmodels' ``NormalIndPower`` solved for the
    sample size with the standardised effect ``baseline_rate *
    minimum_detectable_effect / baseline_std``, one-sided (``larger``) at half
    the alpha. That is ``2 * (z_alpha + z_power)^2 / effect^2`` to within the
    solver's tolerance; a solve with both tails adds the far tail's sliver of
    power (392.443 users against 392.444 at an effect of 0.2). Below about two
    users per variant the solver finds nothing, and the oracle says so.
    """
    corrected = _per_comparison_alpha(alpha, n_variants)
    effect = baseline_rate * minimum_detectable_effect / baseline_std
    n = float(
        NormalIndPower().solve_power(
            effect_size=effect,
            nobs1=None,
            alpha=corrected / 2,
            power=power,
            ratio=1.0,
            alternative="larger",
        )
    )
    if not math.isfinite(n):
        raise ValueError("the solver found no sample size for these inputs")
    return float(math.ceil(n))


def absolute_effect(
    baseline_rate: float, minimum_detectable_effect: float, points: bool = False
) -> float:
    """The absolute effect a relative MDE means on a baseline.

    The guide: "An MDE of 10% on a 5% baseline means you want to detect a lift
    from 5% to 5.5% (absolute change = 0.5 percentage points)". With
    ``points`` it is in percentage points, as the dashboard shows it.
    """
    effect = baseline_rate * minimum_detectable_effect
    return effect * 100 if points else effect


def _number(name: str, value: object) -> float:
    """*value* as a finite float; a boolean or a text is refused, not coerced."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, not {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, not {value!r}")
    return float(value)


def _users_table(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
) -> None:
    """Refuse what two variants' users cannot be.

    Each count is a whole number of users, none negative; a variant has at
    least one user (with none, the pages say nothing bounds the effect, and
    these formulas do not apply); and no more of a variant's users convert
    than were assigned to it.
    """
    for arm, users, converted in (
        ("control", control_users, control_converted),
        ("treatment", treatment_users, treatment_converted),
    ):
        for name, count in ((f"{arm}_users", users), (f"{arm}_converted", converted)):
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError(f"{name} must be a whole number of users")
            if count < 0:
                raise ValueError(f"{name} cannot be negative")
        if users == 0:
            raise ValueError(f"the {arm} has no users")
        if converted > users:
            raise ValueError(f"more {arm} users converted than were assigned")


#: The largest finite double, where the sequential page caps ``lambda_ratio``.
LARGEST_DOUBLE = sys.float_info.max


def _msprt_terms(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    tau_squared: float,
) -> Tuple[float, float, float]:
    """The estimate, ``V`` and ``tau_squared`` the sequential page defines.

    ``api/sequential-testing.md`` ("Always-Valid Confidence Intervals
    (Confidence Sequences)"): the estimate is the observed difference in rates,
    treatment minus control; ``V`` is the Agresti-Caffo variance of that
    difference, one conversion and one non-conversion added to each arm,
    ``p~ = (conversions + 1) / (users + 2)`` and ``V = p~_c (1 - p~_c) / (n_c +
    2) + p~_t (1 - p~_t) / (n_t + 2)``; "the mSPRT uses the same V". ``V`` is
    never 0: ``p~`` lies strictly between 0 and 1. ``tau_squared`` is above 0
    and at most 1 ("Turning it on").
    """
    _users_table(control_users, control_converted, treatment_users, treatment_converted)
    tau_squared = _number("tau_squared", tau_squared)
    if not 0 < tau_squared <= 1:
        raise ValueError("tau_squared must be above 0 and at most 1")
    p_c = (control_converted + 1) / (control_users + 2)
    p_t = (treatment_converted + 1) / (treatment_users + 2)
    variance = p_c * (1 - p_c) / (control_users + 2) + p_t * (1 - p_t) / (
        treatment_users + 2
    )
    estimate = treatment_converted / treatment_users - control_converted / control_users
    return estimate, variance, tau_squared


def _sequential_alpha(alpha: float) -> float:
    """The sequential page's alpha: above 0 and at most 0.2 ("Turning it on")."""
    alpha = _number("alpha", alpha)
    if not 0 < alpha <= 0.2:
        raise ValueError("alpha must be above 0 and at most 0.2")
    return alpha


def _half_width(variance: float, tau_squared: float, alpha: float) -> float:
    """The page's half-width, before the interval is clipped to ``[-1, 1]``."""
    return math.sqrt(
        variance
        * (variance + tau_squared)
        / tau_squared
        * (2 * math.log(1 / alpha) + math.log((variance + tau_squared) / variance))
    )


def msprt_lambda_ratio(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    tau_squared: float = 0.001,
) -> float:
    """The mSPRT's ``lambda_ratio`` for two variants' users, at one look.

    The sequential page ("mSPRT") calls it the likelihood ratio that
    accumulates evidence for an effect, gives ``V`` and the mixing parameter
    ``tau_squared`` (default 0.001) but not its formula. The formula is the
    mixture SPRT of Johari, Koomen, Pekelis and Walsh, "Peeking at A/B Tests:
    Why it matters, and what to do about it" (KDD 2017), with a normal mixing
    distribution, which the page names as its source: the
    likelihood ratio of the estimate ``d ~ N(theta, V)`` against ``theta = 0``,
    averaged over ``theta ~ N(0, tau_squared)``, which is

        lambda = sqrt(V / (V + tau_squared))
                 * exp(tau_squared * d^2 / (2 V (V + tau_squared)))

    (their ``2 sigma^2 / n`` is the page's ``V``). It is computed in log space
    and capped at the largest finite double, as the page says the analysis
    does ("about 1.8 x 10^308"). The unit tests integrate the mixture itself
    and find this number, and check that ``lambda >= 1/alpha`` exactly when 0
    is outside ``msprt_confidence_sequence``, as the page promises.
    """
    estimate, variance, tau_squared = _msprt_terms(
        control_users,
        control_converted,
        treatment_users,
        treatment_converted,
        tau_squared,
    )
    log_ratio = 0.5 * math.log(variance / (variance + tau_squared)) + (
        tau_squared * estimate**2 / (2 * variance * (variance + tau_squared))
    )
    if log_ratio >= math.log(LARGEST_DOUBLE):
        return LARGEST_DOUBLE
    return math.exp(log_ratio)


def msprt_always_valid_p(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    tau_squared: float = 0.001,
) -> float:
    """The mSPRT's ``always_valid_p_value`` at one look: ``min(1, 1/lambda)``.

    Johari et al. (2017): the always-valid p-value is the running minimum of
    ``1/lambda`` over the looks, and never more than 1. With one
    look, which is what the journey takes (every user is sent before the
    one read), that is ``min(1, 1 / msprt_lambda_ratio(...))``. The sequential
    page's example agrees: ``lambda_ratio`` 5.73 with ``always_valid_p_value``
    0.174.
    """
    ratio = msprt_lambda_ratio(
        control_users,
        control_converted,
        treatment_users,
        treatment_converted,
        tau_squared,
    )
    return min(1.0, 1.0 / ratio)


def msprt_confidence_sequence(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    tau_squared: float = 0.001,
    alpha: float = 0.05,
    bound: str = "lower",
) -> float:
    """One bound of the mSPRT's ``confidence_sequence``, by the page's formula.

    ``api/sequential-testing.md`` ("Always-Valid Confidence Intervals
    (Confidence Sequences)"), on the difference in conversion rates, treatment
    minus control::

        estimate +/- sqrt( V (V + tau^2) / tau^2
                           * (2 ln(1/alpha) + ln((V + tau^2) / V)) )

    with the estimate and ``V`` of ``_msprt_terms``, clipped to ``[-1, 1]``
    ("A difference in rates cannot leave [-1, 1], so the interval is clipped to
    that range"). ``bound`` is ``lower`` or ``upper``. ``alpha`` is above 0 and
    at most 0.2 (default 0.05); the stopping boundary is ``1/alpha``.
    """
    estimate, variance, tau_squared = _msprt_terms(
        control_users,
        control_converted,
        treatment_users,
        treatment_converted,
        tau_squared,
    )
    half_width = _half_width(variance, tau_squared, _sequential_alpha(alpha))
    if bound == "lower":
        return max(-1.0, estimate - half_width)
    if bound == "upper":
        return min(1.0, estimate + half_width)
    raise ValueError(f"bound must be 'lower' or 'upper', not {bound!r}")


def bayes_prob_best(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    variant: str = "treatment",
) -> float:
    """The probability that *variant*'s conversion rate is the higher of two.

    ``api/bayesian.md`` ("Overview"): a Beta-Binomial model of each variant's
    converting users; the posterior is the prior (``bayesian_config``'s
    ``alpha`` and ``beta``, both above 0, default 1.0) updated with the
    conversions and the non-conversions, ``Beta(alpha + converted, beta +
    users - converted)``; "Probability to be best" is the share of Monte Carlo
    draws in which the variant has the highest rate. With two variants that
    share estimates ``P(rate_t > rate_c)`` (a tie has probability 0), which is
    computed here exactly rather than by drawing:

        P(rate_t > rate_c) = integral over x of f_t(x) F_c(x)
                           = integral over u in (0, 1) of F_c(F_t^-1(u))

    (the second form substitutes ``u = F_t(x)``: a bounded, smooth integrand
    however narrow the posteriors), by ``scipy.integrate.quad``. The unit tests
    check it against Evan Miller's closed form for whole-number parameters
    ("Formulas for Bayesian A/B Testing"). ``variant`` is ``treatment``
    or ``control``.
    """
    _users_table(control_users, control_converted, treatment_users, treatment_converted)
    prior_alpha = _number("prior_alpha", prior_alpha)
    prior_beta = _number("prior_beta", prior_beta)
    if prior_alpha <= 0 or prior_beta <= 0:
        raise ValueError("the prior's alpha and beta must be above 0")
    posteriors = {
        "control": stats.beta(
            prior_alpha + control_converted,
            prior_beta + control_users - control_converted,
        ),
        "treatment": stats.beta(
            prior_alpha + treatment_converted,
            prior_beta + treatment_users - treatment_converted,
        ),
    }
    if variant not in posteriors:
        raise ValueError(f"variant must be 'control' or 'treatment', not {variant!r}")
    best = posteriors[variant]
    other = posteriors["control" if variant == "treatment" else "treatment"]
    probability, _ = integrate.quad(
        lambda u: float(other.cdf(best.ppf(u))),
        0.0,
        1.0,
        epsabs=1e-12,
        epsrel=1e-12,
        limit=200,
    )
    return min(1.0, max(0.0, float(probability)))


ORACLES: Dict[str, Callable[..., float]] = {
    "fisher_exact_p": fisher_exact_p,
    "sample_ratio_p": sample_ratio_p,
    "sample_size_proportions": sample_size_proportions,
    "sample_size_means": sample_size_means,
    "absolute_effect": absolute_effect,
    "msprt_lambda_ratio": msprt_lambda_ratio,
    "msprt_always_valid_p": msprt_always_valid_p,
    "msprt_confidence_sequence": msprt_confidence_sequence,
    "bayes_prob_best": bayes_prob_best,
}
