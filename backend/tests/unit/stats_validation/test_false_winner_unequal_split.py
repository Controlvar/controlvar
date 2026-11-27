"""How often ``/results`` names a variant that is no better, on mean metrics
with an uneven split (#1736, #2041).

Most experiments here are A/A experiments on revenue: both variants draw from
the same distribution, so every SHIP_VARIANT is a false winner.  Each user
buys with probability 0.05; after each order a buyer orders again with
probability 0.3; each order's value is lognormal(3, 1).  Most users have 0 and
a few have a lot: zero-inflated and right-skewed.

Each battery simulates its experiments (2,000 for every A/A battery and for
the equal-means batteries, 1,000 for the two power batteries) and runs the
product on each, the way ``AnalysisService.get_experiment_results`` does:
``per_user_mean_metric_result`` on each variant's assigned users and the
totals of its buyers (alpha ``1 - 0.95``; the default Benjamini-Hochberg
correction, which leaves a single treatment's p-value as it is), then
``AnalysisService._sample_size_adequate``
(100 users per variant unless the metric sets another minimum) and
``AnalysisService._summarise_decision``.  It counts the experiments
recommended SHIP_VARIANT (the treatment significantly higher, or lower when
lower is better) and KEEP_CONTROL (every treatment significantly worse), and
asserts that it ran the experiments its docstring states, that every
compared treatment's p-value came from the permutation test, and that the
summary's and the metric's winner agree with SHIP_VARIANT.  Run ``r`` of a
battery with seed ``s`` gives its metric and variants the ids
``uuid5(UUID(int=s), f"{r}/{role}")``, so no two runs share an identity, as
no two experiments do; the permutation test's seed is built from them.

At alpha 0.05 the two-sided test should call the treatment significantly
higher in about 2.5% of A/A experiments, 50 of 2,000, and lower in as many.
The band is 29 to 74 of 2,000: the central 99.9% of Binomial(2,000, 0.025).

Before #2041, ``/results`` took a mean metric's p-value from Welch's t-test,
and at these seeds measured:

* 600 control and 5,400 treatment users: SHIP_VARIANT 150 (7.5%),
  KEEP_CONTROL 12.  The treatment, the larger arm, was the one wrongly named,
  far above the band;
* 3,000 users in each: SHIP_VARIANT 46, KEEP_CONTROL 45, both in the band;
* 5,400 control and 600 treatment users: 13 and 159; lower is better at
  600/5,400: 6 and 155; three arms at 600/2,700/2,700: SHIP_VARIANT 134,
  any treatment significant 141; a count metric at 600/5,400: 97 and 23.

At three other seeds (1, 2, 3) the uneven split gave 150 to 166 SHIP_VARIANT
and the equal split 38 to 48.  Welch's t-test from scipy
(``ttest_ind(equal_var=False)`` on every user's value, 0 for a non-buyer) on
the same draws gave the same counts, so it was the method's behaviour, not a
computation defect.  In the 600-user arm a sample's mean and its standard
deviation rise and fall together (correlation 0.85 measured over 2,000
draws), so a control that drew low also looks precise, and the larger
treatment is then called significantly higher.  A mean metric's p-value now
comes from a permutation test of Welch's t
(``backend/app/services/permutation_test.py``), and these batteries hold that
the recommendation meets alpha in both directions on A/A experiments.

The guarantee is for identical distributions.  A variant that is no better
but changes the shape of the values (more buyers who each spend less) is not
covered by it; ``test_a_shape_change_at_5400_600_is_held_as_measured`` holds
the count measured at a lopsided split.

``docs/getting-started/limitations.md`` and the significance section of
``docs/getting-started/concepts.md`` say what was measured here; these tests
hold those sentences (``docs/_claims``).

The draws come from one ``numpy`` Generator per battery with a committed
seed, so every count is the same on every run.  Each test asserts its band
before the exact counts: a change in numpy's random stream reads as an exact
mismatch with the band holding, a change in the method as the band failing.
The exact counts were measured on macOS arm64 and must be what Linux CI
measures; each assertion prints what it measured.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from typing import Dict, FrozenSet, List, Tuple

import numpy as np
import pytest
from scipy.stats import binom

from backend.app.services.analysis_service import AnalysisService
from backend.app.services.mean_arms import prepared_for_tests
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    mean_metric_result,
    per_user_mean_metric_result,
)

pytestmark = pytest.mark.unit

SIMS = 2_000
#: The two power batteries run 1,000 experiments, as measured on main.
POWER_SIMS = 1_000
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
#: Three arms (BH at alpha 0.05, shared-control correlation 0.818): any
#: treatment significant 59 to 118, SHIP_VARIANT at most 66, KEEP_CONTROL at
#: most 40.
MIN_ANY_OF_TWO = 59
MAX_ANY_OF_TWO = 118
MAX_SHIP_OF_TWO = 66
MAX_KEEP_OF_TWO = 40
#: The 99.95% point of Binomial(2,000, 0.05): both directions together.
MAX_EITHER = 133


@dataclass(frozen=True)
class Revenue:
    """Users buy with ``buyer_share``, order again with ``REPEAT``, and each
    order is lognormal(``MU``, ``SIGMA``) times ``value_scale``."""

    buyer_share: float = BUYER_SHARE
    value_scale: float = 1.0

    def __call__(self, rng: np.random.Generator, users: int) -> Tuple[int, List[float]]:
        buyers = int(rng.binomial(users, self.buyer_share))
        orders = rng.geometric(1.0 - REPEAT, size=buyers)
        values = rng.lognormal(MU, SIGMA, size=int(orders.sum())) * self.value_scale
        owner = np.repeat(np.arange(buyers), orders)
        return buyers, np.bincount(owner, weights=values, minlength=buyers).tolist()


@dataclass(frozen=True)
class Count:
    """Users send the event with probability 0.05, then again with
    ``REPEAT``: each sender's value is their number of events."""

    def __call__(self, rng: np.random.Generator, users: int) -> Tuple[int, List[float]]:
        senders = int(rng.binomial(users, BUYER_SHARE))
        events = rng.geometric(1.0 - REPEAT, size=senders)
        return senders, events.astype(float).tolist()


@dataclass(frozen=True)
class Normal:
    """Every user has a value from normal(``mean``, 10): no zeros, no skew."""

    mean: float = 50.0

    def __call__(self, rng: np.random.Generator, users: int) -> Tuple[int, List[float]]:
        return users, rng.normal(self.mean, 10.0, size=users).tolist()


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


#: An arm: its users and how a user's value is drawn.
Arm = Tuple[int, object]

ROLES = ("control", "t1", "t2")


def _ids(seed: int, run: int, roles: Tuple[str, ...]) -> List[str]:
    namespace = uuid.UUID(int=seed)
    return [str(uuid.uuid5(namespace, f"{run}/{role}")) for role in roles]


@lru_cache(maxsize=None)
def battery(
    arms: Tuple[Arm, ...],
    seed: int,
    runs: int = SIMS,
    lower_is_better: bool = False,
    metric_type: str = "revenue",
) -> Battery:
    """``runs`` experiments with these arms (the first is the control)."""
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
                "variant_results": [{"sample_size": users} for users, _ in arms],
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
    roles = ROLES[: len(arms)]
    for run in range(runs):
        metric_id, *variant_ids = _ids(seed, run, ("metric",) + roles)
        metric = SimpleNamespace(
            id=metric_id,
            name=metric_type,
            metric_type=metric_type,
            is_primary=True,
            minimum_sample_size=None,
            lower_is_better=lower_is_better,
        )
        rows = []
        for index, ((users, draw), variant_id) in enumerate(zip(arms, variant_ids)):
            converted, totals = draw(rng, users)
            variant = BinomialVariant(variant_id, roles[index], index == 0)
            rows.append((variant, users, converted, totals))
        result = per_user_mean_metric_result(
            prepared_for_tests(rows), alpha, CORRECTION_METHOD, metric=metric
        )
        decision = AnalysisService._summarise_decision(experiment, [result], adequate)
        if decision["recommendation"] == "SHIP_VARIANT":
            assert decision["winning_variant_id"] in variant_ids[1:]
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
    return Battery(runs=runs, tests=frozenset(tests), **counts)


def revenue_aa(control_users: int, treatment_users: int) -> Tuple[Arm, ...]:
    return ((control_users, Revenue()), (treatment_users, Revenue()))


#: The batteries, by name: (arms, seed, runs, lower is better, metric type).
#: Seeds 1736001 and 1736002 are #1736's; the others are #2041's, fixed
#: before the permutation test was run on them.
SCENARIOS: Dict[str, Tuple[Tuple[Arm, ...], int, int, bool, str]] = {
    "lopsided": (revenue_aa(600, 5_400), 1_736_001, SIMS, False, "revenue"),
    "equal": (revenue_aa(3_000, 3_000), 1_736_002, SIMS, False, "revenue"),
    "mirrored": (revenue_aa(5_400, 600), 2_041_001, SIMS, False, "revenue"),
    "lower_is_better": (revenue_aa(600, 5_400), 2_041_021, SIMS, True, "revenue"),
    "three_arms": (
        ((600, Revenue()), (2_700, Revenue()), (2_700, Revenue())),
        2_041_002,
        SIMS,
        False,
        "revenue",
    ),
    "count": (((600, Count()), (5_400, Count())), 2_041_003, SIMS, False, "count"),
    "normal": (
        ((600, Normal()), (5_400, Normal())),
        2_041_004,
        SIMS,
        False,
        "custom",
    ),
    "normal_power": (
        ((600, Normal()), (5_400, Normal(51.0))),
        2_041_022,
        POWER_SIMS,
        False,
        "custom",
    ),
    "buyers_power": (
        ((3_000, Revenue()), (3_000, Revenue(buyer_share=1.5 * BUYER_SHARE))),
        2_041_012,
        POWER_SIMS,
        False,
        "revenue",
    ),
    "mild_shape_change": (
        (
            (600, Revenue()),
            (5_400, Revenue(buyer_share=1.25 * BUYER_SHARE, value_scale=0.8)),
        ),
        2_041_031,
        SIMS,
        False,
        "revenue",
    ),
    "strong_shape_change": (
        (
            (5_400, Revenue()),
            (600, Revenue(buyer_share=2.5 * BUYER_SHARE, value_scale=0.4)),
        ),
        2_041_041,
        SIMS,
        False,
        "revenue",
    ),
}


def measure(name: str) -> Battery:
    arms, seed, runs, lower_is_better, metric_type = SCENARIOS[name]
    measured = battery(arms, seed, runs, lower_is_better, metric_type)
    treatments = len(arms) - 1
    assert measured.runs == runs and measured.compared == runs * treatments, (
        f"{name}: {measured.runs} runs and {measured.compared} compared "
        f"treatments; the battery is {runs} runs of {treatments} treatment(s)"
    )
    return measured


def assert_permutation(name: str, measured: Battery) -> None:
    """Every compared treatment's p-value came from the permutation test:
    none fell back to Welch's t-test inside the band."""
    assert measured.tests == {"permutation_t_test"}, (
        f"{name}: the compared treatments name {sorted(measured.tests)}"
    )


def assert_in_band(name: str, split: str, measured: Battery) -> None:
    """SHIP_VARIANT and KEEP_CONTROL each within 29 to 74 of 2,000."""
    assert measured.ship <= MAX_ONE_SIDED, (
        f"SHIP_VARIANT in {measured.ship} of {measured.runs} A/A experiments at "
        f"{split}; the band's top is {MAX_ONE_SIDED}"
    )
    assert measured.keep >= MIN_ONE_SIDED, (
        f"KEEP_CONTROL in {measured.keep} of {measured.runs} A/A experiments at "
        f"{split}; the band's bottom is {MIN_ONE_SIDED}"
    )
    assert measured.ship >= MIN_ONE_SIDED, (
        f"SHIP_VARIANT in {measured.ship} of {measured.runs} A/A experiments at "
        f"{split}; the band's bottom is {MIN_ONE_SIDED}"
    )
    assert measured.keep <= MAX_ONE_SIDED, (
        f"KEEP_CONTROL in {measured.keep} of {measured.runs} A/A experiments at "
        f"{split}; the band's top is {MAX_ONE_SIDED}"
    )


def assert_one_winner_field(name: str, measured: Battery) -> None:
    """With one treatment and every arm above the minimum sample, the
    summary's ``has_winner`` and winner, and the metric's winner, are set
    exactly when the recommendation is SHIP_VARIANT, and a significant
    treatment is either the winner or worse (KEEP_CONTROL).  First, every
    p-value came from the permutation test."""
    assert_permutation(name, measured)
    fields = (measured.has_winner, measured.summary_winner, measured.metric_winner)
    assert fields == (measured.ship,) * 3, (
        f"{name}: has_winner, the summary's and the metric's winner are "
        f"{fields}; SHIP_VARIANT is {measured.ship}"
    )
    assert measured.significant == measured.ship + measured.keep, (
        f"{name}: {measured.significant} significant treatments; SHIP_VARIANT "
        f"{measured.ship} and KEEP_CONTROL {measured.keep}"
    )


#: The exact counts measured with the product, per battery: (SHIP_VARIANT,
#: KEEP_CONTROL); for the lopsided and equal batteries also the runs whose
#: interval excludes 0 and those where the interval and the test disagree; for three
#: arms (any treatment significant, SHIP_VARIANT, KEEP_CONTROL); for the
#: power batteries the experiments detected (SHIP_VARIANT).
MEASURED: Dict[str, Tuple[int, ...]] = {
    "lopsided": (48, 52, 162, 142),
    "equal": (46, 45, 91, 8),
    "mirrored": (54, 49),
    "lower_is_better": (48, 49),
    "three_arms": (93, 46, 26),
    "count": (43, 48),
    "normal": (53, 49),
    "normal_power": (647,),
    "buyers_power": (703,),
    "mild_shape_change": (66, 51),
    "strong_shape_change": (71, 18),
}


def assert_measured(name: str, actual: Tuple[int, ...]) -> None:
    assert actual == MEASURED[name], (
        f"{name}: measured {actual}, pinned {MEASURED[name]}; the band still holds"
    )


def test_the_band_is_the_central_99_9_percent_of_one_direction():
    """29 to 74 of 2,000: Binomial(2,000, 0.025), alpha 0.05 split two ways.

    The other bands follow the same rule: three arms' 59 to 118, 66 and 40
    are the central 99.9% of Binomial(2,000, p) at the rates normal theory
    gives for BH at alpha 0.05 with a shared control (4.35%, 2.17%, 1.16%),
    and 133 is the 99.95% point of Binomial(2,000, 0.05)."""
    assert MIN_ONE_SIDED == binom.ppf(0.0005, SIMS, 0.025)
    assert MAX_ONE_SIDED == binom.ppf(0.9995, SIMS, 0.025)
    assert MIN_ANY_OF_TWO == binom.ppf(0.0005, SIMS, 0.0435)
    assert MAX_ANY_OF_TWO == binom.ppf(0.9995, SIMS, 0.0435)
    assert MAX_SHIP_OF_TWO == binom.ppf(0.9995, SIMS, 0.0217)
    assert MAX_KEEP_OF_TWO == binom.ppf(0.9995, SIMS, 0.0116)
    assert MAX_EITHER == binom.ppf(0.9995, SIMS, 0.05)


@pytest.mark.regression
def test_a_lopsided_split_names_a_false_winner_no_more_often_than_alpha():
    """600 control and 5,400 treatment users (seed 1736001, 2,000 runs):
    neither the treatment nor the control is named more often than alpha
    allows in its direction.  Measured: SHIP_VARIANT 48, KEEP_CONTROL 52.

    #2041: with Welch's t-test the treatment, the larger arm, was recommended
    to ship in 150 of 2,000 A/A experiments at this seed (7.5%, against 2.5%
    for one direction at alpha 0.05), and the control in 12.

    The interval on the difference is still Welch's: it excludes 0 in 162
    runs, as before, where the test is significant in 100; the two disagree
    in 142 runs (7.1%).  Before #2041 they always agreed."""
    measured = measure("lopsided")
    assert_in_band("lopsided", "600/5,400", measured)
    assert_one_winner_field("lopsided", measured)
    assert_measured(
        "lopsided",
        (
            measured.ship,
            measured.keep,
            measured.interval_excludes_zero,
            measured.disagree,
        ),
    )


@pytest.mark.regression
def test_the_mirrored_split_names_neither_arm_beyond_the_band():
    """5,400 control and 600 treatment users (seed 2041001, 2,000 runs).
    Welch named the control (KEEP_CONTROL) in 159.  Measured: SHIP_VARIANT
    54, KEEP_CONTROL 49."""
    measured = measure("mirrored")
    assert_in_band("mirrored", "5,400/600", measured)
    assert_one_winner_field("mirrored", measured)
    assert_measured("mirrored", (measured.ship, measured.keep))


@pytest.mark.regression
def test_lower_is_better_follows_the_metric_direction():
    """600/5,400 on a lower-is-better revenue metric (seed 2041021, 2,000
    runs): the winner is the lower mean.  Welch gave KEEP_CONTROL in 155.
    Measured: SHIP_VARIANT 48, KEEP_CONTROL 49."""
    measured = measure("lower_is_better")
    assert_in_band("lower_is_better", "600/5,400, lower is better", measured)
    assert_one_winner_field("lower_is_better", measured)
    assert_measured("lower_is_better", (measured.ship, measured.keep))


@pytest.mark.regression
def test_three_arms_with_a_small_control_stay_within_their_bands():
    """600 control users and two treatments of 2,700 (seed 2041002, 2,000
    runs, so 4,000 compared treatments; B is 1,999 for two treatments).
    Welch had any treatment significant in 141 and SHIP_VARIANT in 134.

    The bands: Benjamini-Hochberg at alpha 0.05 with the shared control
    (correlation (1/600) / (1/600 + 1/2,700) = 0.818); normal theory over
    2 * 10**7 draws gives 4.35% any significant, 2.17% SHIP_VARIANT and
    1.16% KEEP_CONTROL (every treatment worse), and each band is the central
    99.9% of Binomial(2,000, rate): 59 to 118, at most 66, at most 40, with
    no lower edge on the two directions.  Measured: any 93, SHIP_VARIANT 46,
    KEEP_CONTROL 26."""
    measured = measure("three_arms")
    assert MIN_ANY_OF_TWO <= measured.any_significant <= MAX_ANY_OF_TWO, (
        f"any treatment significant in {measured.any_significant} of {SIMS} "
        f"A/A experiments at 600/2,700/2,700; the band is {MIN_ANY_OF_TWO} to "
        f"{MAX_ANY_OF_TWO}"
    )
    assert measured.ship <= MAX_SHIP_OF_TWO, (
        f"SHIP_VARIANT in {measured.ship} of {SIMS} at 600/2,700/2,700; the "
        f"band's top is {MAX_SHIP_OF_TWO}"
    )
    assert measured.keep <= MAX_KEEP_OF_TWO, (
        f"KEEP_CONTROL in {measured.keep} of {SIMS} at 600/2,700/2,700; the "
        f"band's top is {MAX_KEEP_OF_TWO}"
    )
    assert_permutation("three_arms", measured)
    assert measured.has_winner == measured.summary_winner == measured.ship
    assert_measured(
        "three_arms", (measured.any_significant, measured.ship, measured.keep)
    )


def test_an_equal_split_recommends_a_false_winner_within_the_band():
    """3,000 users in each (seed 1736002, 2,000 runs): SHIP_VARIANT 46 and
    KEEP_CONTROL 45, both within the nominal rate's band.  Welch gave the same
    46 and 45 at this seed; what tells them apart is the interval, Welch's
    either way: it excludes 0 in 91 runs, and disagrees with the permutation
    test in 8 of them, where it agreed with Welch's test in all."""
    measured = measure("equal")
    assert_in_band("equal", "3,000/3,000", measured)
    assert_one_winner_field("equal", measured)
    assert_measured(
        "equal",
        (
            measured.ship,
            measured.keep,
            measured.interval_excludes_zero,
            measured.disagree,
        ),
    )


@pytest.mark.regression
def test_a_count_metric_at_a_lopsided_split_stays_within_the_band():
    """A count metric (5% of users send the event, then again with
    probability 0.3) at 600/5,400 (seed 2041003, 2,000 runs).  Welch gave
    SHIP_VARIANT 97.  Measured: SHIP_VARIANT 43, KEEP_CONTROL 48."""
    measured = measure("count")
    assert_in_band("count", "600/5,400 (count)", measured)
    assert_one_winner_field("count", measured)
    assert_measured("count", (measured.ship, measured.keep))


def test_a_metric_that_is_not_skewed_stays_within_the_band():
    """Every user's value normal(50, 10) at 600/5,400 (seed 2041004, 2,000
    runs): no zeros and no skew.  Welch gave 54 and 46.  Measured:
    SHIP_VARIANT 53, KEEP_CONTROL 49."""
    measured = measure("normal")
    assert_in_band("normal", "600/5,400 (normal)", measured)
    assert_one_winner_field("normal", measured)
    assert_measured("normal", (measured.ship, measured.keep))


def test_a_metric_that_is_not_skewed_keeps_its_power():
    """normal(50, 10) against normal(51, 10) at 600/5,400 (seed 2041022,
    1,000 runs).  Welch detected the lift (SHIP_VARIANT) in 645; measured:
    647."""
    measured = measure("normal_power")
    assert_one_winner_field("normal_power", measured)
    assert_measured("normal_power", (measured.ship,))


def test_a_balanced_split_keeps_its_power():
    """3,000 users in each, the treatment's buyers 1.5 times as many (7.5%)
    (seed 2041012, 1,000 runs).  Welch detected it in 704; measured: 703."""
    measured = measure("buyers_power")
    assert_one_winner_field("buyers_power", measured)
    assert_measured("buyers_power", (measured.ship,))


def test_the_statistic_is_studentized_under_equal_means():
    """Equal means with a mild change of shape: the treatment has 6.25% buyers
    whose values are 0.8 times the control's, so its mean per user is the
    same, at 600/5,400 (seed 2041031, 2,000 runs).  A raw difference of means
    is not studentized and names the treatment above the band here.

    The correct method's rate here is about 3.1% SHIP_VARIANT and 5.5% in
    total, so a later engine change that re-rolls the draws can land above
    74 by chance; that is investigated, never answered by loosening the band.
    Welch gave 154 and 13.  Measured: SHIP_VARIANT 66, KEEP_CONTROL 51."""
    measured = measure("mild_shape_change")
    assert measured.ship <= MAX_ONE_SIDED, (
        f"SHIP_VARIANT in {measured.ship} of {SIMS} equal-means experiments at "
        f"600/5,400; the band's top is {MAX_ONE_SIDED}"
    )
    assert measured.ship + measured.keep <= MAX_EITHER, (
        f"SHIP_VARIANT and KEEP_CONTROL in {measured.ship + measured.keep} of "
        f"{SIMS} equal-means experiments at 600/5,400; the top is {MAX_EITHER}"
    )
    assert_one_winner_field("mild_shape_change", measured)
    assert_measured("mild_shape_change", (measured.ship, measured.keep))


def test_a_shape_change_at_5400_600_is_held_as_measured():
    """The guarantee is for identical distributions.  Here the treatment has
    12.5% buyers whose values are 0.4 times the control's, so its mean per
    user is the same, at 5,400 control and 600 treatment users (seed 2041041,
    fixed before the run; 2,000 runs).  Welch gave SHIP_VARIANT 20 and
    KEEP_CONTROL 82.

    Measured: SHIP_VARIANT 71, KEEP_CONTROL 18, against about 50 expected in
    each direction.  71 is below the band's top (74) at this seed, so this
    holds the measured count and claims nothing beyond it."""
    measured = measure("strong_shape_change")
    assert_one_winner_field("strong_shape_change", measured)
    assert_measured("strong_shape_change", (measured.ship, measured.keep))


#: (SHIP_VARIANT, KEEP_CONTROL) of the lopsided battery with each arm given
#: as its sums alone.
FROM_SUMS_MEASURED = (150, 12)


def test_a_mean_from_sums_alone_keeps_welchs_t_test():
    """A warehouse analysis of a mean has only each variant's sums, so
    ``mean_metric_result`` compares it with Welch's t-test: the lopsided
    battery's 2,000 A/A experiments (600/5,400, seed 1736001), each arm given
    as its count and its sums centred on the grand mean, still recommend
    shipping the treatment in 150 and keeping the control in 12, as
    ``/results`` did before engine 1.13.0.  ``limitations.md`` says so."""
    arms, seed, runs, _, _ = SCENARIOS["lopsided"]
    rng = np.random.default_rng(seed)
    alpha = 1.0 - CONFIDENCE_LEVEL
    experiment = SimpleNamespace(metric_definitions=[])
    ship = keep = 0
    tests = set()
    for run in range(runs):
        metric_id, control_id, treatment_id = _ids(
            seed, run, ("metric", "control", "t1")
        )
        drawn = [draw(rng, users) for users, draw in arms]
        users = [n for n, _ in arms]
        everyone = [v for _, totals in drawn for v in totals]
        k = math.fsum(everyone) / sum(users)
        rows = []
        for (n, (_, totals)), variant in zip(
            zip(users, drawn),
            (
                BinomialVariant(control_id, "control", True),
                BinomialVariant(treatment_id, "t1", False),
            ),
        ):
            zeros = n - len(totals)
            sum_d = math.fsum([v - k for v in totals] + [-k * zeros])
            sum_d2 = math.fsum([(v - k) ** 2 for v in totals] + [k * k * zeros])
            rows.append((variant, n, sum_d, sum_d2))
        result = mean_metric_result(
            k,
            rows,
            alpha,
            CORRECTION_METHOD,
            metric=SimpleNamespace(
                id=metric_id, name="revenue", metric_type="revenue", is_primary=True
            ),
        )
        tests.update(v["statistical_test_used"] for v in result["variants"][1:])
        decision = AnalysisService._summarise_decision(experiment, [result], True)
        ship += decision["recommendation"] == "SHIP_VARIANT"
        keep += decision["recommendation"] == "KEEP_CONTROL"
    assert tests == {"welch_t_test"}, sorted(tests)
    assert ship > MAX_ONE_SIDED, (
        f"SHIP_VARIANT in {ship} of {runs} from sums alone: no longer above the "
        f"band's top ({MAX_ONE_SIDED}), so limitations.md's warehouse row is out "
        "of date"
    )
    assert (ship, keep) == FROM_SUMS_MEASURED, (
        f"from sums: measured {(ship, keep)}, pinned {FROM_SUMS_MEASURED}"
    )
