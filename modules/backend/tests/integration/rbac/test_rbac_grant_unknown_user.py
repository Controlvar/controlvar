"""A grant to a user id with no account answers 404, not 500 (#1002).

``POST /api/v1/rbac/users/{user_id}/grant`` inserted the grant without looking
the user up, and the foreign key refused the row on flush: a 500. The user is
now looked up after the admin check and before the insert, and an id with no
account answers 404 "User not found", the answer of
``GET /api/v1/rbac/users/{user_id}/permissions``. Nothing is written and no
audit entry is made.

Real sign-in through local tokens; the only override is the database session.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import cleanup

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

V1 = "/api/v1"
TAG = "gnt1002"


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def admin(db_session):
    return make_user(db_session, UserRole.ADMIN, tag=TAG)


@pytest.fixture
def viewer(db_session):
    return make_user(db_session, UserRole.VIEWER, tag=TAG)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    db_session.rollback()
    users = db_session.query(User).filter(User.username.like(f"{TAG}%")).all()
    ids = [u.id for u in users]
    if ids:
        db_session.execute(
            text(
                "DELETE FROM audit_logs WHERE user_id = ANY(:ids) "
                "OR entity_id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        db_session.commit()
    cleanup(db_session, TAG, ids)


def _grant(client, admin, user_id):
    return client.post(
        f"{V1}/rbac/users/{user_id}/grant",
        json={"user_id": str(user_id), "resource": "experiment", "actions": ["read"]},
        headers=auth(admin),
    )


def _grant_rows(db_session, user_id) -> int:
    db_session.rollback()
    return db_session.execute(
        text("SELECT count(*) FROM direct_permission_grants WHERE user_id = :u"),
        {"u": user_id},
    ).scalar_one()


def _grant_entries(db_session, user_id) -> int:
    db_session.expire_all()
    return (
        db_session.query(AuditLog)
        .filter(
            AuditLog.entity_id == user_id, AuditLog.action_type == "permission_grant"
        )
        .count()
    )


@pytest.mark.regression
def test_a_grant_to_an_id_with_no_account_answers_404_and_writes_nothing(
    client, db_session, admin
):
    missing = uuid.uuid4()
    response = _grant(client, admin, missing)
    # A 500's body is plain text: compare the status first.
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "User not found"}
    assert _grant_rows(db_session, missing) == 0
    assert _grant_entries(db_session, missing) == 0


def test_a_grant_to_a_known_user_is_made_and_recorded(
    client, db_session, admin, viewer
):
    """The control: the same request for an account that exists is a 201, its
    row is stored and its entry written, so the two counts above can see one."""
    response = _grant(client, admin, viewer.id)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "granted"
    assert _grant_rows(db_session, viewer.id) == 1
    assert _grant_entries(db_session, viewer.id) == 1


def test_a_viewer_is_refused_before_the_lookup(client, viewer):
    """The admin check comes first: a caller who may not grant gets 403 for an
    unknown id too, so the 404 tells no one else which ids have accounts."""
    response = _grant(client, viewer, uuid.uuid4())
    assert response.status_code == 403, response.text
