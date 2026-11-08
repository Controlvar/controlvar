"""The size limit on an event's metadata, in the schema (#1085).

``EventCreate`` and ``UntaggedEventCreate`` refuse ``metadata`` or
``properties`` larger than 16,384 bytes as compact JSON (no spaces, characters
not escaped) in UTF-8. Depth is checked first; each field is held on its own;
a JSON-encoded string is measured after it is decoded. Every payload here is
byte-exact: each test asserts the size it builds before it sends it.
``backend/tests/integration/api/test_tracking_metadata_size.py`` holds the
same rules on the three routes.
"""

import json
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from backend.app.core.context_limits import compact_size
from backend.app.schemas.tracking import (
    EVENT_METADATA_MAX_BYTES,
    EVENT_METADATA_MAX_DEPTH,
    EventCreate,
    UntaggedEventCreate,
)

pytestmark = [pytest.mark.unit]

#: The documented limit, written out on purpose: a change to the constant
#: fails here until this file, the integration tests and the pages follow.
DOCUMENTED_MAX_BYTES = 16384

N = EVENT_METADATA_MAX_BYTES
FIELDS = ["properties", "metadata"]
MODELS = [EventCreate, UntaggedEventCreate]

#: The fields every event here carries besides its metadata. ``EventCreate``
#: needs an experiment or a flag; ``UntaggedEventCreate`` takes it too.
BASE: Dict[str, Any] = {
    "event_name": "click",
    "user_id": "u1085",
    "experiment_id": "00000000-0000-4000-8000-000000000001",
}


def sized(n: int, fill: str = "a", pad: str = "a") -> Dict[str, Any]:
    """``{"k": <text>}`` that is exactly ``n`` bytes as compact UTF-8 JSON.

    The text is as many ``fill`` characters as fit, then ``pad`` (one byte)
    for the rest, so a multi-byte ``fill`` still lands on ``n`` exactly.
    """
    room = n - compact_size({"k": ""})
    width = len(fill.encode("utf-8"))
    count = room // width
    value = {"k": fill * count + pad * (room - count * width)}
    assert compact_size(value) == n
    return value


def nested(levels: int, leaf: Any = 1) -> Any:
    value = leaf
    for _ in range(levels):
        value = {"a": value}
    return value


def build(model, field: str, value: Any):
    return model(**BASE, **{field: value})


def refusal(model, field: str, value: Any) -> Dict[str, Any]:
    with pytest.raises(ValidationError) as caught:
        build(model, field, value)
    errors = caught.value.errors()
    assert len(errors) == 1, errors
    return errors[0]


def size_message(field: str) -> str:
    return f"Value error, {field} must not be larger than {DOCUMENTED_MAX_BYTES} bytes"


def test_the_limit_in_the_schema_is_the_documented_one():
    assert EVENT_METADATA_MAX_BYTES == DOCUMENTED_MAX_BYTES


# ---------------------------------------------------------------------------
# Row 1 and 2: N-1 and N are accepted, N+1 refused, on each field of each model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("n", [0, N - 1, N], ids=["empty", "N-1", "N"])
def test_metadata_up_to_the_limit_is_accepted_as_sent(model, field, n):
    value = {} if n == 0 else sized(n)
    event = build(model, field, value)
    assert getattr(event, field) == value


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
@pytest.mark.parametrize("field", FIELDS)
def test_metadata_over_the_limit_is_refused_naming_the_field(model, field):
    error = refusal(model, field, sized(N + 1))
    assert error["loc"] == (field,)
    assert error["msg"] == size_message(field)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_absent_and_null_metadata_are_accepted(model):
    event = model(**BASE)
    assert event.properties is None and event.metadata is None
    event = model(**BASE, properties=None, metadata=None)
    assert event.properties is None and event.metadata is None


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_each_field_is_held_on_its_own_not_their_sum(model):
    # Both at the limit: 2N between them, each within it.
    event = model(**BASE, properties=sized(N), metadata=sized(N))
    assert compact_size(event.properties) == compact_size(event.metadata) == N
    # One over, the other small: only that one is refused.
    for over, small in (("properties", "metadata"), ("metadata", "properties")):
        with pytest.raises(ValidationError) as caught:
            model(**BASE, **{over: sized(N + 1), small: {"k": "v"}})
        assert [e["loc"] for e in caught.value.errors()] == [(over,)]


# ---------------------------------------------------------------------------
# Rows 3 and 4: bytes of compact, unescaped JSON
# ---------------------------------------------------------------------------

#: A character of each UTF-8 width above one byte.
WIDE = [("e-acute", "é", 2), ("euro", "€", 3), ("emoji", "\U0001f600", 4)]


@pytest.mark.parametrize("name, char, width", WIDE, ids=[w[0] for w in WIDE])
def test_the_limit_counts_bytes_not_characters(name, char, width):
    over = sized(N + 1, fill=char)
    text = json.dumps(over, separators=(",", ":"), ensure_ascii=False)
    # Far fewer characters than the limit, one byte over it.
    assert len(text) < N
    assert len(text.encode("utf-8")) == N + 1
    error = refusal(UntaggedEventCreate, "properties", over)
    assert error["msg"] == size_message("properties")


@pytest.mark.parametrize("name, char, width", WIDE, ids=[w[0] for w in WIDE])
def test_the_limit_reads_characters_unescaped_and_without_spaces(name, char, width):
    at = sized(N, fill=char)
    # Escaped (\uXXXX) or with spaces, the same value would be far over N.
    assert len(json.dumps(at).encode("utf-8")) > N
    event = build(UntaggedEventCreate, "properties", at)
    assert event.properties == at


def test_many_small_keys_are_measured_as_one_compact_object():
    value: Dict[str, Any] = {}
    i = 0
    while compact_size(value) < N - 40:
        value[f"key{i:05d}"] = i
        i += 1
    remaining = N - compact_size(value) - len(',"pad":""')
    value["pad"] = "x" * remaining
    assert compact_size(value) == N
    build(UntaggedEventCreate, "properties", value)
    value["pad"] += "x"
    assert compact_size(value) == N + 1
    refusal(UntaggedEventCreate, "properties", value)


# ---------------------------------------------------------------------------
# Row 5: depth before size, each with its own message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", FIELDS)
def test_a_value_over_both_limits_gets_the_depth_message(field):
    # The object is level 1 and ``deep`` adds 64: 65 levels, and N+ bytes wide.
    value = {"wide": "a" * N, "deep": nested(EVENT_METADATA_MAX_DEPTH)}
    assert compact_size(value) > N
    error = refusal(UntaggedEventCreate, field, value)
    assert error["msg"] == (
        f"Value error, {field} must not be nested more than 64 levels deep"
    )


@pytest.mark.parametrize("field", FIELDS)
def test_a_value_over_the_size_only_gets_the_size_message(field):
    value = {"wide": "a" * N, "deep": nested(EVENT_METADATA_MAX_DEPTH - 1)}
    assert compact_size(value) > N
    error = refusal(UntaggedEventCreate, field, value)
    assert error["msg"] == size_message(field)


# ---------------------------------------------------------------------------
# Row 7: a JSON-encoded string is measured after it is decoded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", FIELDS)
def test_a_json_string_is_measured_decoded(field):
    at = sized(N, fill="é")
    # Escaped as é (6 bytes each) and indented: about three times N.
    text = json.dumps(at, indent=2)
    assert len(text.encode("utf-8")) > 2 * N
    event = build(UntaggedEventCreate, field, text)
    assert getattr(event, field) == at

    over = json.dumps(sized(N + 1))
    error = refusal(UntaggedEventCreate, field, over)
    assert error["loc"] == (field,)
    assert error["msg"] == size_message(field)
