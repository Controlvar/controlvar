"""``/api/v1/admin/users``: listing, updating and deleting need a superuser.

An account that is not a superuser is refused with 403, whatever its role, and
nothing is changed; a superuser gets past the check.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.core.security import get_password_hash
from backend.app.models.user import User, UserRole
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration]

URL = "/api/v1/admin/users/{}"


def _account(db_session, *, is_superuser: bool, role: UserRole) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"admin_routes_{suffix}",
        email=f"admin_routes_{suffix}@example.com",
        hashed_password=get_password_hash("Str0ng-Passw0rd"),
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _row(db_session, user_id) -> dict:
    db_session.expire_all()
    user = db_session.get(User, user_id)
    return {c.name: getattr(user, c.name) for c in User.__table__.columns}


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
def test_a_non_superuser_cannot_update_or_delete_through_the_admin_routes(
    db_session, role
):
    actor = _account(db_session, is_superuser=False, role=role)
    target = _account(db_session, is_superuser=False, role=UserRole.VIEWER)
    before = _row(db_session, target.id)
    client = make_client_for_user(db_session, actor)

    put = client.put(
        URL.format(target.id),
        json={"username": target.username, "email": target.email, "full_name": "x"},
    )
    assert put.status_code == 403, put.text
    delete = client.delete(URL.format(target.id))
    assert delete.status_code == 403, delete.text
    assert _row(db_session, target.id) == before


def test_a_superuser_updates_and_deletes_through_the_admin_routes(db_session):
    actor = _account(db_session, is_superuser=True, role=UserRole.ADMIN)
    target = _account(db_session, is_superuser=False, role=UserRole.VIEWER)
    client = make_client_for_user(db_session, actor)

    put = client.put(
        URL.format(target.id),
        json={"username": target.username, "email": target.email, "full_name": "y"},
    )
    assert put.status_code == 200, put.text
    assert client.delete(URL.format(target.id)).status_code == 204
