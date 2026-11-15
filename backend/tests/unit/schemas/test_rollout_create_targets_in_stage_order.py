"""A new schedule's targets are judged in ``stage_order``, whatever the list order (#1460).

The scheduler starts stages by ``stage_order``. Create checked that the targets
did not decrease in the order the list gave them, so
``[{stage_order: 2, 10%}, {stage_order: 1, 50%}]``, a schedule that runs 50%
and then 10%, was accepted, and ``[{stage_order: 2, 50%}, {stage_order: 1,
10%}]``, which runs 10% and then 50%, was refused.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from backend.app.schemas.rollout_schedule import RolloutScheduleCreate

pytestmark = [pytest.mark.unit]


def _create(*stages):
    return RolloutScheduleCreate(
        name="order",
        feature_flag_id=uuid.uuid4(),
        stages=[
            {
                "name": f"stage {order}",
                "stage_order": order,
                "target_percentage": target,
                "trigger_type": "manual",
            }
            for order, target in stages
        ],
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "stages",
    [[(2, 10), (1, 50)], [(1, 50), (2, 10)], [(3, 100), (1, 10), (2, 5)]],
    ids=["listed-backwards", "listed-in-order", "middle-dips"],
)
def test_targets_that_decrease_in_stage_order_are_refused(stages):
    with pytest.raises(ValidationError) as refused:
        _create(*stages)
    assert [(e["loc"], e["msg"]) for e in refused.value.errors()] == [
        ((), "Value error, Stage percentages must be non-decreasing in stage_order")
    ]


@pytest.mark.parametrize(
    "stages",
    [[(2, 50), (1, 10)], [(1, 10), (2, 50)], [(3, 100), (1, 10), (2, 10)]],
    ids=["listed-backwards", "listed-in-order", "equal-targets"],
)
def test_targets_that_rise_in_stage_order_are_accepted(stages):
    created = _create(*stages)
    assert sorted(
        (s.stage_order, s.target_percentage) for s in created.stages
    ) == sorted(stages)
