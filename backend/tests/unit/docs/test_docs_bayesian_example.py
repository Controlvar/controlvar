"""``docs/api/bayesian.md``'s example response follows from its own posteriors.

The page prints a full response for a two-variant experiment ("the numbers are
illustrative") and then explains its decision under the decision table. Every
number in it is a function of the two posteriors it prints, so this computes
them twice:

* through the product: ``BayesianService.analyze`` with the documented default
  config (prior Beta(1, 1), ``loss_threshold`` 0.001, 95% credible level, no
  ``rope``), given the conversions and users the printed posteriors imply, at
  the page's ``seed`` and ``n_samples``;
* exactly, from the two Beta posteriors (scipy): the mean, the equal-tailed
  interval, ``P(rate_t > rate_c)`` and each expected loss
  ``E[max(other - this, 0)]``, by quadrature.

It holds each posterior's ``alpha`` and ``beta`` to the product's; each mean
and credible bound to the exact value, to the places printed; each probability
to be best and expected loss to the exact value, to the places printed, and to
the product's Monte Carlo answer within six of its standard deviations (plus
half the last printed place); the decision to the product's at the page's seed
and to the page's own rules applied to the exact values; and the sentence under
the table to the response it explains.
"""

from __future__ import annotations

import functools
import json
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from scipy import integrate, stats

from backend.app.schemas.bayesian import BayesianConfig
from backend.app.services.bayesian_service import BayesianService

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "api" / "bayesian.md"
#: Where the example starts on the page, and the sentence that explains it.
INTRO = "With traffic, the full response looks like this; the numbers are illustrative:"
SENTENCE = re.compile(
    r"The example response above answers `(?P<decision>[A-Z_]{1,20})`: the"
    r" treatment's expected loss, (?P<loss>[0-9.]{1,12}), is\s{1,4}below the default"
    r" `loss_threshold`, but its probability to be best, (?P<probability>[0-9.]{1,12}),"
    r" is under (?P<threshold>[0-9.]{1,12})\."
)
#: The decision table: STOP_WINNER needs at least this probability to be best.
STOP_WINNER_PROBABILITY = 0.975
#: How many Monte Carlo standard deviations a printed figure may be from the
#: product's answer at the page's seed (beyond half its last printed place).
SIGMAS = 6.0
#: How far the exact probability must be from 0.975, in those standard
#: deviations, so that the decision does not hang on the seed.
DECISION_MARGIN = 5.0
FIGURES = ("probability_to_be_best", "expected_loss")


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _example() -> Dict[str, Any]:
    """The JSON block after INTRO, its decimals kept as printed (Decimal)."""
    text = _page()
    block = re.compile(r"```json\n(.{1,4000}?)\n```", re.S).search(
        text, text.index(INTRO)
    )
    assert block, "no JSON block after the example's introduction"
    return json.loads(block.group(1), parse_float=Decimal)


def _half_place(printed: Decimal) -> float:
    """Half a unit of the printed number's last place."""
    return 0.5 * 10.0 ** printed.as_tuple().exponent


def _posteriors(example: Dict[str, Any]) -> List[Tuple[float, float]]:
    return [
        (float(v["posterior"]["alpha"]), float(v["posterior"]["beta"]))
        for v in example["variant_results"]
    ]


def _p_above(a_x: float, b_x: float, a_y: float, b_y: float) -> float:
    """P(X > Y) for X ~ Beta(a_x, b_x), Y ~ Beta(a_y, b_y): the integral over
    u in (0, 1) of F_Y(F_X^-1(u))."""
    x, y = stats.beta(a_x, b_x), stats.beta(a_y, b_y)
    value, _ = integrate.quad(
        lambda u: float(y.cdf(x.ppf(u))), 0, 1, epsabs=1e-13, epsrel=1e-12, limit=400
    )
    return value


def _loss_moments(
    a_x: float, b_x: float, a_y: float, b_y: float
) -> Tuple[float, float]:
    """E[max(Y - X, 0)] and E[max(Y - X, 0)^2]: the expected loss of choosing X,
    and its second moment (for the Monte Carlo spread). For Y ~ Beta(a, b),
    E[Y^k 1{Y > t}] = E[Y^k] P(Beta(a + k, b) > t), so given X = t
    E[(Y - t)+] = m1 S1(t) - t S0(t) and
    E[(Y - t)+^2] = m2 S2(t) - 2 t m1 S1(t) + t^2 S0(t)."""
    x = stats.beta(a_x, b_x)
    s0, s1, s2 = (stats.beta(a_y + k, b_y) for k in (0, 1, 2))
    m1 = a_y / (a_y + b_y)
    m2 = m1 * (a_y + 1) / (a_y + b_y + 1)

    def first(t: float) -> float:
        return m1 * s1.sf(t) - t * s0.sf(t)

    def second(t: float) -> float:
        return m2 * s2.sf(t) - 2 * t * m1 * s1.sf(t) + t * t * s0.sf(t)

    moments = [
        integrate.quad(lambda u: float(f(x.ppf(u))), 0, 1, epsabs=1e-15, limit=400)[0]
        for f in (first, second)
    ]
    return moments[0], moments[1]


@functools.lru_cache(maxsize=1)
def _computed() -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """(the example, the product's analysis of it, the exact figures and their
    Monte Carlo standard deviations, per variant in the example's order)."""
    example = _example()
    config = BayesianConfig()
    observations = []
    for alpha, beta in _posteriors(example):
        conversions = alpha - config.alpha
        total = alpha + beta - config.alpha - config.beta
        assert conversions == int(conversions) and total == int(total)
        observations.append({"conversions": int(conversions), "total": int(total)})
    product = BayesianService(config).analyze(
        observations, n_samples=int(example["n_samples"]), seed=int(example["seed"])
    )
    n = int(example["n_samples"])
    (a_c, b_c), (a_t, b_t) = _posteriors(example)
    p_t = _p_above(a_t, b_t, a_c, b_c)
    loss_c, square_c = _loss_moments(a_c, b_c, a_t, b_t)
    loss_t, square_t = _loss_moments(a_t, b_t, a_c, b_c)
    # E[(T - C)+] - E[(C - T)+] = E[T] - E[C]: the two quadratures agree.
    assert loss_c - loss_t == pytest.approx(
        a_t / (a_t + b_t) - a_c / (a_c + b_c), abs=1e-10
    )
    p_spread = math.sqrt(p_t * (1 - p_t) / n)
    exact = {
        "probability_to_be_best": ((1 - p_t, p_spread), (p_t, p_spread)),
        "expected_loss": (
            (loss_c, math.sqrt((square_c - loss_c**2) / n)),
            (loss_t, math.sqrt((square_t - loss_t**2) / n)),
        ),
    }
    return example, product, exact


def _figure_problems(field: str, index: int, printed: Decimal) -> List[str]:
    """Why *printed* is not variant *index*'s *field*: [] when it is."""
    _, product, exact = _computed()
    value, spread = exact[field][index]
    answer = product[field][index]
    problems = []
    if abs(float(printed) - value) > _half_place(printed):
        problems.append(f"{field}[{index}] {printed} is not {value} to its places")
    band = SIGMAS * spread + _half_place(printed)
    if abs(float(printed) - answer) > band:
        problems.append(
            f"{field}[{index}] {printed}: the product gives {answer} (band {band})"
        )
    return problems


def _decision() -> str:
    """The page's rules applied to the exact figures (no ``rope``)."""
    _, _, exact = _computed()
    probabilities = [p for p, _ in exact["probability_to_be_best"]]
    leader = max(range(len(probabilities)), key=lambda i: probabilities[i])
    stops = (
        probabilities[leader] >= STOP_WINNER_PROBABILITY
        and exact["expected_loss"][leader][0] < BayesianConfig().loss_threshold
    )
    spread = exact["probability_to_be_best"][leader][1]
    assert (
        abs(probabilities[leader] - STOP_WINNER_PROBABILITY) > DECISION_MARGIN * spread
    ), "the decision would hang on the seed"
    return "STOP_WINNER" if stops else "CONTINUE"


def test_the_product_is_given_the_documented_defaults():
    """The sentence under the table says "the default loss_threshold"; the
    analysis here uses the config the page's table documents."""
    config = BayesianConfig()
    assert (config.alpha, config.beta) == (1.0, 1.0)
    assert config.loss_threshold == 0.001
    assert config.credible_level == 0.95
    assert config.rope is None


def test_each_posterior_mean_and_interval_follows_from_its_alpha_and_beta():
    example, product, _ = _computed()
    for variant, (alpha, beta), posterior, (lower, upper) in zip(
        example["variant_results"],
        _posteriors(example),
        product["posteriors"],
        product["credible_intervals"],
        strict=True,
    ):
        printed = variant["posterior"]
        assert (posterior["alpha"], posterior["beta"]) == (alpha, beta)
        distribution = stats.beta(alpha, beta)
        exact = {
            "mean": alpha / (alpha + beta),
            "credible_interval_lower": float(distribution.ppf(0.025)),
            "credible_interval_upper": float(distribution.ppf(0.975)),
        }
        for field, value in exact.items():
            assert abs(float(printed[field]) - value) <= _half_place(printed[field]), (
                variant["variant_key"],
                field,
                value,
            )
        assert lower == pytest.approx(exact["credible_interval_lower"], abs=1e-12)
        assert upper == pytest.approx(exact["credible_interval_upper"], abs=1e-12)


def test_each_probability_and_loss_is_the_posteriors_and_the_products():
    example, _, _ = _computed()
    problems = [
        problem
        for field in FIGURES
        for index, variant in enumerate(example["variant_results"])
        for problem in _figure_problems(field, index, variant[field])
    ]
    assert not problems, problems


def test_the_decision_is_the_products_and_the_pages_rules():
    example, product, _ = _computed()
    assert product["seed"] == int(example["seed"])
    assert product["decision"].value == example["decision"]
    assert _decision() == example["decision"]


def test_the_sentence_under_the_table_explains_the_example():
    """The sentence quotes the example's decision, Treatment's expected loss and
    its probability to be best; each is the posteriors' and the product's, and
    what it says of them is true."""
    example, product, _ = _computed()
    found = SENTENCE.search(_page())
    assert found, "the sentence explaining the example's decision is gone"
    index, treatment = next(
        (i, v)
        for i, v in enumerate(example["variant_results"])
        if v["variant_key"] == "treatment"
    )
    assert found["decision"] == example["decision"] == _decision()
    assert found["decision"] == product["decision"].value
    loss, probability = Decimal(found["loss"]), Decimal(found["probability"])
    assert loss == treatment["expected_loss"]
    assert probability == treatment["probability_to_be_best"]
    assert _figure_problems("expected_loss", index, loss) == []
    assert _figure_problems("probability_to_be_best", index, probability) == []
    assert float(found["threshold"]) == STOP_WINNER_PROBABILITY
    assert float(loss) < BayesianConfig().loss_threshold
    assert float(probability) < STOP_WINNER_PROBABILITY
