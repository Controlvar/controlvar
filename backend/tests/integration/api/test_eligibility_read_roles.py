"""The two reads behind the experiment page's group and holdout rows (#1449).

The dashboard reads ``GET /api/v1/mutual-exclusion-groups/{id}`` and
``GET /api/v1/holdout`` only for a user ``canReadEligibility`` admits
(``frontend/src/utils/experimentPermissions.ts``: ADMIN, DEVELOPER and
superusers), and shows ANALYST and VIEWER a refusal without asking. That is
right only while both routes check exactly that: ``_require_developer``,
EXPERIMENT CREATE. This pins it on the real routes, on Postgres, signed in
with local tokens as real users:

* ADMIN, DEVELOPER and a superuser: 200; ANALYST and VIEWER: 403. Each role is
  a NON-superuser (a superuser passes every check, so a superuser fixture would
  prove nothing about the role); the superuser is one extra cell.
* The group's answer carries the ``name`` and ``status`` the page reads, and
  ``status`` is ``"archived"`` for an archived group.
* The holdout route's 200 is JSON ``null`` when no holdout is active, which the
  page shows as "None active". No cell activates a holdout: the database is
  shared, and an active holdout applies to every experiment. So only that
  branch runs, and an active holdout's fields (``name``,
  ``holdout_percentage``, ``is_active``) are not pinned here. The assertion
  would accept an active holdout another test left behind only if it had
  them; the page reads an answer without them as its failure state, never as
  "None active".

A route moved to ``_require_admin`` fails the DEVELOPER cell, and one opened to
every role fails ANALYST and VIEWER: in both cases the page would be wrong.

The same table pins the role check on the routes that add an experiment to a
group and remove it from one (``POST`` and ``DELETE
/mutual-exclusion-groups/{id}/experiments``): a refused caller gets 403 and
the experiment's group is left as it was.
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
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

SCHEMA = "test_experimentation"
P = "er1449"
V1 = "/api/v1"

# (role, superuser) -> what both reads answer
USERS = {
    "admin": (UserRole.ADMIN, False, 200),
    "developer": (UserRole.DEVELOPER, False, 200),
    "analyst": (UserRole.ANALYST, False, 403),
    "viewer": (UserRole.VIEWER, False, 403),
    "superuser": (UserRole.VIEWER, True, 200),
}


# --- fixtures -----------------------------------------------------------------


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
        session.execute(text(f"SET search_path TO {SCHEMA}"))
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


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Remove every experiment, group and user this module created."""
    yield
    s = db_session
    s.rollback()
    for experiment in s.query(Experiment).filter(Experiment.name.like(f"{P}%")):
        s.delete(experiment)
    s.commit()
    users = s.query(User).filter(User.username.like(f"{P}_%")).all()
    s.query(MutualExclusionGroup).filter(
        MutualExclusionGroup.name.like(f"{P}%")
    ).delete(synchronize_session=False)
    for user in users:
        s.delete(user)
    s.commit()


def _user(db_session, who: str) -> User:
    from backend.app.api.v1.endpoints.users import get_password_hash

    role, superuser, _ = USERS[who]
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{P}_{who}_{suffix}",
        email=f"{P}_{who}_{suffix}@example.com",
        full_name="Eligibility reads user",
        hashed_password=get_password_hash("Er1449-Passw0rd"),
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _group(db_session, status=MutualExclusionGroupStatus.ACTIVE):
    group = MutualExclusionGroup(
        name=f"{P} group {uuid.uuid4().hex[:8]}",
        traffic_allocation=1.0,
        status=status,
    )
    db_session.add(group)
    db_session.commit()
    return group


# --- both reads × every role --------------------------------------------------


@pytest.mark.parametrize("who", list(USERS))
def test_reading_a_group_by_role(client, db_session, who):
    group = _group(db_session, status=MutualExclusionGroupStatus.ARCHIVED)
    response = client.get(
        f"{V1}/mutual-exclusion-groups/{group.id}",
        headers=_auth(_user(db_session, who)),
    )
    expected = USERS[who][2]
    assert response.status_code == expected, (who, response.text)
    if expected == 200:
        body = response.json()
        assert body["name"] == group.name
        # The page marks an archived group by this exact value.
        assert body["status"] == "archived"


@pytest.mark.parametrize("who", list(USERS))
def test_reading_the_holdout_by_role(client, db_session, who):
    response = client.get(f"{V1}/holdout", headers=_auth(_user(db_session, who)))
    expected = USERS[who][2]
    assert response.status_code == expected, (who, response.text)
    if expected == 200:
        body = response.json()
        # No active holdout is JSON null; the page shows "None active" for it.
        assert body is None or (
            isinstance(body["name"], str)
            and isinstance(body["holdout_percentage"], int)
            and body["is_active"] is True
        ), body


# --- adding an experiment to a group, and removing it, × every role ------------


def _group_of(db_session, experiment_id):
    db_session.expire_all()
    return db_session.get(Experiment, experiment_id).mutual_exclusion_group_id


@pytest.mark.parametrize("action", ["add", "remove"])
@pytest.mark.parametrize("who", list(USERS))
def test_adding_or_removing_an_experiment_by_role(client, db_session, who, action):
    user = _user(db_session, who)
    group = _group(db_session)
    experiment = Experiment(
        name=f"{P} experiment {uuid.uuid4().hex[:8]}",
        status=ExperimentStatus.DRAFT,
        owner_id=user.id,
        mutual_exclusion_group_id=group.id if action == "remove" else None,
    )
    db_session.add(experiment)
    db_session.commit()
    before = _group_of(db_session, experiment.id)

    path = f"{V1}/mutual-exclusion-groups/{group.id}/experiments"
    if action == "add":
        response = client.post(
            path, json={"experiment_id": str(experiment.id)}, headers=_auth(user)
        )
    else:
        response = client.delete(f"{path}/{experiment.id}", headers=_auth(user))

    expected = USERS[who][2]
    assert response.status_code == expected, (who, action, response.text)
    after = group.id if action == "add" else None
    assert _group_of(db_session, experiment.id) == (
        after if expected == 200 else before
    ), (who, action)
