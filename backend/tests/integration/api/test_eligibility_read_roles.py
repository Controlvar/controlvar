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
* The fields the page reads are in the answers: the group's ``name`` and
  ``status``; the holdout route's ``null`` or an object with ``name``,
  ``holdout_percentage`` and ``is_active``.

A route moved to ``_require_admin`` fails the DEVELOPER cell, and one opened to
every role fails ANALYST and VIEWER: in both cases the page would be wrong.
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
    """Remove every group and user this module created."""
    yield
    s = db_session
    s.rollback()
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
