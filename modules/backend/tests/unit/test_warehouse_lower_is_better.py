"""A warehouse analysis reads every metric as higher is better (#1386).

A warehouse metric source has no lower-is-better setting, so the estimators
are never told a direction: a significant drop names no winner and a
significant rise does.  This pins the documented limit until a source can say
which way is better.
"""

import uuid

import pytest

from modules.backend.app.models.warehouse_source import WarehouseSource
from modules.backend.app.schemas.warehouse_sources import MetricSourceCreate
from modules.backend.app.services import warehouse_estimators as est

pytestmark = pytest.mark.unit

CONTROL = est.VariantRef(uuid.UUID(int=2), "control", True)
T1 = est.VariantRef(uuid.UUID(int=1), "t1", False)


def _proportion(control_converted: int, t1_converted: int) -> dict:
    counts = [
        est.VariantCounts(CONTROL, 2000, control_converted),
        est.VariantCounts(T1, 2000, t1_converted),
    ]
    metric = est.MetricRef(uuid.UUID(int=3), "Uninstall", True)
    return est.proportion_result(
        counts, alpha=0.05, correction_method="none", metric=metric
    )


def _mean(t1_sum_d: float) -> dict:
    # k = 2.0: the control's mean is 2.2 and t1's is 2.0 + t1_sum_d / 500.
    sums = [
        est.VariantMeanSums(CONTROL, 500, 100.0, 1000.0),
        est.VariantMeanSums(T1, 500, t1_sum_d, 1100.0),
    ]
    metric = est.MetricRef(
        uuid.UUID(int=4), "Checkout errors", True, metric_type="mean"
    )
    return est.mean_result(
        2.0, sums, alpha=0.05, correction_method="none", metric=metric
    )


@pytest.mark.parametrize("kind", ["proportion", "mean"])
def test_a_warehouse_result_names_a_winner_only_for_a_significant_increase(kind):
    if kind == "proportion":
        drop, rise = _proportion(200, 125), _proportion(125, 200)
    else:
        drop, rise = _mean(-150.0), _mean(250.0)
    for result in (drop, rise):
        t1 = next(v for v in result["variants"] if v["variant_id"] == str(T1.id))
        assert t1["is_significant"] is True
        assert result["lower_is_better"] is False
    assert drop["winning_variant_id"] is None
    assert rise["winning_variant_id"] == str(T1.id)


def test_a_warehouse_metric_source_has_no_lower_is_better_setting():
    assert "lower_is_better" not in WarehouseSource.__table__.columns
    assert "lower_is_better" not in MetricSourceCreate.model_fields
    assert "lower_is_better" not in est.MetricRef.__dataclass_fields__
