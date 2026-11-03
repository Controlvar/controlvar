"""The dashboard's permission table equals the server's (#891, check #41).

``frontend/src/tests/fixtures/permission-table.json`` is the truth the
dashboard's Jest matrix (``frontend/src/tests/utils/permissionMatrix.test.ts``,
run by both profiles' Jest) holds every permission helper to:

* ``role_permissions``: which built-in roles hold each experiment and
  feature-flag action (``ROLE_PERMISSIONS``);
* ``audit_log_read_roles``: which read other users' audit entries
  (``AUDIT_LOG_READ_ALL_ROLES``);
* ``administer_eligibility_roles``: which may archive exclusion groups and run
  global holdouts (what both ``_require_admin`` functions admit);
* ``grantable``: the eleven pairs a custom role or direct grant can hold
  (``GRANTABLE``).

This test builds the table again from those and compares it with the file,
so the JSON cannot drift from the backend. After a change on either side,
write it again (from the repository root) with::

    python -m backend.tests.unit.core.test_dashboard_permission_table --write
"""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from backend.app.api.v1.endpoints import global_holdout, mutual_exclusion_groups
from backend.app.core.permissions import (
    AUDIT_LOG_READ_ALL_ROLES,
    GRANTABLE,
    ROLE_PERMISSIONS,
    Action,
    ResourceType,
)
from backend.app.models.user import UserRole

pytestmark = [pytest.mark.unit]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
TABLE = REPO_ROOT / "frontend" / "src" / "tests" / "fixtures" / "permission-table.json"
WRITE_COMMAND = (
    "python -m backend.tests.unit.core.test_dashboard_permission_table --write"
)

#: The resources the dashboard's helpers decide from ROLE_PERMISSIONS.
RESOURCES = (ResourceType.EXPERIMENT, ResourceType.FEATURE_FLAG)
#: The built-in roles, in the dashboard's order (``Role`` in services/api.ts).
ROLE_ORDER = ("ADMIN", "DEVELOPER", "ANALYST", "VIEWER")


def _admitted_by(require_admin) -> List[str]:
    admitted = []
    for name in ROLE_ORDER:
        try:
            require_admin(SimpleNamespace(role=UserRole[name], is_superuser=False))
        except HTTPException as refused:
            assert refused.status_code == 403
        else:
            admitted.append(name)
    return admitted


def build_table() -> Dict[str, Any]:
    """The table as the backend says it is now."""
    role_permissions = {
        resource.value: {
            action.value: [
                name
                for name in ROLE_ORDER
                if action in ROLE_PERMISSIONS.get(UserRole[name], {}).get(resource, [])
            ]
            for action in Action
        }
        for resource in RESOURCES
    }
    holdout_admins = _admitted_by(global_holdout._require_admin)
    group_admins = _admitted_by(mutual_exclusion_groups._require_admin)
    assert holdout_admins == group_admins, (holdout_admins, group_admins)
    return {
        "_generated_by": WRITE_COMMAND,
        "roles": list(ROLE_ORDER),
        "role_permissions": role_permissions,
        "audit_log_read_roles": [
            n for n in ROLE_ORDER if UserRole[n] in AUDIT_LOG_READ_ALL_ROLES
        ],
        "administer_eligibility_roles": holdout_admins,
        "grantable": [list(pair) for pair in sorted(GRANTABLE)],
    }


def render(table: Dict[str, Any]) -> str:
    return json.dumps(table, indent=2) + "\n"


def test_the_committed_table_is_the_servers():
    committed = json.loads(TABLE.read_text("utf-8"))
    assert committed == build_table(), (
        f"{TABLE.relative_to(REPO_ROOT)} differs from the backend; "
        f"write it again with: {WRITE_COMMAND}"
    )


def test_the_table_is_not_empty():
    # A table built from an empty source would compare equal to an empty file.
    table = build_table()
    assert len(table["grantable"]) == 11
    assert table["roles"] == ["ADMIN", "DEVELOPER", "ANALYST", "VIEWER"]
    assert table["role_permissions"]["experiment"]["create"] == ["ADMIN", "DEVELOPER"]
    assert table["role_permissions"]["feature_flag"]["update"] == ["ADMIN", "DEVELOPER"]
    assert table["audit_log_read_roles"] == ["ADMIN", "ANALYST"]
    assert table["administer_eligibility_roles"] == ["ADMIN"]


def test_a_changed_role_list_is_a_difference():
    # The positive control: the comparison above sees a single role moved.
    table = build_table()
    changed = json.loads(render(table))
    changed["role_permissions"]["experiment"]["create"].append("ANALYST")
    assert changed != table


if __name__ == "__main__":  # pragma: no cover - the writer
    if sys.argv[1:] != ["--write"]:
        sys.exit(f"usage: {WRITE_COMMAND}")
    TABLE.write_text(render(build_table()), "utf-8")
    print(f"wrote {TABLE.relative_to(REPO_ROOT)}")
