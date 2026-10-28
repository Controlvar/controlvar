"""The sequential (mSPRT) and Bayesian oracles, and the results journey's steps that use them.

The results journey (``experiment-results.yaml``) ends by turning the
sequential and the Bayesian analysis on for two of its running experiments and
holding the API's numbers to oracles (``docs_runner/oracles.py``). What decides
whether those checks can fail is pure, and is tested here, in the unit job:

* each oracle is the formula its page states. ``msprt_confidence_sequence``
  gives every number ``api/sequential-testing.md`` prints; ``msprt_lambda_ratio``
  is the normal mixture of likelihood ratios it is defined as (integrated here);
  0 is outside the interval exactly when lambda reaches 1/alpha, as the page
  promises; and ``bayes_prob_best`` agrees with Evan Miller's closed form and
  with a second integral;
* each oracle refuses what no experiment or setting can be;
* each step's oracle is given the counts its experiment's traffic sends and
  the settings its PUT stores (or the page's defaults), the literals beside it
  follow from the oracles, and its tolerance holds the right answer while
  refusing the answer each defect would give;
* the second Bayesian read, near a probability of 0.93, fails a defect in how
  the draws are made (modelled with numpy) that the first, near 0.99, cannot see;
* the runner fails each step on an answer such a defect would give.

No network, no browser, no product code.
"""

from __future__ import annotations

import functools
import importlib
import math
import re
import sys
import types
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import numpy as np
import pytest
import yaml
from scipy import integrate, stats
from scipy.special import betaln

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import log, oracles

JOURNEY = RUNNER_ROOT / "journeys" / "experiment-results.yaml"

#: ``api/sequential-testing.md`` ("Turning it on"): the defaults.
DEFAULT_TAU_SQUARED = 0.001
DEFAULT_ALPHA = 0.05
#: ``api/bayesian.md``: the decision's probability threshold, and the default prior.
STOP_WINNER_PROBABILITY = 0.975
DEFAULT_PRIOR = (1.0, 1.0)
#: How many standard deviations of Monte Carlo error a tolerance must hold,
#: and how many of its own a defect's answer must lie outside it (the journey's
#: comment says "at least 20").
SIGMAS = 6.0
DEFECT_SIGMAS = 20.0
#: The API's Monte Carlo draws per arm (the journey's ``n_samples``), and how
#: many seeds the draw-level model below is run at.
BAYES_DRAWS = 100_000
BAYES_SEEDS = 20

SEQUENTIAL_READS = ("sequential-results", "floor-sequential", "floor-sequential-alpha")
#: The second Bayesian read sits near a probability of 0.93, where a defect in
#: how the draws are made moves the estimate far more than its Monte Carlo
#: spread; near 0.99 (the first) it barely moves it.
BAYESIAN_READS = ("floor-bayesian", "bayesian-results")
COUNT_ARGS = (
    "control_users",
    "control_converted",
    "treatment_users",
    "treatment_converted",
)


def _cs(
    control_users: int,
    control_converted: int,
    treatment_users: int,
    treatment_converted: int,
    tau_squared: float = DEFAULT_TAU_SQUARED,
    alpha: float = DEFAULT_ALPHA,
) -> Tuple[float, float]:
    """The confidence sequence's (lower, upper)."""
    counts = (control_users, control_converted, treatment_users, treatment_converted)
    return tuple(
        oracles.msprt_confidence_sequence(*counts, tau_squared, alpha, bound)
        for bound in ("lower", "upper")
    )


# ---------------------------------------------------------------------------
# The sequential page's numbers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "users, places, printed",
    [(1_000, 3, 0.041), (10_000, 4, 0.0136), (10_000_000, 5, 0.00055)],
)
def test_the_half_width_is_the_pages_at_a_ten_percent_rate(users, places, printed):
    """ "At a 10% conversion rate, alpha = 0.05 and the default tau^2 = 0.001,
    the half-width is about 0.041 at 1,000 users per arm, 0.0136 at 10,000 and
    0.00055 at 10 million." """
    lower, upper = _cs(users, users // 10, users, users // 10)
    assert lower == -upper
    assert round(upper, places) == printed


def test_ten_users_a_variant_at_half_is_clipped_to_the_whole_range():
    """ "with 10 users per arm at a 50% rate the formula gives about +/-3.27,
    and the response reports [-1, 1]" """
    _, variance, tau_squared = oracles._msprt_terms(10, 5, 10, 5, DEFAULT_TAU_SQUARED)
    assert round(oracles._half_width(variance, tau_squared, DEFAULT_ALPHA), 2) == 3.27
    assert _cs(10, 5, 10, 5) == (-1.0, 1.0)


def test_one_outcome_in_every_arm_still_gives_a_finite_interval():
    """ "0 of 500 against 0 of 500 gives about +/-0.0093 around 0 and cannot
    stop, while 0 of 500 against 500 of 500 can." """
    lower, upper = _cs(500, 0, 500, 0)
    assert lower == -upper and round(upper, 4) == 0.0093
    assert oracles.msprt_lambda_ratio(500, 0, 500, 0) < 1 / DEFAULT_ALPHA
    assert oracles.msprt_lambda_ratio(500, 0, 500, 500) >= 1 / DEFAULT_ALPHA
    assert _cs(500, 0, 500, 500)[0] > 0


@pytest.mark.parametrize(
    "treatment, printed", [(1_300, (0.0157, 0.0443)), (700, (-0.0427, -0.0173))]
)
def test_the_pages_two_stops_at_ten_thousand_users(treatment, printed):
    """ "10% against 13% and 10% against 7% at 10,000 users per arm both answer
    stop_for_effect ... The first has a confidence_sequence of 0.0157 to 0.0443,
    the second -0.0427 to -0.0173" """
    lower, upper = _cs(10_000, 1_000, 10_000, treatment)
    assert (round(lower, 4), round(upper, 4)) == printed
    assert oracles.msprt_lambda_ratio(10_000, 1_000, 10_000, treatment) >= 20


def test_an_overwhelming_difference_is_capped_at_the_largest_double():
    """ "capped at the largest finite double (about 1.8 x 10^308), so ... 10%
    against 11% with ten million users per arm, still reports a finite number
    and can stop" """
    counts = (10_000_000, 1_000_000, 10_000_000, 1_100_000)
    assert oracles.msprt_lambda_ratio(*counts) == sys.float_info.max
    assert oracles.msprt_always_valid_p(*counts) == 1 / sys.float_info.max
    assert _cs(*counts)[0] > 0


def test_the_always_valid_p_value_is_one_over_lambda_and_at_most_one():
    """The page's example pairs lambda_ratio 5.73 with always_valid_p_value 0.174:
    the reciprocals of the lambdas that print as 5.73 reach into what prints as
    0.174."""
    assert 1 / 5.735 < 0.1745 and 1 / 5.725 >= 0.1735
    strong = (400, 40, 400, 64)
    ratio = oracles.msprt_lambda_ratio(*strong)
    assert ratio > 1
    assert oracles.msprt_always_valid_p(*strong) == 1 / ratio
    assert oracles.msprt_lambda_ratio(500, 0, 500, 0) < 1
    assert oracles.msprt_always_valid_p(500, 0, 500, 0) == 1.0


def test_v_is_the_agresti_caffo_variance_and_the_estimate_the_raw_difference():
    estimate, variance, tau_squared = oracles._msprt_terms(400, 40, 400, 64, 0.001)
    p_c, p_t = Fraction(41, 402), Fraction(65, 402)
    by_hand = p_c * (1 - p_c) / 402 + p_t * (1 - p_t) / 402
    assert variance == pytest.approx(float(by_hand), rel=1e-15)
    assert estimate == pytest.approx(64 / 400 - 40 / 400, rel=1e-15)
    assert tau_squared == 0.001


def _mixture(estimate: float, variance: float, tau_squared: float) -> float:
    """The mSPRT's definition, integrated: the likelihood ratio of the estimate
    under theta against theta = 0, ``exp((2 d theta - theta^2) / (2 V))``, averaged
    over theta ~ N(0, tau^2). The integrand is a normal bump; it is integrated
    over 40 of its widths either side of its peak."""
    peak = tau_squared * estimate / (variance + tau_squared)
    width = math.sqrt(variance * tau_squared / (variance + tau_squared))

    def integrand(theta: float) -> float:
        return math.exp(
            (2 * estimate * theta - theta * theta) / (2 * variance)
        ) * stats.norm.pdf(theta, 0.0, math.sqrt(tau_squared))

    value, _ = integrate.quad(
        integrand,
        peak - 40 * width,
        peak + 40 * width,
        points=[peak],
        epsabs=0,
        epsrel=1e-12,
        limit=200,
    )
    return value


MIXTURE_CASES = [
    ((400, 40, 400, 64), 0.001),
    ((60, 6, 60, 24), 0.01),
    ((300, 30, 200, 40), 0.001),
    ((5_000, 500, 5_000, 560), 0.001),
    ((2_000, 300, 1_500, 200), 0.1),
    ((150, 15, 160, 40), 1.0),
    ((800, 80, 800, 79), 0.01),
]


@pytest.mark.parametrize("counts, tau_squared", MIXTURE_CASES)
def test_lambda_is_the_normal_mixture_it_is_defined_as(counts, tau_squared):
    estimate, variance, _ = oracles._msprt_terms(*counts, tau_squared)
    assert oracles.msprt_lambda_ratio(*counts, tau_squared) == pytest.approx(
        _mixture(estimate, variance, tau_squared), rel=1e-7
    )


GRID = [
    (users_c, converted_c, users_t, converted_t)
    for users_c, users_t in ((60, 60), (400, 400), (300, 200), (3_000, 3_200))
    for converted_c, converted_t in (
        (users_c // 10, users_t // 10),
        (users_c // 10, users_t // 7),
        (users_c // 10, users_t // 5),
        (users_c // 4, users_t // 10),
        (0, users_t // 50),
        (users_c, users_t - 1),
    )
]


@pytest.mark.parametrize("tau_squared", [0.001, 0.01, 0.1, 1.0])
@pytest.mark.parametrize("alpha", [0.2, 0.05, 0.001])
def test_zero_is_outside_the_interval_exactly_when_lambda_reaches_one_over_alpha(
    tau_squared, alpha
):
    """ "Because the interval and the stop decision share V, tau^2 and alpha,
    they cannot disagree: 0 is outside the interval exactly when can_stop is
    true." Both kinds are in the grid."""
    seen = set()
    for counts in GRID:
        ratio = oracles.msprt_lambda_ratio(*counts, tau_squared)
        if abs(math.log(ratio * alpha)) < 1e-9:
            continue
        lower, upper = _cs(*counts, tau_squared, alpha)
        stops = ratio >= 1 / alpha
        assert stops == (lower > 0 or upper < 0), counts
        seen.add(stops)
    assert seen == {True, False}


SEQUENTIAL_REFUSALS: List[Tuple[Callable[[], float], str]] = [
    (
        lambda: oracles.msprt_lambda_ratio(-1, 0, 10, 1),
        "control_users cannot be negative",
    ),
    (lambda: oracles.msprt_lambda_ratio(10, -1, 10, 1), "control_converted cannot be"),
    (lambda: oracles.msprt_lambda_ratio(10, 1, -10, 1), "treatment_users cannot be"),
    (lambda: oracles.msprt_lambda_ratio(10, 1, 10, -1), "treatment_converted cannot"),
    (lambda: oracles.msprt_lambda_ratio(0, 0, 10, 1), "the control has no users"),
    (lambda: oracles.msprt_always_valid_p(10, 1, 0, 0), "the treatment has no users"),
    (lambda: oracles.msprt_lambda_ratio(10, 11, 10, 1), "more control users converted"),
    (lambda: oracles.msprt_lambda_ratio(10, 1, 10, 11), "more treatment users conv"),
    (
        lambda: oracles.msprt_lambda_ratio(10.0, 1, 10, 1),
        "control_users must be a whole",
    ),
    (
        lambda: oracles.msprt_lambda_ratio(10, True, 10, 1),
        "control_converted must be a",
    ),
    (
        lambda: oracles.msprt_lambda_ratio(10, 1, 10, 1, 0.0),
        "tau_squared must be above",
    ),
    (
        lambda: oracles.msprt_lambda_ratio(10, 1, 10, 1, -0.01),
        "tau_squared must be above",
    ),
    (
        lambda: oracles.msprt_lambda_ratio(10, 1, 10, 1, 1.5),
        "tau_squared must be above",
    ),
    (
        lambda: oracles.msprt_lambda_ratio(10, 1, 10, 1, True),
        "tau_squared must be a number",
    ),
    (
        lambda: oracles.msprt_lambda_ratio(10, 1, 10, 1, "0.01"),
        "tau_squared must be a num",
    ),
    (
        lambda: oracles.msprt_lambda_ratio(10, 1, 10, 1, math.nan),
        "tau_squared must be fin",
    ),
    (
        lambda: oracles.msprt_confidence_sequence(10, 1, 10, 1, 0.001, 0.0, "lower"),
        "alpha must be above 0 and at most 0.2",
    ),
    (
        lambda: oracles.msprt_confidence_sequence(10, 1, 10, 1, 0.001, 0.25, "upper"),
        "alpha must be above 0 and at most 0.2",
    ),
    (
        lambda: oracles.msprt_confidence_sequence(10, 1, 10, 1, 0.001, True, "upper"),
        "alpha must be a number",
    ),
    (
        lambda: oracles.msprt_confidence_sequence(10, 1, 10, 1, 0.001, 0.05, "middle"),
        "bound must be 'lower' or 'upper'",
    ),
]


@pytest.mark.parametrize("call, message", SEQUENTIAL_REFUSALS)
def test_the_sequential_oracles_refuse_what_no_experiment_or_setting_can_be(
    call, message
):
    with pytest.raises(ValueError, match=re.escape(message)):
        call()


def test_the_page_bounds_tau_squared_at_one_and_alpha_at_a_fifth():
    """The boundary values themselves are accepted ("at most 1", "at most 0.2")."""
    assert oracles.msprt_lambda_ratio(10, 1, 10, 5, 1.0) > 0
    assert oracles.msprt_confidence_sequence(10, 1, 10, 5, 1.0, 0.2, "upper") <= 1


# ---------------------------------------------------------------------------
# The Bayesian oracle
# ---------------------------------------------------------------------------
def _miller(alpha_c: float, beta_c: float, alpha_t: float, beta_t: float) -> float:
    """P(rate_t > rate_c) by Evan Miller's closed form, for a whole-number alpha_t:
    the sum over i < alpha_t of B(alpha_c + i, beta_c + beta_t) /
    ((beta_t + i) B(1 + i, beta_t) B(alpha_c, beta_c))."""
    total = 0.0
    for i in range(int(alpha_t)):
        total += math.exp(
            betaln(alpha_c + i, beta_c + beta_t)
            - math.log(beta_t + i)
            - betaln(1 + i, beta_t)
            - betaln(alpha_c, beta_c)
        )
    return total


def _by_density(alpha_c: float, beta_c: float, alpha_t: float, beta_t: float) -> float:
    """P(rate_t > rate_c) as the integral of f_t(x) F_c(x) over x, the other form."""
    control = stats.beta(alpha_c, beta_c)
    treatment = stats.beta(alpha_t, beta_t)
    lower, upper = treatment.ppf(1e-15), treatment.isf(1e-15)
    value, _ = integrate.quad(
        lambda x: float(treatment.pdf(x) * control.cdf(x)),
        lower,
        upper,
        points=[treatment.mean()],
        epsabs=1e-13,
        limit=400,
    )
    return value


BAYES_CASES = [
    # (control users, converted, treatment users, converted, prior alpha, prior beta)
    (60, 6, 60, 24, 20, 180),
    (60, 6, 60, 24, 1, 1),
    (400, 40, 400, 64, 1, 1),
    (400, 40, 400, 64, 10, 90),
    (400, 40, 400, 64, 100, 900),
    (300, 30, 200, 40, 2, 3),
    (100, 30, 100, 20, 1, 1),
    (5_000, 600, 5_000, 640, 1, 1),
]


@pytest.mark.parametrize(
    "users_c, conv_c, users_t, conv_t, prior_a, prior_b", BAYES_CASES
)
def test_the_probability_to_be_best_is_millers_closed_form(
    users_c, conv_c, users_t, conv_t, prior_a, prior_b
):
    posterior = (
        prior_a + conv_c,
        prior_b + users_c - conv_c,
        prior_a + conv_t,
        prior_b + users_t - conv_t,
    )
    treatment = oracles.bayes_prob_best(
        users_c, conv_c, users_t, conv_t, float(prior_a), float(prior_b)
    )
    control = oracles.bayes_prob_best(
        users_c, conv_c, users_t, conv_t, float(prior_a), float(prior_b), "control"
    )
    assert treatment == pytest.approx(_miller(*posterior), rel=1e-9, abs=1e-12)
    assert treatment == pytest.approx(_by_density(*posterior), rel=1e-8, abs=1e-12)
    assert treatment + control == pytest.approx(1.0, abs=1e-10)


def test_two_identical_variants_are_even_and_swapping_them_swaps_the_answer():
    assert oracles.bayes_prob_best(200, 30, 200, 30) == pytest.approx(0.5, abs=1e-10)
    forward = oracles.bayes_prob_best(300, 30, 200, 40, 2.0, 3.0)
    backward = oracles.bayes_prob_best(200, 40, 300, 30, 2.0, 3.0, "control")
    assert forward == pytest.approx(backward, abs=1e-10)


def test_the_documented_default_prior_is_beta_one_one():
    assert oracles.bayes_prob_best(60, 6, 60, 24) == oracles.bayes_prob_best(
        60, 6, 60, 24, *DEFAULT_PRIOR
    )


BAYES_REFUSALS: List[Tuple[Callable[[], float], str]] = [
    (lambda: oracles.bayes_prob_best(-1, 0, 10, 1), "control_users cannot be negative"),
    (lambda: oracles.bayes_prob_best(10, 1, 10, -1), "treatment_converted cannot be"),
    (lambda: oracles.bayes_prob_best(10, 1, 0, 0), "the treatment has no users"),
    (lambda: oracles.bayes_prob_best(10, 11, 10, 1), "more control users converted"),
    (lambda: oracles.bayes_prob_best(10, 1, 10, 1, 0.0), "must be above 0"),
    (lambda: oracles.bayes_prob_best(10, 1, 10, 1, 1.0, -2.0), "must be above 0"),
    (
        lambda: oracles.bayes_prob_best(10, 1, 10, 1, True),
        "prior_alpha must be a number",
    ),
    (
        lambda: oracles.bayes_prob_best(10, 1, 10, 1, 1.0, "1"),
        "prior_beta must be a num",
    ),
    (
        lambda: oracles.bayes_prob_best(10, 1, 10, 1, 1.0, 1.0, "best"),
        "variant must be",
    ),
]


@pytest.mark.parametrize("call, message", BAYES_REFUSALS)
def test_the_bayesian_oracle_refuses_what_no_experiment_or_prior_can_be(call, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        call()


def test_the_registry_names_each_new_oracle():
    for name in (
        "msprt_lambda_ratio",
        "msprt_always_valid_p",
        "msprt_confidence_sequence",
        "bayes_prob_best",
    ):
        assert oracles.ORACLES[name] is getattr(oracles, name)


# ---------------------------------------------------------------------------
# The journey's steps
# ---------------------------------------------------------------------------
PLACEHOLDER = re.compile(r"\{\{([a-z0-9-]+)\}\}")


@functools.lru_cache(maxsize=1)
def _steps() -> List[Dict[str, Any]]:
    """The journey's steps, parsed once; no test changes them."""
    return yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))["steps"]


def _index(steps: List[Dict[str, Any]], step_id: str) -> int:
    return next(i for i, step in enumerate(steps) if step["id"] == step_id)


def _read_setup(steps: List[Dict[str, Any]], read_id: str) -> Dict[str, Any]:
    """What a read step reads, and what the journey did to it before."""
    read = steps[_index(steps, read_id)]
    path = read["do"]["api"]["path"]
    (experiment,) = PLACEHOLDER.findall(path)
    query = dict(
        part.split("=", 1) for part in path.partition("?")[2].split("&") if part
    )
    traffic = [
        (i, s)
        for i, s in enumerate(steps)
        if (s["do"].get("traffic") or {}).get("experiment") == experiment
    ]
    puts = [
        (i, s)
        for i, s in enumerate(steps)
        if (s["do"].get("api") or {}).get("method") == "PUT"
        and (s["do"]["api"]["path"] == f"/api/v1/experiments/{{{{{experiment}}}}}")
    ]
    return {
        "read": read,
        "at": _index(steps, read_id),
        "experiment": experiment,
        "query": query,
        "traffic": traffic,
        "puts": puts,
    }


def _turned_on(setup: Dict[str, Any], field: str) -> Dict[str, Any]:
    """The last PUT before the read that turns *field* on: its body."""
    bodies = [
        s["do"]["api"]["body"]
        for i, s in setup["puts"]
        if i < setup["at"] and s["do"]["api"]["body"].get(field) is True
    ]
    assert bodies, f"nothing turns {field} on before {setup['read']['id']}"
    return bodies[-1]


def _sent(setup: Dict[str, Any]) -> Dict[str, int]:
    ((_, traffic),) = setup["traffic"]
    variants = traffic["do"]["traffic"]["variants"]
    return {
        "control_users": variants["Control"]["assigned"],
        "control_converted": variants["Control"]["converted"],
        "treatment_users": variants["Treatment"]["assigned"],
        "treatment_converted": variants["Treatment"]["converted"],
    }


def _stored_alpha(setup: Dict[str, Any]) -> float:
    config = (
        _turned_on(setup, "sequential_testing_enabled").get("sequential_testing_config")
        or {}
    )
    return config.get("alpha", DEFAULT_ALPHA)


def _sequential_settings(setup: Dict[str, Any]) -> Tuple[float, float]:
    """tau_squared and alpha the read is computed at: the request's alpha, else
    the stored config's, else the page's defaults."""
    config = (
        _turned_on(setup, "sequential_testing_enabled").get("sequential_testing_config")
        or {}
    )
    tau_squared = config.get("tau_squared", DEFAULT_TAU_SQUARED)
    alpha = float(setup["query"].get("alpha", _stored_alpha(setup)))
    return tau_squared, alpha


@pytest.mark.parametrize("read_id", SEQUENTIAL_READS + BAYESIAN_READS)
def test_each_analysis_is_read_after_its_traffic_and_after_it_is_turned_on(read_id):
    steps = _steps()
    setup = _read_setup(steps, read_id)
    assert len(setup["traffic"]) == 1, "one population per experiment"
    field = (
        "bayesian_enabled"
        if read_id in BAYESIAN_READS
        else "sequential_testing_enabled"
    )
    route = "/bayesian" if read_id in BAYESIAN_READS else "/sequential"
    assert setup["read"]["do"]["api"]["path"].split("?")[0].endswith(route)
    turned_on = [i for i, s in setup["puts"] if s["do"]["api"]["body"].get(field)]
    assert turned_on and setup["traffic"][0][0] < turned_on[0] < setup["at"]
    for _, put in setup["puts"]:
        assert put["expect"]["status"] == 200


@pytest.mark.parametrize("read_id", SEQUENTIAL_READS + BAYESIAN_READS)
def test_each_oracle_is_given_the_counts_and_the_settings_the_journey_sends(read_id):
    steps = _steps()
    setup = _read_setup(steps, read_id)
    sent = _sent(setup)
    computed = setup["read"]["expect"]["computed"]
    assert computed, "no oracle is named: the step reads nothing"
    for path, check in computed.items():
        args = check["oracle"]["args"]
        assert {k: args[k] for k in COUNT_ARGS} == sent, path
        if read_id in BAYESIAN_READS:
            config = _turned_on(setup, "bayesian_enabled")["bayesian_config"]
            assert (args["prior_alpha"], args["prior_beta"]) == (
                config["alpha"],
                config["beta"],
            ), path
            assert config["prior_family"] == "beta"
        else:
            tau_squared, alpha = _sequential_settings(setup)
            assert args["tau_squared"] == tau_squared, path
            if check["oracle"]["name"] == "msprt_confidence_sequence":
                assert args["alpha"] == alpha, path
                assert args["bound"] == path.rsplit(".", 1)[1], path


def test_every_oracle_named_in_the_journey_gives_a_number():
    def calls(node: Any) -> List[Dict[str, Any]]:
        if isinstance(node, dict):
            found = [node] if node.keys() == {"name", "args"} else []
            return found + [c for value in node.values() for c in calls(value)]
        if isinstance(node, list):
            return [c for value in node for c in calls(value)]
        return []

    named = calls(_steps())
    assert {c["name"] for c in named} >= {
        "msprt_lambda_ratio",
        "msprt_always_valid_p",
        "msprt_confidence_sequence",
        "bayes_prob_best",
    }
    for call in named:
        assert math.isfinite(oracles.ORACLES[call["name"]](**call["args"])), call


@pytest.mark.parametrize("read_id", SEQUENTIAL_READS)
def test_the_sequential_literals_follow_from_the_oracle(read_id):
    """The boundary is 1/alpha; it stops exactly when lambda reaches it; a stop
    is strong_for_effect and stop_for_effect, anything else continue."""
    steps = _steps()
    setup = _read_setup(steps, read_id)
    tau_squared, alpha = _sequential_settings(setup)
    ratio = oracles.msprt_lambda_ratio(**_sent(setup), tau_squared=tau_squared)
    stops = ratio >= 1 / alpha
    expected = setup["read"]["expect"]["json"]
    assert expected["msprt_result.boundary"] == pytest.approx(1 / alpha, rel=1e-12)
    assert expected["msprt_result.can_stop"] is stops
    assert expected["recommended_action"] == (
        "stop_for_effect" if stops else "continue"
    )
    if "msprt_result.evidence_strength" in expected:
        assert (
            stops and expected["msprt_result.evidence_strength"] == "strong_for_effect"
        )
    lower, upper = _cs(**_sent(setup), tau_squared=tau_squared, alpha=alpha)
    assert stops == (lower > 0 or upper < 0)


def test_the_journey_reads_both_a_stop_and_a_continue():
    steps = _steps()
    stops = {
        _read_setup(steps, r)["read"]["expect"]["json"]["msprt_result.can_stop"]
        for r in SEQUENTIAL_READS
    }
    assert stops == {True, False}
    tau = {_sequential_settings(_read_setup(steps, r))[0] for r in SEQUENTIAL_READS}
    assert DEFAULT_TAU_SQUARED in tau and len(tau) > 1, "a default and a stored tau^2"


# What a defect in the sequential analysis would answer, per JSON path.
def _plain_variance(counts: Dict[str, int]) -> float:
    p_c = counts["control_converted"] / counts["control_users"]
    p_t = counts["treatment_converted"] / counts["treatment_users"]
    return (
        p_c * (1 - p_c) / counts["control_users"]
        + p_t * (1 - p_t) / counts["treatment_users"]
    )


def _lambda_from(estimate: float, variance: float, tau_squared: float) -> float:
    return math.exp(
        0.5 * math.log(variance / (variance + tau_squared))
        + tau_squared * estimate**2 / (2 * variance * (variance + tau_squared))
    )


def _defect_answers(
    counts: Dict[str, int], tau_squared: float, alpha: float, stored_alpha: float
) -> Dict[str, Dict[str, float]]:
    estimate, variance, _ = oracles._msprt_terms(**counts, tau_squared=tau_squared)
    plain = _plain_variance(counts)
    hw = oracles._half_width(variance, tau_squared, alpha)

    def answers(estimate, variance, tau_squared, half_width, ratio=None):
        ratio = (
            _lambda_from(estimate, variance, tau_squared) if ratio is None else ratio
        )
        return {
            "msprt_result.lambda_ratio": ratio,
            "msprt_result.always_valid_p_value": min(1.0, 1.0 / ratio),
            "confidence_sequence.lower": max(-1.0, estimate - half_width),
            "confidence_sequence.upper": min(1.0, estimate + half_width),
        }

    return {
        "the plain variance in place of Agresti-Caffo's": answers(
            estimate, plain, tau_squared, oracles._half_width(plain, tau_squared, alpha)
        ),
        "control minus treatment": answers(-estimate, variance, tau_squared, hw),
        "the default tau_squared in place of the stored one": answers(
            estimate,
            variance,
            DEFAULT_TAU_SQUARED,
            oracles._half_width(variance, DEFAULT_TAU_SQUARED, alpha),
        ),
        "the stored alpha in place of the request's": answers(
            estimate,
            variance,
            tau_squared,
            oracles._half_width(variance, tau_squared, stored_alpha),
        ),
        "the half-width without ln((V + tau^2) / V)": answers(
            estimate,
            variance,
            tau_squared,
            math.sqrt(
                variance
                * (variance + tau_squared)
                / tau_squared
                * 2
                * math.log(1 / alpha)
            ),
        ),
        "lambda's exponent without its 2": answers(
            estimate,
            variance,
            tau_squared,
            hw,
            math.exp(
                0.5 * math.log(variance / (variance + tau_squared))
                + tau_squared * estimate**2 / (variance * (variance + tau_squared))
            ),
        ),
    }


def _read_defects(read_id: str) -> Dict[str, Tuple[Dict[str, float], float]]:
    """Per defect: its answers at this read, and the largest relative move it
    makes in a number the read checks (0 when it changes none)."""
    setup = _read_setup(_steps(), read_id)
    tau_squared, alpha = _sequential_settings(setup)
    computed = setup["read"]["expect"]["computed"]
    found = {}
    for defect, answers in _defect_answers(
        _sent(setup), tau_squared, alpha, _stored_alpha(setup)
    ).items():
        moved = 0.0
        for path, check in computed.items():
            right = oracles.ORACLES[check["oracle"]["name"]](**check["oracle"]["args"])
            moved = max(moved, abs(answers[path] - right) / abs(right))
        found[defect] = (answers, moved)
    return found


def test_the_sequential_tolerance_refuses_every_defects_answer():
    """Each defect moves at least one checked number by far more than its
    ``rel`` (1e-9): at least 0.4% relative. A defect that gives the right
    answer on one read (the default tau_squared on the default experiment)
    is refused by another."""
    steps = _steps()
    for read_id in SEQUENTIAL_READS:
        for path, check in _read_setup(steps, read_id)["read"]["expect"][
            "computed"
        ].items():
            assert check["rel"] == 1e-9, (read_id, path)
    caught: Dict[str, float] = {}
    for read_id in SEQUENTIAL_READS:
        for defect, (_, moved) in _read_defects(read_id).items():
            assert moved < 1e-12 or moved > 0.004, (read_id, defect, moved)
            caught[defect] = max(caught.get(defect, 0.0), moved)
    assert len(caught) == 6
    assert min(caught.values()) > 0.004, caught


def _bayes_check(read_id: str) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
    steps = _steps()
    setup = _read_setup(steps, read_id)
    expected = setup["read"]["expect"]
    ((path, check),) = expected["computed"].items()
    assert path == "variant_results.1.probability_to_be_best"
    right = oracles.bayes_prob_best(**check["oracle"]["args"])
    return setup, check, right


def _probability(alpha_c, beta_c, alpha_t, beta_t) -> float:
    control, treatment = stats.beta(alpha_c, beta_c), stats.beta(alpha_t, beta_t)
    value, _ = integrate.quad(
        lambda u: float(control.cdf(treatment.ppf(u))), 0, 1, epsabs=1e-12, limit=200
    )
    return value


def _expected_loss(alpha_c, beta_c, alpha_t, beta_t) -> float:
    """E[max(rate_c - rate_t, 0)]: what choosing Treatment gives up when Control is
    better, the page's expected loss. For a Beta(a, b) rate X,
    E[(X - x)+] = a / (a + b) * P(Beta(a + 1, b) > x) - x * P(X > x)."""
    control = stats.beta(alpha_c, beta_c)
    shifted = stats.beta(alpha_c + 1, beta_c)
    mean = alpha_c / (alpha_c + beta_c)
    treatment = stats.beta(alpha_t, beta_t)

    def above(x: float) -> float:
        return mean * shifted.sf(x) - x * control.sf(x)

    value, _ = integrate.quad(
        lambda u: float(above(treatment.ppf(u))), 0, 1, epsabs=1e-14, limit=200
    )
    return value


def _posteriors(setup: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """(alpha_c, beta_c, alpha_t, beta_t): the PUT's prior plus the users sent."""
    config = _turned_on(setup, "bayesian_enabled")["bayesian_config"]
    a, b = config["alpha"], config["beta"]
    sent = _sent(setup)
    return (
        a + sent["control_converted"],
        b + sent["control_users"] - sent["control_converted"],
        a + sent["treatment_converted"],
        b + sent["treatment_users"] - sent["treatment_converted"],
    )


@pytest.mark.parametrize("read_id", BAYESIAN_READS)
def test_the_bayesian_literals_are_the_prior_plus_the_users_sent(read_id):
    setup, check, _ = _bayes_check(read_id)
    sent = _sent(setup)
    config = _turned_on(setup, "bayesian_enabled")["bayesian_config"]
    expected = setup["read"]["expect"]["json"]
    for index, arm in enumerate(("control", "treatment")):
        users, converted = sent[f"{arm}_users"], sent[f"{arm}_converted"]
        assert expected[f"variant_results.{index}.variant_key"] == arm.title()
        assert expected[f"variant_results.{index}.posterior.alpha"] == (
            config["alpha"] + converted
        )
        assert expected[f"variant_results.{index}.posterior.beta"] == (
            config["beta"] + users - converted
        )
    assert check["oracle"]["args"]["variant"] == "treatment"
    assert expected["is_enabled"] is True


@pytest.mark.parametrize("read_id", BAYESIAN_READS)
def test_the_bayesian_tolerance_holds_the_draws_and_refuses_each_defect(read_id):
    """The API's probability is the share of ``n_samples`` draws, so its standard
    deviation is sqrt(p (1 - p) / n_samples). ``rel`` holds 6 of them; each
    defect's answer lies at least 20 of its own outside the band."""
    setup, check, right = _bayes_check(read_id)
    n_samples = setup["read"]["expect"]["json"]["n_samples"]
    assert n_samples == 100_000
    band = check["rel"] * right
    spread = math.sqrt(right * (1 - right) / n_samples)
    assert band >= SIGMAS * spread, (band, spread)
    sent = _sent(setup)
    a, b = check["oracle"]["args"]["prior_alpha"], check["oracle"]["args"]["prior_beta"]
    c_conv, c_fail = (
        sent["control_converted"],
        sent["control_users"] - sent["control_converted"],
    )
    t_conv, t_fail = (
        sent["treatment_converted"],
        sent["treatment_users"] - sent["treatment_converted"],
    )
    defects = {
        "the prior ignored": _probability(
            1 + c_conv, 1 + c_fail, 1 + t_conv, 1 + t_fail
        ),
        "the prior's alpha and beta swapped": _probability(
            b + c_conv, a + c_fail, b + t_conv, a + t_fail
        ),
        "a posterior beta counting every user": _probability(
            a + c_conv,
            b + sent["control_users"],
            a + t_conv,
            b + sent["treatment_users"],
        ),
        "the control's probability": 1 - right,
        "the posterior means compared": 1.0,
    }
    for defect, answer in defects.items():
        own = math.sqrt(answer * (1 - answer) / n_samples)
        outside = abs(answer - right) - band
        assert outside >= DEFECT_SIGMAS * max(own, spread / 10), defect
    # The limit of the band: a normal approximation of the two posteriors.
    mean_c, mean_t = (
        (a + c_conv) / (a + b + sent["control_users"]),
        (a + t_conv) / (a + b + sent["treatment_users"]),
    )
    var_c = mean_c * (1 - mean_c) / (a + b + sent["control_users"] + 1)
    var_t = mean_t * (1 - mean_t) / (a + b + sent["treatment_users"] + 1)
    normal = float(stats.norm.cdf((mean_t - mean_c) / math.sqrt(var_c + var_t)))
    assert abs(normal - right) < band


@pytest.mark.parametrize("read_id", BAYESIAN_READS)
def test_the_decision_follows_from_the_posteriors_with_room_on_its_rule(read_id):
    """STOP_WINNER needs a probability to be best of at least 0.975 and an
    expected loss below loss_threshold; with no ``rope`` anything else is
    CONTINUE. A STOP_WINNER read clears both rules with a wide margin; a
    CONTINUE read's probability is far under 0.975 even at the top of its band."""
    setup, check, right = _bayes_check(read_id)
    expected = setup["read"]["expect"]["json"]
    n_samples = expected["n_samples"]
    spread = math.sqrt(right * (1 - right) / n_samples)
    band = check["rel"] * right
    config = _turned_on(setup, "bayesian_enabled")["bayesian_config"]
    assert config.get("rope") is None
    loss = _expected_loss(*_posteriors(setup))
    if expected["decision"] == "STOP_WINNER":
        assert right - band - STOP_WINNER_PROBABILITY > 20 * spread
        assert loss < config["loss_threshold"] / 10, loss
    else:
        assert expected["decision"] == "CONTINUE"
        assert STOP_WINNER_PROBABILITY - (right + band) > 20 * spread


def test_the_journey_reads_both_bayesian_decisions():
    decisions = {
        _read_setup(_steps(), r)["read"]["expect"]["json"]["decision"]
        for r in BAYESIAN_READS
    }
    assert decisions == {"STOP_WINNER", "CONTINUE"}


def _one_stream(posteriors, seed: int) -> float:
    """Treatment's share of draws above Control's, both arms from ONE generator."""
    rng = np.random.default_rng(seed)
    control = rng.beta(posteriors[0], posteriors[1], BAYES_DRAWS)
    treatment = rng.beta(posteriors[2], posteriors[3], BAYES_DRAWS)
    return float(np.mean(treatment > control))


def _same_seed(posteriors, seed: int) -> float:
    """The defect: each arm's draws from a generator started at the same seed."""
    control = np.random.default_rng(seed).beta(
        posteriors[0], posteriors[1], BAYES_DRAWS
    )
    treatment = np.random.default_rng(seed).beta(
        posteriors[2], posteriors[3], BAYES_DRAWS
    )
    return float(np.mean(treatment > control))


def test_only_the_second_read_sees_draws_started_from_one_seed():
    """A defect in how the draws are made, modelled with numpy alone: each
    arm's draws from a generator started at the same seed. Near 0.99 (the
    first read) it moves the probability by less than the band on almost
    every seed; near 0.93 (the second) by tens of standard deviations, so the
    second read fails it on most seeds, while draws from one generator stay
    inside its band on every seed. (On the API's own draws, over 1,000 seeds:
    the first read caught it on 1, the second on 878.)"""
    first_setup, first_check, first_right = _bayes_check("floor-bayesian")
    second_setup, second_check, second_right = _bayes_check("bayesian-results")
    first, second = _posteriors(first_setup), _posteriors(second_setup)
    first_band = first_check["rel"] * first_right
    second_band = second_check["rel"] * second_right
    seeds = range(1, BAYES_SEEDS + 1)
    assert all(abs(_one_stream(second, s) - second_right) < second_band for s in seeds)
    caught_second = sum(
        abs(_same_seed(second, s) - second_right) > second_band for s in seeds
    )
    caught_first = sum(
        abs(_same_seed(first, s) - first_right) > first_band for s in seeds
    )
    assert caught_second >= 0.6 * BAYES_SEEDS, caught_second
    # The limit the second read exists for: the first read cannot see it.
    assert caught_first <= 0.1 * BAYES_SEEDS, caught_first


# ---------------------------------------------------------------------------
# The runner fails each step on a defect's answer
# ---------------------------------------------------------------------------
@pytest.fixture
def execute_module(monkeypatch):
    """docs_runner.execute with a stand-in for Playwright, which the unit job lacks."""
    sync_api = types.ModuleType("playwright.sync_api")

    class _Expect:
        def __call__(self, *args, **kwargs):
            raise AssertionError("no browser here")

        def set_options(self, **kwargs):
            return None

    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = _Expect()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


class _Answer:
    def __init__(self, body: Dict[str, Any]):
        self.status = 200
        self._body = body

    def json(self):
        return self._body


class _Api:
    def __init__(self, body: Dict[str, Any]):
        self.body = body
        self.paths: List[str] = []

    def fetch(self, path, method, headers, data):
        self.paths.append(path)
        return _Answer(self.body)


def _run(execute_module, tmp_path, step_id: str, body: Dict[str, Any]):
    from docs_runner.model import Step
    from docs_runner.stacks import Running

    raw = next(s for s in _steps() if s["id"] == step_id)
    step = Step.model_validate(raw)
    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    running = Running(name="compose-dev", base_url="http://dash/", api_url="http://api")
    runner.tokens[(running.api_url, "admin")] = "a-token-for-the-test"
    runner.values = {"experiment-id": "e1", "floor-experiment-id": "e3"}
    api = _Api(body)
    observed, _ = runner._api_step(step, running, api, "experiment-results", 1)
    return observed, api.paths


def _sequential_body(read_id: str, answers: Dict[str, float] | None = None):
    setup = _read_setup(_steps(), read_id)
    tau_squared, alpha = _sequential_settings(setup)
    counts = _sent(setup)
    numbers = answers or {
        "msprt_result.lambda_ratio": oracles.msprt_lambda_ratio(
            **counts, tau_squared=tau_squared
        ),
        "msprt_result.always_valid_p_value": oracles.msprt_always_valid_p(
            **counts, tau_squared=tau_squared
        ),
        "confidence_sequence.lower": _cs(
            **counts, tau_squared=tau_squared, alpha=alpha
        )[0],
        "confidence_sequence.upper": _cs(
            **counts, tau_squared=tau_squared, alpha=alpha
        )[1],
    }
    stops = numbers["msprt_result.lambda_ratio"] >= 1 / alpha
    return {
        "method": "msprt",
        "msprt_result": {
            "lambda_ratio": numbers["msprt_result.lambda_ratio"],
            "always_valid_p_value": numbers["msprt_result.always_valid_p_value"],
            "can_stop": stops,
            "evidence_strength": "strong_for_effect" if stops else "inconclusive",
            "boundary": 1 / alpha,
        },
        "confidence_sequence": {
            "lower": numbers["confidence_sequence.lower"],
            "upper": numbers["confidence_sequence.upper"],
        },
        "recommended_action": "stop_for_effect" if stops else "continue",
    }


@pytest.mark.parametrize("read_id", SEQUENTIAL_READS)
def test_each_sequential_step_passes_the_formulas_answer_and_fails_a_defects(
    execute_module, tmp_path, read_id
):
    right = _sequential_body(read_id)
    observed, paths = _run(execute_module, tmp_path, read_id, right)
    assert observed == "status 200"
    assert paths[0].startswith("/api/v1/results/e")
    failed = []
    for defect, (answers, moved) in _read_defects(read_id).items():
        body = _sequential_body(read_id, answers)
        # The literals stay right: only the computed numbers are the defect's.
        body["msprt_result"] = {
            **right["msprt_result"],
            **{
                key: body["msprt_result"][key]
                for key in ("lambda_ratio", "always_valid_p_value")
            },
        }
        body["recommended_action"] = right["recommended_action"]
        try:
            _run(execute_module, tmp_path, read_id, body)
        except execute_module._Failed as error:
            assert " gives " in error.observed, defect
            failed.append(defect)
        else:
            assert moved < 1e-12, defect
    assert failed and len(failed) == sum(
        moved > 1e-12 for _, moved in _read_defects(read_id).values()
    )


def _bayesian_body(read_id: str, probability: float) -> Dict[str, Any]:
    setup = _read_setup(_steps(), read_id)
    alpha_c, beta_c, alpha_t, beta_t = _posteriors(setup)
    return {
        "is_enabled": True,
        "decision": setup["read"]["expect"]["json"]["decision"],
        "variant_results": [
            {
                "variant_key": "Control",
                "posterior": {"alpha": alpha_c, "beta": beta_c},
                "probability_to_be_best": 1 - probability,
            },
            {
                "variant_key": "Treatment",
                "posterior": {"alpha": alpha_t, "beta": beta_t},
                "probability_to_be_best": probability,
            },
        ],
        "n_samples": 100000,
    }


@pytest.mark.parametrize(
    "read_id, experiment",
    [("floor-bayesian", "e3"), ("bayesian-results", "e1")],
)
def test_the_bayesian_step_holds_the_draws_and_fails_a_wrong_prior(
    execute_module, tmp_path, read_id, experiment
):
    setup, check, right = _bayes_check(read_id)
    band = check["rel"] * right
    for inside in (right, right - 0.9 * band, right + 0.9 * band):
        observed, paths = _run(
            execute_module, tmp_path, read_id, _bayesian_body(read_id, inside)
        )
        assert observed == "status 200"
        assert paths == [f"/api/v1/results/{experiment}/bayesian"]
    sent = _sent(setup)
    wrong_prior = _probability(
        1 + sent["control_converted"],
        1 + sent["control_users"] - sent["control_converted"],
        1 + sent["treatment_converted"],
        1 + sent["treatment_users"] - sent["treatment_converted"],
    )
    for outside in (wrong_prior, right - 1.1 * band, right + 1.1 * band, 1 - right):
        with pytest.raises(execute_module._Failed, match="bayes_prob_best gives"):
            _run(execute_module, tmp_path, read_id, _bayesian_body(read_id, outside))
