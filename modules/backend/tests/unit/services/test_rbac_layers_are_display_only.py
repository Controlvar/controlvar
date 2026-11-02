"""Custom roles and direct grants are stored and shown, and decide nothing (#1142, #891).

Every route decides with ``check_permission()`` (the base role) or the
superuser flag. ``RBACService.get_effective_permissions`` merges the three
layers into the set ``GET /rbac/users/{id}/permissions`` shows; nothing else
reads the custom roles, the assignments or the grants, and
``check_effective_permission`` is called by no route.
"""

from __future__ import annotations

import ast
import pathlib
import re
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.user import UserRole
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
    "check_effective_permission",
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


def test_no_permission_check_reads_custom_roles_or_direct_grants():
    found = _references()
    # The models, the service that merges them, and the RBAC routes that store
    # and show them. Not backend/app/core/permissions.py, not deps.py, not
    # any other route.
    assert set(found) == {
        "modules/backend/app/models/custom_role.py",
        "modules/backend/app/services/rbac_service.py",
        "modules/backend/app/api/v1/endpoints/rbac.py",
    }
    # check_effective_permission is defined and called by no route.
    assert (
        "check_effective_permission"
        not in found["modules/backend/app/api/v1/endpoints/rbac.py"]
    )


def _user(role):
    return SimpleNamespace(
        id=uuid.uuid4(), username="ana", role=role, is_superuser=False, is_active=True
    )


def _db(custom_permissions=(), grants=()):
    role = SimpleNamespace(name="data-scientist", permissions=list(custom_permissions))
    assignments = [SimpleNamespace(role=role)] if custom_permissions else []
    stored_grants = [
        SimpleNamespace(resource=r, actions=a, is_valid=True) for r, a in grants
    ]

    def query(model):
        q = MagicMock()
        rows = assignments if model is UserCustomRole else stored_grants
        q.filter.return_value.all.return_value = rows
        return q

    db = MagicMock()
    db.query.side_effect = query
    return db


def test_a_custom_role_or_grant_is_shown_and_changes_nothing_the_user_can_do():
    analyst = _user(UserRole.ANALYST)
    assert (
        "export"
        not in RBACService.get_effective_permissions(_db(), analyst).permissions
    )
    db = _db(
        custom_permissions=[
            {"resource": "experiment", "actions": ["create", "update"]}
        ],
        grants=[("export", ["read"]), ("feature_flag", ["update"])],
    )

    shown = RBACService.get_effective_permissions(db, analyst)
    assert "create" in shown.permissions["experiment"]
    assert shown.permissions["export"] == ["read"]
    assert "update" in shown.permissions["feature_flag"]
    assert shown.custom_roles == ["data-scientist"]

    # The service's own check would say yes; no route asks it.
    assert RBACService.check_effective_permission(db, analyst, "experiment", "create")
    # What the routes ask: the analyst base role, unchanged.
    assert not check_permission(analyst, ResourceType.EXPERIMENT, Action.CREATE)
    assert not check_permission(analyst, ResourceType.EXPERIMENT, Action.UPDATE)
    assert not check_permission(analyst, ResourceType.FEATURE_FLAG, Action.UPDATE)


def test_the_layers_only_add_and_actions_are_sorted():
    developer = _user(UserRole.DEVELOPER)
    base = RBACService.get_effective_permissions(_db(), developer).permissions
    with_layers = RBACService.get_effective_permissions(
        _db(
            custom_permissions=[{"resource": "experiment", "actions": []}],
            grants=[("experiment", []), ("report", ["update", "create"])],
        ),
        developer,
    ).permissions
    # Nothing a layer holds removes what the base role gives: no deny.
    for resource, actions in base.items():
        assert set(actions) <= set(with_layers[resource]), resource
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
    }.items():
        assert grant[name] == kind, name
    assert DirectPermissionGrant.__table__.c.expires_at.nullable


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
