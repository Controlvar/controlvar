"""Tracking refuses an edge timestamp and over-deep metadata with 422 (#1684, #1685).

#1684: a ``timestamp`` at the very edge of the date range, with an offset that
pushes it past the range once it is converted to UTC, passed request validation
and then raised ``OverflowError`` while the server normalised it. The handlers
catch ``ValueError``, so ``/track`` and ``/events`` answered 500 and ``/batch``
reported "Could not store this event". Nothing was stored.

#1685: event ``metadata`` had no nesting limit. Nested deeper than about 255
levels, ``/track`` and ``/events`` stored the event and then answered 500 while
building the response, and ``/batch`` stored it and answered 200.

Now each is refused before anything is written: 422 on ``/track`` and
``/events``, an invalid-event item on ``/batch``. The same values one step
inside the range, and metadata exactly at the limit, are stored as before.
"""

import uuid
from typing import Any, Dict, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.regression]

#: The documented limit (docs/api/endpoints.md), written out here on purpose so
#: a change to the constant in the schema fails these tests until the docs follow.
EVENT_METADATA_MAX_DEPTH = 64

#: The two timestamps of #1684: valid as sent, past the range once in UTC.
EDGE_TIMESTAMPS = [
    ("max-minus-one-hour", "9999-12-31T23:59:59-01:00"),
    ("min-plus-one-hour", "0001-01-01T00:00:00+01:00"),
]

#: The same edges with an offset that stays in range: still stored.
IN_RANGE_TIMESTAMPS = [
    ("max-utc", "9999-12-31T23:59:59Z"),
    ("max-plus-one-hour", "9999-12-31T23:59:59+01:00"),
    ("min-utc", "0001-01-01T00:00:00Z"),
    ("min-minus-one-hour", "0001-01-01T00:00:00-01:00"),
]

ROUTES = ["track", "events", "batch"]


@pytest.fixture
def experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    created = make_experiment(
        name=f"Edge input {suffix}",
        key=f"edge-input-{suffix}",
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


def _written(db_session) -> Tuple[int, ...]:
    """Row counts of the tables a tracking route writes, committed only."""
    with db_session.get_bind().connect() as conn:
        conn.execute(text("SET search_path TO test_experimentation"))
        return tuple(
            conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in ("events", "event_dedup_keys")
        )


def _nested(levels: int, leaf: Any = 1, *, as_list: bool = False) -> Any:
    """An object (or array) that is ``levels`` levels deep, built iteratively."""
    value: Any = leaf
    for _ in range(levels):
        value = [value] if as_list else {"a": value}
    return value


def _send(
    client, experiment, route: str, *, timestamp=None, metadata=None
) -> Tuple[Any, Dict[str, Any]]:
    """Send one event on ``route``; return the response and the body sent."""
    user = f"u1684-{uuid.uuid4().hex[:10]}"
    event: Dict[str, Any] = {"event_type": "click", "user_id": user}
    if route == "events":
        event["event_name"] = "click"
        event["experiment_id"] = str(experiment.id)
    else:
        event["experiment_key"] = experiment.key
    if timestamp is not None:
        event["timestamp"] = timestamp
    if metadata is not None:
        event["metadata"] = metadata
    if route == "batch":
        return client.post("/api/v1/tracking/batch", json={"events": [event]}), event
    return client.post(f"/api/v1/tracking/{route}", json=event), event


def _assert_refused(response, route: str, field: str) -> None:
    """The answer a refused event gets: 422 / an invalid item, naming ``field``."""
    if route == "batch":
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success_count"] == 0
        assert body["failure_count"] == 1
        message = body["errors"][0]["error"]
        assert "Could not store" not in message, message
    else:
        assert response.status_code == 422, response.text
        message = response.text
        assert "Could not store" not in message
    assert field in message, message


# ---------------------------------------------------------------------------
# #1684: the edge timestamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "label, stamp", EDGE_TIMESTAMPS, ids=[i for i, _ in EDGE_TIMESTAMPS]
)
def test_edge_timestamp_is_refused_and_nothing_is_stored(
    sdk_client, db_session, experiment, route, label, stamp
):
    before = _written(db_session)
    response, _ = _send(sdk_client, experiment, route, timestamp=stamp)
    _assert_refused(response, route, "timestamp")
    assert _written(db_session) == before


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "label, stamp", IN_RANGE_TIMESTAMPS, ids=[i for i, _ in IN_RANGE_TIMESTAMPS]
)
def test_the_same_edges_in_range_are_still_stored(
    sdk_client, db_session, experiment, route, label, stamp
):
    before = _written(db_session)
    response, _ = _send(sdk_client, experiment, route, timestamp=stamp)
    assert response.status_code == 200, response.text
    if route == "batch":
        assert response.json()["success_count"] == 1
    assert _written(db_session)[0] == before[0] + 1


# ---------------------------------------------------------------------------
# #1685: the metadata depth
# ---------------------------------------------------------------------------


def test_the_limit_in_the_schema_is_the_documented_one():
    from backend.app.schemas.tracking import (
        EVENT_METADATA_MAX_DEPTH as in_schema,
    )

    assert in_schema == EVENT_METADATA_MAX_DEPTH


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("as_list", [False, True], ids=["objects", "arrays"])
@pytest.mark.parametrize(
    "levels", [EVENT_METADATA_MAX_DEPTH + 1, 300], ids=["just-over", "well-over"]
)
def test_over_deep_metadata_is_refused_and_nothing_is_stored(
    sdk_client, db_session, experiment, route, as_list, levels
):
    # An array cannot be the metadata itself (it is an object), so the arrays
    # sit under one key: that key's object is level 1, the arrays below it the rest.
    metadata = {"a": _nested(levels - 1, as_list=True)} if as_list else _nested(levels)
    before = _written(db_session)
    response, _ = _send(sdk_client, experiment, route, metadata=metadata)
    # /track and /batch build the stored event, which calls the field
    # ``properties``; /events takes it as sent.
    _assert_refused(response, route, "properties" if route != "events" else "metadata")
    assert _written(db_session) == before


@pytest.mark.parametrize("route", ROUTES)
def test_metadata_at_the_limit_is_stored(sdk_client, db_session, experiment, route):
    before = _written(db_session)
    response, _ = _send(
        sdk_client,
        experiment,
        route,
        metadata=_nested(EVENT_METADATA_MAX_DEPTH),
    )
    assert response.status_code == 200, response.text
    if route == "batch":
        assert response.json()["success_count"] == 1
    assert _written(db_session)[0] == before[0] + 1


def test_events_route_refuses_over_deep_properties_too(
    sdk_client, db_session, experiment
):
    before = _written(db_session)
    response = sdk_client.post(
        "/api/v1/tracking/events",
        json={
            "event_type": "click",
            "event_name": "click",
            "user_id": f"u1685-{uuid.uuid4().hex[:10]}",
            "experiment_id": str(experiment.id),
            "properties": _nested(EVENT_METADATA_MAX_DEPTH + 1),
        },
    )
    assert response.status_code == 422, response.text
    assert "properties must not be nested more than 64 levels deep" in response.text
    assert _written(db_session) == before


def test_one_over_deep_item_does_not_fail_its_batch(sdk_client, db_session, experiment):
    before = _written(db_session)
    good = {
        "event_type": "click",
        "user_id": f"u1685-{uuid.uuid4().hex[:10]}",
        "experiment_key": experiment.key,
        "metadata": {"page": "/home"},
    }
    bad = dict(good, metadata=_nested(EVENT_METADATA_MAX_DEPTH + 1))
    response = sdk_client.post("/api/v1/tracking/batch", json={"events": [good, bad]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["success_count"], body["failure_count"]) == (1, 1)
    assert body["errors"][0]["index"] == 1
    assert _written(db_session)[0] == before[0] + 1
