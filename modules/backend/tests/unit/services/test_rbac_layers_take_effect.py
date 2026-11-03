"""Custom roles and direct grants take effect, through one capability (#891).

Every permission check (``check_permission``, ``can_act_on_feature_flag``,
``can_read_all_audit_logs`` in ``backend/app/core/permissions.py``) asks, when
the built-in role says no, what the user's custom roles and direct grants
hold. The core reads them only through the ``rbac.granted_permissions``
capability, which ``modules/backend/app/register.py`` installs as
``rbac_service.granted_permission_pairs``; the core never names a custom-role
table. ``GET /rbac/users/{id}/permissions`` is built on the same checks.

These pins are structural; the behaviour is held on Postgres through real
sign-in by ``modules/backend/tests/integration/api/test_rbac_enforcement_routes.py``
(each pair) and ``test_rbac_revocation.py`` (a revoke on the next request).
"""

from __future__ import annotations

import ast
import pathlib
import re
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session, make_transient_to_detached

from backend.app.core import hooks
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.user import User, UserRole
from modules.backend.app.models.custom_role import (
    CustomRole,
    DirectPermissionGrant,
    UserCustomRole,
)
from modules.backend.app.services.rbac_service import RBACService

REPO = pathlib.Path(__file__).resolve().parents[5]
TREES = [REPO / "backend" / "app", REPO / "modules" / "backend" / "app"]
LAYER_NAMES = {
    "CustomRole",
    "UserCustomRole",
    "DirectPermissionGrant",
    "RBACService",
    "granted_permission_pairs",
    "get_effective_permissions",
}


def _references():
    found = {}
    for tree in TREES:
        for path in sorted(tree.rglob("*.py")):
            module = ast.parse(path.read_text())
            names = set()
            for node in ast.walk(module):
                if isinstance(node, ast.Name) and node.id in LAYER_NAMES:
                    names.add(node.id)
                elif isinstance(node, ast.Attribute) and node.attr in LAYER_NAMES:
                    names.add(node.attr)
                elif isinstance(node, ast.alias) and node.name in LAYER_NAMES:
                    names.add(node.name)
                elif isinstance(node, (ast.ClassDef, ast.FunctionDef)) and (
                    node.name in LAYER_NAMES
                ):
                    names.add(node.name)
            if names:
                found[path.relative_to(REPO).as_posix()] = names
    return found


def test_the_permission_checks_read_custom_roles_and_grants_through_one_capability():
    found = _references()
    # The models, the service, the RBAC routes, and the registration that
    # installs the provider. Not permissions.py, not deps.py: the core reads
    # the layers only through the capability.
    assert set(found) == {
        "modules/backend/app/models/custom_role.py",
        "modules/backend/app/services/rbac_service.py",
        "modules/backend/app/api/v1/endpoints/rbac.py",
        "modules/backend/app/register.py",
    }
    assert found["modules/backend/app/register.py"] == {"granted_permission_pairs"}
    core = (REPO / "backend" / "app" / "core" / "permissions.py").read_text()
    assert "granted_permissions_provider()" in core
    accessor = (REPO / "backend" / "app" / "core" / "optional_modules.py").read_text()
    assert 'hooks.get_capability("rbac.granted_permissions")' in accessor
    # check_effective_permission, a second decision function no route called,
    # is gone: display and enforcement are built on the same checks.
    assert not hasattr(RBACService, "check_effective_permission")


def _bound(role):
    user = User(
        id=uuid.uuid4(),
        username="ana",
        email="ana@layers.test",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role=role,
    )
    make_transient_to_detached(user)
    Session().add(user)
    return user


@pytest.fixture
def registered_provider():
    """The module's own provider, registered as register.py does, with its two
    queries answered by a fake session-level stub."""
    saved = hooks.get_capability("rbac.granted_permissions")
    yield
    hooks.register_capability("rbac.granted_permissions", saved)


def test_a_custom_role_or_grant_changes_what_the_user_can_do_and_is_shown(
    registered_provider, monkeypatch
):
    analyst = _bound(UserRole.ANALYST)
    # What register.py installs, fed a role holding experiment create and
    # update and a grant of export read (never effective) and flag update.
    from modules.backend.app.services import rbac_service

    def fake_pairs(session, user_id):
        assert user_id == analyst.id
        return [
            ("experiment", "create"),
            ("experiment", "update"),
            ("export", "read"),
            ("feature_flag", "update"),
        ]

    monkeypatch.setattr(rbac_service, "granted_permission_pairs", fake_pairs)
    hooks.register_capability(
        "rbac.granted_permissions", rbac_service.granted_permission_pairs
    )

    # What the routes ask now admits the granted pairs ...
    assert check_permission(analyst, ResourceType.EXPERIMENT, Action.CREATE)
    assert check_permission(analyst, ResourceType.EXPERIMENT, Action.UPDATE)
    assert check_permission(analyst, ResourceType.FEATURE_FLAG, Action.UPDATE)
    # ... and nothing else: not the neighbouring pair, not a stored pair no
    # check reads.
    assert not check_permission(analyst, ResourceType.EXPERIMENT, Action.DELETE)
    assert not check_permission(analyst, ResourceType.FEATURE_FLAG, Action.DELETE)

    shown = RBACService.get_effective_permissions(MagicMock(), analyst)
    assert shown.permissions == {
        "experiment": ["create", "list", "read", "update"],
        "feature_flag": ["list", "read", "update"],
        "audit_log": ["read"],
    }
    assert "export" not in shown.permissions


def test_the_layers_only_add_and_actions_are_sorted(registered_provider, monkeypatch):
    developer = _bound(UserRole.DEVELOPER)
    hooks.register_capability("rbac.granted_permissions", None)
    base = RBACService.get_effective_permissions(MagicMock(), developer).permissions
    hooks.register_capability(
        "rbac.granted_permissions",
        lambda session, user_id: [("audit_log", "read"), ("report", "update")],
    )
    with_layers = RBACService.get_effective_permissions(
        MagicMock(), _bound(UserRole.DEVELOPER)
    ).permissions
    # Nothing a layer holds removes what the base role gives: no deny.
    for resource, actions in base.items():
        assert set(actions) <= set(with_layers[resource]), resource
    assert with_layers["audit_log"] == ["read"]
    for actions in with_layers.values():
        assert actions == sorted(actions)


def test_include_system_and_nothing_creates_a_system_role():
    db = MagicMock()
    RBACService.list_custom_roles(db, include_system=False)
    (condition,) = db.query.return_value.filter.call_args.args
    assert str(condition) == str(CustomRole.is_system_role == False)

    for tree in TREES:
        for path in tree.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.keyword) and node.arg == "is_system_role":
                    assert not (
                        isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ), f"{path}: creates a system role"
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Attribute) and t.attr == "is_system_role"
                    for t in node.targets
                ):
                    raise AssertionError(f"{path}:{node.lineno} sets is_system_role")


def test_the_three_data_models():
    def described(model):
        return {
            name: (type(column.type).__name__, bool(column.foreign_keys))
            for name, column in model.__table__.columns.items()
        }

    custom = described(CustomRole)
    assert custom == {
        "id": ("UUID", False),
        "name": ("String", False),
        "description": ("Text", False),
        "is_system_role": ("Boolean", False),
        "permissions": ("JSONB", False),
        "created_by_id": ("UUID", True),
        "created_at": ("DateTime", False),
        "updated_at": ("DateTime", False),
    }
    assert CustomRole.__table__.c.name.type.length == 64
    assert CustomRole.__table__.c.name.unique
    assignment = described(UserCustomRole)
    for name, kind in {
        "id": ("UUID", False),
        "user_id": ("UUID", True),
        "role_id": ("UUID", True),
        "assigned_by_id": ("UUID", True),
        "reason": ("Text", False),
        "assigned_at": ("DateTime", False),
        "activated_at": ("DateTime", False),
    }.items():
        assert assignment[name] == kind, name
    grant = described(DirectPermissionGrant)
    for name, kind in {
        "id": ("UUID", False),
        "user_id": ("UUID", True),
        "resource": ("String", False),
        "actions": ("JSONB", False),
        "granted_by_id": ("UUID", True),
        "reason": ("Text", False),
        "expires_at": ("DateTime", False),
        "is_active": ("Boolean", False),
        "activated_at": ("DateTime", False),
    }.items():
        assert grant[name] == kind, name
    assert DirectPermissionGrant.__table__.c.expires_at.nullable
    assert UserCustomRole.__table__.c.activated_at.nullable
    assert DirectPermissionGrant.__table__.c.activated_at.nullable


def test_role_names_are_lower_case_slugs():
    from pydantic import ValidationError

    from modules.backend.app.schemas.rbac import CustomRoleCreate

    for name in ("data-scientist", "read_only_experiments", "analyst2"):
        assert CustomRoleCreate(name=name).name == name
    for name in ("MyRole", "1role", "a", "has space"):
        with pytest.raises(ValidationError):
            CustomRoleCreate(name=name)


def test_the_routes_name_roles_and_users_in_their_paths():
    import inspect

    from modules.backend.app.api.v1.endpoints import rbac as endpoint

    for name in ("get_role", "update_role", "delete_role"):
        params = inspect.signature(getattr(endpoint, name)).parameters
        assert params["role_name"].annotation in (str, "str"), name
    revoke = inspect.signature(endpoint.revoke_permission).parameters
    assert {"user_id", "resource"} <= set(revoke)
    assert "UUID" in str(revoke["user_id"].annotation)


def test_every_rbac_write_route_writes_its_audit_entry():
    """Each RBAC route that changes something hands its own action to a writer
    (#891). Was ``..._write_no_audit_entry`` for the grant routes, before they
    were recorded; ``test_audit_module_events.py`` pins each entry itself."""
    source = (
        REPO / "modules" / "backend" / "app" / "api" / "v1" / "endpoints" / "rbac.py"
    )
    functions = {
        node.name: node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.FunctionDef)
    }
    expected = {
        "create_role": ("_record_role_definition", "ROLE_CREATE"),
        "update_role": ("_record_role_definition", "ROLE_UPDATE"),
        "delete_role": ("_record_role_definition", "ROLE_DELETE"),
        "assign_role": ("_record_custom_role", "ROLE_ASSIGN"),
        "revoke_role": ("_record_custom_role", "ROLE_UNASSIGN"),
        "grant_permission": ("_record_direct_grant", "PERMISSION_GRANT"),
        "revoke_permission": ("_record_direct_grant", "PERMISSION_REVOKE"),
    }
    for name, (writer, action) in expected.items():
        calls = [
            n
            for n in ast.walk(functions[name])
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == writer
        ]
        assert len(calls) == 1, f"{name} calls {writer} {len(calls)} times"
        named = {
            n.attr
            for n in ast.walk(calls[0])
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            if n.value.id == "ActionType"
        }
        assert named == {action}, f"{name} records {named}"
    # Each helper reaches the writer that commits after the change.
    for helper in (
        "_record_role_definition",
        "_record_custom_role",
        "_record_direct_grant",
    ):
        assert "record_after_commit" in {
            n.attr for n in ast.walk(functions[helper]) if isinstance(n, ast.Attribute)
        }, helper


def test_the_roles_page_needs_a_superuser():
    page = REPO / "modules" / "frontend" / "src" / "pages" / "admin" / "roles.tsx"
    source = page.read_text()
    assert "export default withAdminGuard(" in source
    # The guard's default audience is superusers; only an explicit
    # `superuser: false` opens a page to anyone else.
    assert "superuser: false" not in source
    guard = (
        REPO / "frontend" / "src" / "components" / "admin" / "withAdminGuard.tsx"
    ).read_text()
    assert re.search(r"superuser\s*=\s*true", guard)
