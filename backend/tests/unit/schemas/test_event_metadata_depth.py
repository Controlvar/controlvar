"""The depth walk behind the event metadata limit, and the edge timestamp (#1684, #1685)."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.models.event import normalize_event_timestamp
from backend.app.schemas.tracking import (
    EVENT_METADATA_MAX_DEPTH,
    UntaggedEventCreate,
    exceeds_nesting_depth,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def _nested(levels: int):
    value = 1
    for _ in range(levels):
        value = {"a": value}
    return value


def test_the_limit_counts_the_object_itself_as_level_one():
    assert not exceeds_nesting_depth({"a": 1}, 1)
    assert exceeds_nesting_depth({"a": {"b": 1}}, 1)
    assert not exceeds_nesting_depth({"a": [1, 2]}, 2)
    assert exceeds_nesting_depth({"a": [[1]]}, 2)
    assert not exceeds_nesting_depth(None, 1)


def test_a_wide_flat_object_is_not_deep():
    assert not exceeds_nesting_depth({str(i): i for i in range(10_000)}, 1)


def test_a_very_deep_value_is_walked_without_recursion():
    # Far deeper than the interpreter's recursion limit: a recursive walk
    # would raise RecursionError here.
    assert exceeds_nesting_depth(_nested(50_000), EVENT_METADATA_MAX_DEPTH)


def test_the_schema_refuses_one_level_over_and_accepts_the_limit():
    base = {"event_name": "click", "user_id": "u"}
    UntaggedEventCreate(**base, properties=_nested(EVENT_METADATA_MAX_DEPTH))
    with pytest.raises(ValidationError) as caught:
        UntaggedEventCreate(**base, properties=_nested(EVENT_METADATA_MAX_DEPTH + 1))
    assert "properties must not be nested more than 64 levels deep" in str(caught.value)


@pytest.mark.parametrize(
    "moment",
    [
        datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-1))),
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        "9999-12-31T23:59:59-01:00",
        "0001-01-01T00:00:00+01:00",
    ],
)
def test_an_edge_timestamp_past_the_range_is_a_value_error(moment):
    with pytest.raises(ValueError, match="timestamp"):
        normalize_event_timestamp(moment)


def test_an_edge_timestamp_inside_the_range_is_normalised():
    assert (
        normalize_event_timestamp("9999-12-31T23:59:59+01:00")
        == "9999-12-31T22:59:59+00:00"
    )
