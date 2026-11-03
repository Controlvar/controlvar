"""A revoke takes effect on the next request (#891, check #34).

Each test asserts the whole sequence, 403 -> 2xx -> 403, through real sign-in
(a real local token, or the Cognito path with only the call to Cognito
stubbed) and the API's own sessions: a cache that never saw the grant would
pass a test that only asserted the final 403. The route is
``POST /api/v1/experiments/`` (``experiment (create)``).

Kinds of revoke: ``DELETE /rbac/users/{id}/grant/{resource}``;
``POST /rbac/roles/revoke``; ``DELETE /rbac/roles/{name}``; a ``PUT`` that
narrows the role; an expiry set by SQL; a ``DELETE`` by SQL outside the API.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.core.config import settings
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import cleanup

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "rev891"


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


def _create(client, headers) -> int:
    suffix = uuid.uuid4().hex[:8]
    return client.post(
        f"{V1}/experiments/",
        headers=headers,
        json={
            "name": f"rev {suffix}",
            "key": f"rev-{suffix}",
            "hypothesis": "revoke",
            "experiment_type": "a_b",
            "variants": [
                {"name": "control", "is_control": True, "traffic_allocation": 50},
                {"name": "b", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Purchase",
                    "event_name": f"purchase_{suffix}",
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
    ).status_code


def _sequence(client, headers, give, take) -> None:
    before = _create(client, headers)
    assert before == 403, f"before grant: expected 403, got {before}"
    give()
    after_grant = _create(client, headers)
    assert after_grant == 201, f"after grant: expected 201, got {after_grant}"
    take()
    after_revoke = _create(client, headers)
    assert after_revoke == 403, f"after revoke: expected 403, got {after_revoke}"


def _grant(client, admin, user):
    resp = client.post(
        f"{V1}/rbac/users/{user.id}/grant",
        json={"user_id": str(user.id), "resource": "experiment", "actions": ["create"]},
        headers=auth(admin),
    )
    assert resp.status_code == 201, resp.text


def _role(client, admin, actions=("create", "read")) -> str:
    name = f"{TAG}-{uuid.uuid4().hex[:10]}"
    resp = client.post(
        f"{V1}/rbac/roles",
        json={
            "name": name,
            "permissions": [{"resource": "experiment", "actions": list(actions)}],
        },
        headers=auth(admin),
    )
    assert resp.status_code == 201, resp.text
    return name


def _assign(client, admin, user, name):
    resp = client.post(
        f"{V1}/rbac/roles/assign",
        json={"user_id": str(user.id), "role_name": name},
        headers=auth(admin),
    )
    assert resp.status_code == 200, resp.text


def test_delete_grant(client, admin, viewer):
    def take():
        resp = client.delete(
            f"{V1}/rbac/users/{viewer.id}/grant/experiment", headers=auth(admin)
        )
        assert resp.status_code == 200 and resp.json()["count"] == 1, resp.text

    _sequence(client, auth(viewer), lambda: _grant(client, admin, viewer), take)


def test_revoke_role(client, admin, viewer):
    name = _role(client, admin)

    def take():
        resp = client.post(
            f"{V1}/rbac/roles/revoke",
            json={"user_id": str(viewer.id), "role_name": name},
            headers=auth(admin),
        )
        assert resp.status_code == 200, resp.text

    _sequence(client, auth(viewer), lambda: _assign(client, admin, viewer, name), take)


def test_delete_role(client, admin, viewer):
    name = _role(client, admin)

    def take():
        resp = client.delete(f"{V1}/rbac/roles/{name}", headers=auth(admin))
        assert resp.status_code == 204, resp.text

    _sequence(client, auth(viewer), lambda: _assign(client, admin, viewer, name), take)


def test_put_narrowing_the_role(client, admin, viewer):
    name = _role(client, admin)

    def take():
        resp = client.put(
            f"{V1}/rbac/roles/{name}",
            json={"permissions": [{"resource": "experiment", "actions": ["read"]}]},
            headers=auth(admin),
        )
        assert resp.status_code == 200, resp.text

    _sequence(client, auth(viewer), lambda: _assign(client, admin, viewer, name), take)


def test_expiry_set_by_sql(client, db_session, admin, viewer):
    def take():
        db_session.execute(
            text(
                "UPDATE direct_permission_grants SET expires_at = now() - "
                "interval '1 second' WHERE user_id = :u"
            ),
            {"u": viewer.id},
        )
        db_session.commit()

    _sequence(client, auth(viewer), lambda: _grant(client, admin, viewer), take)


def test_delete_by_sql_outside_the_api(client, db_session, admin, viewer):
    def take():
        db_session.execute(
            text("DELETE FROM direct_permission_grants WHERE user_id = :u"),
            {"u": viewer.id},
        )
        db_session.commit()

    _sequence(client, auth(viewer), lambda: _grant(client, admin, viewer), take)


def test_the_cognito_path_with_a_role_sync_request(client, db_session, monkeypatch):
    """The production sign-in: AUTH_PROVIDER=cognito, the call to Cognito
    stubbed, ``resolve_cognito_user`` real. A request in the middle commits a
    role sync (and refreshes the user) before the permission check. The
    administrator signs in through Cognito too (the Admins group)."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", False)

    def identity(name, groups):
        sub = f"sub-{uuid.uuid4().hex[:10]}"
        return {
            "username": f"{TAG}cog_{name}_{sub}",
            "attributes": {"sub": sub, "email": f"{name}-{sub}@rev.test"},
            "groups": groups,
        }

    claims = {
        "cognito-viewer": identity("viewer", []),
        "cognito-admin": identity("admin", ["Admins"]),
    }
    monkeypatch.setattr(
        "backend.app.api.deps.auth_service.get_user_with_groups",
        lambda token: claims[token],
    )
    headers = {"Authorization": "Bearer cognito-viewer"}
    admin_headers = {"Authorization": "Bearer cognito-admin"}

    first = _create(client, headers)  # the first sign-in creates the user
    assert first == 403, f"before grant: expected 403, got {first}"
    db_session.expire_all()
    user = (
        db_session.query(User)
        .filter(User.username == claims["cognito-viewer"]["username"])
        .one()
    )
    assert user.role == UserRole.VIEWER

    resp = client.post(
        f"{V1}/rbac/users/{user.id}/grant",
        json={"user_id": str(user.id), "resource": "experiment", "actions": ["create"]},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    after_grant = _create(client, headers)
    assert after_grant == 201, f"after grant: expected 201, got {after_grant}"

    # A role change out of band, then a request that syncs it back from the
    # groups: resolve_cognito_user commits and refreshes the user, and the
    # check that follows still reads the grant.
    db_session.execute(
        text("UPDATE users SET role = 'ANALYST' WHERE id = :u"), {"u": user.id}
    )
    db_session.commit()
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", True)
    after_sync = _create(client, headers)
    assert after_sync == 201, f"after role sync: expected 201, got {after_sync}"
    db_session.expire_all()
    assert db_session.get(User, user.id).role == UserRole.VIEWER

    resp = client.delete(
        f"{V1}/rbac/users/{user.id}/grant/experiment", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    after_revoke = _create(client, headers)
    assert after_revoke == 403, f"after revoke: expected 403, got {after_revoke}"
