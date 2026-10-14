"""Which ``BayesianDecision`` values the stopping rules return.

``STOP_FUTILE`` is a value of the enum (the response and the stored
``bayesian_decision`` accept it), but no rule returns it: over posteriors from
"nothing yet" to a decided winner, and over the configs a caller can set,
``BayesianService.analyze`` answers ``CONTINUE``, ``STOP_WINNER`` or
``STOP_EQUIVALENT``, and each of the three occurs. The cases include the ones a
futility rule would be about: two arms that are almost surely equal at large
samples, and a treatment almost surely worse than the control.
"""

from __future__ import annotations

import itertools

import pytest

from backend.app.schemas.bayesian import BayesianConfig, BayesianDecision
from backend.app.services.bayesian_service import BayesianService

pytestmark = pytest.mark.unit

#: (conversions, users) per arm.
OBSERVATIONS = [
    [(0, 0), (0, 0)],
    [(5, 100), (6, 100)],
    [(100, 1_000), (100, 1_000)],
    [(1_000, 10_000), (1_000, 10_000)],
    [(10_000, 100_000), (10_000, 100_000)],
    [(10_000, 100_000), (10_050, 100_000)],
    [(1_000, 10_000), (700, 10_000)],
    [(1_000, 10_000), (1_300, 10_000)],
    [(5_000, 10_000), (100, 10_000)],
    [(50, 1_000), (60, 1_000), (55, 1_000)],
    [(1_000, 10_000), (900, 10_000), (800, 10_000)],
    [(20_000, 100_000), (20_000, 100_000), (20_000, 100_000)],
]

CONFIGS = [
    BayesianConfig(),
    BayesianConfig(loss_threshold=0.0001),
    BayesianConfig(loss_threshold=1.0),
    BayesianConfig(rope=[-0.01, 0.01]),
    BayesianConfig(rope=[-0.001, 0.001]),
    BayesianConfig(rope=[-0.5, 0.5], loss_threshold=0.0001),
    BayesianConfig(alpha=10.0, beta=10.0, rope=[-0.02, 0.02]),
]


def test_stop_futile_is_a_value_of_the_decision():
    assert BayesianDecision("STOP_FUTILE") is BayesianDecision.STOP_FUTILE


@pytest.mark.regression
def test_no_rule_returns_stop_futile():
    seen = set()
    for observations, config in itertools.product(OBSERVATIONS, CONFIGS):
        variant_observations = [{"conversions": c, "total": n} for c, n in observations]
        result = BayesianService(config).analyze(
            variant_observations, n_samples=20_000, seed=1142
        )
        decision = BayesianDecision(result["decision"])
        assert decision is not BayesianDecision.STOP_FUTILE, (observations, config)
        seen.add(decision)
    assert seen == {
        BayesianDecision.CONTINUE,
        BayesianDecision.STOP_WINNER,
        BayesianDecision.STOP_EQUIVALENT,
    }, seen
