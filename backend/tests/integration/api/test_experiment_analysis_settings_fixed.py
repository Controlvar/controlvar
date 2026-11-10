"""Once an experiment leaves draft, its analysis settings are fixed (#848), end to end.

#580 fixed ``correction_method`` and ``confidence_level`` once an experiment
leaves draft. #848 holds the five other settings that decide how its results
are analysed to the same rule: ``variance_reduction_config`` (CUPED),
``sequential_testing_enabled`` and ``sequential_testing_config``, and
``bayesian_enabled`` and ``bayesian_config``. Through the API against a real
database, with the columns read back with SQL:

* **C1** on an ACTIVE, PAUSED, COMPLETED or ARCHIVED experiment a superuser's
  ``PUT`` naming one of the five answers 400 with #580's detail, ``Cannot
  update <field> for experiments in <status> status``, and nothing is written;
  a body naming several is refused with the first in the route's list. A
  developer meets the earlier refusal of every change outside draft first
  (``Cannot update experiments in <status> status``): those ids pin the order
  of the two refusals and are not regression tests.
* **C2** a draft takes each of the five, from a developer.
* **C3** the refusal is on presence: the stored value sent back is refused,
  and so is ``null`` for the four that accept it.
  ``sequential_testing_enabled: null`` is 422 at every status, a draft
  included, because its column cannot be null (#541).
* **C4** the rule covers these fields only: a superuser still changes
  ``description`` on an ACTIVE experiment, a developer still changes a PAUSED
  experiment's targeting on its own, and ``start_date`` and ``end_date`` are
  not fields of the request, so a body naming them answers 200 and changes
  neither.
* **C6** the way out: a clone of a running experiment is a draft, and each of
  the five can be set on it.
* **C11** with #448's ``balance_within``, also fixed after draft: a
  superuser's body naming it and one of the five gets one 400 naming
  ``balance_within``, its answer before #848, whichever the body sends first;
  a body naming every fixed field is refused naming each in the route's order.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    SCHEMA,
    api_client,
    auth,
    create_experiment,
    local_sign_in,
    make_user,
    start,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"

#: What each experiment here is created with: every setting on, so a value is
#: stored for each of the five and sending it back is a real same-value PUT.
STORED: Dict[str, Any] = {
    "variance_reduction_config": {"method": "cuped", "covariate_lookback_days": 7},
    "sequential_testing_enabled": True,
    "sequential_testing_config": {"alpha": 0.05, "planned_looks": 5},
    "bayesian_enabled": True,
    "bayesian_config": {"alpha": 100.0, "beta": 900.0},
}

#: A value of each field that differs from ``STORED``. Written out here, not
#: read from the route's list, so a field dropped from that list turns its own
#: ids red instead of disappearing from the test.
CHANGES: Dict[str, Any] = {
    "variance_reduction_config": {"method": "cuped", "covariate_lookback_days": 14},
    "sequential_testing_enabled": False,
    "sequential_testing_config": {"alpha": 0.1, "planned_looks": 5},
    "bayesian_enabled": False,
    "bayesian_config": {"alpha": 2.0, "beta": 8.0},
}
FIELDS = list(CHANGES)

#: The four that accept ``null`` in the request schema.
NULLABLE = [
    "variance_reduction_config",
    "sequential_testing_config",
    "bayesian_enabled",
    "bayesian_config",
]

NOT_DRAFT = [
    ExperimentStatus.ACTIVE,
    ExperimentStatus.PAUSED,
    ExperimentStatus.COMPLETED,
    ExperimentStatus.ARCHIVED,
]
STATUS_IDS = [s.value for s in NOT_DRAFT]

FIELD_REFUSED = "Cannot update {} for experiments in {} status"
NOT_DRAFT_REFUSED = "Cannot update experiments in {} status"

RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "GB"}
            ],
        }
    ],
}

COLUMNS = (
    "variance_reduction_config",
    "sequential_testing_enabled",
    "sequential_testing_config",
    "bayesian_enabled",
    "bayesian_config",
    "description",
    "targeting_rules",
    "start_date",
    "end_date",
    "updated_at",
)


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def superuser(db_session):
    return make_user(db_session, UserRole.ADMIN, superuser=True, tag="fix848su")


@pytest.fixture
def developer(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="fix848dev")


def _raw(test_db, experiment_id: str) -> Dict[str, Any]:
    """The columns a refused update could have changed, read with SQL."""
    with test_db.connect() as conn:
        row = (
            conn.execute(
                text(
                    f"SELECT {', '.join(COLUMNS)} FROM {SCHEMA}.experiments "
                    "WHERE id = :id"
                ),
                {"id": experiment_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _set_status(
    test_db, experiment_id: str, status: ExperimentStatus, **columns
) -> None:
    session = sessionmaker(bind=test_db, expire_on_commit=False)()
    try:
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        row = session.get(Experiment, uuid.UUID(experiment_id))
        row.status = status
        for name, value in columns.items():
            setattr(row, name, value)
        session.commit()
    finally:
        session.close()


def _experiment(client, owner, test_db, status=ExperimentStatus.DRAFT, **columns):
    """An experiment created through the API with ``STORED``, then put in *status*."""
    created = create_experiment(client, owner, **STORED)
    if status != ExperimentStatus.DRAFT or columns:
        _set_status(test_db, created["id"], status, **columns)
    return created


def _put(client, experiment_id: str, body: Dict[str, Any], user):
    return client.put(f"{EXPERIMENTS}/{experiment_id}", json=body, headers=auth(user))


def _holds(stored: Any, sent: Any) -> bool:
    """The stored value is the one sent: a config holds every key sent."""
    if isinstance(sent, dict):
        return isinstance(stored, dict) and all(
            stored.get(key) == value for key, value in sent.items()
        )
    return stored == sent


# ---------------------------------------------------------------------------
# C1: refused outside draft, for a superuser, with #580's detail
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("status", NOT_DRAFT, ids=STATUS_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_a_superuser_cannot_change_an_analysis_setting_once_out_of_draft(
    client, superuser, test_db, status, field
):
    created = _experiment(client, superuser, test_db, status)
    before = _raw(test_db, created["id"])

    response = _put(client, created["id"], {field: CHANGES[field]}, superuser)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == FIELD_REFUSED.format(field, status.value)
    assert _raw(test_db, created["id"]) == before


@pytest.mark.regression
@pytest.mark.parametrize(
    "body, named",
    [
        (
            {
                "bayesian_config": CHANGES["bayesian_config"],
                "variance_reduction_config": CHANGES["variance_reduction_config"],
            },
            "variance_reduction_config",
        ),
        (
            {"sequential_testing_enabled": False, "confidence_level": 0.9},
            "confidence_level",
        ),
    ],
    ids=["cuped-before-bayesian", "580-before-848"],
)
def test_a_body_naming_several_is_refused_with_the_first_in_the_routes_list(
    client, superuser, test_db, body, named
):
    created = _experiment(client, superuser, test_db, ExperimentStatus.ACTIVE)
    before = _raw(test_db, created["id"])

    response = _put(client, created["id"], body, superuser)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == FIELD_REFUSED.format(named, "active")
    assert _raw(test_db, created["id"]) == before


@pytest.mark.parametrize("status", NOT_DRAFT, ids=STATUS_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_a_developer_meets_the_refusal_of_every_change_outside_draft_first(
    client, developer, test_db, status, field
):
    """The guard order, not the fix: this answer is the same before #848."""
    created = _experiment(client, developer, test_db, status)
    before = _raw(test_db, created["id"])

    response = _put(client, created["id"], {field: CHANGES[field]}, developer)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == NOT_DRAFT_REFUSED.format(status.value)
    assert _raw(test_db, created["id"]) == before


# ---------------------------------------------------------------------------
# C2: a draft takes each of the five
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field", FIELDS)
def test_a_draft_takes_each_analysis_setting(client, developer, test_db, field):
    created = _experiment(client, developer, test_db)

    response = _put(client, created["id"], {field: CHANGES[field]}, developer)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"
    assert _holds(response.json()[field], CHANGES[field]), response.json()[field]
    assert _holds(_raw(test_db, created["id"])[field], CHANGES[field])


# ---------------------------------------------------------------------------
# C3: on presence, whatever the value
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("status", NOT_DRAFT, ids=STATUS_IDS)
@pytest.mark.parametrize("field", FIELDS)
def test_sending_back_the_stored_value_is_refused_too(
    client, superuser, test_db, status, field
):
    """The value as GET returns it: a GET-then-PUT of a running experiment."""
    created = _experiment(client, superuser, test_db, status)
    fetched = client.get(f"{EXPERIMENTS}/{created['id']}", headers=auth(superuser))
    assert fetched.status_code == 200, fetched.text
    stored = fetched.json()[field]
    assert _holds(stored, STORED[field]), stored
    before = _raw(test_db, created["id"])

    response = _put(client, created["id"], {field: stored}, superuser)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == FIELD_REFUSED.format(field, status.value)
    assert _raw(test_db, created["id"]) == before


@pytest.mark.regression
@pytest.mark.parametrize("status", NOT_DRAFT, ids=STATUS_IDS)
@pytest.mark.parametrize("field", NULLABLE)
def test_null_is_refused_outside_draft_for_the_four_that_accept_it(
    client, superuser, test_db, status, field
):
    created = _experiment(client, superuser, test_db, status)
    before = _raw(test_db, created["id"])

    response = _put(client, created["id"], {field: None}, superuser)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == FIELD_REFUSED.format(field, status.value)
    assert _raw(test_db, created["id"]) == before


@pytest.mark.parametrize(
    "status", [ExperimentStatus.DRAFT, *NOT_DRAFT], ids=["draft", *STATUS_IDS]
)
def test_a_null_sequential_testing_enabled_is_422_at_every_status(
    client, superuser, test_db, status
):
    created = _experiment(client, superuser, test_db, status)
    before = _raw(test_db, created["id"])

    response = _put(
        client, created["id"], {"sequential_testing_enabled": None}, superuser
    )

    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "null_not_allowed"
    assert error["loc"] == ["body", "sequential_testing_enabled"]
    assert _raw(test_db, created["id"]) == before


# ---------------------------------------------------------------------------
# C4: the rule covers these fields only
# ---------------------------------------------------------------------------
def test_a_superuser_still_changes_the_description_of_an_active_experiment(
    client, superuser, test_db
):
    created = _experiment(client, superuser, test_db, ExperimentStatus.ACTIVE)
    before = _raw(test_db, created["id"])

    response = _put(
        client, created["id"], {"description": "edited while running"}, superuser
    )

    assert response.status_code == 200, response.text
    after = _raw(test_db, created["id"])
    assert after["description"] == "edited while running"
    assert {f: after[f] for f in FIELDS} == {f: before[f] for f in FIELDS}


def test_a_developer_still_changes_a_paused_experiments_targeting_on_its_own(
    client, developer, test_db
):
    created = _experiment(client, developer, test_db, ExperimentStatus.PAUSED)
    before = _raw(test_db, created["id"])

    response = _put(client, created["id"], {"targeting_rules": RULES}, developer)

    assert response.status_code == 200, response.text
    after = _raw(test_db, created["id"])
    assert after["targeting_rules"] == RULES
    assert {f: after[f] for f in FIELDS} == {f: before[f] for f in FIELDS}


@pytest.mark.parametrize(
    "status", [ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE], ids=["draft", "active"]
)
def test_start_date_and_end_date_in_a_body_change_nothing(
    client, superuser, test_db, status
):
    """Not fields of the request: the route's list keeps them, and they never
    match there, because the request schema drops them first."""
    # Naive UTC: the column stores and returns it so (UTCDateTime).
    started = datetime(2026, 9, 1, 9, 0)
    created = _experiment(client, superuser, test_db, status, start_date=started)
    before = _raw(test_db, created["id"])
    assert before["start_date"] == started and before["end_date"] is None

    response = _put(
        client,
        created["id"],
        {"start_date": "2026-10-01T00:00:00Z", "end_date": "2026-12-01T00:00:00Z"},
        superuser,
    )

    assert response.status_code == 200, response.text
    after = _raw(test_db, created["id"])
    assert (after["start_date"], after["end_date"]) == (started, None)
    assert {f: after[f] for f in FIELDS} == {f: before[f] for f in FIELDS}


# ---------------------------------------------------------------------------
# C6: the way out is a clone, which is a draft
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["developer", "superuser"])
def test_a_clone_of_a_running_experiment_takes_the_settings_as_a_draft(
    client, db_session, test_db, role
):
    user = (
        make_user(db_session, UserRole.ADMIN, superuser=True, tag="fix848clone")
        if role == "superuser"
        else make_user(db_session, UserRole.DEVELOPER, tag="fix848clone")
    )
    source = _experiment(client, user, test_db)
    start(client, source, user)
    source_before = _raw(test_db, source["id"])

    refused = _put(client, source["id"], {"bayesian_enabled": False}, user)
    assert refused.status_code == 400, refused.text

    cloned = client.post(f"{EXPERIMENTS}/{source['id']}/clone", headers=auth(user))
    assert cloned.status_code == 201, cloned.text
    copy = cloned.json()
    assert copy["status"] == "draft"
    for field in FIELDS:
        assert _holds(copy[field], STORED[field]), (field, copy[field])

    for field in FIELDS:
        response = _put(client, copy["id"], {field: CHANGES[field]}, user)
        assert response.status_code == 200, (field, response.text)
        assert _holds(_raw(test_db, copy["id"])[field], CHANGES[field]), field

    assert _raw(test_db, source["id"]) == source_before


# ---------------------------------------------------------------------------
# C11: with #448's balance_within, which is fixed after draft too
# ---------------------------------------------------------------------------
TIER = {"attribute": "tier", "values": ["gold", "silver"]}


def _balance_within(test_db, experiment_id: str) -> Any:
    with test_db.connect() as conn:
        return conn.execute(
            text(f"SELECT balance_within FROM {SCHEMA}.experiments WHERE id = :id"),
            {"id": experiment_id},
        ).scalar_one()


@pytest.mark.regression
@pytest.mark.parametrize(
    "setting_first", [True, False], ids=["setting-first", "balance-first"]
)
@pytest.mark.parametrize(
    "status",
    [ExperimentStatus.ACTIVE, ExperimentStatus.PAUSED],
    ids=["active", "paused"],
)
@pytest.mark.parametrize("field", FIELDS)
def test_balance_within_with_an_analysis_setting_is_refused_naming_balance_within(
    client, superuser, test_db, field, status, setting_first
):
    """One 400, and it names ``balance_within``, in whichever order the body
    sends the two: the route's list puts #448's field before #848's five, so
    the answer is the one a superuser got for this body before #848."""
    created = _experiment(client, superuser, test_db, status)
    before = _raw(test_db, created["id"])
    pairs = [(field, CHANGES[field]), ("balance_within", TIER)]
    body = dict(pairs if setting_first else reversed(pairs))

    response = _put(client, created["id"], body, superuser)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == FIELD_REFUSED.format(
        "balance_within", status.value
    )
    assert _raw(test_db, created["id"]) == before
    assert _balance_within(test_db, created["id"]) is None


def test_a_developer_sending_both_meets_the_refusal_of_every_change_outside_draft_first(
    client, developer, test_db
):
    """The guard order, as for each field alone: the same before #848."""
    created = _experiment(client, developer, test_db, ExperimentStatus.ACTIVE)

    response = _put(
        client,
        created["id"],
        {"bayesian_enabled": False, "balance_within": TIER},
        developer,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == NOT_DRAFT_REFUSED.format("active")
    assert _balance_within(test_db, created["id"]) is None


#: Every request field the route fixes after draft, in the order its 400 names
#: them. Written out, not read from the route's list.
NAMED_IN_ORDER = (
    "variants",
    "metrics",
    "correction_method",
    "confidence_level",
    "balance_within",
    "variance_reduction_config",
    "sequential_testing_enabled",
    "sequential_testing_config",
    "bayesian_enabled",
    "bayesian_config",
)


@pytest.mark.regression
def test_a_body_naming_every_fixed_field_is_refused_naming_each_in_the_routes_order(
    client, superuser, test_db
):
    """Send them all, drop the one the 400 names, send the rest again."""
    created = _experiment(client, superuser, test_db, ExperimentStatus.ACTIVE)
    before = _raw(test_db, created["id"])
    body: Dict[str, Any] = {
        "variants": [
            {"name": "control", "is_control": True, "traffic_allocation": 50},
            {"name": "treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Signup",
                "event_name": "signup_c11",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
        "correction_method": "bonferroni",
        "confidence_level": 0.9,
        "balance_within": TIER,
        **CHANGES,
    }
    assert set(body) == set(NAMED_IN_ORDER)

    named = []
    while body:
        response = _put(client, created["id"], body, superuser)
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        matches = [f for f in body if detail == FIELD_REFUSED.format(f, "active")]
        assert len(matches) == 1, (detail, sorted(body))
        named.append(matches[0])
        del body[matches[0]]

    assert tuple(named) == NAMED_IN_ORDER
    assert _raw(test_db, created["id"]) == before
    assert _balance_within(test_db, created["id"]) is None
