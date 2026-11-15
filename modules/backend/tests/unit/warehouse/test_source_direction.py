"""A warehouse metric source's direction, on every type that carries it (#1386).

``warehouse_sources.lower_is_better`` is stored (``modules_0003_lower_is_better``),
a metric source's request body sets it, the response returns it, and a run
freezes it in ``MetricPlan`` and hands it to the estimators in ``MetricRef``.
This pins the field on each of those types, and that the estimators name the
winner in the direction ``MetricRef`` carries.  That a run actually carries it
from the source is ``test_runner_direction.py``'s.

It replaces ``modules/backend/tests/unit/test_warehouse_lower_is_better.py``,
which pinned the documented limit (every metric read as higher is better).
"""

import dataclasses
import uuid

import pytest
from pydantic import StrictBool
from sqlalchemy import Boolean

from modules.backend.app.models.warehouse_source import WarehouseSource
from modules.backend.app.schemas.responses_warehouse import SourceOut
from modules.backend.app.schemas.warehouse_sources import (
    AssignmentSourceCreate,
    AssignmentSourceUpdate,
    MetricSourceCreate,
    MetricSourceUpdate,
)
from modules.backend.app.services import warehouse_estimators as est
from modules.backend.app.services import warehouse_runner as runner

pytestmark = pytest.mark.unit

CONTROL = est.VariantRef(uuid.UUID(int=2), "control", True)
T1 = est.VariantRef(uuid.UUID(int=1), "t1", False)


def _proportion(control_converted: int, t1_converted: int, lower: bool) -> dict:
    counts = [
        est.VariantCounts(CONTROL, 2000, control_converted),
        est.VariantCounts(T1, 2000, t1_converted),
    ]
    metric = est.MetricRef(uuid.UUID(int=3), "Uninstall", True, lower_is_better=lower)
    return est.proportion_result(
        counts, alpha=0.05, correction_method="none", metric=metric
    )


def _mean(t1_sum_d: float, lower: bool) -> dict:
    # k = 2.0: the control's mean is 2.2 and t1's is 2.0 + t1_sum_d / 500.
    sums = [
        est.VariantMeanSums(CONTROL, 500, 100.0, 1000.0),
        est.VariantMeanSums(T1, 500, t1_sum_d, 1100.0),
    ]
    metric = est.MetricRef(
        uuid.UUID(int=4),
        "Checkout errors",
        True,
        metric_type="mean",
        lower_is_better=lower,
    )
    return est.mean_result(
        2.0, sums, alpha=0.05, correction_method="none", metric=metric
    )


@pytest.mark.parametrize("kind", ["proportion", "mean"])
@pytest.mark.parametrize("lower", [True, False])
def test_the_estimators_name_the_winner_in_the_direction_the_metric_carries(
    kind, lower
):
    if kind == "proportion":
        drop, rise = _proportion(200, 125, lower), _proportion(125, 200, lower)
    else:
        drop, rise = _mean(-150.0, lower), _mean(250.0, lower)
    for result in (drop, rise):
        t1 = next(v for v in result["variants"] if v["variant_id"] == str(T1.id))
        assert t1["is_significant"] is True
        assert result["lower_is_better"] is lower
    assert drop["winning_variant_id"] == (str(T1.id) if lower else None)
    assert rise["winning_variant_id"] == (None if lower else str(T1.id))


def test_the_direction_is_declared_on_every_type_that_carries_it():
    # Stored: NOT NULL, false unless set, on the database's side too.
    column = WarehouseSource.__table__.columns["lower_is_better"]
    assert isinstance(column.type, Boolean)
    assert column.nullable is False
    assert column.default.arg is False
    assert str(column.server_default.arg.compile()) == "false"

    # Requested: a metric body only, a JSON boolean, false when left out.
    for model in (MetricSourceCreate, MetricSourceUpdate):
        field = model.model_fields["lower_is_better"]
        assert field.annotation is bool and field.default is False, model
        assert tuple(field.metadata) == StrictBool.__metadata__, model
    for model in (AssignmentSourceCreate, AssignmentSourceUpdate):
        assert "lower_is_better" not in model.model_fields, model

    # Returned: null for an assignment source, so it is optional.
    returned = SourceOut.model_fields["lower_is_better"]
    assert returned.annotation == bool | None and returned.default is None

    # Run: frozen in the plan with no default, handed to the estimators.
    plan = runner.MetricPlan.__dataclass_fields__["lower_is_better"]
    assert plan.default is dataclasses.MISSING
    assert est.MetricRef.__dataclass_fields__["lower_is_better"].default is False
