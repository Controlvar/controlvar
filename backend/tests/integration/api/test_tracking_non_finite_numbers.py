"""The tracking routes refuse non-finite numbers and out-of-range values, on PostgreSQL (#1630).

Through the real routes and a real database:

* NaN or Infinity inside ``metadata`` answers 422 ``finite_number`` on
  ``/track``, ``/batch`` and ``/events``, and nothing is written. It used to
  answer 500 on ``/track`` and ``/events`` (PostgreSQL refuses NaN in JSONB),
  and ``/batch`` reported "Could not store this event".
* An event ``value`` outside -1e12 to 1e12 is refused: ``/track`` 422,
  ``/events`` 422 at ``["body", "value"]``, ``/batch`` that entry in
  ``errors`` with the others stored. -1e12 and 1e12 are stored as sent.
* The query ``docs/api/endpoints.md`` gives for events already stored with a
  value outside the range (``STORED_VALUES_QUERY``, quoted byte for byte by
  ``backend/tests/unit/docs/test_non_finite_number_docs.py``) is run here as
  written, on rows seeded by SQL: it finds NaN, both infinities and every value
  past the bound, and nothing else.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.tests.integration.conftest import make_client_for_user
from backend.tests.unit.docs.test_non_finite_number_docs import STORED_VALUES_QUERY

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

JSON = {"Content-Type": "application/json"}
INVALID_VALUE = "Invalid event: value not valid"


@pytest.fixture
def experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    created = make_experiment(
        name=f"Finite numbers {suffix}",
        key=f"finite-numbers-{suffix}",
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


def _values(db_session, experiment) -> List[Any]:
    db_session.expire_all()
    return sorted(
        value
        for (value,) in db_session.query(Event.value).filter(
            Event.experiment_id == experiment.id
        )
    )


def _event(experiment, route: str, **fields: Any) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "event_type": "purchase",
        "user_id": f"u1630-{uuid.uuid4().hex[:10]}",
        **fields,
    }
    if route == "events":
        event["event_name"] = "purchase"
        event["experiment_id"] = str(experiment.id)
    else:
        event["experiment_key"] = experiment.key
    return event


def _send(client, experiment, route: str, **fields: Any):
    """One event on *route*, encoded by Python's own encoder, which writes NaN
    and Infinity as JavaScript does (the test client's refuses them)."""
    event = _event(experiment, route, **fields)
    body = {"events": [event]} if route == "batch" else event
    return client.post(
        f"/api/v1/tracking/{route}", content=json.dumps(body), headers=JSON
    )


# ---------------------------------------------------------------------------
# A non-finite number in metadata
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("route", ["track", "batch", "events"])
@pytest.mark.parametrize("number", [math.nan, math.inf, -math.inf], ids=repr)
def test_a_non_finite_number_in_metadata_answers_422_and_nothing_is_written(
    sdk_client, db_session, experiment, route, number
):
    before = _written(db_session)
    response = _send(sdk_client, experiment, route, metadata={"a": [1, number]})
    assert response.status_code == 422, response.text
    loc = ["body", "metadata", "a", 1]
    if route == "batch":
        loc = ["body", "events", 0, "metadata", "a", 1]
    assert response.json()["detail"] == [
        {"type": "finite_number", "loc": loc, "msg": "Input should be a finite number"}
    ]
    assert _written(db_session) == before


@pytest.mark.parametrize("route", ["track", "batch", "events"])
def test_finite_metadata_is_stored(sdk_client, db_session, experiment, route):
    before = _written(db_session)
    response = _send(
        sdk_client, experiment, route, metadata={"a": [1, 1e308, -0.0], "s": "NaN"}
    )
    assert response.status_code == 200, response.text
    assert _written(db_session)[0] == before[0] + 1


# ---------------------------------------------------------------------------
# The value range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["track", "events"])
def test_the_bound_itself_is_stored_as_sent(sdk_client, db_session, experiment, route):
    for value in (1e12, -1e12):
        response = _send(sdk_client, experiment, route, value=value)
        assert response.status_code == 200, response.text
    assert _values(db_session, experiment) == [-1e12, 1e12]


@pytest.mark.regression
@pytest.mark.parametrize("route", ["track", "events"])
@pytest.mark.parametrize(
    "value", [math.nextafter(1e12, math.inf), -2e12, 1e308, "NaN", "1e400"], ids=repr
)
def test_a_value_outside_the_range_answers_422_and_nothing_is_written(
    sdk_client, db_session, experiment, route, value
):
    before = _written(db_session)
    response = _send(sdk_client, experiment, route, value=value)
    assert response.status_code == 422, response.text
    if route == "track":
        assert response.json() == {"detail": INVALID_VALUE}
    else:
        (error,) = response.json()["detail"]
        assert error["loc"] == ["body", "value"]
    assert _written(db_session) == before


def test_batch_stores_the_entries_within_the_range(sdk_client, db_session, experiment):
    events = [_event(experiment, "batch", value=v) for v in (5, 2e12, 7)]
    response = sdk_client.post("/api/v1/tracking/batch", json={"events": events})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["success_count"], body["failure_count"]) == (2, 1)
    assert [(e["index"], e["error"]) for e in body["errors"]] == [(1, INVALID_VALUE)]
    assert _values(db_session, experiment) == [5.0, 7.0]


def test_one_non_finite_number_refuses_the_whole_batch(
    sdk_client, db_session, experiment
):
    """Unlike a value outside the range, which fails only its own entry."""
    before = _written(db_session)
    events = [_event(experiment, "batch", value=v) for v in (5, math.nan, 7)]
    response = sdk_client.post(
        "/api/v1/tracking/batch", content=json.dumps({"events": events}), headers=JSON
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "events", 1, "value"]
    assert _written(db_session) == before


# ---------------------------------------------------------------------------
# The query for events already stored
# ---------------------------------------------------------------------------

#: (experiment, value) seeded by SQL into a copy of the events table: NaN and
#: the infinities, values past the bound, the bound itself, an ordinary value
#: and NULL. ``None`` is an event with no experiment.
SEEDED = [
    ("A", math.nan),
    ("A", math.inf),
    ("A", 5.0),
    ("A", 1e12),
    ("B", -math.inf),
    ("B", 1e308),
    ("B", -1e12),
    ("B", None),
    (None, 2e12),
]
FOUND = {"A": 2, "B": 2, None: 1}


def test_the_documented_query_finds_the_stored_values_outside_the_range(db_session):
    """Run as written, so on the schema it names, ``experimentation``: a copy
    of the test schema's events table is made there inside a transaction that
    is rolled back."""
    ids = {"A": str(uuid.uuid4()), "B": str(uuid.uuid4()), None: None}
    with db_session.get_bind().connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("CREATE SCHEMA experimentation"))
            conn.execute(
                text(
                    "CREATE TABLE experimentation.events "
                    "(LIKE test_experimentation.events INCLUDING DEFAULTS)"
                )
            )
            for n, (experiment, value) in enumerate(SEEDED):
                conn.execute(
                    text(
                        "INSERT INTO experimentation.events (id, event_type, "
                        "user_id, experiment_id, value, created_at, updated_at) "
                        "VALUES (:id, 'purchase', :user, :experiment, :value, "
                        "'2026-11-05T00:00:00Z', now())"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "user": f"u1630-{n}",
                        "experiment": ids[experiment],
                        "value": value,
                    },
                )
            stored = conn.execute(
                text("SELECT value FROM experimentation.events")
            ).scalars()
            assert sum(1 for v in stored if v is not None and math.isnan(v)) == 1
            rows = conn.execute(text(STORED_VALUES_QUERY)).all()
        finally:
            transaction.rollback()
    names = {key: name for name, key in ids.items()}
    found = {
        names[None if experiment_id is None else str(experiment_id)]: count
        for experiment_id, count in rows
    }
    assert found == FOUND
