"""``POST /tracking/events`` answers 404 for an id that names nothing (#400).

The route takes stored ids directly. An ``experiment_id``, ``feature_flag_id``
or ``variant_id`` that is a well-formed UUID naming no stored row used to go
to the database, which refused the row: a 500 carrying the fixed "Could not
store the event" sentence, indistinguishable from an outage. It now answers
404 naming each field that was not found, never its value, and stores
nothing.

What does not change: an id that is not a UUID still answers 422, before any
lookup; an event whose ids all name stored rows is stored as before. The
check refuses no more than the database did. Like every other tracking route
it looks rows up across the deployment, with no owner or workspace rule, so a
key whose owner owns none of the rows still records the event, and a UUID
that names a row of another kind answers exactly what a random one does.

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

import uuid
from typing import Callable, Dict

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.security import hash_api_key
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.tests.integration.conftest import make_client_for_user
from backend.tests.integration.helpers import unique_flag_key

pytestmark = [pytest.mark.integration, pytest.mark.regression]

URL = "/api/v1/tracking/events"

#: The 404 sentence for each id field, in the order the detail lists them.
NOT_FOUND = {
    "experiment_id": "No experiment has that experiment_id.",
    "feature_flag_id": "No feature flag has that feature_flag_id.",
    "variant_id": "No variant has that variant_id.",
}

#: The 422 an id that is not a UUID answers, before and after #400.
NOT_A_UUID = "Invalid event: badly formed hexadecimal UUID string"

#: Response fields whose value is made by the server for each request.
VOLATILE = frozenset({"id", "timestamp", "created_at", "updated_at"})


def _user() -> str:
    return f"u400-{uuid.uuid4().hex[:10]}"


def _stored(db_session, user_id: str) -> int:
    db_session.rollback()
    return db_session.query(Event).filter(Event.user_id == user_id).count()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def experiment(db_session, make_experiment):
    """An ACTIVE experiment with a control and a treatment variant."""
    suffix = uuid.uuid4().hex[:8]
    created = make_experiment(
        name=f"Unknown ids {suffix}",
        key=f"unknown-ids-{suffix}",
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
    db_session.commit()


@pytest.fixture
def variant(db_session, experiment) -> Variant:
    return (
        db_session.query(Variant)
        .filter(Variant.experiment_id == experiment.id)
        .order_by(Variant.name)
        .first()
    )


@pytest.fixture
def flag(db_session, make_feature_flag):
    created = make_feature_flag(
        key=unique_flag_key("unknownids"),
        name="Unknown ids flag",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=25,
    )
    yield created
    db_session.rollback()
    db_session.query(Event).filter(Event.feature_flag_id == created.id).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == created.id).delete()
    db_session.commit()


@pytest.fixture
def sdk_client(db_session, viewer_user):
    """A client sending a real API key owned by a viewer who owns no rows.

    The API-key dependency is not overridden: the key is looked up as an SDK's
    would be. A 500 comes back as a response instead of being raised.
    """
    raw = f"k400-{uuid.uuid4().hex}"
    key = APIKey(
        key=hash_api_key(raw),
        name=f"unknown-ids-{uuid.uuid4().hex[:6]}",
        is_active=True,
        user_id=viewer_user.id,
    )
    db_session.add(key)
    db_session.commit()
    make_client_for_user(db_session, viewer_user)
    app.dependency_overrides.pop(deps.get_api_key, None)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            c.headers.update({"X-API-Key": raw})
            yield c
    finally:
        app.dependency_overrides.clear()
        db_session.rollback()
        db_session.query(APIKey).filter(APIKey.id == key.id).delete()
        db_session.commit()


def _event(user_id: str, **ids) -> Dict[str, object]:
    return {
        "event_type": "click",
        "event_name": "click",
        "user_id": user_id,
        **{field: str(value) for field, value in ids.items()},
    }


# ---------------------------------------------------------------------------
# An id that names nothing: 404 naming the field, nothing stored
# ---------------------------------------------------------------------------

#: For each field, the ids of a request in which only that field names nothing.
ONE_UNKNOWN: Dict[str, Callable[[Experiment, FeatureFlag, uuid.UUID], dict]] = {
    "experiment_id": lambda e, f, missing: {"experiment_id": missing},
    "feature_flag_id": lambda e, f, missing: {"feature_flag_id": missing},
    "variant_id": lambda e, f, missing: {"experiment_id": e.id, "variant_id": missing},
}


@pytest.mark.parametrize("field", list(ONE_UNKNOWN))
def test_an_id_that_names_nothing_answers_404_naming_the_field(
    sdk_client: TestClient, db_session, experiment, flag, field: str
) -> None:
    user_id = _user()
    missing = uuid.uuid4()

    resp = sdk_client.post(
        URL, json=_event(user_id, **ONE_UNKNOWN[field](experiment, flag, missing))
    )

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": NOT_FOUND[field]}
    assert str(missing) not in resp.text
    assert _stored(db_session, user_id) == 0


def test_every_field_that_names_nothing_is_named(
    sdk_client: TestClient, db_session
) -> None:
    user_id = _user()

    resp = sdk_client.post(
        URL,
        json=_event(
            user_id,
            experiment_id=uuid.uuid4(),
            feature_flag_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
        ),
    )

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": " ".join(NOT_FOUND.values())}
    assert _stored(db_session, user_id) == 0


def test_an_id_naming_a_row_of_another_kind_answers_what_a_random_one_does(
    sdk_client: TestClient, db_session, experiment, variant, flag
) -> None:
    """A UUID that is some other row's id answers the same bytes as one that is
    nobody's: the answer depends on the field's own table only."""
    user_id = _user()

    other_kind = sdk_client.post(
        URL,
        json=_event(
            user_id,
            experiment_id=variant.id,
            feature_flag_id=experiment.id,
            variant_id=flag.id,
        ),
    )
    random_ids = sdk_client.post(
        URL,
        json=_event(
            user_id,
            experiment_id=uuid.uuid4(),
            feature_flag_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
        ),
    )

    assert other_kind.status_code == random_ids.status_code == 404
    assert other_kind.content == random_ids.content
    assert _stored(db_session, user_id) == 0


# ---------------------------------------------------------------------------
# What does not change
# ---------------------------------------------------------------------------


def test_an_event_whose_ids_all_exist_answers_as_before(
    admin_client: TestClient, db_session, experiment, variant, flag
) -> None:
    user_id = _user()

    resp = admin_client.post(
        URL,
        json={
            **_event(
                user_id,
                experiment_id=experiment.id,
                feature_flag_id=flag.id,
                variant_id=variant.id,
            ),
            "value": 2.5,
            "properties": {"plan": "pro"},
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == VOLATILE | {
        "event_type",
        "event_name",
        "user_id",
        "session_id",
        "experiment_id",
        "feature_flag_id",
        "variant_id",
        "value",
        "properties",
        "metadata",
        # #1543: whether it was already stored, and the event_id sent (none).
        "duplicate",
        "event_id",
    }
    assert {k: v for k, v in body.items() if k not in VOLATILE} == {
        "event_name": "click",
        "event_type": "click",
        "experiment_id": str(experiment.id),
        "feature_flag_id": str(flag.id),
        "metadata": None,
        "properties": {"plan": "pro"},
        "session_id": None,
        "user_id": user_id,
        "value": 2.5,
        "variant_id": str(variant.id),
        "duplicate": False,
        "event_id": None,
    }
    db_session.rollback()
    row = db_session.query(Event).filter(Event.id == uuid.UUID(body["id"])).one()
    assert (row.experiment_id, row.feature_flag_id, row.variant_id) == (
        experiment.id,
        flag.id,
        variant.id,
    )


def test_a_key_whose_owner_owns_none_of_the_rows_still_records_the_event(
    sdk_client: TestClient, db_session, experiment, variant, flag
) -> None:
    """No owner or workspace rule: the check refuses only what the database
    refused before it."""
    user_id = _user()

    resp = sdk_client.post(
        URL,
        json=_event(
            user_id,
            experiment_id=experiment.id,
            feature_flag_id=flag.id,
            variant_id=variant.id,
        ),
    )

    assert resp.status_code == 200, resp.text
    assert _stored(db_session, user_id) == 1


@pytest.mark.parametrize("field", list(NOT_FOUND))
def test_an_id_that_is_not_a_uuid_still_answers_422(
    sdk_client: TestClient, db_session, experiment, field: str
) -> None:
    user_id = _user()
    ids = {"experiment_id": experiment.id, field: "not-a-uuid"}

    resp = sdk_client.post(URL, json=_event(user_id, **ids))

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": NOT_A_UUID}
    assert _stored(db_session, user_id) == 0


def test_an_id_that_is_not_a_uuid_answers_422_before_any_lookup(
    sdk_client: TestClient, db_session
) -> None:
    """A request with both a malformed id and one naming nothing gets the 422."""
    user_id = _user()

    resp = sdk_client.post(
        URL,
        json=_event(user_id, experiment_id=uuid.uuid4(), variant_id="not-a-uuid"),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": NOT_A_UUID}
    assert _stored(db_session, user_id) == 0
