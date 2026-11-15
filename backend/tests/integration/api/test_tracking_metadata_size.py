"""Tracking refuses event metadata over 16,384 bytes (#1085).

An event's ``metadata`` (``properties`` too, on ``/tracking/events``) larger
than 16,384 bytes as compact JSON (no spaces, characters not escaped) in UTF-8
is refused before anything is written: ``/track`` and ``/events`` answer 422,
and ``/batch`` reports the entry in ``errors`` and stores the others. Until
#1085 such metadata was stored, up to the 5 MiB body limit. Metadata of 16,384
bytes or fewer is stored as sent.

Every payload is byte-exact (each test asserts the size it sends) and far
under the 5 MiB body limit, so a 413 cannot pass for a refusal; statuses are
asserted exactly. The depth limit's tests are
``test_tracking_edge_timestamp_and_metadata_depth.py``, whose row counter
(``_written``) and nesting builder these reuse.
"""

import json
import uuid
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.app.core.context_limits import compact_size
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.app.services.event_service import EventService
from backend.tests.integration.api.test_tracking_edge_timestamp_and_metadata_depth import (
    _nested,
    _written,
)
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration]

#: The documented limit, written out here on purpose so a change to the
#: constant in the schema fails these tests until the docs follow.
EVENT_METADATA_MAX_BYTES = 16384
N = EVENT_METADATA_MAX_BYTES

#: Each route with each name it takes the metadata under. ``/track`` and
#: ``/batch`` take ``metadata``; ``/events`` takes ``metadata`` or ``properties``.
ROUTE_FIELDS = [
    ("track", "metadata"),
    ("batch", "metadata"),
    ("events", "metadata"),
    ("events", "properties"),
]
ROUTE_FIELD_IDS = [f"{r}-{f}" for r, f in ROUTE_FIELDS]

#: What ``/track`` and ``/batch`` say for a refused metadata, either limit.
INVALID = "Invalid event: properties not valid"


@pytest.fixture
def experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    created = make_experiment(
        name=f"Metadata size {suffix}",
        key=f"metadata-size-{suffix}",
        status=ExperimentStatus.ACTIVE,
    )
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=created.id,
                name=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={},
            )
        )
    db_session.commit()
    db_session.refresh(created)
    yield created
    db_session.rollback()
    db_session.query(Event).filter(Event.experiment_id == created.id).delete()
    db_session.query(Assignment).filter(Assignment.experiment_id == created.id).delete()
    db_session.commit()


@pytest.fixture
def sdk_client(db_session, admin_user):
    """A client that shows a 500 as a response instead of raising."""
    make_client_for_user(db_session, admin_user)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def sized(n: int, fill: str = "a", marker: str = "") -> Dict[str, Any]:
    """``{"k": <text>}`` that is exactly ``n`` bytes as compact UTF-8 JSON,
    its text starting with ``marker``."""
    room = n - compact_size({"k": marker})
    width = len(fill.encode("utf-8"))
    count = room // width
    value = {"k": marker + fill * count + "a" * (room - count * width)}
    assert compact_size(value) == n
    return value


def _marker() -> str:
    return f"MARK{uuid.uuid4().hex}"


def _event(experiment, route: str, user: str, **fields: Any) -> Dict[str, Any]:
    event: Dict[str, Any] = {"event_type": "click", "user_id": user}
    if route == "events":
        event["event_name"] = "click"
        event["experiment_id"] = str(experiment.id)
    else:
        event["experiment_key"] = experiment.key
    event.update(fields)
    return event


def _post(client, route: str, events: List[Dict[str, Any]]):
    if route == "batch":
        return client.post("/api/v1/tracking/batch", json={"events": events})
    (event,) = events
    return client.post(f"/api/v1/tracking/{route}", json=event)


def _user() -> str:
    return f"u1085-{uuid.uuid4().hex[:10]}"


def _stored_metadata(db_session, user: str) -> List[Optional[Dict[str, Any]]]:
    """``event_metadata`` of every committed event of ``user``."""
    with db_session.get_bind().connect() as conn:
        conn.execute(text("SET search_path TO test_experimentation"))
        rows = conn.execute(
            text("SELECT event_metadata FROM events WHERE user_id = :u"),
            {"u": user},
        ).all()
    return [row[0] for row in rows]


def _assert_size_refusal(response, route: str, field: str, user: str) -> None:
    """The exact answer for one event refused for its metadata: on ``/track``
    and ``/batch`` for either limit, on ``/events`` for its size."""
    if route == "batch":
        assert response.status_code == 200, response.text
        assert response.json() == {
            "success_count": 0,
            "failure_count": 1,
            "duplicate_count": 0,
            "errors": [
                {
                    "index": 0,
                    "event_type": "click",
                    "user_id": user,
                    "error": INVALID,
                }
            ],
        }
    elif route == "track":
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": INVALID}
    else:
        assert response.status_code == 422, response.text
        (error,) = response.json()["detail"]
        assert error["type"] == "value_error"
        assert error["loc"] == ["body", field]
        assert error["msg"] == (
            f"Value error, {field} must not be larger than {N} bytes"
        )
        assert "input" not in error


def test_the_limit_in_the_schema_is_the_documented_one():
    from backend.app.schemas.tracking import EVENT_METADATA_MAX_BYTES as in_schema

    assert in_schema == EVENT_METADATA_MAX_BYTES


# ---------------------------------------------------------------------------
# The boundary, on every route and field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route, field", ROUTE_FIELDS, ids=ROUTE_FIELD_IDS)
@pytest.mark.parametrize("n", [N - 1, N], ids=["N-1", "N"])
@pytest.mark.parametrize("fill", ["a", "é"], ids=["ascii", "two-byte"])
def test_metadata_within_the_limit_is_stored_as_sent(
    sdk_client, db_session, experiment, route, field, n, fill
):
    user = _user()
    sent = sized(n, fill=fill)
    before = _written(db_session)
    response = _post(
        sdk_client, route, [_event(experiment, route, user, **{field: sent})]
    )
    assert response.status_code == 200, response.text[:300]
    if route == "batch":
        assert response.json()["success_count"] == 1
    assert _written(db_session)[0] == before[0] + 1
    # JSONB reorders keys and drops whitespace: compared as values.
    assert _stored_metadata(db_session, user) == [sent]


@pytest.mark.regression
@pytest.mark.parametrize("route, field", ROUTE_FIELDS, ids=ROUTE_FIELD_IDS)
def test_metadata_over_the_limit_is_refused_and_nothing_is_stored(
    sdk_client, db_session, experiment, route, field
):
    user = _user()
    marker = _marker()
    sent = sized(N + 1, marker=marker)
    before = _written(db_session)
    response = _post(
        sdk_client, route, [_event(experiment, route, user, **{field: sent})]
    )
    _assert_size_refusal(response, route, field, user)
    assert marker not in response.text
    assert _written(db_session) == before
    assert _stored_metadata(db_session, user) == []


@pytest.mark.regression
@pytest.mark.parametrize("route", ["track", "batch"])
@pytest.mark.parametrize("limit", ["depth", "size"])
def test_track_and_batch_read_the_same_text_for_either_limit(
    sdk_client, db_session, experiment, route, limit
):
    """``/track`` and ``/batch`` name the field, never the limit: the text is
    the same for metadata too deep and too large."""
    user = _user()
    metadata = _nested(65) if limit == "depth" else sized(N + 1)
    before = _written(db_session)
    response = _post(
        sdk_client, route, [_event(experiment, route, user, metadata=metadata)]
    )
    _assert_size_refusal(response, route, "metadata", user)
    assert _written(db_session) == before


# ---------------------------------------------------------------------------
# /events: both fields, the message, depth first, the string form
# ---------------------------------------------------------------------------


def test_events_stores_both_fields_at_the_limit_each(
    sdk_client, db_session, experiment
):
    user = _user()
    properties, metadata = sized(N, marker="P"), sized(N, marker="M")
    before = _written(db_session)
    response = _post(
        sdk_client,
        "events",
        [_event(experiment, "events", user, properties=properties, metadata=metadata)],
    )
    assert response.status_code == 200, response.text[:300]
    assert _written(db_session)[0] == before[0] + 1
    # Only one is stored, and ``properties`` wins: the limit is per field.
    assert _stored_metadata(db_session, user) == [properties]


@pytest.mark.regression
@pytest.mark.parametrize(
    "over, small", [("properties", "metadata"), ("metadata", "properties")]
)
def test_events_refuses_the_field_that_is_over(
    sdk_client, db_session, experiment, over, small
):
    user = _user()
    before = _written(db_session)
    response = _post(
        sdk_client,
        "events",
        [_event(experiment, "events", user, **{over: sized(N + 1), small: {"k": "v"}})],
    )
    _assert_size_refusal(response, "events", over, user)
    assert _written(db_session) == before


@pytest.mark.regression
@pytest.mark.parametrize("field", ["metadata", "properties"])
@pytest.mark.parametrize("as_string", [False, True], ids=["object", "json-string"])
def test_the_events_refusal_names_the_field_and_the_limit_never_the_value(
    sdk_client, db_session, experiment, field, as_string
):
    user = _user()
    marker = _marker()
    value: Any = sized(N + 1, marker=marker)
    if as_string:
        value = json.dumps(value)
    before = _written(db_session)
    response = _post(
        sdk_client, "events", [_event(experiment, "events", user, **{field: value})]
    )
    # The value first: pydantic's message reaches this answer, and no part
    # of what was sent may.
    assert response.status_code == 422, response.text[:300]
    assert marker not in response.text
    _assert_size_refusal(response, "events", field, user)
    assert "16384" in response.text
    assert _written(db_session) == before


@pytest.mark.regression
@pytest.mark.parametrize("field", ["metadata", "properties"])
def test_events_checks_depth_before_size(sdk_client, db_session, experiment, field):
    both = {"wide": "a" * N, "deep": _nested(64)}  # 65 levels, over N bytes
    wide = {"wide": "a" * N, "deep": _nested(63)}  # 64 levels, over N bytes
    assert compact_size(both) > N and compact_size(wide) > N
    before = _written(db_session)
    for value, words in (
        (both, f"{field} must not be nested more than 64 levels deep"),
        (wide, f"{field} must not be larger than {N} bytes"),
    ):
        response = _post(
            sdk_client,
            "events",
            [_event(experiment, "events", _user(), **{field: value})],
        )
        assert response.status_code == 422, response.text[:300]
        (error,) = response.json()["detail"]
        assert error["msg"] == f"Value error, {words}"
    assert _written(db_session) == before


@pytest.mark.regression
@pytest.mark.parametrize("field", ["metadata", "properties"])
def test_events_measures_a_json_string_after_decoding_it(
    sdk_client, db_session, experiment, field
):
    user = _user()
    at = sized(N, fill="é")
    # Escaped (6 bytes a character) and indented: about three times N as sent.
    text_value = json.dumps(at, indent=2)
    assert len(text_value.encode("utf-8")) > 2 * N
    before = _written(db_session)
    response = _post(
        sdk_client,
        "events",
        [_event(experiment, "events", user, **{field: text_value})],
    )
    assert response.status_code == 200, response.text[:300]
    assert _written(db_session)[0] == before[0] + 1
    assert _stored_metadata(db_session, user) == [at]

    over_user = _user()
    response = _post(
        sdk_client,
        "events",
        [_event(experiment, "events", over_user, **{field: json.dumps(sized(N + 1))})],
    )
    _assert_size_refusal(response, "events", field, over_user)
    assert _stored_metadata(db_session, over_user) == []


# ---------------------------------------------------------------------------
# /batch: per entry, never summed
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("position", [0, 1, 2], ids=["first", "middle", "last"])
def test_batch_reports_the_oversize_entry_and_stores_the_others(
    sdk_client, db_session, experiment, position
):
    users = [_user() for _ in range(3)]
    events = [
        _event(experiment, "batch", user, metadata={"page": "/home"}) for user in users
    ]
    events[position]["metadata"] = sized(N + 1)
    before = _written(db_session)
    response = _post(sdk_client, "batch", events)
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert (body["success_count"], body["failure_count"]) == (2, 1)
    assert body["errors"] == [
        {
            "index": position,
            "event_type": "click",
            "user_id": users[position],
            "error": INVALID,
        }
    ]
    assert "Could not store" not in response.text
    assert _written(db_session)[0] == before[0] + 2
    for index, user in enumerate(users):
        expected = [] if index == position else [{"page": "/home"}]
        assert _stored_metadata(db_session, user) == expected


def test_batch_holds_each_entry_to_the_limit_not_their_sum(
    sdk_client, db_session, experiment
):
    users = [_user() for _ in range(3)]
    sent = [sized(N, marker=str(i)) for i in range(3)]
    before = _written(db_session)
    response = _post(
        sdk_client,
        "batch",
        [_event(experiment, "batch", u, metadata=m) for u, m in zip(users, sent)],
    )
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert (body["success_count"], body["failure_count"], body["errors"]) == (
        3,
        0,
        None,
    )
    assert _written(db_session)[0] == before[0] + 3
    for user, metadata in zip(users, sent):
        assert _stored_metadata(db_session, user) == [metadata]


# ---------------------------------------------------------------------------
# A retry of a stored event_id, and events stored before the limit
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("route", ["track", "events", "batch"])
@pytest.mark.parametrize("first", ["small", "oversize-stored-before-the-limit"])
def test_an_oversize_retry_of_a_stored_event_id_is_refused_not_a_duplicate(
    sdk_client, db_session, experiment, route, first
):
    user = _user()
    event_id = f"e1085-{uuid.uuid4().hex}"
    if first == "small":
        stored = {"page": "/home"}
        response = _post(
            sdk_client,
            "track",
            [_event(experiment, "track", user, metadata=stored, event_id=event_id)],
        )
        assert response.status_code == 200, response.text[:300]
    else:
        # As a release before the limit stored it: the service takes a
        # mapping and builds no schema.
        stored = sized(2 * N)
        _, created, _ = EventService(db_session).track_event_once(
            {
                "user_id": user,
                "event_type": "click",
                "event_name": "click",
                "experiment_id": str(experiment.id),
                "properties": stored,
            },
            event_id,
            allow_untagged=False,
        )
        assert created
    before = _written(db_session)

    response = _post(
        sdk_client,
        route,
        [_event(experiment, route, user, metadata=sized(N + 1), event_id=event_id)],
    )
    # Not answered as a duplicate: /batch's duplicate_count is 0.
    _assert_size_refusal(response, route, "metadata", user)
    assert _written(db_session) == before
    # The stored event is still the one stored, unchanged.
    assert _stored_metadata(db_session, user) == [stored]
