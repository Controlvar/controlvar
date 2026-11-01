"""Metric names on experiment create and update (#1461).

Create and update give one answer for the same metric list:

* a name that repeats answers 422 at ``["body", "metrics"]`` with a fixed
  message naming the 0-based positions of the first repeated pair, never the
  name. Create used to answer 400 (the database's unique index refused it);
  update answered 500, and its metrics stayed as they were;
* names are compared exactly, as that unique index compares them: case and
  spaces count, so ``Revenue`` and ``revenue`` are two metrics, as before;
* ``metrics: []`` on update answers 422 ``too_short``, as on create, where it
  removed every metric; ``metrics`` left out or null leaves them unchanged;
* the wizard's submit, which builds the same create schema, answers
  ``success: false`` with the message;
* the order is 401, then the schema's 422, then the role's 403 or the
  missing experiment's 404, then the experiment's state (400): a non-draft
  experiment sent ``[]`` or a repeat answers 422, for a superuser too.

Every request is a real one with a local access token, so the order is the
application's own and not an override's. Expected messages are written out
here, not imported, so a change to the text fails a test.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.experiment import Experiment, Metric
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration]

BASE = "/api/v1/experiments"
WIZARD = "/api/v1/wizard"

#: The 422 message for a repeated name, as the UX text gives it.
DUPLICATE = (
    "duplicate metric name; each name may appear once per experiment "
    "(first seen at metrics[{first}], repeated at metrics[{second}])"
)

#: Create's answer for ``metrics: []`` before and after #1461; update's now.
TOO_SHORT = {
    "type": "too_short",
    "loc": ["body", "metrics"],
    "msg": "List should have at least 1 item after validation, not 0",
    "ctx": {"field_type": "List", "min_length": 1, "actual_length": 0},
}

TWO_VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
]


def _duplicate(first: int, second: int) -> Dict[str, Any]:
    return {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "metrics"],
                "msg": DUPLICATE.format(first=first, second=second),
            }
        ]
    }


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _factory(db_session):
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def session():
        s = factory()
        s.execute(text("SET search_path TO test_experimentation"))
        return s

    return session


def _user(db_session, role: UserRole, *, superuser: bool = False) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"metricnames_{role.value}_{suffix}",
        email=f"metricnames_{role.value}_{suffix}@names.test",
        full_name="Metric Names User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session):
    """A DEVELOPER, not a superuser: may create and change drafts."""
    return _user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def viewer(db_session):
    """A VIEWER: may read, and is refused create and update with 403."""
    return _user(db_session, UserRole.VIEWER)


@pytest.fixture
def superuser(db_session):
    return _user(db_session, UserRole.ADMIN, superuser=True)


@pytest.fixture
def client(db_session):
    session = _factory(db_session)

    def override_get_db():
        s = session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


# --- helpers ------------------------------------------------------------------


def _auth(user: User) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _metrics(names: Optional[List[str]]) -> Optional[List[Dict[str, Any]]]:
    if names is None:
        return None
    return [
        {
            "name": name,
            "event_name": "purchase",
            "metric_type": "conversion",
            "is_primary": position == 0,
        }
        for position, name in enumerate(names)
    ]


def _create_body(names: Optional[List[str]]) -> Dict[str, Any]:
    return {
        "name": f"metric names {uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "variants": TWO_VARIANTS,
        "metrics": _metrics(names),
    }


def _create(client, user, names=("Original",)) -> str:
    response = client.post(
        f"{BASE}/", json=_create_body(list(names)), headers=_auth(user)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _put_metrics(client, user, experiment_id, names):
    return client.put(
        f"{BASE}/{experiment_id}",
        json={"metrics": _metrics(names)},
        headers=_auth(user),
    )


def _stored_names(db_session, experiment_id: str) -> List[str]:
    """The experiment's stored metric names, read from the table."""
    session = _factory(db_session)()
    try:
        rows = session.execute(
            select(Metric.name).where(Metric.experiment_id == uuid.UUID(experiment_id))
        )
        return sorted(name for (name,) in rows)
    finally:
        session.close()


def _experiments_owned_by(db_session, user: User) -> int:
    session = _factory(db_session)()
    try:
        return session.execute(
            select(func.count())
            .select_from(Experiment)
            .where(Experiment.owner_id == user.id)
        ).scalar_one()
    finally:
        session.close()


# --- 4a: a repeated name on update ---------------------------------------------


@pytest.mark.regression
def test_an_update_repeating_a_name_is_422_and_keeps_the_stored_metrics(
    client, db_session, developer
):
    """Main answered 500 ("Could not update the experiment"); the update was
    rolled back, so the stored metrics were kept then too."""
    experiment_id = _create(client, developer, names=("Conversion", "Revenue"))

    response = _put_metrics(
        client, developer, experiment_id, ["Signup", "Purchase", "Signup"]
    )

    assert response.status_code == 422, response.text
    assert response.json() == _duplicate(0, 2)
    assert _stored_names(db_session, experiment_id) == ["Conversion", "Revenue"]


# --- 4b: an empty list on update -----------------------------------------------


@pytest.mark.regression
def test_an_update_with_an_empty_list_is_422_too_short_as_on_create(
    client, db_session, developer
):
    """Main answered 200 and removed every metric."""
    experiment_id = _create(client, developer, names=("Conversion", "Revenue"))

    response = _put_metrics(client, developer, experiment_id, [])

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": [TOO_SHORT]}
    assert _stored_names(db_session, experiment_id) == ["Conversion", "Revenue"]

    created = client.post(f"{BASE}/", json=_create_body([]), headers=_auth(developer))
    assert created.status_code == 422, created.text
    assert created.json() == response.json()


# --- 4c and 4d: one answer for the same list on create and update ---------------

#: (names, create's status, update's status, the 422 body or None). On a 2xx
#: the stored names are the names sent; on a 422 an update keeps the stored
#: ones. ``None`` is a null ``metrics``: create requires a list, an update
#: leaves the metrics unchanged.
PARITY = {
    "exact repeat": (["Signup", "Signup"], 422, 422, _duplicate(0, 1)),
    "first repeated pair": (["a", "b", "b", "a"], 422, 422, _duplicate(1, 2)),
    "case differs": (["Revenue", "revenue"], 201, 200, None),
    "leading space": (["a", " a"], 201, 200, None),
    "trailing space": (["a", "a "], 201, 200, None),
    "empty": ([], 422, 422, {"detail": [TOO_SHORT]}),
    "one metric": (["Signup"], 201, 200, None),
    "null": (None, 422, 200, None),
}


@pytest.mark.regression
@pytest.mark.parametrize("case", sorted(PARITY))
def test_create_and_update_answer_the_same_list_alike(
    case, client, db_session, developer
):
    names, create_status, update_status, refusal = PARITY[case]
    owned = _experiments_owned_by(db_session, developer)

    created = client.post(
        f"{BASE}/", json=_create_body(names), headers=_auth(developer)
    )
    assert created.status_code == create_status, created.text
    if create_status == 201:
        assert _stored_names(db_session, created.json()["id"]) == sorted(names)
    else:
        # Refused: no experiment was written.
        assert _experiments_owned_by(db_session, developer) == owned
        if refusal is not None:
            assert created.json() == refusal

    experiment_id = _create(client, developer)
    updated = _put_metrics(client, developer, experiment_id, names)

    assert updated.status_code == update_status, updated.text
    if update_status == 422:
        assert updated.json() == refusal
        assert _stored_names(db_session, experiment_id) == ["Original"]
    elif names is None:
        assert _stored_names(db_session, experiment_id) == ["Original"]
    else:
        assert _stored_names(db_session, experiment_id) == sorted(names)


def test_an_update_without_metrics_leaves_them_unchanged(client, db_session, developer):
    experiment_id = _create(client, developer, names=("Conversion", "Revenue"))

    response = client.put(
        f"{BASE}/{experiment_id}", json={"name": "renamed"}, headers=_auth(developer)
    )

    assert response.status_code == 200, response.text
    assert _stored_names(db_session, experiment_id) == ["Conversion", "Revenue"]


# --- 4g: the create route's second build, and the wizard ------------------------


def test_a_create_with_distinct_names_passes_the_routes_second_build(
    client, db_session, developer
):
    """The create route validates the body, then builds ``ExperimentCreate``
    again from its dump. A check that kept state between the two would refuse
    the second build of a list it accepted the first time. The second request
    sends the same names in the other order, so state kept by name or by
    position between requests is refused too."""
    first = client.post(
        f"{BASE}/", json=_create_body(["Signup", "Purchase"]), headers=_auth(developer)
    )
    second = client.post(
        f"{BASE}/", json=_create_body(["Purchase", "Signup"]), headers=_auth(developer)
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert _stored_names(db_session, second.json()["id"]) == ["Purchase", "Signup"]


@pytest.mark.regression
def test_the_wizard_refuses_a_guardrail_that_repeats_the_primary_metric(
    client, db_session, developer
):
    """Main answered success false with "The experiment could not be
    created. Please try again." from the database's refusal."""
    headers = _auth(developer)
    draft = client.post(
        f"{WIZARD}/drafts", json={"experiment_type": "ab"}, headers=headers
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]
    step = client.put(
        f"{WIZARD}/drafts/{draft_id}/step",
        json={
            "step": "define_hypothesis",
            "data": {
                "name": f"metric names wizard {uuid.uuid4().hex[:8]}",
                "hypothesis": "A green button raises checkout completion",
                "primary_metric_id": "checkout_completed",
                "guardrail_metric_ids": ["checkout_completed"],
            },
        },
        headers=headers,
    )
    assert step.status_code == 200, step.text
    before = _experiments_owned_by(db_session, developer)

    submitted = client.post(f"{WIZARD}/drafts/{draft_id}/submit", headers=headers)

    assert submitted.status_code == 200, submitted.text
    assert submitted.json() == {
        "success": False,
        "experiment_id": None,
        "errors": ["metrics: " + DUPLICATE.format(first=0, second=1)],
    }
    assert _experiments_owned_by(db_session, developer) == before


# --- 4h: the order of the answers -----------------------------------------------


@pytest.mark.parametrize("names", [[], ["Signup", "Signup"]], ids=["empty", "repeat"])
def test_without_a_valid_token_the_answer_is_401(client, developer, names):
    """Authentication comes first: an invalid body from a caller with no
    token, or a bad one, answers 401, never the schema's 422."""
    experiment_id = _create(client, developer)

    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        response = client.put(
            f"{BASE}/{experiment_id}",
            json={"metrics": _metrics(names)},
            headers=headers,
        )
        assert response.status_code == 401, response.text


@pytest.mark.regression
@pytest.mark.parametrize("names", [[], ["Signup", "Signup"]], ids=["empty", "repeat"])
def test_the_schema_answers_before_the_role_the_existence_and_the_state(
    client, developer, viewer, superuser, names
):
    """On main a VIEWER got 403, a missing experiment 404 and a non-draft
    experiment 400 for these bodies, and an empty list on a draft 200."""
    draft_id = _create(client, developer)
    active_id = _create(client, developer)
    started = client.post(f"{BASE}/{active_id}/start", headers=_auth(developer))
    assert started.status_code == 200, started.text
    missing_id = str(uuid.uuid4())

    # Positive controls: a valid list gets the role's, the existence's and the
    # state's own answer, so each 422 below is the schema answering first.
    valid = ["Signup"]
    assert _put_metrics(client, viewer, draft_id, valid).status_code == 403
    assert _put_metrics(client, developer, missing_id, valid).status_code == 404
    active = _put_metrics(client, developer, active_id, valid)
    assert active.status_code == 400, active.text
    assert active.json() == {"detail": "Cannot update experiments in active status"}
    as_superuser = _put_metrics(client, superuser, active_id, valid)
    assert as_superuser.status_code == 400, as_superuser.text
    assert as_superuser.json() == {
        "detail": "Cannot update metrics for experiments in active status"
    }

    answers = {}
    for label, user, experiment_id in (
        ("viewer, draft", viewer, draft_id),
        ("developer, missing", developer, missing_id),
        ("developer, active", developer, active_id),
        ("superuser, active", superuser, active_id),
    ):
        response = _put_metrics(client, user, experiment_id, names)
        body = response.json()
        loc = body["detail"][0].get("loc") if isinstance(body["detail"], list) else None
        answers[label] = (response.status_code, loc)

    assert answers == {
        "viewer, draft": (422, ["body", "metrics"]),
        "developer, missing": (422, ["body", "metrics"]),
        "developer, active": (422, ["body", "metrics"]),
        "superuser, active": (422, ["body", "metrics"]),
    }


@pytest.mark.parametrize("names", [[], ["Signup", "Signup"]], ids=["empty", "repeat"])
def test_a_create_refused_by_the_schema_is_422_before_the_role(client, viewer, names):
    """A VIEWER may not create (403 with a valid body); an invalid list is
    refused by the schema first."""
    valid = client.post(
        f"{BASE}/", json=_create_body(["Signup"]), headers=_auth(viewer)
    )
    assert valid.status_code == 403, valid.text

    response = client.post(f"{BASE}/", json=_create_body(names), headers=_auth(viewer))

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "metrics"]
