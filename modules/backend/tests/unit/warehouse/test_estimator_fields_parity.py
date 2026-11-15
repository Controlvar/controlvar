"""What a warehouse run's results carry after #1496.

A warehouse proportion result is ``binomial_metric_result`` on the counts the
warehouse returned, so it carries the difference from the control, its
interval, the relative change's interval and the incremental impact, as
``/results`` does on the same counts.  A warehouse mean result is
``mean_metric_result``, which carries none of them: the narrowing
docs/api/warehouse-analytics.md and limitations.md state.  Each key set is
pinned exactly, so a field that appears or disappears in a warehouse response
(``Dict[str, Any]`` on beta routes, with no schema to notice) fails here.
"""

import json
import uuid

import pytest

from backend.app.services.sufficient_stats_analysis import (
    DIFFERENCE_FIELDS,
    binomial_metric_result,
    mean_metric_result,
)
from modules.backend.app.services import warehouse_estimators as est

pytestmark = pytest.mark.unit

CONTROL = est.VariantRef(uuid.UUID(int=2), "control", True)
T1 = est.VariantRef(uuid.UUID(int=1), "t1", False)
T2 = est.VariantRef(uuid.UUID(int=5), "t2", False)

#: The keys of a variant every result has had before #1496.
_VARIANT_KEYS = {
    "variant_id",
    "variant_name",
    "is_control",
    "sample_size",
    "conversions",
    "mean",
    "std_dev",
    "confidence_interval",
    "p_value",
    "adjusted_p_value",
    "is_significant",
    "effect_size",
    "effect_size_label",
    "relative_improvement_pct",
    "power",
    "statistical_test_used",
}
PROPORTION_VARIANT_KEYS = _VARIANT_KEYS | set(DIFFERENCE_FIELDS)
MEAN_VARIANT_KEYS = _VARIANT_KEYS | {"note"}

_PROPORTION_METRIC = est.MetricRef(uuid.UUID(int=3), "Purchases", True)
_MEAN_METRIC = est.MetricRef(uuid.UUID(int=4), "Order value", True, metric_type="mean")


def _counts():
    return [
        est.VariantCounts(CONTROL, 2000, 200),
        est.VariantCounts(T1, 2000, 250),
        est.VariantCounts(T2, 150, 0),
    ]


def _sums():
    return [
        est.VariantMeanSums(CONTROL, 500, 100.0, 1000.0),
        est.VariantMeanSums(T1, 500, 250.0, 1100.0),
    ]


def test_a_proportion_run_carries_the_difference_fields():
    result = est.proportion_result(
        _counts(),
        alpha=0.05,
        correction_method="benjamini_hochberg",
        metric=_PROPORTION_METRIC,
    )
    variants = {v["variant_name"]: v for v in result["variants"]}
    assert all(set(v) == PROPORTION_VARIANT_KEYS for v in variants.values())
    assert all(variants["control"][f] is None for f in DIFFERENCE_FIELDS)
    t1 = variants["t1"]
    assert t1["absolute_difference"] == pytest.approx(0.025, abs=1e-15)
    assert t1["incremental_impact"] == pytest.approx(50.0, abs=1e-9)
    # JSON values: the intervals are lists, and every number is finite.
    for field in ("absolute_difference_ci", "relative_improvement_pct_ci"):
        low, high = t1[field]
        assert 0 < low < high
    json.dumps(result, allow_nan=False)


def test_a_proportion_run_is_the_core_estimators_output_as_json():
    """The same counts through ``binomial_metric_result`` directly, made JSON
    (tuples as lists, UUIDs as text): equal, field for field."""
    variants = [(c.variant, c.n, c.n_converted) for c in _counts()]
    core = binomial_metric_result(
        variants, 0.05, "benjamini_hochberg", metric=_PROPORTION_METRIC
    )
    assert est.proportion_result(
        _counts(),
        alpha=0.05,
        correction_method="benjamini_hochberg",
        metric=_PROPORTION_METRIC,
    ) == json.loads(json.dumps(core, default=str))


def test_a_mean_run_carries_none_of_the_difference_fields():
    result = est.mean_result(
        2.0, _sums(), alpha=0.05, correction_method="none", metric=_MEAN_METRIC
    )
    assert all(set(v) == MEAN_VARIANT_KEYS for v in result["variants"])
    core = mean_metric_result(
        2.0,
        [(s.variant, s.n, s.sum_d, s.sum_d2) for s in _sums()],
        0.05,
        "none",
        metric=_MEAN_METRIC,
    )
    assert result == json.loads(json.dumps(core, default=str))
