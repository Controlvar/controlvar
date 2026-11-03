"""A rollout stage's ``trigger_configuration.duration`` is a number of hours (#1535).

The scheduler completes a time-based stage once ``duration`` hours (default
24) have passed since it started. The request models took any value: a
string, null or a number the scheduler cannot use left the stage in progress
for ever, a negative one completed it at once, and ``true`` read as one hour.
``RolloutStageCreate`` (create's stages and add-stage) and
``RolloutStageUpdate`` (``PUT /stages/{stage_id}``) now refuse a ``duration``
that is not a number from 0 to 8760, at a ``loc`` ending
``trigger_configuration``. The response model is not checked: a stage stored
with a bad value before the check still reads back.

The answers over HTTP are pinned by
``backend/tests/integration/api/test_rollout_stage_duration_api.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.schemas.rollout_schedule import (
    DURATION_REFUSED,
    MAX_STAGE_DURATION_HOURS,
    RolloutScheduleCreate,
    RolloutStageCreate,
    RolloutStageResponse,
    RolloutStageUpdate,
)

pytestmark = [pytest.mark.unit]

REFUSED = [
    pytest.param("abc", id="string"),
    pytest.param("1", id="numeric-string"),
    pytest.param(None, id="null"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(10**20, id="1e20"),
    pytest.param(10**400, id="1e400-int"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(-5, id="-5"),
    pytest.param(-0.5, id="-0.5"),
    pytest.param(MAX_STAGE_DURATION_HOURS + 0.5, id="8760.5"),
    pytest.param([24], id="list"),
    pytest.param({"hours": 24}, id="object"),
]

ACCEPTED = [0, 0.0, 0.5, 1, 24, 48.5, MAX_STAGE_DURATION_HOURS]


def _stage(**configuration) -> dict:
    return {
        "name": "Stage 1",
        "stage_order": 1,
        "target_percentage": 10,
        "trigger_type": "time_based",
        "trigger_configuration": configuration,
    }


def _bodies(value):
    """``(model, body, loc)`` for each request model that takes a stage."""
    stage = _stage(duration=value)
    return [
        (RolloutStageCreate, stage, ("trigger_configuration",)),
        (
            RolloutStageUpdate,
            {"trigger_configuration": {"duration": value}},
            ("trigger_configuration",),
        ),
        (
            RolloutScheduleCreate,
            {
                "name": "Durations",
                "feature_flag_id": str(uuid.uuid4()),
                "stages": [stage],
            },
            ("stages", 0, "trigger_configuration"),
        ),
    ]


@pytest.mark.regression
@pytest.mark.parametrize("value", REFUSED)
def test_a_duration_that_is_not_hours_from_0_to_8760_is_refused(value):
    # Before: every one of these was accepted by all three models.
    for model, body, loc in _bodies(value):
        with pytest.raises(ValidationError) as caught:
            model.model_validate(body)
        errors = [(e["type"], e["loc"], e["msg"]) for e in caught.value.errors()]
        assert errors == [("value_error", loc, DURATION_REFUSED)], model


@pytest.mark.parametrize("value", ACCEPTED)
def test_a_duration_from_0_to_8760_is_accepted(value):
    for model, body, _ in _bodies(value):
        model.model_validate(body)


@pytest.mark.parametrize(
    "configuration",
    [None, {}, {"scheduled_date": "2027-01-08T00:00:00Z", "time_zone": "UTC"}],
    ids=["none", "empty", "no-duration"],
)
def test_a_configuration_without_duration_is_not_checked(configuration):
    body = {**_stage(), "trigger_configuration": configuration}
    assert RolloutStageCreate.model_validate(body).trigger_configuration == (
        configuration
    )
    update = RolloutStageUpdate.model_validate({"trigger_configuration": configuration})
    assert update.trigger_configuration == configuration


def test_the_message_never_echoes_the_value():
    marker = "marker-" + uuid.uuid4().hex
    with pytest.raises(ValidationError) as caught:
        RolloutStageUpdate.model_validate(
            {"trigger_configuration": {"duration": marker}}
        )
    assert marker not in str(
        [(e["type"], e["loc"], e["msg"], e.get("ctx")) for e in caught.value.errors()]
    )


@pytest.mark.parametrize("stored", ["abc", None, "1", True, 10**20, -5])
def test_a_response_describes_a_stage_stored_with_a_bad_duration(stored):
    # Gate 1q at the model: the check on RolloutStageBase, which the response
    # inherits, would make every read of such a stage a 500.
    now = datetime.now(timezone.utc)
    stage = RolloutStageResponse.model_validate(
        {
            **_stage(duration=stored),
            "id": uuid.uuid4(),
            "rollout_schedule_id": uuid.uuid4(),
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
    )
    assert stage.trigger_configuration == {"duration": stored}
