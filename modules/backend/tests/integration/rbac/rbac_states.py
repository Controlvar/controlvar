"""Grant states for the #891 enforcement tests, planted by SQL.

Every state is written straight into the tables, the way a row would exist
after the API (or an older release) wrote it, so the tests decide nothing
about how a row came to be. An expiry is set relative to the database's own
clock (``now() - interval``), never by sleeping.

``STATES`` are the eight states of check #30: two of them take effect
(``role-active`` and ``grant-active``), six do not.
"""

from __future__ import annotations

import json
import uuid
from typing import Iterable, List, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

#: The eleven pairs a custom role or a direct grant can hold, written out here
#: so the tests' oracle does not read the constant it checks.
ELEVEN = frozenset(
    {("experiment", a) for a in ("create", "read", "update", "delete", "list")}
    | {("feature_flag", a) for a in ("create", "read", "update", "delete", "list")}
    | {("audit_log", "read")}
)

#: The eight grant states, in a fixed order; the two that take effect first.
STATES: Tuple[str, ...] = (
    "role-active",
    "grant-active",
    "none",
    "role-not-activated",
    "grant-not-activated",
    "grant-is-active-false",
    "grant-expired",
    "every-other-pair",
)

#: The states in which the pair under test takes effect.
ACTIVE_STATES = frozenset({"role-active", "grant-active"})


def _role(db: Session, tag: str, pairs: Iterable[Tuple[str, str]]) -> uuid.UUID:
    """A custom role holding *pairs*, grouped by resource, in request shape."""
    grouped: dict = {}
    for resource, action in pairs:
        grouped.setdefault(resource, []).append(action)
    permissions = [{"resource": r, "actions": a} for r, a in grouped.items()]
    role_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO custom_roles (id, name, description, is_system_role, "
            "permissions, created_at, updated_at) VALUES (:id, :name, NULL, false, "
            "CAST(:perms AS jsonb), now(), now())"
        ),
        {
            "id": role_id,
            "name": f"{tag}-{uuid.uuid4().hex[:10]}",
            "perms": json.dumps(permissions),
        },
    )
    return role_id


def assign(db: Session, user_id, role_id, *, activated: bool) -> None:
    db.execute(
        text(
            "INSERT INTO user_custom_roles (id, user_id, role_id, assigned_at, "
            "activated_at, created_at, updated_at) VALUES (:id, :user, :role, now(), "
            + ("now()" if activated else "NULL")
            + ", now(), now())"
        ),
        {"id": uuid.uuid4(), "user": user_id, "role": role_id},
    )


def grant(
    db: Session,
    user_id,
    resource: str,
    actions: List[str],
    *,
    activated: bool = True,
    is_active: bool = True,
    expires_sql: str = "NULL",
) -> None:
    """A direct grant row; *expires_sql* is SQL (``now() - interval '1 hour'``)."""
    db.execute(
        text(
            "INSERT INTO direct_permission_grants (id, user_id, resource, actions, "
            "expires_at, is_active, activated_at, created_at, updated_at) VALUES "
            "(:id, :user, :resource, CAST(:actions AS jsonb), "
            + expires_sql
            + ", :is_active, "
            + ("now()" if activated else "NULL")
            + ", now(), now())"
        ),
        {
            "id": uuid.uuid4(),
            "user": user_id,
            "resource": resource,
            "actions": json.dumps(actions),
            "is_active": is_active,
        },
    )


def plant(db: Session, user_id, pair: Tuple[str, str], state: str, tag: str) -> None:
    """Write *state* for *pair* and commit."""
    resource, action = pair
    if state == "none":
        pass
    elif state in ("role-active", "role-not-activated"):
        role_id = _role(db, tag, [pair])
        assign(db, user_id, role_id, activated=state == "role-active")
    elif state == "grant-active":
        grant(db, user_id, resource, [action])
    elif state == "grant-not-activated":
        grant(db, user_id, resource, [action], activated=False)
    elif state == "grant-is-active-false":
        grant(db, user_id, resource, [action], is_active=False)
    elif state == "grant-expired":
        grant(db, user_id, resource, [action], expires_sql="now() - interval '1 hour'")
    elif state == "every-other-pair":
        others = sorted(ELEVEN - {pair})
        role_id = _role(db, tag, others)
        assign(db, user_id, role_id, activated=True)
    else:  # pragma: no cover - a typo in a test
        raise ValueError(state)
    db.commit()


def cleanup(db: Session, tag: str, user_ids) -> None:
    """Delete what a test planted: its users' rows and its roles."""
    db.rollback()
    ids = list(user_ids)
    if ids:
        db.execute(
            text("DELETE FROM direct_permission_grants WHERE user_id = ANY(:ids)"),
            {"ids": ids},
        )
        db.execute(
            text("DELETE FROM user_custom_roles WHERE user_id = ANY(:ids)"),
            {"ids": ids},
        )
    db.execute(
        text("DELETE FROM custom_roles WHERE name LIKE :pattern"),
        {"pattern": f"{tag}-%"},
    )
    db.commit()
