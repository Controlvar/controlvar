"""A metric's result in ``/results``: a conversion, whatever the metric's type.

``binomial_metric_result`` is what the results engine builds every metric's
result with. It compares each treatment with the control by two-sided
Fisher's exact test on converted and not-converted users, and its effect size
is Cohen's h, for a revenue metric as for a conversion one.
"""

import math
from types import SimpleNamespace

import pytest

from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
)

pytestmark = [pytest.mark.unit]

CONTROL = BinomialVariant("c", "control", True)
TREATMENT = BinomialVariant("t", "treatment", False)


@pytest.mark.parametrize("metric_type", ["conversion", "revenue", "count"])
def test_every_metric_is_a_fisher_test_on_conversions_with_cohens_h(metric_type):
    metric = SimpleNamespace(
        id="m", name="Purchase", metric_type=metric_type, is_primary=True
    )
    result = binomial_metric_result(
        [(CONTROL, 200, 20), (TREATMENT, 200, 40)], 0.05, "none", metric=metric
    )
    [treatment] = [v for v in result["variants"] if not v["is_control"]]
    assert treatment["statistical_test_used"] == "fisher_exact"
    cohens_h = 2 * math.asin(math.sqrt(0.2)) - 2 * math.asin(math.sqrt(0.1))
    assert treatment["effect_size"] == pytest.approx(cohens_h)
