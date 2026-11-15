"""GD-1 at four arms: how often an A/A/A/A experiment has ANY treatment significant.

``test_golden_aa_significance_rate.py`` holds the two-arm rate. With three
treatments the question changes: the correction applied across the treatments
of a metric (Benjamini-Hochberg unless the creator picked another) is what
keeps the chance that at least one of them is called significant near the
significance level. Without it each treatment is tested at the full level and
the chance is about 1 - 0.95^3.

This battery simulates 2,000 A/A/A/A experiments (4 arms, 5,000 users per arm,
a 10% conversion rate), passes each one's counts to ``binomial_metric_result``
with the stored confidence level and the stored default correction, and counts
the experiments in which ANY treatment row has ``is_significant``. It does not
count winners (a winner also needs a positive lift, which would hold half the
error rate to the whole band) and it does not count the control row.

The band is the two-arm battery's: 3.4% to 6.6% of 2,000 (68 to 132). Measured
at this seed (16_001): Benjamini-Hochberg 95, Bonferroni 91, no correction 218.
A correction that is not applied (the method read as ``none``) is therefore
outside the band, and ``test_without_a_correction_the_same_count_is_outside_the_band``
runs that arm as the positive control that the count can see it.

The draws come from one ``numpy`` Generator with a committed seed, so the
counts are the same on every run.
"""

from __future__ import annotations

from functools import lru_cache
from types import SimpleNamespace

import numpy as np
import pytest

from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
)

pytestmark = pytest.mark.unit

SIMS = 2_000
ARMS = 4
USERS_PER_ARM = 5_000
RATE = 0.10
#: The experiment's stored level; ``/results`` uses ``1 - level`` as alpha.
CONFIDENCE_LEVEL = 0.95
#: The correction an experiment stores unless its creator picks another.
CORRECTION_METHOD = "benjamini_hochberg"
SEED = 16_001

#: 3.4% and 6.6% of ``SIMS``, as in the two-arm battery.
MIN_SIGNIFICANT = 68
MAX_SIGNIFICANT = 132

VARIANTS = [
    BinomialVariant("00000000-0000-4000-8000-0000000000c0", "Control", True),
    *(
        BinomialVariant(f"00000000-0000-4000-8000-0000000000e{i}", f"T{i}", False)
        for i in range(1, ARMS)
    ),
]
METRIC = SimpleNamespace(
    id="00000000-0000-4000-8000-0000000000a0",
    name="conversion",
    metric_type="conversion",
    is_primary=True,
)


@lru_cache(maxsize=None)
def experiments_with_a_significant_treatment(correction_method: str) -> int:
    """How many of the ``SIMS`` experiments report at least one treatment significant."""
    rng = np.random.default_rng(SEED)
    conversions = rng.binomial(USERS_PER_ARM, RATE, size=(SIMS, ARMS))
    alpha = 1.0 - CONFIDENCE_LEVEL
    treatment_ids = {v.id for v in VARIANTS if not v.is_control}
    count = 0
    for row in conversions:
        result = binomial_metric_result(
            [(v, USERS_PER_ARM, int(x)) for v, x in zip(VARIANTS, row)],
            alpha,
            correction_method,
            metric=METRIC,
        )
        rows = [v for v in result["variants"] if v["variant_id"] in treatment_ids]
        assert len(rows) == ARMS - 1
        count += any(bool(r["is_significant"]) for r in rows)
    return count


def test_four_arm_aa_family_wise_rate_is_within_the_band():
    """With the stored correction, 68 to 132 of 2,000 A/A/A/A experiments have a significant treatment."""
    count = experiments_with_a_significant_treatment(CORRECTION_METHOD)
    assert MIN_SIGNIFICANT <= count <= MAX_SIGNIFICANT, (
        f"{count} of {SIMS} A/A/A/A experiments have a significant treatment "
        f"({count / SIMS:.2%}) with {CORRECTION_METHOD}; the band is "
        f"{MIN_SIGNIFICANT} to {MAX_SIGNIFICANT}"
    )


def test_without_a_correction_the_same_count_is_outside_the_band():
    """The count sees a missing correction: three uncorrected treatments exceed the band."""
    count = experiments_with_a_significant_treatment("none")
    assert count > MAX_SIGNIFICANT, (
        f"{count} of {SIMS} with no correction; it should be above {MAX_SIGNIFICANT}"
    )
