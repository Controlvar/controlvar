"""How often ``/results`` recommends shipping a variant that is no better, on
revenue with an uneven split (#1736, #2041).

Every experiment here is an A/A experiment on revenue: both variants draw from
the same distribution, so every SHIP_VARIANT is a false winner.  Each user
buys with probability 0.05; after each order a buyer orders again with
probability 0.3; each order's value is lognormal(3, 1).  Most users have 0 and
a few have a lot: zero-inflated and right-skewed.

Each battery simulates 2,000 experiments and runs the product on each, the
way ``AnalysisService.get_experiment_results`` does:
``per_user_mean_metric_result`` on each variant's assigned users and the
totals of its buyers (alpha ``1 - 0.95``; the default Benjamini-Hochberg
correction, which leaves a single treatment's p-value as it is), then
``AnalysisService._sample_size_adequate``
(100 users per variant unless the metric sets another minimum) and
``AnalysisService._summarise_decision``.  It counts the experiments
recommended SHIP_VARIANT (the treatment significantly higher) and KEEP_CONTROL
(the treatment significantly lower).  Run ``r`` of a battery with seed ``s``
gives its metric and variants the ids ``uuid5(UUID(int=s), f"{r}/{role}")``,
so no two runs share an identity, as no two experiments do.

At alpha 0.05 the two-sided test should call the treatment significantly
higher in about 2.5% of A/A experiments, 50 of 2,000, and lower in as many.
The band is 29 to 74 of 2,000: the central 99.9% of Binomial(2,000, 0.025).

Before #2041, ``/results`` took a mean metric's p-value from Welch's t-test,
and at these seeds measured:

* 600 control and 5,400 treatment users: SHIP_VARIANT 150 (7.5%),
  KEEP_CONTROL 12.  The treatment, the larger arm, was the one wrongly named,
  far above the band;
* 3,000 users in each: SHIP_VARIANT 46, KEEP_CONTROL 45, both in the band.

At three other seeds (1, 2, 3) the uneven split gave 150 to 166 SHIP_VARIANT
and the equal split 38 to 48.  Welch's t-test from scipy
(``ttest_ind(equal_var=False)`` on every user's value, 0 for a non-buyer) on
the same draws gave the same counts, so it was the method's behaviour, not a
computation defect.  In the 600-user arm a sample's mean and its standard
deviation rise and fall together (correlation 0.85 measured over 2,000
draws), so a control that drew low also looks precise, and the larger
treatment is then called significantly higher.  The lopsided test below holds
that the recommendation now meets alpha in both directions.

``docs/getting-started/limitations.md`` and the significance section of
``docs/getting-started/concepts.md`` say what was measured here; these tests
hold those sentences (``docs/_claims``).

The draws come from one ``numpy`` Generator per battery with a committed
seed, so every count is the same on every run.  Each test asserts its band
before the exact counts: a change in numpy's random stream reads as an exact
mismatch with the band holding, a change in the method as the band failing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from typing import FrozenSet, List, Tuple

import numpy as np
import pytest
from scipy.stats import binom

from backend.app.services.analysis_service import AnalysisService
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    per_user_mean_metric_result,
)

pytestmark = pytest.mark.unit

SIMS = 2_000
#: The experiment's stored level; ``/results`` uses ``1 - level`` as alpha.
CONFIDENCE_LEVEL = 0.95
#: The correction an experiment stores unless its creator picks another.
CORRECTION_METHOD = "benjamini_hochberg"

#: The share of users who buy at least once.
BUYER_SHARE = 0.05
#: After each order, the chance that the buyer orders again.
REPEAT = 0.3
#: Each order's value is ``exp(MU + SIGMA * z)``.
MU = 3.0
SIGMA = 1.0

#: The central 99.9% of Binomial(2,000, 0.025): one direction at alpha 0.05.
MIN_ONE_SIDED = 29
MAX_ONE_SIDED = 74

#: (control users, treatment users, seed) -> measured (SHIP_VARIANT,
#: KEEP_CONTROL) of ``SIMS``.
LOPSIDED = (600, 5_400, 1_736_001)
EQUAL = (3_000, 3_000, 1_736_002)
EQUAL_MEASURED = (46, 45)


@dataclass(frozen=True)
class Battery:
    """What a battery counted over its runs."""

    runs: int
    #: Treatments that received a p-value, over every run.
    compared: int
    ship: int
    keep: int
    #: The summary's ``has_winner``, and runs whose summary and whose metric
    #: name a winning variant.
    has_winner: int
    summary_winner: int
    metric_winner: int
    #: Treatments with ``is_significant``, and runs with any of them.
    significant: int
    any_significant: int
    #: Treatments whose ``absolute_difference_ci`` excludes 0, and those
    #: where that disagrees with ``is_significant``.
    interval_excludes_zero: int
    disagree: int
    #: Every compared treatment's ``statistical_test_used``.
    tests: FrozenSet[str]


def _buyer_totals(rng: np.random.Generator, users: int) -> Tuple[int, List[float]]:
    """How many of ``users`` buy, and each buyer's total; every other user's
    is 0, which ``per_user_mean_metric_result`` counts from ``users``."""
    buyers = int(rng.binomial(users, BUYER_SHARE))
    orders = rng.geometric(1.0 - REPEAT, size=buyers)
    values = rng.lognormal(MU, SIGMA, size=int(orders.sum()))
    owner = np.repeat(np.arange(buyers), orders)
    return buyers, np.bincount(owner, weights=values, minlength=buyers).tolist()


def _ids(seed: int, run: int, roles: Tuple[str, ...]) -> List[str]:
    namespace = uuid.UUID(int=seed)
    return [str(uuid.uuid5(namespace, f"{run}/{role}")) for role in roles]


@lru_cache(maxsize=None)
def battery(control_users: int, treatment_users: int, seed: int) -> Battery:
    """``SIMS`` A/A experiments on revenue at this split and seed."""
    rng = np.random.default_rng(seed)
    alpha = 1.0 - CONFIDENCE_LEVEL
    experiment = SimpleNamespace(
        metric_definitions=[
            SimpleNamespace(id="m", is_primary=True, minimum_sample_size=None)
        ]
    )
    adequate = AnalysisService._sample_size_adequate(
        experiment,
        [
            {
                "metric_id": "m",
                "variant_results": [
                    {"sample_size": control_users},
                    {"sample_size": treatment_users},
                ],
            }
        ],
    )
    counts = dict.fromkeys(
        (
            "compared",
            "ship",
            "keep",
            "has_winner",
            "summary_winner",
            "metric_winner",
            "significant",
            "any_significant",
            "interval_excludes_zero",
            "disagree",
        ),
        0,
    )
    tests = set()
    for run in range(SIMS):
        metric_id, control_id, treatment_id = _ids(
            seed, run, ("metric", "control", "treatment")
        )
        metric = SimpleNamespace(
            id=metric_id,
            name="revenue",
            metric_type="revenue",
            is_primary=True,
            minimum_sample_size=None,
        )
        control_buyers, control_totals = _buyer_totals(rng, control_users)
        treatment_buyers, treatment_totals = _buyer_totals(rng, treatment_users)
        result = per_user_mean_metric_result(
            [
                (
                    BinomialVariant(control_id, "Control", True),
                    control_users,
                    control_buyers,
                    control_totals,
                ),
                (
                    BinomialVariant(treatment_id, "Treatment", False),
                    treatment_users,
                    treatment_buyers,
                    treatment_totals,
                ),
            ],
            alpha,
            CORRECTION_METHOD,
            metric=metric,
        )
        decision = AnalysisService._summarise_decision(experiment, [result], adequate)
        if decision["recommendation"] == "SHIP_VARIANT":
            assert decision["winning_variant_id"] == treatment_id
            counts["ship"] += 1
        elif decision["recommendation"] == "KEEP_CONTROL":
            counts["keep"] += 1
        counts["has_winner"] += bool(decision["has_winner"])
        counts["summary_winner"] += decision["winning_variant_id"] is not None
        counts["metric_winner"] += result["winning_variant_id"] is not None
        counts["any_significant"] += bool(result["has_significant_result"])
        for variant in result["variants"]:
            if variant["is_control"] or variant["p_value"] is None:
                continue
            counts["compared"] += 1
            tests.add(variant["statistical_test_used"])
            counts["significant"] += bool(variant["is_significant"])
            interval = variant["absolute_difference_ci"]
            excludes = interval is not None and (interval[0] > 0 or interval[1] < 0)
            counts["interval_excludes_zero"] += excludes
            counts["disagree"] += excludes != bool(variant["is_significant"])
    return Battery(runs=SIMS, tests=frozenset(tests), **counts)


def test_the_band_is_the_central_99_9_percent_of_one_direction():
    """29 to 74 of 2,000: Binomial(2,000, 0.025), alpha 0.05 split two ways."""
    assert MIN_ONE_SIDED == binom.ppf(0.0005, SIMS, 0.025)
    assert MAX_ONE_SIDED == binom.ppf(0.9995, SIMS, 0.025)


@pytest.mark.regression
def test_a_lopsided_split_names_a_false_winner_no_more_often_than_alpha():
    """600 control and 5,400 treatment users: neither the treatment nor the
    control is named more often than alpha allows in its direction.

    #2041: with Welch's t-test the treatment, the larger arm, was recommended
    to ship in 150 of 2,000 A/A experiments at this seed (7.5%, against 2.5%
    for one direction at alpha 0.05).  A mean metric's p-value now comes from
    a permutation test of Welch's t, which holds alpha at this split."""
    measured = battery(*LOPSIDED)
    assert measured.runs == SIMS and measured.compared == SIMS, (
        f"{measured.runs} runs and {measured.compared} compared treatments; "
        f"the battery is {SIMS} runs of one treatment"
    )
    assert measured.ship <= MAX_ONE_SIDED, (
        f"SHIP_VARIANT in {measured.ship} of {SIMS} A/A experiments at "
        f"600/5,400; the band's top is {MAX_ONE_SIDED}"
    )
    assert measured.keep >= MIN_ONE_SIDED, (
        f"KEEP_CONTROL in {measured.keep} of {SIMS} A/A experiments at "
        f"600/5,400; the band's bottom is {MIN_ONE_SIDED}"
    )
    assert measured.ship >= MIN_ONE_SIDED, (
        f"SHIP_VARIANT in {measured.ship} of {SIMS} A/A experiments at "
        f"600/5,400; the band's bottom is {MIN_ONE_SIDED}"
    )
    assert measured.keep <= MAX_ONE_SIDED, (
        f"KEEP_CONTROL in {measured.keep} of {SIMS} A/A experiments at "
        f"600/5,400; the band's top is {MAX_ONE_SIDED}"
    )
    assert measured.tests == {"permutation_t_test"}, (
        f"the compared treatments name {sorted(measured.tests)}"
    )


def test_an_equal_split_recommends_a_false_winner_within_the_band():
    """3,000 users in each: 46 SHIP_VARIANT and 45 KEEP_CONTROL of 2,000,
    both within the nominal rate's band."""
    measured = battery(*EQUAL)
    ship, keep = measured.ship, measured.keep
    assert MIN_ONE_SIDED <= ship <= MAX_ONE_SIDED, (
        f"SHIP_VARIANT in {ship} of {SIMS} A/A experiments at 3,000/3,000; "
        f"the band is {MIN_ONE_SIDED} to {MAX_ONE_SIDED}"
    )
    assert MIN_ONE_SIDED <= keep <= MAX_ONE_SIDED, (
        f"KEEP_CONTROL in {keep} of {SIMS} A/A experiments at 3,000/3,000; "
        f"the band is {MIN_ONE_SIDED} to {MAX_ONE_SIDED}"
    )
    assert (ship, keep) == EQUAL_MEASURED, (
        f"(SHIP_VARIANT, KEEP_CONTROL) = {(ship, keep)}, measured "
        f"{EQUAL_MEASURED}; the band still holds"
    )
