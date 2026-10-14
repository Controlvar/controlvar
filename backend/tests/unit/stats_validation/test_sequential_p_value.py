"""The always-valid p-value of the sequential analysis, against reference values.

``compute_msprt`` reports ``always_valid_p_value = min(1, 1 / Lambda)``, where
Lambda is the normal-mixture mSPRT ratio (Johari et al. 2017)

    Lambda = sqrt(V / (V + tau^2)) * exp(tau^2 * Z^2 / (2 (V + tau^2))),
    Z = delta_hat / sqrt(V),

and V is the Agresti-Caffo variance of the difference in rates: one success
and one failure added to each arm, ``p~ = (x + 1) / (n + 2)``,
``V = p~_c (1 - p~_c) / (n_c + 2) + p~_t (1 - p~_t) / (n_t + 2)``.

The reference values are computed here from that formula, with exact
fractions for V and delta and 50-digit ``decimal`` arithmetic for the
logarithm and the exponential; nothing is taken from the service. Each is also
written out as a literal (computed once the same way), with its exact V, so a
reader can re-derive it by hand. Range checks such as ``0 < p <= 1`` hold for
any p-value scaled by a constant below 1; these do not.
"""

from __future__ import annotations

from decimal import Decimal, getcontext
from fractions import Fraction

import numpy as np
import pytest

from backend.app.services.sequential_testing_service import SequentialTestingService

pytestmark = pytest.mark.unit

ALPHA = 0.05
TAU_SQUARED = Fraction(1, 1000)

#: (control conversions, control users, treatment conversions, treatment users)
#: -> (exact Agresti-Caffo V, always-valid p-value), at alpha 0.05, tau^2 0.001.
REFERENCE = {
    (100, 1000, 150, 1000): (Fraction(109751, 503006004), 2.143015765429052e-02),
    (100, 1000, 145, 1000): (Fraction(215977, 1006012008), 4.899175217901125e-02),
    (100, 1000, 140, 1000): (Fraction(106201, 503006004), 1.048593066354004e-01),
    (100, 1000, 130, 1000): (Fraction(102551, 503006004), 3.884720623422566e-01),
    (100, 1000, 55, 1000): (Fraction(143977, 1006012008), 5.799471817745054e-03),
    (1000, 10000, 1300, 10000): (
        Fraction(10165001, 500300060004),
        2.649664897606927e-09,
    ),
}

#: Equal rates: Lambda is below 1, so the p-value is exactly 1.
NO_EVIDENCE = [(500, 5000, 500, 5000), (1000, 10000, 1000, 10000)]


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def reference_p_value(xc: int, nc: int, xt: int, nt: int) -> Decimal:
    """min(1, 1 / Lambda) from the formula in the module docstring."""
    getcontext().prec = 50
    pc = Fraction(xc + 1, nc + 2)
    pt = Fraction(xt + 1, nt + 2)
    variance = _decimal(pc * (1 - pc) / (nc + 2) + pt * (1 - pt) / (nt + 2))
    delta = _decimal(Fraction(xt, nt) - Fraction(xc, nc))
    tau = _decimal(TAU_SQUARED)
    z_squared = delta * delta / variance
    log_lambda = (variance / (variance + tau)).ln() / 2 + tau * z_squared / (
        2 * (variance + tau)
    )
    return min(Decimal(1), 1 / log_lambda.exp())


def exact_variance(xc: int, nc: int, xt: int, nt: int) -> Fraction:
    pc = Fraction(xc + 1, nc + 2)
    pt = Fraction(xt + 1, nt + 2)
    return pc * (1 - pc) / (nc + 2) + pt * (1 - pt) / (nt + 2)


def _p_value(counts) -> float:
    return (
        SequentialTestingService()
        .compute_msprt(*counts, tau_squared=float(TAU_SQUARED), alpha=ALPHA)
        .always_valid_p_value
    )


def test_the_reference_table_is_the_formula():
    """The literals are what the formula gives, and V is the one written down."""
    for counts, (variance, p_value) in REFERENCE.items():
        assert exact_variance(*counts) == variance, counts
        assert float(reference_p_value(*counts)) == pytest.approx(p_value, rel=1e-12)


@pytest.mark.regression
@pytest.mark.parametrize("counts", sorted(REFERENCE))
def test_the_always_valid_p_value_is_one_over_the_mixture_ratio(counts):
    """The service's p-value equals min(1, 1/Lambda) computed independently."""
    expected = float(reference_p_value(*counts))
    assert _p_value(counts) == pytest.approx(expected, rel=1e-9)
    assert _p_value(counts) == pytest.approx(REFERENCE[counts][1], rel=1e-9)


@pytest.mark.parametrize("counts", NO_EVIDENCE)
def test_no_evidence_gives_a_p_value_of_exactly_one(counts):
    assert float(reference_p_value(*counts)) == 1.0
    assert _p_value(counts) == 1.0


@pytest.mark.parametrize("counts", sorted(REFERENCE) + NO_EVIDENCE)
def test_the_p_value_is_below_alpha_exactly_when_the_analysis_can_stop(counts):
    """p <= alpha and Lambda >= 1/alpha are the same decision."""
    result = SequentialTestingService().compute_msprt(
        *counts, tau_squared=float(TAU_SQUARED), alpha=ALPHA
    )
    assert (result.always_valid_p_value <= ALPHA) == result.can_stop, counts


#: binom.ppf(.999, 2000, .05): see test_sequential_confidence_sequence.py.
PATHS = 2_000
LOOKS = 20
MAX_FALSE_REJECTIONS = 131


@pytest.mark.regression
def test_aa_paths_read_through_the_p_value_reject_at_most_alpha():
    """2,000 A/A paths, each read 20 times; stopping at the first p <= 0.05.

    The paths are drawn as in test_sequential_confidence_sequence.py (base rate,
    arm-size ratio and look spacing chosen per path, fixed seed). The share of
    paths ever rejected stays under the binomial 0.999 quantile at the nominal
    level, so the p-value can be read at every look without inflating the
    false-positive rate. A p-value that is too small rejects far more paths.
    """
    service = SequentialTestingService()
    rng = np.random.default_rng(1_142_001)
    rejected = 0
    for _ in range(PATHS):
        rate = float(rng.choice((0.02, 0.05, 0.10, 0.30, 0.50)))
        ratio = float(rng.choice((0.25, 0.5, 1.0, 2.0, 3.0)))
        step_c = int(rng.choice((100, 400, 1_000)))
        step_t = max(1, int(round(step_c * ratio)))
        conv_c = np.cumsum(rng.binomial(step_c, rate, size=LOOKS))
        conv_t = np.cumsum(rng.binomial(step_t, rate, size=LOOKS))
        for k in range(LOOKS):
            p_value = service.compute_msprt(
                int(conv_c[k]),
                step_c * (k + 1),
                int(conv_t[k]),
                step_t * (k + 1),
                tau_squared=float(TAU_SQUARED),
                alpha=ALPHA,
            ).always_valid_p_value
            if p_value <= ALPHA:
                rejected += 1
                break
    assert rejected <= MAX_FALSE_REJECTIONS, rejected
