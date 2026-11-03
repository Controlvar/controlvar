"""The rollout schedule request models' input rules (#962, #1086, #1529, #1534).

* ``min_stage_duration`` is bounded to 0..8760 hours on
  ``RolloutScheduleCreate`` and ``RolloutScheduleUpdate`` only. The response
  model is not bounded: a value stored before the bound still reads back.
* ``RolloutScheduleUpdate`` refuses ``stages`` whatever its value, and keeps
  the field so the key is not silently dropped.
* Dates compare as UTC instants, an unzoned one taken as UTC, so a zoned and
  an unzoned date no longer raise a ``TypeError`` (a 500 over HTTP); the end
  must be strictly after the start, on create and on an update that sends both.

The answers over HTTP are pinned by
``backend/tests/integration/api/test_rollout_schedule_input_api.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.schemas.rollout_schedule import (
    MAX_MIN_STAGE_DURATION_HOURS,
    STAGES_ON_UPDATE_REFUSED,
    RolloutScheduleBase,
    RolloutScheduleCreate,
    RolloutScheduleResponse,
    RolloutScheduleUpdate,
)

pytestmark = [pytest.mark.unit]

DATE_MSG = "Value error, End date must be after start date"


def _create(**extra) -> dict:
    body = {
        "name": "Input rules",
        "feature_flag_id": str(uuid.uuid4()),
        "stages": [
            {
                "name": "Stage 1",
                "stage_order": 1,
                "target_percentage": 10,
                "trigger_type": "manual",
            }
        ],
    }
    body.update(extra)
    return body


def _errors(model, body) -> list:
    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)
    return [(e["type"], e["loc"], e["msg"]) for e in caught.value.errors()]


# --- min_stage_duration --------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "value, kind",
    [
        (-1, "greater_than_equal"),
        (-5, "greater_than_equal"),
        (MAX_MIN_STAGE_DURATION_HOURS + 1, "less_than_equal"),
        (2**31 - 1, "less_than_equal"),
        (2**31, "less_than_equal"),
    ],
)
def test_min_stage_duration_outside_the_range_is_refused_on_create_and_update(
    value, kind
):
    # Before: both models accepted every integer.
    for model, body in (
        (RolloutScheduleCreate, _create(min_stage_duration=value)),
        (RolloutScheduleUpdate, {"min_stage_duration": value}),
    ):
        errors = _errors(model, body)
        assert [(t, loc) for t, loc, _ in errors] == [(kind, ("min_stage_duration",))]


@pytest.mark.parametrize("value", [0, 1, 24, 48, MAX_MIN_STAGE_DURATION_HOURS, None])
def test_min_stage_duration_in_the_range_or_null_is_accepted(value):
    assert (
        RolloutScheduleCreate.model_validate(
            _create(min_stage_duration=value)
        ).min_stage_duration
        == value
    )
    update = RolloutScheduleUpdate.model_validate({"min_stage_duration": value})
    assert update.min_stage_duration == value
    assert "min_stage_duration" in update.model_fields_set  # null still clears


def test_the_bound_is_a_year_of_hours():
    assert MAX_MIN_STAGE_DURATION_HOURS == 365 * 24


@pytest.mark.parametrize("stored", [-5, MAX_MIN_STAGE_DURATION_HOURS + 1, 100000])
def test_a_response_and_the_base_describe_a_stored_value_outside_the_range(stored):
    # Gate 1e at the model: a bound on the base (which the response inherits)
    # would make every read of such a row a 500.
    now = datetime.now(timezone.utc)
    response = RolloutScheduleResponse.model_validate(
        {
            **_create(min_stage_duration=stored),
            "stages": [],
            "id": uuid.uuid4(),
            "owner_id": None,
            "status": "draft",
            "created_at": now,
            "updated_at": now,
        }
    )
    assert response.min_stage_duration == stored
    base = RolloutScheduleBase.model_validate(_create(min_stage_duration=stored))
    assert base.min_stage_duration == stored


def test_the_openapi_bounds_are_on_the_requests_only():
    def field(model):
        return model.model_json_schema()["properties"]["min_stage_duration"]

    for model in (RolloutScheduleCreate, RolloutScheduleUpdate):
        bounded = field(model)["anyOf"][0]
        assert (bounded["minimum"], bounded["maximum"]) == (0, 8760), model
    for model in (RolloutScheduleBase, RolloutScheduleResponse):
        branches = field(model)["anyOf"]
        assert not any("minimum" in b or "maximum" in b for b in branches), model


# --- stages on update ----------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "stages",
    [None, [], [{"name": "x"}], [{"stage_order": 1, "target_percentage": 20}]],
    ids=["null", "empty", "name-only", "non-empty"],
)
def test_an_update_with_stages_is_refused_whatever_its_value(stages):
    # Before: accepted, and the service ignored them.
    assert _errors(RolloutScheduleUpdate, {"name": "x", "stages": stages}) == [
        ("value_error", ("stages",), STAGES_ON_UPDATE_REFUSED)
    ]


def test_an_update_without_stages_is_accepted():
    update = RolloutScheduleUpdate.model_validate({"name": "x"})
    assert "stages" not in update.model_fields_set


def test_the_refusal_names_both_stage_routes_and_nothing_from_the_value():
    assert "POST /api/v1/rollout-schedules/{schedule_id}/stages" in (
        STAGES_ON_UPDATE_REFUSED
    )
    assert "PUT /api/v1/rollout-schedules/stages/{stage_id}" in STAGES_ON_UPDATE_REFUSED
    marker = "marker-" + uuid.uuid4().hex
    errors = _errors(RolloutScheduleUpdate, {"stages": [{"name": marker}]})
    assert marker not in str(errors)


def test_stages_on_update_is_kept_and_marked_deprecated():
    # Removing the field would let pydantic drop the key silently (#595's trap).
    schema = RolloutScheduleUpdate.model_json_schema()["properties"]["stages"]
    assert schema["deprecated"] is True
    assert "refused with 422" in schema["description"]


# --- dates --------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "start, end",
    [
        ("2027-01-08T00:00:00Z", "2027-01-09T00:00:00"),
        ("2027-01-08T00:00:00", "2027-01-09T00:00:00Z"),
    ],
    ids=["aware-naive", "naive-aware"],
)
def test_a_zoned_and_an_unzoned_date_are_compared_as_utc(start, end):
    # Before: TypeError (can't compare offset-naive and offset-aware
    # datetimes), which is not a ValidationError: a 500 over HTTP.
    created = RolloutScheduleCreate.model_validate(
        _create(start_date=start, end_date=end)
    )
    assert (created.start_date, created.end_date) == (
        datetime(2027, 1, 8, tzinfo=timezone.utc),
        datetime(2027, 1, 9, tzinfo=timezone.utc),
    )
    inverted = _create(start_date=end, end_date=start)
    assert _errors(RolloutScheduleCreate, inverted) == [("value_error", (), DATE_MSG)]


@pytest.mark.parametrize("model", [RolloutScheduleCreate, RolloutScheduleUpdate])
@pytest.mark.parametrize(
    "start, end",
    [
        ("2027-01-09T00:00:00Z", "2027-01-08T00:00:00Z"),
        ("2027-01-08T08:00:00Z", "2027-01-08T00:00:00-08:00"),  # the same instant
        ("2027-01-08T00:00:00", "2027-01-08T00:00:00Z"),  # the same instant
    ],
    ids=["inverted", "equal-offset", "equal-mixed"],
)
def test_an_end_not_after_the_start_is_refused(model, start, end):
    body = {"start_date": start, "end_date": end}
    if model is RolloutScheduleCreate:
        body = _create(**body)
    assert _errors(model, body) == [("value_error", (), DATE_MSG)]


def test_an_update_with_one_date_is_left_to_the_stored_one():
    update = RolloutScheduleUpdate.model_validate({"end_date": "2020-01-01T00:00:00"})
    assert update.end_date == datetime(2020, 1, 1, tzinfo=timezone.utc)
