from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.state import InstanceState

from backend.app.core import optional_modules
from backend.app.models.user import UserRole


class ResourceType(str, Enum):
    """Types of resources that can be protected."""

    EXPERIMENT = "experiment"
    FEATURE_FLAG = "feature_flag"
    USER = "user"
    ROLE = "role"
    PERMISSION = "permission"
    REPORT = "report"
    # User-owned SDK keys (``/api/v1/api-keys``).  Every authenticated user
    # manages their *own* keys without consulting this resource; it governs
    # acting on *other* users' keys (``?all=true`` listing, deleting someone
    # else's key), which only ADMIN may do.
    API_KEY = "api_key"


class Action(str, Enum):
    """Actions that can be performed on resources."""

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LIST = "list"


# Role-based access control (RBAC) permissions mapping
ROLE_PERMISSIONS: Dict[UserRole, Dict[ResourceType, List[Action]]] = {
    UserRole.ADMIN: {
        ResourceType.EXPERIMENT: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.FEATURE_FLAG: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.USER: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.ROLE: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.PERMISSION: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.REPORT: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.API_KEY: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
    },
    UserRole.DEVELOPER: {
        ResourceType.API_KEY: [Action.READ],
        ResourceType.EXPERIMENT: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.FEATURE_FLAG: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
        ResourceType.USER: [Action.READ, Action.LIST],
        ResourceType.ROLE: [Action.READ, Action.LIST],
        ResourceType.PERMISSION: [Action.READ],
        ResourceType.REPORT: [Action.READ, Action.LIST],
    },
    UserRole.ANALYST: {
        ResourceType.API_KEY: [Action.READ],
        ResourceType.EXPERIMENT: [Action.READ, Action.LIST],
        ResourceType.FEATURE_FLAG: [Action.READ, Action.LIST],
        ResourceType.USER: [Action.READ],
        ResourceType.ROLE: [Action.READ],
        ResourceType.PERMISSION: [Action.READ],
        ResourceType.REPORT: [
            Action.CREATE,
            Action.READ,
            Action.UPDATE,
            Action.DELETE,
            Action.LIST,
        ],
    },
    UserRole.VIEWER: {
        ResourceType.API_KEY: [Action.READ],
        ResourceType.EXPERIMENT: [Action.READ, Action.LIST],
        ResourceType.FEATURE_FLAG: [Action.READ, Action.LIST],
        ResourceType.USER: [Action.READ],
        ResourceType.ROLE: [Action.READ],
        ResourceType.PERMISSION: [Action.READ],
        ResourceType.REPORT: [Action.READ, Action.LIST],
    },
}


def has_permission(role: UserRole, resource: ResourceType, action: Action) -> bool:
    """Check if a role has permission to perform an action on a resource."""
    if role not in ROLE_PERMISSIONS:
        return False
    if resource not in ROLE_PERMISSIONS[role]:
        return False
    return action in ROLE_PERMISSIONS[role][resource]


def check_permission(user: Any, resource: ResourceType, action: Action) -> bool:
    """
    Check if a user has permission to perform an action on a resource.

    Args:
        user: The user to check permissions for
        resource: The resource type being accessed
        action: The action being performed

    Returns:
        bool: True if the user has permission, False otherwise
    """
    # Superusers always have all permissions
    if hasattr(user, "is_superuser") and user.is_superuser:
        return True

    # Get user's role
    try:
        role = getattr(user, "role", UserRole.VIEWER)
    except (AttributeError, TypeError):
        # Default to Viewer role if there's any issue
        role = UserRole.VIEWER

    # Check role permissions. A user whose role is None gets nothing here.
    if has_permission(role, resource, action):
        return True

    # Then what the user's custom roles and direct grants hold (full profile;
    # always empty in the core profile). Asked only when the role says no, so
    # a check the role allows reads nothing more.
    return _pair(resource, action) in granted_permissions(user)


#: Every (resource, action) pair a custom role or a direct grant can hold
#: (#891): exactly the pairs a permission check reads. The RBAC module refuses
#: any other pair with a 422 when a role or grant is written, and
#: :func:`granted_permissions` drops any other pair it is given, so a pair
#: stored before that check (``report``, ``export``, ``user`` ...) is kept and
#: shown but never takes effect.
#:
#: Widening this set activates, at once, every pair already stored in an
#: active custom role or direct grant that holds it: nothing re-checks a stored
#: row. Widening needs its own plan.
GRANTABLE: FrozenSet[Tuple[str, str]] = frozenset(
    {("experiment", a) for a in ("create", "read", "update", "delete", "list")}
    | {("feature_flag", a) for a in ("create", "read", "update", "delete", "list")}
    | {("audit_log", "read")}
)

#: Where :func:`granted_permissions` keeps a session's answers: one dict per
#: session, keyed by the user's id.
GRANTED_PERMISSIONS_CACHE_KEY = "controlvar.granted_permissions"


def _pair(resource: Any, action: Any) -> Tuple[str, str]:
    return (
        str(getattr(resource, "value", resource)),
        str(getattr(action, "value", action)),
    )


def granted_permissions(user: Any) -> FrozenSet[Tuple[str, str]]:
    """The grantable pairs *user*'s active custom roles and direct grants hold.

    Empty when no provider is registered (the core profile, or a process that
    mounted no module routes), and when *user* is not a row bound to a
    ``Session``: a ``SimpleNamespace``, a mock, a transient, pending or
    detached instance all get nothing, and the provider is not called. The
    strict test matters: ``inspect`` of a ``MagicMock`` answers a mock whose
    "session" is a mock too.

    Otherwise the provider (the ``rbac.granted_permissions`` capability) is
    asked once per user per session, on that session, and its answer,
    filtered to :data:`GRANTABLE`, is kept in ``session.info`` for the
    session's life. A request has its own session, so a grant or revoke made
    by one request is seen by the next. :func:`forget_granted_permissions`
    drops what a session has kept; the RBAC writers call it on their own
    session.

    A provider that raises is not caught: the check that would have been
    refused becomes an error, and nothing is kept.
    """
    provider = optional_modules.granted_permissions_provider()
    if provider is None:
        return frozenset()
    state = sqlalchemy_inspect(user, raiseerr=False)
    if not isinstance(state, InstanceState):
        return frozenset()
    session = state.session
    if not isinstance(session, Session):
        return frozenset()
    identity = state.identity
    if not identity:
        return frozenset()
    user_id = identity[0]
    cache = session.info.setdefault(GRANTED_PERMISSIONS_CACHE_KEY, {})
    key = str(user_id)
    if key not in cache:
        pairs = frozenset(_pair(r, a) for r, a in provider(session, user_id))
        cache[key] = pairs & GRANTABLE
    return cache[key]


def forget_granted_permissions(session: Any) -> None:
    """Drop every answer :func:`granted_permissions` kept on *session*."""
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info.pop(GRANTED_PERMISSIONS_CACHE_KEY, None)


# Roles that read every audit log entry. Everyone else reads only the entries
# they made themselves. This is a standalone predicate rather than a
# ResourceType in ROLE_PERMISSIONS: a new ResourceType member would also be
# picked up by the custom-role seeding in the RBAC module.
AUDIT_LOG_READ_ALL_ROLES = frozenset({UserRole.ADMIN, UserRole.ANALYST})


def can_read_all_audit_logs(user: Any) -> bool:
    """Return True if *user* may read audit log entries made by other users.

    Superusers, ADMIN and ANALYST, and in the full profile anyone whose custom
    role or direct grant gives ``audit_log (read)``, read other users' entries
    too. Anyone else reads only their own entries, and is refused the
    per-entity history and the statistics, which aggregate across users. The
    compliance audit events, reports and exports do not ask this.
    """
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "role", None) in AUDIT_LOG_READ_ALL_ROLES:
        return True
    return ("audit_log", "read") in granted_permissions(user)


def check_ownership(user: Any, resource_obj: Any) -> bool:
    """
    Check if a user owns a resource.

    Args:
        user: The user to check ownership for
        resource_obj: The specific resource instance

    Returns:
        bool: True if the user owns the resource, False otherwise
    """
    # Check if the resource has an owner_id attribute
    if hasattr(resource_obj, "owner_id") and hasattr(user, "id"):
        return str(resource_obj.owner_id) == str(user.id)
    return False


class Scope(str, Enum):
    """Which feature flags a role's permission to act on flags extends to."""

    ANY = "any"  # every flag, including one with no owner
    OWN = "own"  # only flags whose owner_id is the user's id; no owner never matches


# Whose flags each role may act on, once ROLE_PERMISSIONS has said the role may
# take the action at all.  Access to feature flags is by permission, not by who
# created the flag: ADMIN and DEVELOPER act on any flag; ANALYST and VIEWER read
# any flag and, by their role, change none (ROLE_PERMISSIONS gives them no UPDATE
# or DELETE).  A custom role or direct grant (full profile) of feature_flag
# create, update or delete admits any flag and never reads this table.  The
# owner is recorded and shown, but it is not an access rule.
#
# Intent: per-flag restrictions (locking a flag to its owner, approvals) belong in
# this table, not in ROLE_PERMISSIONS.  An entry missing here means OWN, the
# narrowest scope that still lets an owner work.
#
# Rule (#891): this table binds the built-in roles only.  A holder of a custom
# role or direct grant of a feature_flag action is admitted to ANY flag before
# this table is read (can_act_on_feature_flag), so a per-flag lock added here
# would not bind them.  Such a lock must also be checked on the grant path.
FEATURE_FLAG_SCOPE: Dict[UserRole, Dict[Action, Scope]] = {
    UserRole.ADMIN: {
        Action.READ: Scope.ANY,
        Action.UPDATE: Scope.ANY,
        Action.DELETE: Scope.ANY,
    },
    UserRole.DEVELOPER: {
        Action.READ: Scope.ANY,
        Action.UPDATE: Scope.ANY,
        Action.DELETE: Scope.ANY,
    },
    UserRole.ANALYST: {Action.READ: Scope.ANY},
    UserRole.VIEWER: {Action.READ: Scope.ANY},
}


def can_act_on_feature_flag(
    user: Any,
    owner_id: Optional[Any],
    action: Action,
    scope_table: Optional[Dict[UserRole, Dict[Action, Scope]]] = None,
) -> bool:
    """Whether *user* may take *action* on a feature flag owned by *owner_id*.

    The one decision for every per-flag route.  A superuser may always act.
    When the built-in role holds *action* on feature flags in ROLE_PERMISSIONS,
    the scope table decides whose flags the permission covers.  Otherwise a
    custom role or direct grant of ``feature_flag (<action>)`` (full profile)
    admits any flag, without reading the scope table (access is by
    permission, not ownership); without one the answer is no, so owning a flag
    never lets a read-only role change it.

    *owner_id* comes from the stored flag, never from a response dict (which
    renders a missing owner as the string ``"None"``).  *scope_table* exists for
    tests; callers leave it at the default.
    """
    if getattr(user, "is_superuser", False):
        return True
    try:
        role = getattr(user, "role", UserRole.VIEWER)
    except (AttributeError, TypeError):
        role = UserRole.VIEWER
    if not has_permission(role, ResourceType.FEATURE_FLAG, action):
        return _pair(ResourceType.FEATURE_FLAG, action) in granted_permissions(user)
    table = FEATURE_FLAG_SCOPE if scope_table is None else scope_table
    scope = table.get(role, {}).get(action, Scope.OWN)
    if scope is Scope.ANY:
        return True
    return owner_id is not None and str(owner_id) == str(getattr(user, "id", None))


def get_permission_error_message(resource: ResourceType, action: Action) -> str:
    """
    Get a user-friendly error message for permission denial.

    Args:
        resource: The resource being accessed
        action: The action being performed

    Returns:
        str: A formatted error message
    """
    action_names = {
        Action.CREATE: "create",
        Action.READ: "view",
        Action.UPDATE: "update",
        Action.DELETE: "delete",
        Action.LIST: "list",
    }

    resource_names = {
        ResourceType.EXPERIMENT: "experiments",
        ResourceType.FEATURE_FLAG: "feature flags",
        ResourceType.USER: "users",
        ResourceType.ROLE: "roles",
        ResourceType.PERMISSION: "permissions",
        ResourceType.REPORT: "reports",
    }

    action_name = action_names.get(action, str(action))
    resource_name = resource_names.get(resource, str(resource))

    return f"You don't have permission to {action_name} {resource_name}"


def get_required_role(resource: ResourceType, action: Action) -> Optional[UserRole]:
    """
    Determine the minimum role required for an action on a resource.

    Args:
        resource: The resource being accessed
        action: The action being performed

    Returns:
        UserRole: The minimum role required or None if no role has permission
    """
    for role in [UserRole.VIEWER, UserRole.ANALYST, UserRole.DEVELOPER, UserRole.ADMIN]:
        if has_permission(role, resource, action):
            return role

    # If no role has permission
    return None
