"""The server half of the dashboard's "Edit variants and metrics" (#1453).

The experiment page sends ``PUT /experiments/{id}`` with exactly
``{variants, metrics}``, both complete, every stored field of each item
included (``buildDesignPayload`` in frontend/src/components/experiments/new/
formState.ts). The update replaces both lists in one commit. This pins what
that body meets on a real database:

* who may send it, by state (QA #4): on a DRAFT, ADMIN, DEVELOPER and a
  superuser get 200 and ANALYST and VIEWER 403; on a PAUSED experiment every
  role the role check admits gets 400, the superuser included, because the
  lists are restricted outside DRAFT for everyone. The role check runs first,
  so ANALYST and VIEWER keep their 403 there.
* a round trip keeps every field (QA #7): GET, PUT the lists back as the page
  builds them, GET again, and every field of every item is what was created.
  The ids, ``experiment_id`` and the two timestamps are the server's own and
  change, because every save deletes the rows and inserts new ones.

Every request is real: real ``User`` rows and a local JWT, with only
``deps.get_db`` overridden.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
PREFIX = "design1453"

#: The fields VariantBase and MetricBase hold: what the page sends per item.
VARIANT_FIELDS = (
    "name",
    "description",
    "is_control",
    "traffic_allocation",
    "configuration",
)
METRIC_FIELDS = (
    "name",
    "description",
    "event_name",
    "metric_type",
    "is_primary",
    "aggregation_method",
    "minimum_sample_size",
    "expected_effect",
    "event_value_path",
    "lower_is_better",
)

#: Every field away from its default (``is_control`` and ``is_primary`` can be
#: true on one item each).
VARIANTS = [
    {
        "name": "Control",
        "description": "The page as it is",
        "is_control": True,
        "traffic_allocation": 34,
        "configuration": {"color": "grey", "layout": {"columns": 2}},
    },
    {
        "name": "Blue CTA",
        "description": "A blue button",
        "is_control": False,
        "traffic_allocation": 33,
        "configuration": {"color": "blue"},
    },
    {
        "name": "Green CTA",
        "description": "A green button",
        "is_control": False,
        "traffic_allocation": 33,
        "configuration": {"color": "green", "label": "Été ✓"},
    },
]
METRICS = [
    {
        "name": "Revenue",
        "description": "Order value",
        "event_name": "purchase",
        "metric_type": "revenue",
        "is_primary": True,
        "aggregation_method": "sum",
        "minimum_sample_size": 500,
        "expected_effect": 0.1,
        "event_value_path": "amount",
        "lower_is_better": True,
    },
    {
        "name": "Errors",
        "description": "Client errors",
        "event_name": "error",
        "metric_type": "count",
        "is_primary": False,
        "aggregation_method": "median",
        "minimum_sample_size": 250,
        "expected_effect": 0.03,
        "event_value_path": "count",
        "lower_is_better": True,
    },
]


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def client(test_db):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


#: (column, role, superuser)
CALLERS = {
    "admin": (UserRole.ADMIN, False),
    "developer": (UserRole.DEVELOPER, False),
    "analyst": (UserRole.ANALYST, False),
    "viewer": (UserRole.VIEWER, False),
    "superuser": (UserRole.VIEWER, True),
}


@pytest.fixture
def people(db_session):
    suffix = uuid.uuid4().hex[:8]

    def make(name, role, is_superuser):
        user = User(
            username=f"{PREFIX}_{name}_{suffix}",
            email=f"{PREFIX}_{name}_{suffix}@design.test",
            full_name="Design Edit",
            hashed_password="unused: this user signs in by token only",
            is_active=True,
            is_superuser=is_superuser,
            role=role,
        )
        db_session.add(user)
        return user

    users = {name: make(name, role, su) for name, (role, su) in CALLERS.items()}
    users["creator"] = make("creator", UserRole.DEVELOPER, False)
    db_session.commit()
    return users


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _create(client, people):
    response = client.post(
        f"{EXPERIMENTS}/",
        headers=_auth(people["creator"]),
        json={
            "name": f"{PREFIX} {uuid.uuid4().hex[:8]}",
            "experiment_type": "a_b",
            "variants": VARIANTS,
            "metrics": METRICS,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _set_status(db_session, experiment_id, status):
    row = db_session.get(Experiment, uuid.UUID(experiment_id))
    row.status = status
    db_session.commit()


def _design_body(experiment):
    """The page's body for an unchanged form: each item's VariantBase or
    MetricBase fields, as read. (``buildDesignPayload`` leaves out a null
    ``aggregation_method`` or ``minimum_sample_size``; the API never answers
    with one, so neither is null here.)"""
    return {
        "variants": [{k: v[k] for k in VARIANT_FIELDS} for v in experiment["variants"]],
        "metrics": [{k: m[k] for k in METRIC_FIELDS} for m in experiment["metrics"]],
    }


def _by_name(items, fields):
    return {item["name"]: {k: item[k] for k in fields} for item in items}


# --- QA #4: who may send it, by state ------------------------------------------

EXPECTED = {
    ExperimentStatus.DRAFT: {
        "admin": 200,
        "developer": 200,
        "analyst": 403,
        "viewer": 403,
        "superuser": 200,
    },
    ExperimentStatus.PAUSED: {
        "admin": 400,
        "developer": 400,
        "analyst": 403,
        "viewer": 403,
        "superuser": 400,
    },
}
CELLS = [(status, column) for status in EXPECTED for column in CALLERS]


@pytest.mark.parametrize(
    ("status", "column"), CELLS, ids=[f"{s.value}-{c}" for s, c in CELLS]
)
def test_who_may_replace_the_lists_by_state(client, db_session, people, status, column):
    experiment_id = _create(client, people)
    stored = client.get(
        f"{EXPERIMENTS}/{experiment_id}", headers=_auth(people["creator"])
    )
    assert stored.status_code == 200, stored.text
    body = _design_body(stored.json())
    body["variants"][1]["name"] = "Blue button"
    _set_status(db_session, experiment_id, status)

    response = client.put(
        f"{EXPERIMENTS}/{experiment_id}", headers=_auth(people[column]), json=body
    )

    assert response.status_code == EXPECTED[status][column], response.text
    after = client.get(
        f"{EXPERIMENTS}/{experiment_id}", headers=_auth(people["creator"])
    )
    names = sorted(v["name"] for v in after.json()["variants"])
    if response.status_code == 200:
        assert names == ["Blue button", "Control", "Green CTA"]
    else:
        # Refused: nothing was replaced.
        assert names == ["Blue CTA", "Control", "Green CTA"]
        assert {v["id"] for v in after.json()["variants"]} == {
            v["id"] for v in stored.json()["variants"]
        }


@pytest.mark.parametrize("only", ["variants", "metrics"])
def test_a_superuser_is_refused_either_list_alone_outside_draft(
    client, db_session, people, only
):
    """Each list is restricted on its own, so neither can slip through."""
    experiment_id = _create(client, people)
    stored = client.get(
        f"{EXPERIMENTS}/{experiment_id}", headers=_auth(people["creator"])
    )
    body = {only: _design_body(stored.json())[only]}
    _set_status(db_session, experiment_id, ExperimentStatus.PAUSED)

    response = client.put(
        f"{EXPERIMENTS}/{experiment_id}", headers=_auth(people["superuser"]), json=body
    )

    assert response.status_code == 400, response.text
    assert (
        response.json()["detail"]
        == f"Cannot update {only} for experiments in paused status"
    )


# --- QA #7: a round trip keeps every field --------------------------------------


def test_a_round_trip_keeps_every_field_of_every_item(client, people):
    experiment_id = _create(client, people)
    headers = _auth(people["developer"])
    before = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=headers)
    assert before.status_code == 200, before.text
    # What was created is what is read: every field, by name.
    assert _by_name(before.json()["variants"], VARIANT_FIELDS) == _by_name(
        VARIANTS, VARIANT_FIELDS
    )
    assert _by_name(before.json()["metrics"], METRIC_FIELDS) == _by_name(
        METRICS, METRIC_FIELDS
    )

    body = _design_body(before.json())
    assert sorted(body["variants"][0]) == sorted(VARIANT_FIELDS)
    assert sorted(body["metrics"][0]) == sorted(METRIC_FIELDS)
    response = client.put(f"{EXPERIMENTS}/{experiment_id}", headers=headers, json=body)
    assert response.status_code == 200, response.text

    after = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=headers)
    assert after.status_code == 200, after.text
    for items, fields, expected in (
        ("variants", VARIANT_FIELDS, VARIANTS),
        ("metrics", METRIC_FIELDS, METRICS),
    ):
        assert _by_name(after.json()[items], fields) == _by_name(expected, fields)
        assert _by_name(response.json()[items], fields) == _by_name(expected, fields)
        # The rows are new: every save deletes and inserts them.
        assert {i["id"] for i in after.json()[items]}.isdisjoint(
            {i["id"] for i in before.json()[items]}
        )


def test_a_moved_primary_takes_the_planned_effect(client, people):
    """The page moves ``expected_effect`` to a new primary (C7); the API stores
    exactly that, and nothing else on either metric changes."""
    experiment_id = _create(client, people)
    headers = _auth(people["developer"])
    before = client.get(f"{EXPERIMENTS}/{experiment_id}", headers=headers).json()
    body = _design_body(before)
    by_name = {m["name"]: m for m in body["metrics"]}
    by_name["Revenue"].update(is_primary=False, expected_effect=None)
    by_name["Errors"].update(is_primary=True, expected_effect=0.1)

    response = client.put(f"{EXPERIMENTS}/{experiment_id}", headers=headers, json=body)
    assert response.status_code == 200, response.text

    after = _by_name(
        client.get(f"{EXPERIMENTS}/{experiment_id}", headers=headers).json()["metrics"],
        METRIC_FIELDS,
    )
    expected = _by_name(METRICS, METRIC_FIELDS)
    expected["Revenue"].update(is_primary=False, expected_effect=None)
    expected["Errors"].update(is_primary=True, expected_effect=0.1)
    assert after == expected
