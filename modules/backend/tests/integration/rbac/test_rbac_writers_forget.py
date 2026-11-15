"""Each RBAC writer drops what the permission checks kept on its session (#891, check #33).

A permission check keeps a user's custom-role and grant answer on its
session for the session's life (``granted_permissions``). Each of the seven
writers in ``modules/backend/app/api/v1/endpoints/rbac.py`` is called here
directly, on the session a check has just used, and the next check on that
session must see the change. The route functions are called, not requested:
through the API every request has its own session, so a writer that kept the
old answer would pass every request-level test (check #34 holds those).
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from backend.app.core import hooks
from backend.app.core.permissions import (
    GRANTED_PERMISSIONS_CACHE_KEY,
    Action,
    ResourceType,
    check_permission,
)
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import make_user
from modules.backend.app.api.v1.endpoints import rbac
from modules.backend.app.schemas.rbac import (
    AssignRoleRequest,
    CustomRoleCreate,
    CustomRoleUpdate,
    DelegatePermissionRequest,
    PermissionGrant,
    RevokeRoleRequest,
)
from modules.backend.app.services.rbac_service import granted_permission_pairs
from modules.backend.tests.integration.rbac.rbac_states import assign, cleanup, grant

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

TAG = "fgt891"
UPDATE = [{"resource": "experiment", "actions": ["update"]}]
READ = [{"resource": "experiment", "actions": ["read"]}]


@pytest.fixture(autouse=True)
def _provider():
    """The module's provider, registered as ``register.py`` does."""
    saved = hooks.get_capability("rbac.granted_permissions")
    hooks.register_capability("rbac.granted_permissions", granted_permission_pairs)
    yield
    hooks.register_capability("rbac.granted_permissions", saved)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    cleanup(db_session, TAG, [u.id for u in users])


@pytest.fixture
def admin(db_session):
    return make_user(db_session, UserRole.ADMIN, tag=TAG)


@pytest.fixture
def viewer(db_session):
    return make_user(db_session, UserRole.VIEWER, tag=TAG)


def _role(db_session, stored) -> str:
    name = f"{TAG}-{uuid.uuid4().hex[:10]}"
    db_session.execute(
        text(
            "INSERT INTO custom_roles (id, name, is_system_role, permissions, "
            "created_at, updated_at) VALUES (:id, :name, false, "
            "CAST(:perms AS jsonb), now(), now())"
        ),
        {"id": uuid.uuid4(), "name": name, "perms": json.dumps(stored)},
    )
    return name


def _role_id(db_session, name: str):
    return db_session.execute(
        text("SELECT id FROM custom_roles WHERE name = :name"), {"name": name}
    ).scalar_one()


def _create_role(db, admin, viewer):
    rbac.create_role(
        CustomRoleCreate(
            name=f"{TAG}-new-{uuid.uuid4().hex[:8]}",
            permissions=[PermissionGrant(resource="experiment", actions=["update"])],
        ),
        db=db,
        current_user=admin,
    )


def _update_role(db, admin, viewer):
    name = _role(db, READ)
    assign(db, viewer.id, _role_id(db, name), activated=True)
    db.commit()
    return lambda: rbac.update_role(
        name,
        CustomRoleUpdate(
            permissions=[PermissionGrant(resource="experiment", actions=["update"])]
        ),
        db=db,
        current_user=admin,
    )


def _delete_role(db, admin, viewer):
    name = _role(db, UPDATE)
    assign(db, viewer.id, _role_id(db, name), activated=True)
    db.commit()
    return lambda: rbac.delete_role(name, db=db, current_user=admin)


def _assign_role(db, admin, viewer):
    name = _role(db, UPDATE)
    db.commit()
    return lambda: rbac.assign_role(
        AssignRoleRequest(user_id=str(viewer.id), role_name=name),
        db=db,
        current_user=admin,
    )


def _revoke_role(db, admin, viewer):
    name = _role(db, UPDATE)
    assign(db, viewer.id, _role_id(db, name), activated=True)
    db.commit()
    return lambda: rbac.revoke_role(
        RevokeRoleRequest(user_id=str(viewer.id), role_name=name),
        db=db,
        current_user=admin,
    )


def _grant_permission(db, admin, viewer):
    return lambda: rbac.grant_permission(
        viewer.id,
        DelegatePermissionRequest(
            user_id=str(viewer.id), resource="experiment", actions=["update"]
        ),
        db=db,
        current_user=admin,
    )


def _revoke_permission(db, admin, viewer):
    grant(db, viewer.id, "experiment", ["update"])
    db.commit()
    return lambda: rbac.revoke_permission(
        viewer.id, "experiment", db=db, current_user=admin
    )


#: writer: (set up and return the call, may update before, may update after)
WRITERS = {
    "update_role": (_update_role, False, True),
    "delete_role": (_delete_role, True, False),
    "assign_role": (_assign_role, False, True),
    "revoke_role": (_revoke_role, True, False),
    "grant_permission": (_grant_permission, False, True),
    "revoke_permission": (_revoke_permission, True, False),
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_the_next_check_on_the_writers_session_sees_the_change(
    db_session, admin, viewer, writer
):
    setup, before, after = WRITERS[writer]
    call = setup(db_session, admin, viewer)

    assert check_permission(viewer, ResourceType.EXPERIMENT, Action.UPDATE) is before
    assert str(viewer.id) in db_session.info.get(GRANTED_PERMISSIONS_CACHE_KEY, {})

    call()

    assert check_permission(viewer, ResourceType.EXPERIMENT, Action.UPDATE) is after, (
        f"{writer}: the session kept the answer from before the change"
    )


def test_creating_a_role_drops_what_the_session_kept(db_session, admin, viewer):
    # A new role is held by no one, so no answer changes; what the session
    # kept is dropped all the same.
    assert check_permission(viewer, ResourceType.EXPERIMENT, Action.UPDATE) is False
    assert str(viewer.id) in db_session.info.get(GRANTED_PERMISSIONS_CACHE_KEY, {})

    _create_role(db_session, admin, viewer)

    assert GRANTED_PERMISSIONS_CACHE_KEY not in db_session.info, (
        "create_role: the session kept the answer from before the change"
    )
