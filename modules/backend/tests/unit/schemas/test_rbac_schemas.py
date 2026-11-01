"""Unit tests for RBAC Pydantic schemas."""

import pytest
from pydantic import ValidationError

from modules.backend.app.schemas.rbac import (
    GRANTABLE,
    AssignRoleRequest,
    CustomRoleCreate,
    CustomRoleUpdate,
    DelegatePermissionRequest,
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    not_grantable_message,
)


class TestCustomRoleCreate:
    def test_valid_role_name(self):
        role = CustomRoleCreate(name="data-scientist", permissions=[])
        assert role.name == "data-scientist"

    def test_name_must_start_with_letter(self):
        with pytest.raises(ValidationError):
            CustomRoleCreate(name="1invalid", permissions=[])

    def test_name_uppercase_rejected(self):
        with pytest.raises(ValidationError):
            CustomRoleCreate(name="MyRole", permissions=[])

    def test_name_too_short_rejected(self):
        with pytest.raises(ValidationError):
            CustomRoleCreate(name="a", permissions=[])

    def test_name_with_underscores_valid(self):
        role = CustomRoleCreate(name="read_only_experiments", permissions=[])
        assert role.name == "read_only_experiments"

    def test_permissions_default_empty(self):
        role = CustomRoleCreate(name="empty-role", permissions=[])
        assert role.permissions == []

    def test_description_optional(self):
        role = CustomRoleCreate(name="my-role", permissions=[])
        assert role.description is None


class TestPermissionGrant:
    def test_valid_grant(self):
        grant = PermissionGrant(
            resource=PermissionResource.EXPERIMENT, actions=[PermissionAction.READ]
        )
        assert grant.resource == PermissionResource.EXPERIMENT

    def test_empty_actions_rejected(self):
        with pytest.raises(ValidationError):
            PermissionGrant(resource=PermissionResource.EXPERIMENT, actions=[])

    def test_multiple_actions(self):
        grant = PermissionGrant(
            resource=PermissionResource.FEATURE_FLAG,
            actions=[PermissionAction.READ, PermissionAction.LIST],
        )
        assert len(grant.actions) == 2


class TestAssignRoleRequest:
    def test_valid_request(self):
        req = AssignRoleRequest(user_id="some-uuid", role_name="analyst-plus")
        assert req.role_name == "analyst-plus"

    def test_reason_optional(self):
        req = AssignRoleRequest(user_id="uid", role_name="role")
        assert req.reason is None


class TestDelegatePermissionRequest:
    def test_valid_delegation(self):
        req = DelegatePermissionRequest(
            user_id="uid",
            resource=PermissionResource.EXPERIMENT,
            actions=[PermissionAction.CREATE],
        )
        assert req.resource == PermissionResource.EXPERIMENT

    def test_expires_at_optional(self):
        req = DelegatePermissionRequest(
            user_id="uid",
            resource=PermissionResource.AUDIT_LOG,
            actions=[PermissionAction.READ],
        )
        assert req.expires_at is None


# --- what a role or a grant may hold (#891) ------------------------------------

#: The eleven pairs, written out: what the refusal must leave through.
ELEVEN = {
    ("experiment", "create"),
    ("experiment", "read"),
    ("experiment", "update"),
    ("experiment", "delete"),
    ("experiment", "list"),
    ("feature_flag", "create"),
    ("feature_flag", "read"),
    ("feature_flag", "update"),
    ("feature_flag", "delete"),
    ("feature_flag", "list"),
    ("audit_log", "read"),
}

TAIL = (
    "cannot be granted. A custom role or a direct grant can hold only "
    "experiment (create, read, update, delete, list), feature_flag (create, read, "
    "update, delete, list) and audit_log (read)"
)


def _role_create(resource, actions):
    return CustomRoleCreate(
        name="r1", permissions=[{"resource": resource, "actions": actions}]
    )


def _role_update(resource, actions):
    return CustomRoleUpdate(permissions=[{"resource": resource, "actions": actions}])


def _grant(resource, actions):
    return DelegatePermissionRequest(user_id="u", resource=resource, actions=actions)


MODELS = {
    "CustomRoleCreate": (_role_create, ("permissions",)),
    "CustomRoleUpdate": (_role_update, ("permissions",)),
    "DelegatePermissionRequest": (_grant, ()),
}


def _refusal(build, resource, actions):
    """The one error a refused request gives, or None if it is accepted."""
    try:
        build(resource, actions)
    except ValidationError as error:
        (only,) = error.errors()
        return only
    return None


@pytest.mark.regression
@pytest.mark.parametrize("model", sorted(MODELS))
def test_exactly_the_eleven_pairs_are_accepted(model):
    """Generated over every resource and action the request models know
    (8 x 5 = 40): exactly the eleven are accepted, and each refusal names its
    own pair. On main every one of the 40 was accepted."""
    build, loc = MODELS[model]
    pairs = [(r.value, a.value) for r in PermissionResource for a in PermissionAction]
    assert len(pairs) == 40
    accepted = set()
    for resource, action in pairs:
        refusal = _refusal(build, resource, [action])
        if refusal is None:
            accepted.add((resource, action))
            continue
        assert refusal["type"] == "not_grantable", (resource, action, refusal)
        assert tuple(refusal["loc"]) == loc, (resource, action, refusal["loc"])
        assert refusal["msg"] == f"{resource} ({action}) {TAIL}"
    assert accepted == ELEVEN, (
        f"{model} accepted {len(accepted)}, expected 11: "
        f"extra {sorted(accepted - ELEVEN)}, missing {sorted(ELEVEN - accepted)}"
    )


def test_the_set_is_the_eleven_pairs():
    assert set(GRANTABLE) == ELEVEN
    assert len(GRANTABLE) == 11


def test_the_message_names_only_the_refused_actions_in_request_order():
    refusal = _refusal(_role_create, "audit_log", ["read", "list", "list", "create"])
    assert refusal["msg"] == f"audit_log (list, create) {TAIL}"


@pytest.mark.parametrize(
    "permissions, named",
    [
        ([{"resource": "export", "actions": ["read"]}], "export (read)"),
        (
            [
                {"resource": "export", "actions": ["read"]},
                {"resource": "report", "actions": ["read", "list"]},
            ],
            "export (read) and report (read, list)",
        ),
        (
            [
                {"resource": "role", "actions": ["create"]},
                {"resource": "experiment", "actions": ["read"]},
                {"resource": "export", "actions": ["read"]},
                {"resource": "role", "actions": ["create", "delete"]},
                {"resource": "audit_log", "actions": ["read", "list"]},
            ],
            "role (create, delete), export (read) and audit_log (list)",
        ),
    ],
    ids=["one", "two", "three, repeats merged"],
)
def test_the_refused_resources_are_named_once_in_request_order(permissions, named):
    with pytest.raises(ValidationError) as raised:
        CustomRoleCreate(name="r1", permissions=permissions)
    (only,) = raised.value.errors()
    assert only["msg"] == f"{named} {TAIL}"
    assert (
        not_grantable_message((p["resource"], p["actions"]) for p in permissions)
        == f"{named} {TAIL}"
    )


def test_a_role_update_without_permissions_is_not_checked():
    assert CustomRoleUpdate(description="only the description").permissions is None
    assert CustomRoleUpdate(permissions=[]).permissions == []


def test_the_response_item_is_not_checked():
    """A stored legacy role still reads: ``PermissionGrant`` is also the item
    of ``CustomRoleResponse`` and carries no refusal."""
    assert PermissionGrant(resource="export", actions=["read"]).resource == "export"
