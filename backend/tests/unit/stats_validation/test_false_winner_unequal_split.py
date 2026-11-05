"""How often ``/results`` recommends shipping a variant that is no better, on
revenue with an uneven split (#1736).

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
(the treatment significantly lower).

At alpha 0.05 the two-sided test should call the treatment significantly
higher in about 2.5% of A/A experiments, 50 of 2,000, and lower in as many.
The band is 29 to 74 of 2,000: the central 99.9% of Binomial(2,000, 0.025).

Measured at these seeds:

* 600 control and 5,400 treatment users: SHIP_VARIANT 150 (7.5%),
  KEEP_CONTROL 12.  The treatment, the larger arm, is the one wrongly named,
  far above the band;
* 3,000 users in each: SHIP_VARIANT 46, KEEP_CONTROL 45, both in the band.

At three other seeds (1, 2, 3) the uneven split gave 150 to 166 SHIP_VARIANT
and the equal split 38 to 48.  Welch's t-test from scipy
(``ttest_ind(equal_var=False)`` on every user's value, 0 for a non-buyer) on
the same draws gives the same counts, so this is the documented method's
behaviour, not a computation defect.  In the 600-user arm a sample's mean and
its standard deviation rise and fall together (correlation 0.85 measured over
2,000 draws), so a control that drew low also looks precise, and the larger
treatment is then called significantly higher.

``docs/getting-started/limitations.md`` and the significance section of
``docs/getting-started/concepts.md`` say what was measured here; these tests
hold those sentences (``docs/_claims``).

The draws come from one ``numpy`` Generator per battery with a committed
seed, so every count is the same on every run.  Each test asserts its band
before the exact counts: a change in numpy's random stream reads as an exact
mismatch with the band holding, a change in the method as the band failing.
"""

from __future__ import annotations

from functools import lru_cache
from types import SimpleNamespace
from typing import List, Tuple

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
LOPSIDED_MEASURED = (150, 12)
EQUAL = (3_000, 3_000, 1_736_002)
EQUAL_MEASURED = (46, 45)

CONTROL = BinomialVariant("00000000-0000-4000-8000-0000000000c0", "Control", True)
TREATMENT = BinomialVariant("00000000-0000-4000-8000-0000000000e1", "Treatment", False)
METRIC = SimpleNamespace(
    id="00000000-0000-4000-8000-0000000000a3",
    name="revenue",
    metric_type="revenue",
    is_primary=True,
    minimum_sample_size=None,
)
EXPERIMENT = SimpleNamespace(metric_definitions=[METRIC])


def _buyer_totals(rng: np.random.Generator, users: int) -> Tuple[int, List[float]]:
    """How many of ``users`` buy, and each buyer's total; every other user's
    is 0, which ``per_user_mean_metric_result`` counts from ``users``."""
    buyers = int(rng.binomial(users, BUYER_SHARE))
    orders = rng.geometric(1.0 - REPEAT, size=buyers)
    values = rng.lognormal(MU, SIGMA, size=int(orders.sum()))
    owner = np.repeat(np.arange(buyers), orders)
    return buyers, np.bincount(owner, weights=values, minlength=buyers).tolist()


@lru_cache(maxsize=None)
def recommendations(
    control_users: int, treatment_users: int, seed: int
) -> Tuple[int, int]:
    """(SHIP_VARIANT, KEEP_CONTROL) over ``SIMS`` A/A experiments."""
    rng = np.random.default_rng(seed)
    alpha = 1.0 - CONFIDENCE_LEVEL
    raw = [
        {
            "metric_id": str(METRIC.id),
            "variant_results": [
                {"sample_size": control_users},
                {"sample_size": treatment_users},
            ],
        }
    ]
    adequate = AnalysisService._sample_size_adequate(EXPERIMENT, raw)
    ship = keep = 0
    for _ in range(SIMS):
        control_buyers, control_totals = _buyer_totals(rng, control_users)
        treatment_buyers, treatment_totals = _buyer_totals(rng, treatment_users)
        result = per_user_mean_metric_result(
            [
                (CONTROL, control_users, control_buyers, control_totals),
                (TREATMENT, treatment_users, treatment_buyers, treatment_totals),
            ],
            alpha,
            CORRECTION_METHOD,
            metric=METRIC,
        )
        decision = AnalysisService._summarise_decision(EXPERIMENT, [result], adequate)
        if decision["recommendation"] == "SHIP_VARIANT":
            assert decision["winning_variant_id"] == TREATMENT.id
            ship += 1
        elif decision["recommendation"] == "KEEP_CONTROL":
            keep += 1
    return ship, keep


def test_the_band_is_the_central_99_9_percent_of_one_direction():
    """29 to 74 of 2,000: Binomial(2,000, 0.025), alpha 0.05 split two ways."""
    assert MIN_ONE_SIDED == binom.ppf(0.0005, SIMS, 0.025)
    assert MAX_ONE_SIDED == binom.ppf(0.9995, SIMS, 0.025)


@pytest.mark.regression
def test_an_uneven_split_recommends_a_false_winner_above_the_band():
    """600 control and 5,400 treatment users: the treatment, the larger arm,
    is recommended to ship in 150 of 2,000 A/A experiments (7.5%, against
    2.5% for one direction at alpha 0.05), and the control is not named
    beyond the band (12).

    #1736: the docs gave alpha's rate unconditionally; this holds the caveat
    that now says otherwise, and fails if the product stops doing what it
    describes."""
    ship, keep = recommendations(*LOPSIDED)
    assert ship > MAX_ONE_SIDED, (
        f"SHIP_VARIANT in {ship} of {SIMS} A/A experiments at 600/5,400, no "
        f"longer above the band's top ({MAX_ONE_SIDED}): the caveat in "
        "limitations.md and concepts.md no longer describes the product"
    )
    assert keep <= MAX_ONE_SIDED, (
        f"KEEP_CONTROL in {keep} of {SIMS}: the smaller arm is named beyond "
        "the band too"
    )
    assert (ship, keep) == LOPSIDED_MEASURED, (
        f"(SHIP_VARIANT, KEEP_CONTROL) = {(ship, keep)}, measured "
        f"{LOPSIDED_MEASURED}; the band still holds"
    )


def test_an_equal_split_recommends_a_false_winner_within_the_band():
    """3,000 users in each: 46 SHIP_VARIANT and 45 KEEP_CONTROL of 2,000,
    both within the nominal rate's band."""
    ship, keep = recommendations(*EQUAL)
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
