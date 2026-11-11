"""
Two first assignments for one user that cross (#1026).

``assign_user`` looks for the user's stored assignment, then inserts one. On
an API that runs as several processes, two first ``POST /tracking/assign``
calls for the same user can land on different processes: both find nothing,
both insert, and the unique index on ``(experiment_id, user_id)`` refuses the
second. That request answered the fixed 500.

One process cannot show it: the async handler runs one request's database
work at a time, so an in-process concurrent test passes on the old code. The
tests below force the interleaving instead. A hook on
``GlobalHoldoutService.is_user_in_holdout`` -- the first call after the
existence check, before the holdout population row and the insert -- has a
second session (the "other process") store the user's assignment and commit,
then lets the request carry on into its own insert.

The other process stores the variant the hash would NOT pick, so an answer
carrying that variant can only have come from the stored row.

Covered:
* ``/tracking/assign``: 200 with the stored row's variant, ``assigned`` true,
  exactly one row, and the user recorded as seen once more, as a sticky hit is;
* ``/tracking/assign/batch``: the same answer for that user, the rest of the
  list assigned, and no view recorded by the batch;
* the holdout population (#445): exactly one row for the user, whether the
  other process recorded it or stored only the assignment; on the batch too,
  with the user last, so no later commit stores the row this request wrote
  again (the batch records no view, and the single route's view commits it);
* any other refusal of the insert (a foreign key, a duplicate primary key)
  still answers the fixed 500 and stores nothing;
* a balanced experiment (#448): the losing request's place in its group is
  given back with its row, so the group's places stay exactly ``0 .. n - 1``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Callable, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, EventType
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.user import User, UserRole
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.global_holdout_service import (
    GlobalHoldoutService,
    holdout_bucket,
)
from backend.tests.integration.balance_checks import balance_rows

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SINGLE_URL = "/api/v1/tracking/assign"
BATCH_URL = "/api/v1/tracking/assign/batch"
KEYS_URL = "/api/v1/api-keys"
SCHEMA = "test_experimentation"
HOLDOUT_PERCENTAGE = 20
SINGLE_FAILURE = "Could not assign the user to the experiment"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def session_factory(db_session):
    """Sessions on the test database, expiring on commit as production's do."""
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
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


@pytest.fixture
def headers(client, db_session) -> Dict[str, str]:
    """A key valid on both routes: the batch needs ``sdk:ruleset`` and an
    owner who can change experiments."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"conflict_{suffix}",
        email=f"conflict_{suffix}@conflict.test",
        full_name="Assign Conflict User",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    resp = client.post(
        KEYS_URL,
        json={"name": f"key-{suffix}", "scopes": ["sdk:ruleset"]},
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return {"X-API-Key": resp.json()["key"]}


@contextmanager
def _parked_holdouts(db_session):
    """No active global holdout for the duration, restored afterwards."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        for row in parked:
            db_session.merge(row).is_active = True
        db_session.commit()


@pytest.fixture
def experiment(db_session, make_experiment):
    """An ACTIVE two-arm experiment with fixed allocation, no holdout active."""
    with _parked_holdouts(db_session):
        suffix = uuid.uuid4().hex[:8]
        exp = make_experiment(
            name=f"First conflict {suffix}",
            key=f"first-conflict-{suffix}",
            status=ExperimentStatus.ACTIVE,
        )
        for i, name in enumerate(("control", "treatment")):
            db_session.add(
                Variant(
                    experiment_id=exp.id,
                    name=name,
                    description=f"{name} variant",
                    is_control=i == 0,
                    traffic_allocation=50,
                    configuration={"arm": name},
                )
            )
        db_session.commit()
        db_session.refresh(exp)
        exp_id = exp.id
        yield exp
        db_session.rollback()
        db_session.query(Event).filter(Event.experiment_id == exp_id).delete()
        db_session.query(Assignment).filter(Assignment.experiment_id == exp_id).delete()
        db_session.commit()


@pytest.fixture
def measurable_holdout(db_session, experiment):
    """An active holdout whose population is recorded (#445)."""
    holdout = GlobalHoldoutService(db_session).create_holdout(
        name=f"first-conflict-{uuid.uuid4().hex[:8]}",
        holdout_percentage=HOLDOUT_PERCENTAGE,
        is_active=True,
    )
    assert holdout.is_measurable
    holdout_id = holdout.id
    yield holdout
    db_session.rollback()
    # holdout_population rows go with it (ON DELETE CASCADE).
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id == holdout_id).delete()
    db_session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_user(prefix: str = "first") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _variant_the_hash_does_not_pick(db_session, experiment: Experiment, user_id: str):
    picked = AssignmentService(db_session)._hash_user_to_variant(user_id, experiment)
    others = [v.id for v in experiment.variants if str(v.id) != str(picked)]
    assert len(others) == 1
    return others[0]


def _full_assign(variant_id) -> Callable:
    """The other process runs the whole first assignment: the population row
    when a holdout is measurable, the assignment, a recorded view, a commit."""

    def write(session, user_id: str, experiment_id) -> None:
        AssignmentService(session).assign_user(
            user_id=user_id,
            experiment_id=str(experiment_id),
            override_variant_id=variant_id,
        )

    return write


def _assignment_only(variant_id) -> Callable:
    """The other writer stores the assignment row and nothing else."""

    def write(session, user_id: str, experiment_id) -> None:
        session.add(
            Assignment(
                user_id=user_id, experiment_id=experiment_id, variant_id=variant_id
            )
        )
        session.commit()

    return write


def _another_request_stores_it_first(
    monkeypatch, session_factory, user_id: str, experiment_id, write: Callable
) -> List[str]:
    """Run *write* in a second session after the request's existence check.

    ``is_user_in_holdout`` is the first call ``assign_user`` makes for a new
    user once it has found no stored row. Returns the list the hook appends
    the user to when it fires, so a test can show the interleaving happened.
    """
    real = GlobalHoldoutService.is_user_in_holdout
    fired: List[str] = []

    def hooked(self, uid, *args, **kwargs):
        if uid == user_id and not fired:
            fired.append(uid)
            other = session_factory()
            try:
                write(other, uid, experiment_id)
            finally:
                other.close()
        return real(self, uid, *args, **kwargs)

    monkeypatch.setattr(GlobalHoldoutService, "is_user_in_holdout", hooked)
    return fired


def _rows(session_factory, experiment_id, user_id) -> List[str]:
    session = session_factory()
    try:
        return [
            str(v)
            for (v,) in session.query(Assignment.variant_id).filter(
                Assignment.experiment_id == experiment_id,
                Assignment.user_id == user_id,
            )
        ]
    finally:
        session.close()


def _views(session_factory, experiment_id, user_id) -> List[str]:
    session = session_factory()
    try:
        return [
            str(v)
            for (v,) in session.query(Event.variant_id).filter(
                Event.experiment_id == experiment_id,
                Event.user_id == user_id,
                Event.event_type == EventType.EXPOSURE.value,
            )
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The crossing first assignments
# ---------------------------------------------------------------------------


def test_single_assign_answers_the_row_another_process_stored(
    client, headers, experiment, db_session, session_factory, monkeypatch
):
    user_id = _new_user()
    stored = _variant_the_hash_does_not_pick(db_session, experiment, user_id)
    fired = _another_request_stores_it_first(
        monkeypatch, session_factory, user_id, experiment.id, _full_assign(stored)
    )

    resp = client.post(
        SINGLE_URL,
        json={"experiment_key": experiment.key, "user_id": user_id},
        headers=headers,
    )

    assert fired == [user_id]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variant_id"] == str(stored)
    assert body["assigned"] is True
    assert body["reason"] == "assigned"
    assert _rows(session_factory, experiment.id, user_id) == [str(stored)]
    # One view from the other process, one from this request: a sticky hit's.
    assert _views(session_factory, experiment.id, user_id) == [str(stored)] * 2


def test_batch_answers_the_row_another_process_stored(
    client, headers, experiment, db_session, session_factory, monkeypatch
):
    user_id, later = _new_user(), _new_user("later")
    stored = _variant_the_hash_does_not_pick(db_session, experiment, user_id)
    fired = _another_request_stores_it_first(
        monkeypatch, session_factory, user_id, experiment.id, _full_assign(stored)
    )

    resp = client.post(
        BATCH_URL,
        json={
            "experiment_key": experiment.key,
            "users": [{"user_id": user_id}, {"user_id": later}],
        },
        headers=headers,
    )

    assert fired == [user_id]
    assert resp.status_code == 200, resp.text
    answers = {
        a["user_id"]: (a["variant_id"], a["assigned"], a["reason"])
        for a in resp.json()["assignments"]
    }
    assert answers[user_id] == (str(stored), True, "assigned")
    assert answers[later][1:] == (True, "assigned")
    assert _rows(session_factory, experiment.id, user_id) == [str(stored)]
    assert _rows(session_factory, experiment.id, later) == [answers[later][0]]
    # The batch records no view; the one there is the other process's.
    assert _views(session_factory, experiment.id, user_id) == [str(stored)]
    assert _views(session_factory, experiment.id, later) == []


def _outside_the_holdout(holdout, prefix: str) -> str:
    """A new user the holdout does not hold out, so eligible and inserted."""
    while True:
        user_id = _new_user(prefix)
        if holdout_bucket(user_id, holdout.hash_salt) >= HOLDOUT_PERCENTAGE:
            return user_id


def _population(session_factory, holdout_id, user_id) -> List[tuple]:
    session = session_factory()
    try:
        return [
            tuple(row)
            for row in session.query(HoldoutPopulation.in_holdout).filter(
                HoldoutPopulation.holdout_id == holdout_id,
                HoldoutPopulation.user_id == user_id,
            )
        ]
    finally:
        session.close()


@pytest.mark.parametrize("writer", ["full_assign", "assignment_only"])
def test_holdout_population_is_recorded_once_under_the_conflict(
    writer,
    client,
    headers,
    experiment,
    measurable_holdout,
    db_session,
    session_factory,
    monkeypatch,
):
    """The rollback drops this request's population row; the user is still
    recorded exactly once, whether or not the other writer recorded them."""
    user_id = _outside_the_holdout(measurable_holdout, "pop")
    stored = _variant_the_hash_does_not_pick(db_session, experiment, user_id)
    write = {"full_assign": _full_assign, "assignment_only": _assignment_only}[writer]
    fired = _another_request_stores_it_first(
        monkeypatch, session_factory, user_id, experiment.id, write(stored)
    )

    resp = client.post(
        SINGLE_URL,
        json={"experiment_key": experiment.key, "user_id": user_id},
        headers=headers,
    )

    assert fired == [user_id]
    assert resp.status_code == 200, resp.text
    assert resp.json()["variant_id"] == str(stored)
    assert _rows(session_factory, experiment.id, user_id) == [str(stored)]
    assert _population(session_factory, measurable_holdout.id, user_id) == [(False,)]


def test_batch_holdout_population_is_committed_under_the_conflict(
    client,
    headers,
    experiment,
    measurable_holdout,
    db_session,
    session_factory,
    monkeypatch,
):
    """The batch, with the crossing user last and a writer that stores only
    the assignment row: nothing after the conflict commits for this request
    (no view, no later user), so the population row it writes again after
    the rollback is stored only by its own commit."""
    earlier = _outside_the_holdout(measurable_holdout, "pop-earlier")
    user_id = _outside_the_holdout(measurable_holdout, "pop-last")
    stored = _variant_the_hash_does_not_pick(db_session, experiment, user_id)
    fired = _another_request_stores_it_first(
        monkeypatch, session_factory, user_id, experiment.id, _assignment_only(stored)
    )

    resp = client.post(
        BATCH_URL,
        json={
            "experiment_key": experiment.key,
            "users": [{"user_id": earlier}, {"user_id": user_id}],
        },
        headers=headers,
    )

    assert fired == [user_id]
    assert resp.status_code == 200, resp.text
    answers = {a["user_id"]: a for a in resp.json()["assignments"]}
    assert answers[user_id]["variant_id"] == str(stored)
    assert answers[user_id]["assigned"] is True
    assert _rows(session_factory, experiment.id, user_id) == [str(stored)]
    assert _views(session_factory, experiment.id, user_id) == []
    assert _population(session_factory, measurable_holdout.id, user_id) == [(False,)]
    assert _population(session_factory, measurable_holdout.id, earlier) == [(False,)]


@pytest.fixture
def balanced_experiment(db_session, experiment):
    """The same experiment, balancing within ``tier`` (#448)."""
    db_session.execute(
        text(
            "UPDATE experiments SET balance_within = CAST(:setting AS jsonb), "
            "balance_seed = :seed WHERE id = :id"
        ),
        {
            "setting": '{"attribute": "tier", "values": ["gold"]}',
            "seed": "b" * 64,
            "id": str(experiment.id),
        },
    )
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


def _balanced_assign(session, user_id: str, experiment_id) -> None:
    """The other process assigns the user through the blocks, as a request
    with the same ``context`` would."""
    AssignmentService(session).assign_user(
        user_id=user_id,
        experiment_id=str(experiment_id),
        context={"tier": "gold"},
    )


def test_a_balanced_first_assignment_gives_its_place_back_when_it_loses(
    client, headers, balanced_experiment, session_factory, monkeypatch
):
    """#448: the losing request took the group's next place before its insert
    was refused; the rollback gives the place back with the row, so the
    group's places stay exactly 0 .. n - 1 and the answer is the stored row."""
    experiment = balanced_experiment
    user_id = _new_user("balanced")
    fired = _another_request_stores_it_first(
        monkeypatch,
        session_factory,
        user_id,
        experiment.id,
        lambda session, uid, eid: _balanced_assign(session, uid, eid),
    )

    resp = client.post(
        SINGLE_URL,
        json={
            "experiment_key": experiment.key,
            "user_id": user_id,
            "context": {"tier": "gold"},
        },
        headers=headers,
    )

    assert fired == [user_id]
    assert resp.status_code == 200, resp.text
    stored = _rows(session_factory, experiment.id, user_id)
    assert len(stored) == 1
    assert resp.json()["variant_id"] == stored[0]
    session = session_factory()
    try:
        found = balance_rows(session, experiment.id)
    finally:
        session.close()
    assert found.problems == [], found.problems
    assert found.groups["gold"].next_seq == 1
    assert sorted(found.groups["gold"].seqs) == [0]


# ---------------------------------------------------------------------------
# Any other refusal of the insert still answers the fixed 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("refusal", ["foreign_key", "primary_key"])
def test_another_refusal_of_the_insert_still_answers_500(
    refusal, client, headers, experiment, session_factory
):
    """Only a duplicate of the user's own row is answered from storage: a
    foreign key, or a duplicate of a different unique key (here the primary
    key of another user's row), is the fixed 500 with nothing stored."""
    user_id = _new_user("refused")
    taken_id = None
    if refusal == "primary_key":
        session = session_factory()
        try:
            other_row = Assignment(
                user_id=_new_user("holder"),
                experiment_id=experiment.id,
                variant_id=experiment.variants[0].id,
            )
            session.add(other_row)
            session.commit()
            taken_id = other_row.id
        finally:
            session.close()

    def break_insert(mapper, connection, target):
        if target.user_id != user_id:
            return
        if refusal == "foreign_key":
            target.variant_id = uuid.uuid4()
        else:
            target.id = taken_id

    event.listen(Assignment, "before_insert", break_insert)
    try:
        resp = client.post(
            SINGLE_URL,
            json={"experiment_key": experiment.key, "user_id": user_id},
            headers=headers,
        )
    finally:
        event.remove(Assignment, "before_insert", break_insert)

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"].startswith(SINGLE_FAILURE)
    assert _rows(session_factory, experiment.id, user_id) == []
    assert _views(session_factory, experiment.id, user_id) == []
