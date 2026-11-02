"""The ``event_id`` field of the three event request schemas (#1543, U1 and C9).

``EventRequest`` (``/tracking/track``, and each item of ``/tracking/batch``),
``EventCreate`` (``/tracking/events``) and ``UntaggedEventCreate`` (what
``/track`` and ``/batch`` build for an event with neither key) carry an
optional ``event_id``: a string of at most 255 characters, compared exactly.
``""`` and ``null`` both mean none. A number, a longer string, NUL and a lone
surrogate are refused. Today's schema had no such field, so a body's
``event_id`` was dropped before the route ran.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.schemas.tracking import (
    EventBatchRequest,
    EventBatchResponse,
    EventCreate,
    EventRequest,
    EventResponse,
    UntaggedEventCreate,
)

pytestmark = [pytest.mark.unit]

BUILDERS = {
    "EventRequest": lambda event_id: EventRequest(
        event_type="purchase", user_id="u-1", event_id=event_id
    ),
    "batch item": lambda event_id: EventBatchRequest(
        events=[{"event_type": "purchase", "user_id": "u-1", "event_id": event_id}]
    ).events[0],
    "EventCreate": lambda event_id: EventCreate(
        event_name="purchase",
        user_id="u-1",
        experiment_id="3f2b8c1e-5d4a-4b6f-9e7d-0a1b2c3d4e5f",
        event_id=event_id,
    ),
    "UntaggedEventCreate": lambda event_id: UntaggedEventCreate(
        event_name="purchase", user_id="u-1", event_id=event_id
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("schema", sorted(BUILDERS))
def test_the_event_id_is_kept(schema):
    """U1: the field reaches ``model_dump()``, exactly as sent."""
    dumped = BUILDERS[schema]("Order-1001 ").model_dump()
    assert "event_id" in dumped
    assert dumped["event_id"] == "Order-1001 "


@pytest.mark.parametrize("schema", sorted(BUILDERS))
@pytest.mark.parametrize("event_id", ["", None], ids=["empty", "null"])
def test_empty_and_null_mean_no_event_id(schema, event_id):
    assert BUILDERS[schema](event_id).event_id is None


@pytest.mark.parametrize("schema", sorted(BUILDERS))
def test_an_absent_event_id_is_none(schema):
    builders = {
        "EventRequest": lambda: EventRequest(event_type="purchase", user_id="u-1"),
        "batch item": lambda: EventBatchRequest(
            events=[{"event_type": "purchase", "user_id": "u-1"}]
        ).events[0],
        "EventCreate": lambda: EventCreate(
            event_name="purchase",
            user_id="u-1",
            experiment_id="3f2b8c1e-5d4a-4b6f-9e7d-0a1b2c3d4e5f",
        ),
        "UntaggedEventCreate": lambda: UntaggedEventCreate(
            event_name="purchase", user_id="u-1"
        ),
    }
    assert builders[schema]().event_id is None


@pytest.mark.parametrize("schema", sorted(BUILDERS))
def test_255_characters_of_any_width_are_accepted(schema):
    for event_id in ("x" * 255, "\U0001f600" * 255):
        assert BUILDERS[schema](event_id).event_id == event_id


@pytest.mark.parametrize("schema", sorted(BUILDERS))
@pytest.mark.parametrize(
    "event_id, kind",
    [
        (123, "string_type"),
        (12.5, "string_type"),
        (True, "string_type"),
        ({"id": "x"}, "string_type"),
        ("x" * 256, "string_too_long"),
        ("order\x00-1", "value_error"),
        ("order-\ud800", "value_error"),
    ],
    ids=["integer", "float", "bool", "object", "256-characters", "nul", "surrogate"],
)
def test_an_invalid_event_id_is_refused(schema, event_id, kind):
    with pytest.raises(ValidationError) as caught:
        BUILDERS[schema](event_id)
    [error] = caught.value.errors()
    assert error["loc"][-1] == "event_id"
    assert error["type"] == kind


def test_the_responses_carry_the_new_fields():
    """``EventResponse`` gains ``duplicate`` and ``event_id``;
    ``EventBatchResponse`` gains ``duplicate_count``, always present."""
    assert set(EventResponse.model_fields) >= {"duplicate", "event_id"}
    assert EventResponse.model_fields["duplicate"].default is False
    assert EventResponse.model_fields["event_id"].default is None
    assert EventBatchResponse.model_fields["duplicate_count"].is_required()
    answer = EventBatchResponse(success_count=2, failure_count=0, duplicate_count=1)
    assert answer.model_dump()["duplicate_count"] == 1
