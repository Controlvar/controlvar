"""
RBAC Post-MVP schemas — API contract for custom roles and permission management.
"""

from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

#: Every (resource, action) pair a custom role or a direct grant may hold
#: (#891). A request that would store any other pair is refused with a 422 of
#: type ``not_grantable`` (:func:`not_grantable_message`), and nothing is
#: written. Roles and grants stored before the check read back unchanged.
GRANTABLE: frozenset = frozenset(
    {("experiment", a) for a in ("create", "read", "update", "delete", "list")}
    | {("feature_flag", a) for a in ("create", "read", "update", "delete", "list")}
    | {("audit_log", "read")}
)

#: The end of the ``not_grantable`` message: what may be held, in words.
NOT_GRANTABLE_TAIL = (
    "cannot be granted. A custom role or a direct grant can hold only "
    "experiment (create, read, update, delete, list), "
    "feature_flag (create, read, update, delete, list) and audit_log (read)"
)


def _text(value: object) -> str:
    """An enum member's value, or the string itself."""
    return value.value if isinstance(value, Enum) else str(value)


def not_grantable_message(
    grants: Iterable[Tuple[object, Iterable[object]]],
) -> Optional[str]:
    """The ``not_grantable`` message for *grants*, or None if all may be held.

    *grants* is (resource, actions) in request order. Each resource with a
    refused action is named once, as ``resource (action, action)``, with only
    its refused actions, in the order they were sent and without repeats; the
    resources are joined as "A", "A and B", "A, B and C".
    """
    refused: Dict[str, List[str]] = {}
    for resource, actions in grants:
        name = _text(resource)
        for action in actions:
            verb = _text(action)
            if (name, verb) in GRANTABLE:
                continue
            listed = refused.setdefault(name, [])
            if verb not in listed:
                listed.append(verb)
    if not refused:
        return None
    parts = [f"{name} ({', '.join(verbs)})" for name, verbs in refused.items()]
    named = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{named} {NOT_GRANTABLE_TAIL}"


def _refuse_ungrantable(grants: Iterable[Tuple[object, Iterable[object]]]) -> None:
    message = not_grantable_message(grants)
    if message is not None:
        raise PydanticCustomError("not_grantable", message)


class PermissionAction(str, Enum):
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LIST = "list"


class PermissionResource(str, Enum):
    EXPERIMENT = "experiment"
    FEATURE_FLAG = "feature_flag"
    USER = "user"
    ROLE = "role"
    PERMISSION = "permission"
    REPORT = "report"
    AUDIT_LOG = "audit_log"
    EXPORT = "export"


class PermissionGrant(BaseModel):
    resource: PermissionResource
    actions: List[PermissionAction] = Field(..., min_length=1)


class CustomRoleCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    description: Optional[str] = Field(None, max_length=256)
    permissions: List[PermissionGrant] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _only_grantable(cls, value: List[PermissionGrant]) -> List[PermissionGrant]:
        # A field validator, so the error's loc is ["body", "permissions"].
        _refuse_ungrantable((p.resource, p.actions) for p in value)
        return value


# Rebuild to resolve forward references (PermissionGrant used in CustomRoleCreate)
CustomRoleCreate.model_rebuild()


class CustomRoleUpdate(BaseModel):
    description: Optional[str] = Field(None, max_length=256)
    permissions: Optional[List[PermissionGrant]] = None

    @field_validator("permissions")
    @classmethod
    def _only_grantable(
        cls, value: Optional[List[PermissionGrant]]
    ) -> Optional[List[PermissionGrant]]:
        # Only when permissions are sent: a description-only update of a role
        # stored with other pairs is accepted.
        if value is not None:
            _refuse_ungrantable((p.resource, p.actions) for p in value)
        return value


class CustomRoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: Optional[str]
    is_system_role: bool  # True for the 4 built-in roles
    permissions: List[PermissionGrant]
    created_at: datetime
    user_count: int = 0  # How many users have this role


class AssignRoleRequest(BaseModel):
    user_id: str
    role_name: str
    reason: Optional[str] = Field(None, max_length=256)


class RevokeRoleRequest(BaseModel):
    user_id: str
    role_name: str
    reason: Optional[str] = Field(None, max_length=256)


class EffectivePermissionsResponse(BaseModel):
    """Complete resolved permission set for a user."""

    user_id: str
    username: str
    base_role: str  # The user.role field (admin/developer/analyst/viewer)
    custom_roles: List[str]  # Names of custom roles assigned to this user
    permissions: Dict[str, List[str]]  # {"experiment": ["read", "list"], ...}
    is_superuser: bool


class DelegatePermissionRequest(BaseModel):
    """Grant a specific permission to a user directly (not via role)."""

    user_id: str
    resource: PermissionResource
    actions: List[PermissionAction] = Field(..., min_length=1)
    reason: Optional[str] = Field(None, max_length=256)
    expires_at: Optional[datetime] = None  # Optional expiry for temporary grants

    @model_validator(mode="after")
    def _only_grantable(self) -> "DelegatePermissionRequest":
        # A model validator: the pair is two fields, so the loc is ["body"].
        _refuse_ungrantable([(self.resource, self.actions)])
        return self
