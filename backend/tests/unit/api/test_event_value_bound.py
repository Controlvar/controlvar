"""An event ``value`` must be a finite number between -1e12 and 1e12 (#1630).

``BoundedEventValue`` (``backend/app/schemas/tracking.py``) on the models an
event is built from before it is stored. Through ``backend.app.main.app``,
with a database that finds nothing and an event store that records what it
would store (as in ``test_non_finite_numbers_refused.py``):

* ``/tracking/track``: 422 ``Invalid event: value not valid``, nothing stored;
* ``/tracking/events``: 422 at ``["body", "value"]``, nothing stored;
* ``/tracking/batch``: that entry is reported in ``errors`` and the others are
  stored, unlike a non-finite number in the JSON, which refuses the whole body.

-1e12 and 1e12 themselves are accepted. A value sent as a string that reads
as a number (``"NaN"``, ``"1e400"``) is held to the same bound. What a stored
event's value is, on PostgreSQL, is in
``backend/tests/integration/api/test_tracking_non_finite_numbers.py``.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator, List

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.api import deps
from backend.app.main import app
from backend.app.schemas import tracking
from backend.app.schemas.tracking import (
    VALUE_BOUND,
    VALUE_BOUND_MESSAGE,
    EventCreate,
    EventResponse,
    UntaggedEventCreate,
)
from backend.app.services import analysis_service
from backend.app.services.event_service import EventService
from backend.tests.unit.api.test_non_finite_numbers_refused import _Session

pytestmark = [pytest.mark.unit]

UUID = "00000000-0000-4000-8000-000000000001"
INVALID_VALUE = "Invalid event: value not valid"

#: Accepted: the bound itself, both signs, and what is well inside it.
ACCEPTED = [1e12, -1e12, 0.0, -0.0, 49.99, -10.0, 999_999_999_999]
#: Refused: one step past the bound either way, and far past it.
BEYOND = [
    math.nextafter(1e12, math.inf),
    math.nextafter(-1e12, -math.inf),
    2e12,
    -2000000000000.5,
    1e308,
]
#: Refused: strings the float field reads as a number past the bound or not
#: finite. The JSON check does not see them (they are strings in the body).
QUOTED = ['"NaN"', '"Infinity"', '"-Infinity"', '"1e400"', '"2e12"']


@pytest.fixture
def stored(monkeypatch) -> List[Any]:
    events: List[Any] = []

    def track_event_once(self, event_data, event_id, *, allow_untagged):
        event = EventService.build_event(event_data, allow_untagged=allow_untagged)
        event.id = uuid.uuid4()
        event.updated_at = datetime.now(timezone.utc)
        events.append(event)
        return event.id, True, event

    monkeypatch.setattr(EventService, "track_event_once", track_event_once)
    monkeypatch.setattr(EventService, "unknown_id_fields", lambda self, data: [])
    return events


@pytest.fixture
def client(stored) -> Iterator[TestClient]:
    def session() -> Iterator[_Session]:
        yield _Session()

    app.dependency_overrides[deps.get_db] = session
    app.dependency_overrides[deps.get_api_key] = lambda: None
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def _track(client, raw_value: str):
    body = '{"event_type":"purchase","user_id":"u-1630","value":' + raw_value + "}"
    return client.post(
        "/api/v1/tracking/track",
        content=body,
        headers={"Content-Type": "application/json"},
    )


def _by_ids(client, raw_value: str):
    body = (
        '{"event_type":"purchase","event_name":"purchase","user_id":"u-1630",'
        f'"experiment_id":"{UUID}","value":{raw_value}}}'
    )
    return client.post(
        "/api/v1/tracking/events",
        content=body,
        headers={"Content-Type": "application/json"},
    )


@pytest.mark.parametrize("value", ACCEPTED, ids=repr)
def test_a_value_within_the_bound_is_stored(client, stored, value):
    response = _track(client, json.dumps(value))
    assert response.status_code == 200, response.text
    assert [e.value for e in stored] == [value]
    response = _by_ids(client, json.dumps(value))
    assert response.status_code == 200, response.text
    assert [e.value for e in stored] == [value, value]


@pytest.mark.regression
@pytest.mark.parametrize("value", BEYOND, ids=repr)
def test_track_refuses_a_value_beyond_the_bound(client, stored, value):
    response = _track(client, repr(value))
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": INVALID_VALUE}
    assert stored == []


@pytest.mark.parametrize("raw", QUOTED)
def test_track_refuses_a_quoted_number_beyond_the_bound(client, stored, raw):
    response = _track(client, raw)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": INVALID_VALUE}
    assert stored == []


@pytest.mark.parametrize("raw", [repr(v) for v in BEYOND] + QUOTED)
def test_events_refuses_a_value_beyond_the_bound_at_the_field(client, stored, raw):
    response = _by_ids(client, raw)
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "value"]
    assert error["type"] == "value_error"
    assert VALUE_BOUND_MESSAGE in error["msg"]
    assert stored == []


def test_batch_reports_the_entry_beyond_the_bound_and_stores_the_others(client, stored):
    """[5, 2e12, 7]: the middle entry fails alone."""
    events = [
        {"event_type": "purchase", "user_id": f"u-1630-{i}", "value": v}
        for i, v in enumerate([5, 2e12, 7])
    ]
    response = client.post("/api/v1/tracking/batch", json={"events": events})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["success_count"], body["failure_count"]) == (2, 1)
    assert [(e["index"], e["error"]) for e in body["errors"]] == [(1, INVALID_VALUE)]
    assert [e.value for e in stored] == [5.0, 7.0]


def test_a_null_or_absent_value_is_accepted(client, stored):
    assert _track(client, "null").status_code == 200
    response = client.post(
        "/api/v1/tracking/track", json={"event_type": "purchase", "user_id": "u-1630"}
    )
    assert response.status_code == 200, response.text
    assert [e.value for e in stored] == [None, None]


# ---------------------------------------------------------------------------
# The schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [EventCreate, UntaggedEventCreate])
@pytest.mark.parametrize("value", BEYOND + [math.nan, math.inf, -math.inf], ids=repr)
def test_each_stored_event_model_refuses_the_value(model, value):
    with pytest.raises(ValidationError) as raised:
        model(event_name="e", user_id="u", experiment_id=UUID, value=value)
    assert [e["loc"] for e in raised.value.errors()] == [("value",)]


@pytest.mark.parametrize("model", [EventCreate, UntaggedEventCreate])
@pytest.mark.parametrize("value", ACCEPTED, ids=repr)
def test_each_stored_event_model_accepts_the_bound(model, value):
    assert (
        model(event_name="e", user_id="u", experiment_id=UUID, value=value).value
        == value
    )


def test_the_response_model_is_not_bounded():
    """An event stored before the bound still reads back."""
    stored = EventResponse(
        id=UUID,
        event_name="e",
        user_id="u",
        value=2e12,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert stored.value == 2e12


def test_ingest_and_analysis_read_one_constant():
    assert VALUE_BOUND == 1e12
    assert analysis_service.VALUE_BOUND is tracking.VALUE_BOUND
