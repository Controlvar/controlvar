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
from fractions import Fraction
from typing import Any, Callable, Dict, Tuple

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


#: The largest value, in size, a revenue metric adds up: a larger one "adds
#: nothing to its user's total" (``getting-started/limitations.md``). The
#: revenue oracles refuse a ramp that would send one rather than model that.
REVENUE_VALUE_LIMIT = 10**12


def _whole(name: str, value: object) -> int:
    """*value* as a whole number; a boolean, a decimal or a text is refused."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number, not {value!r}")
    return value


def _ramp_sums(converted: int, base: int, step: int, modulo: int) -> Tuple[int, int]:
    """The total and the total of squares of what a variant's buyers send.

    The traffic step's ``value`` (``docs_runner/model.py``, ``TrafficValue``):
    the ``i``-th converting user of a variant, ``i`` = 0, 1, 2, ... in each
    variant on its own, sends one event of ``base + step * (i mod modulo)``.
    Value ``j`` (``j`` < ``modulo``) is sent by ``converted // modulo`` users,
    one more when ``j < converted % modulo``. Whole numbers throughout, so
    both sums are exact integers, never a float sum.
    """
    cycles, rest = divmod(converted, modulo)
    total = 0
    squares = 0
    for j in range(min(modulo, converted)):
        senders = cycles + (1 if j < rest else 0)
        value = base + step * j
        total += senders * value
        squares += senders * value * value
    return total, squares


def _revenue_arms(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    base: int,
    step: int,
    modulo: int,
) -> Dict[str, Tuple[int, int, int]]:
    """Per variant, its users and the two sums of its users' values.

    A revenue metric's unit is the ASSIGNED user ("A revenue, count, duration
    or custom metric is analysed as its mean per assigned user ... for a
    revenue, duration or custom metric it is the total of those events'
    value; a user with none counts as 0", ``getting-started/concepts.md``): a buyer's value
    is the one event the traffic step sends for them, every other user's is 0,
    and 0 adds nothing to either sum. Refused: what ``_users_table`` refuses; a
    ``base`` or ``step`` that is not a whole number (the journeys send whole
    numbers, so the arithmetic below is exact); a ``modulo`` under 1; and a
    ramp that would send a value larger than ``REVENUE_VALUE_LIMIT`` in size.
    """
    _users_table(control_users, control_converted, treatment_users, treatment_converted)
    base = _whole("base", base)
    step = _whole("step", step)
    modulo = _whole("modulo", modulo)
    if modulo < 1:
        raise ValueError("modulo must be at least 1")
    sent = min(modulo, max(control_converted, treatment_converted))
    # The values are base + step * j for j < sent: the extremes are the ends.
    if sent and max(abs(base), abs(base + step * (sent - 1))) > REVENUE_VALUE_LIMIT:
        raise ValueError("the ramp sends a value larger than 10^12 in size")
    return {
        arm: (users, *_ramp_sums(converted, base, step, modulo))
        for arm, users, converted in (
            ("control", control_users, control_converted),
            ("treatment", treatment_users, treatment_converted),
        )
    }


def _mean(users: int, total: int) -> Fraction:
    return Fraction(total, users)


def _welch_terms(
    arms: Dict[str, Tuple[int, int, int]],
) -> Tuple[Fraction, Fraction, Fraction]:
    """Welch's difference, its squared standard error and its degrees of freedom.

    Exact rationals from the users and the two sums: each arm's sample variance
    ``s^2 = (sum y^2 - (sum y)^2 / n) / (n - 1)`` (Bessel's correction), the
    standard error ``se^2 = s_t^2 / n_t + s_c^2 / n_c`` and the
    Welch-Satterthwaite degrees of freedom ``se^4 / ((s_t^2 / n_t)^2 / (n_t -
    1) + (s_c^2 / n_c)^2 / (n_c - 1))``. Refused when an arm has fewer than 2
    users (no variance) or when neither arm's values vary (``se`` is 0).
    """
    shares = {}
    means = {}
    for arm, (users, total, squares) in arms.items():
        if users < 2:
            raise ValueError(f"the {arm} needs at least 2 users for a variance")
        variance = (squares - Fraction(total * total, users)) / (users - 1)
        shares[arm] = (variance / users, users)
        means[arm] = _mean(users, total)
    (share_t, n_t), (share_c, n_c) = shares["treatment"], shares["control"]
    se2 = share_t + share_c
    if se2 == 0:
        raise ValueError("neither variant's values vary: there is no test")
    df = se2 * se2 / (share_t * share_t / (n_t - 1) + share_c * share_c / (n_c - 1))
    return means["treatment"] - means["control"], se2, df


def revenue_mean(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    base: int,
    step: int,
    modulo: int,
    variant: str = "treatment",
) -> float:
    """A revenue metric's ``mean`` for *variant*: its value per assigned user.

    ``getting-started/concepts.md``: "A revenue, count, duration or custom
    metric is analysed as its mean per assigned user ... for a revenue,
    duration or custom metric it is the total of those events' value; a user
    with none counts as 0" (``guides/user-guide.md`` says the same of its
    results).
    So the total the variant's buyers send (``_ramp_sums``) over ALL its
    assigned users, buyers or not. Computed as an exact fraction of two
    integers and rounded once, so the API's number equals it exactly when its
    own arithmetic is exact. ``variant`` is ``treatment`` or ``control``; the
    arguments are the traffic step's counts and its ``value``'s ramp.
    """
    arms = _revenue_arms(
        control_users,
        control_converted,
        treatment_users,
        treatment_converted,
        base,
        step,
        modulo,
    )
    if variant not in arms:
        raise ValueError(f"variant must be 'control' or 'treatment', not {variant!r}")
    users, total, _ = arms[variant]
    return float(_mean(users, total))


def revenue_difference(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    base: int,
    step: int,
    modulo: int,
) -> float:
    """The treatment's ``absolute_difference``: its mean minus the control's.

    ``api/stability.md`` (#439): "each treatment carries
    ``absolute_difference`` and ``absolute_difference_ci``"; the OpenAPI
    document defines it as "this variant's mean minus the control's, per
    assigned user". The two means of ``revenue_mean``, subtracted exactly and
    rounded once.
    """
    arms = _revenue_arms(
        control_users,
        control_converted,
        treatment_users,
        treatment_converted,
        base,
        step,
        modulo,
    )
    return float(_mean(*arms["treatment"][:2]) - _mean(*arms["control"][:2]))


def revenue_welch_p(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    base: int,
    step: int,
    modulo: int,
) -> float:
    """The two-sided p-value of Welch's t-test on the users' values.

    ``getting-started/concepts.md``: a revenue metric's per-user values are
    "compared with Welch's t-test". The test is Welch's, B. L. Welch, "The
    generalization of 'Student's' problem when several different population
    variances are involved", Biometrika 34 (1947), with the degrees of freedom
    of F. E. Satterthwaite, "An approximate distribution of estimates of
    variance components", Biometrics Bulletin 2 (1946); the NIST/SEMATECH
    e-Handbook of Statistical Methods, 1.3.5.3 "Two-Sample t-Test for Equal
    Means", gives both::

        t  = (mean_t - mean_c) / se,   se^2 = s_t^2 / n_t + s_c^2 / n_c
        df = se^4 / ((s_t^2 / n_t)^2 / (n_t - 1) + (s_c^2 / n_c)^2 / (n_c - 1))
        p  = 2 * P(T_df > |t|)

    ``t``'s parts are exact rationals (``_welch_terms``); scipy's Student t
    gives the tail. Every assigned user is one value, 0 for a non-buyer. The
    unit tests check it against ``scipy.stats.ttest_ind(..., equal_var=False)``
    on the users' values written out one by one.
    """
    difference, se2, df = _welch_terms(
        _revenue_arms(
            control_users,
            control_converted,
            treatment_users,
            treatment_converted,
            base,
            step,
            modulo,
        )
    )
    t = float(difference) / math.sqrt(float(se2))
    return float(2 * stats.t.sf(abs(t), float(df)))


def revenue_welch_interval(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    base: int,
    step: int,
    modulo: int,
    confidence_level: float = 0.95,
    bound: str = "lower",
) -> float:
    """One bound of ``absolute_difference_ci``, Welch-Satterthwaite's interval.

    The OpenAPI document: "Two-sided Welch-Satterthwaite confidence interval of
    absolute_difference as (lower, upper), at the response's confidence_level
    and not adjusted for multiple comparisons". With ``se`` and ``df`` of
    ``revenue_welch_p`` (the same sources)::

        (mean_t - mean_c) +/- t(1 - (1 - confidence_level) / 2, df) * se

    ``confidence_level`` is above 0 and below 1 (default 0.95, an
    experiment's own default); ``bound`` is ``lower`` or ``upper``.
    """
    confidence_level = _number("confidence_level", confidence_level)
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be above 0 and below 1")
    if bound not in ("lower", "upper"):
        raise ValueError(f"bound must be 'lower' or 'upper', not {bound!r}")
    difference, se2, df = _welch_terms(
        _revenue_arms(
            control_users,
            control_converted,
            treatment_users,
            treatment_converted,
            base,
            step,
            modulo,
        )
    )
    quantile = float(stats.t.ppf(1 - (1 - confidence_level) / 2, float(df)))
    half_width = quantile * math.sqrt(float(se2))
    if bound == "lower":
        return float(difference) - half_width
    return float(difference) + half_width


# ---------------------------------------------------------------------------
# CUPED (``GET /api/v1/results/{experiment_id}/cuped``)
# ---------------------------------------------------------------------------
def _cuped_counts(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> Dict[str, Tuple[int, int, int, int]]:
    """Per variant: its users, converting users, returning users, and both.

    The traffic step chooses the users and decides who does what
    (``docs_runner/model.py``, ``TrafficHistory``): of a variant's ``users``,
    the first ``converted`` chosen convert after assignment (the outcome ``Y``
    is 1) and the first ``history`` chosen sent the metric's event before it
    (the covariate ``X`` is 1), so ``min(history, converted)`` users have both.
    Refused: what ``_users_table`` refuses; a ``history`` that is not a whole
    number from 0 to the variant's users; and a variant with fewer than 2 users
    (``api/cuped.md``: ``fewer_than_2_units``, a comparison with no numbers).
    """
    _users_table(control_users, control_converted, treatment_users, treatment_converted)
    counts = {}
    for arm, users, converted, history in (
        ("control", control_users, control_converted, control_history),
        ("treatment", treatment_users, treatment_converted, treatment_history),
    ):
        history = _whole(f"{arm}_history", history)
        if not 0 <= history <= users:
            raise ValueError(f"{arm}_history must be from 0 to the {arm}'s users")
        if users < 2:
            raise ValueError(f"the {arm} needs at least 2 users for a comparison")
        counts[arm] = (users, converted, history, min(history, converted))
    return counts


def _cuped_terms(*counts: int) -> Dict[str, Fraction]:
    """Every number of a CUPED comparison, as an exact fraction where it is one.

    ``api/cuped.md`` ("How It Works"), for a conversion metric, with ``Y`` and
    ``X`` of ``_cuped_counts``:

    * each arm's adjusted rate is ``mean(Y) - theta * (mean(X) - overall
      mean(X))``, where ``overall mean(X)`` is "the share of all assigned users
      with history" (with two variants, both arms' users);
    * ``theta`` is "the slope of Y on X, pooled within arms over every arm of
      the experiment: the x coefficient of the regression Y ~ arm + X". By the
      Frisch-Waugh-Lovell theorem that coefficient is the slope of Y on X
      after each is centred on its own arm's mean:
      ``sum over arms of S_xy / sum over arms of S_xx``, with ``S_xy =
      sum (X - mean X)(Y - mean Y)`` and ``S_xx = sum (X - mean X)^2`` within
      the arm; and ``theta`` is 0 "when no user (or every user) has history"
      (both ``S_xx`` are 0). This is the regression form of Deng, Xu, Kohavi
      and Walker, "Improving the sensitivity of online controlled experiments
      by utilizing pre-experiment data" (WSDM 2013), whose ``Y_cv = mean(Y) -
      theta (mean(X) - E[X])`` with ``theta = cov(Y, X) / var(X)`` the page
      cites; pooled within arms, it is the same ``theta`` for every arm;
    * the adjusted effect is the treatment's adjusted rate minus the control's,
      and "its variance is each arm's residual variance over its size": the
      residuals are ``Y - mean(Y) - theta (X - mean(X))`` within the arm, and
      their variance ``(S_yy - 2 theta S_xy + theta^2 S_xx) / (n - 1)``. The
      page does not name the divisor of a variance; this is the sample
      variance (Bessel's ``n - 1``), and the unadjusted effect's variance takes
      the same one (``S_yy / (n - 1)`` over the arm's size), so with ``theta``
      0 the adjusted numbers are the unadjusted ones, as the page says they
      are. The unit tests pin that the divisor ``n`` gives other numbers.

    Refused: a comparison the page lists with ``unavailable_reason`` and no
    numbers: ``no_variation`` (neither arm's ``Y`` varies) and
    ``no_residual_variation`` (history predicts every outcome, so the
    adjusted variance is 0).
    """
    arms = _cuped_counts(*counts)
    sums = {}
    for arm, (users, converted, history, both) in arms.items():
        sums[arm] = {
            "n": users,
            "y": Fraction(converted, users),
            "x": Fraction(history, users),
            "sxy": both - Fraction(history * converted, users),
            "sxx": history - Fraction(history * history, users),
            "syy": converted - Fraction(converted * converted, users),
        }
    sxx = sum(s["sxx"] for s in sums.values())
    theta = sum(s["sxy"] for s in sums.values()) / sxx if sxx else Fraction(0)
    returning = sum(history for _, _, history, _ in arms.values())
    everyone = sum(users for users, _, _, _ in arms.values())
    overall_x = Fraction(returning, everyone)

    def residual(s: Dict[str, Any]) -> Fraction:
        return s["syy"] - 2 * theta * s["sxy"] + theta * theta * s["sxx"]

    unadjusted_variance = sum(s["syy"] / ((s["n"] - 1) * s["n"]) for s in sums.values())
    if unadjusted_variance == 0:
        raise ValueError("neither variant's outcome varies: there is no comparison")
    adjusted_variance = sum(
        residual(s) / ((s["n"] - 1) * s["n"]) for s in sums.values()
    )
    if adjusted_variance == 0:
        raise ValueError("history predicts every outcome: there is no p-value")
    adjusted = {arm: s["y"] - theta * (s["x"] - overall_x) for arm, s in sums.items()}
    return {
        "theta": theta,
        "adjusted_control_mean": adjusted["control"],
        "adjusted_treatment_mean": adjusted["treatment"],
        "adjusted_effect": adjusted["treatment"] - adjusted["control"],
        "unadjusted_effect": sums["treatment"]["y"] - sums["control"]["y"],
        "adjusted_variance": adjusted_variance,
        "unadjusted_variance": unadjusted_variance,
        "coverage": Fraction(100 * returning, everyone),
    }


def cuped_theta(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``theta``: the slope of the outcome on history, pooled within arms.

    ``api/cuped.md`` ("How It Works"; see ``_cuped_terms``). The arguments are
    the traffic step's counts: each variant's users, converting users and
    returning users (its ``history``). 0 when no user, or every user, has
    history.
    """
    return float(
        _cuped_terms(
            control_users,
            control_converted,
            control_history,
            treatment_users,
            treatment_converted,
            treatment_history,
        )["theta"]
    )


def cuped_adjusted_mean(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
    variant: str = "treatment",
) -> float:
    """``adjusted_control_mean`` or ``adjusted_treatment_mean``.

    ``api/cuped.md``: ``mean(Y) - theta * (mean(X) - overall mean(X))`` for
    *variant* (``control`` or ``treatment``), on the conversion-rate scale.
    """
    terms = _cuped_terms(
        control_users,
        control_converted,
        control_history,
        treatment_users,
        treatment_converted,
        treatment_history,
    )
    if variant not in ("control", "treatment"):
        raise ValueError(f"variant must be 'control' or 'treatment', not {variant!r}")
    return float(terms[f"adjusted_{variant}_mean"])


def cuped_adjusted_effect(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``adjusted_effect``: the treatment's adjusted rate minus the control's.

    That is ``(mean Y_t - mean Y_c) - theta (mean X_t - mean X_c)``
    (``api/cuped.md``; ``_cuped_terms``).
    """
    return float(
        _cuped_terms(
            control_users,
            control_converted,
            control_history,
            treatment_users,
            treatment_converted,
            treatment_history,
        )["adjusted_effect"]
    )


def cuped_unadjusted_effect(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``unadjusted_effect``: the treatment's conversion rate minus the control's."""
    return float(
        _cuped_terms(
            control_users,
            control_converted,
            control_history,
            treatment_users,
            treatment_converted,
            treatment_history,
        )["unadjusted_effect"]
    )


def cuped_adjusted_se(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``adjusted_se``: the square root of the adjusted effect's variance.

    Each arm's residual variance over its size, added up (``_cuped_terms``).
    """
    return math.sqrt(
        _cuped_terms(
            control_users,
            control_converted,
            control_history,
            treatment_users,
            treatment_converted,
            treatment_history,
        )["adjusted_variance"]
    )


def cuped_unadjusted_se(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``unadjusted_se``: the same comparison's standard error without CUPED.

    Each arm's sample variance of ``Y`` over its size, added up: the residual
    variance of ``_cuped_terms`` at ``theta`` 0.
    """
    return math.sqrt(
        _cuped_terms(
            control_users,
            control_converted,
            control_history,
            treatment_users,
            treatment_converted,
            treatment_history,
        )["unadjusted_variance"]
    )


def cuped_adjusted_p(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``adjusted_p_value``: two-sided, of a z-test on the adjusted effect.

    The OpenAPI document: "Two-sided p-value of a z-test on the adjusted
    effect, before any multiple-comparison correction". ``z = adjusted_effect
    / adjusted_se`` and ``p = 2 P(Z > |z|)`` for a standard normal ``Z``.
    """
    terms = _cuped_terms(
        control_users,
        control_converted,
        control_history,
        treatment_users,
        treatment_converted,
        treatment_history,
    )
    z = float(terms["adjusted_effect"]) / math.sqrt(terms["adjusted_variance"])
    return float(2 * stats.norm.sf(abs(z)))


def cuped_adjusted_interval(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
    confidence_level: float = 0.95,
    bound: str = "lower",
) -> float:
    """``adjusted_ci_lower`` or ``adjusted_ci_upper``: the z-test's interval.

    ``api/cuped.md``: "The adjusted effect's interval at confidence_level"
    (the experiment's stored level). The interval that goes with the z-test
    of ``cuped_adjusted_p``: ``adjusted_effect +/- z(1 - (1 -
    confidence_level) / 2) * adjusted_se``, so 0 is outside it exactly when
    that p-value is under ``1 - confidence_level``. ``confidence_level`` is
    above 0 and below 1 (default 0.95); ``bound`` is ``lower`` or ``upper``.
    """
    confidence_level = _number("confidence_level", confidence_level)
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be above 0 and below 1")
    if bound not in ("lower", "upper"):
        raise ValueError(f"bound must be 'lower' or 'upper', not {bound!r}")
    terms = _cuped_terms(
        control_users,
        control_converted,
        control_history,
        treatment_users,
        treatment_converted,
        treatment_history,
    )
    quantile = float(stats.norm.ppf(1 - (1 - confidence_level) / 2))
    half_width = quantile * math.sqrt(terms["adjusted_variance"])
    effect = float(terms["adjusted_effect"])
    return effect - half_width if bound == "lower" else effect + half_width


def cuped_variance_reduction_pct(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``variance_reduction_pct``: ``100 * (1 - adjusted / unadjusted variance)``.

    ``api/cuped.md``'s Response Fields, of the effect's two variances
    (``_cuped_terms``). 0 with ``theta`` 0; below 0 when the slope pooled
    over the arms leaves this comparison noisier than no adjustment would
    (the page: it "can be negative").
    """
    terms = _cuped_terms(
        control_users,
        control_converted,
        control_history,
        treatment_users,
        treatment_converted,
        treatment_history,
    )
    return float(100 * (1 - terms["adjusted_variance"] / terms["unadjusted_variance"]))


def cuped_coverage_pct(
    control_users: int,
    control_converted: int,
    control_history: int,
    treatment_users: int,
    treatment_converted: int,
    treatment_history: int,
) -> float:
    """``covariate_coverage_pct``: the share of the comparison's users with history.

    ``api/cuped.md``'s Response Fields: the share of this comparison's users
    (control and treatment), from 0 to 100, with history; the returning users
    of both variants over all their users, times 100.
    """
    return float(
        _cuped_terms(
            control_users,
            control_converted,
            control_history,
            treatment_users,
            treatment_converted,
            treatment_history,
        )["coverage"]
    )


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
    "revenue_mean": revenue_mean,
    "revenue_difference": revenue_difference,
    "revenue_welch_p": revenue_welch_p,
    "revenue_welch_interval": revenue_welch_interval,
    "cuped_theta": cuped_theta,
    "cuped_adjusted_mean": cuped_adjusted_mean,
    "cuped_adjusted_effect": cuped_adjusted_effect,
    "cuped_unadjusted_effect": cuped_unadjusted_effect,
    "cuped_adjusted_se": cuped_adjusted_se,
    "cuped_unadjusted_se": cuped_unadjusted_se,
    "cuped_adjusted_p": cuped_adjusted_p,
    "cuped_adjusted_interval": cuped_adjusted_interval,
    "cuped_variance_reduction_pct": cuped_variance_reduction_pct,
    "cuped_coverage_pct": cuped_coverage_pct,
}
