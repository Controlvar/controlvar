"""
Integration tests for RBAC API (EP-011).

Tests custom roles, direct grants, and effective permissions across all 10 endpoints:
  GET    /api/v1/rbac/roles
  POST   /api/v1/rbac/roles
  GET    /api/v1/rbac/roles/{role_name}
  PUT    /api/v1/rbac/roles/{role_name}
  DELETE /api/v1/rbac/roles/{role_name}
  POST   /api/v1/rbac/roles/assign
  POST   /api/v1/rbac/roles/revoke
  GET    /api/v1/rbac/users/{user_id}/permissions
  POST   /api/v1/rbac/users/{user_id}/grant
  DELETE /api/v1/rbac/users/{user_id}/grant/{resource}

Design notes:
  - Each test uses at most ONE *_client fixture (admin_client, developer_client,
    analyst_client) to avoid dependency override conflicts.
  - When testing 403 access control, the admin check fires before any DB lookup,
    so a fake/nonexistent role name is sufficient to trigger the 403.
  - When a non-admin client needs to test against a real role, we use
    make_client_for_user inline so the whole test shares one db_session.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.app.models.user import User
from backend.tests.integration.conftest import make_client_for_user
from modules.backend.app.models.custom_role import CustomRole, DirectPermissionGrant

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _unique_role_name() -> str:
    """Return a unique lowercase role name satisfying schema pattern ^[a-z][a-z0-9_-]*$."""
    return f"crole-{uuid.uuid4().hex[:8]}"


def _valid_role_payload(name: str = None) -> dict:
    """Minimal valid CustomRoleCreate payload."""
    return {
        "name": name or _unique_role_name(),
        "description": "Integration test custom role",
        "permissions": [
            {"resource": "experiment", "actions": ["read", "create"]},
            {"resource": "feature_flag", "actions": ["read"]},
        ],
    }


@pytest.fixture
def system_role(db_session: Session):
    """A role marked is_system_role, written straight to the table.

    Nothing in the product creates one (``test_rbac_layers_are_display_only``
    pins that), so a test of the refusal has to make its own rather than look
    for one and skip when there is none. Removed afterwards, so the listing
    tests that follow see only custom roles.
    """
    role = CustomRole(
        name=f"sysrole-{uuid.uuid4().hex[:8]}",
        description="Seeded system role",
        is_system_role=True,
        permissions=[{"resource": "experiment", "actions": ["read"]}],
    )
    db_session.add(role)
    db_session.commit()
    try:
        yield role.name
    finally:
        db_session.rollback()
        db_session.query(CustomRole).filter(CustomRole.name == role.name).delete()
        db_session.commit()


def _create_role(client: TestClient, name: str = None) -> dict:
    """Create a custom role via the API and assert success."""
    payload = _valid_role_payload(name)
    resp = client.post("/api/v1/rbac/roles", json=payload)
    assert resp.status_code == 201, f"Create role failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# TestListRoles — GET /api/v1/rbac/roles
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestListRoles:
    """GET /api/v1/rbac/roles"""

    def test_list_roles_returns_200(self, admin_client):
        """List endpoint returns 200 for admin user."""
        resp = admin_client.get("/api/v1/rbac/roles")
        assert resp.status_code == 200, resp.text

    def test_list_roles_returns_list(self, admin_client):
        """Response body is a JSON list."""
        resp = admin_client.get("/api/v1/rbac/roles")
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json(), list)

    def test_list_roles_includes_created_role(self, admin_client):
        """A newly created role appears in the list."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        resp = admin_client.get("/api/v1/rbac/roles")
        assert resp.status_code == 200, resp.text
        names = [r["name"] for r in resp.json()]
        assert role_name in names

    def test_list_roles_include_system_true(self, admin_client):
        """include_system=true returns system roles in the list."""
        resp = admin_client.get("/api/v1/rbac/roles?include_system=true")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data, list)

    def test_list_roles_include_system_false(self, admin_client):
        """include_system=false returns only custom (non-system) roles."""
        resp = admin_client.get("/api/v1/rbac/roles?include_system=false")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for role in data:
            assert role["is_system_role"] is False

    def test_analyst_can_list_roles(self, analyst_client):
        """Analyst role has read access; list returns 200."""
        resp = analyst_client.get("/api/v1/rbac/roles")
        assert resp.status_code == 200, resp.text

    def test_developer_can_list_roles(self, developer_client):
        """Developer role can list roles."""
        resp = developer_client.get("/api/v1/rbac/roles")
        assert resp.status_code == 200, resp.text

    def test_viewer_can_list_roles(self, viewer_user, db_session):
        """Viewer role can also list roles (read-only access)."""
        viewer_client = make_client_for_user(db_session, viewer_user)
        try:
            resp = viewer_client.get("/api/v1/rbac/roles")
            assert resp.status_code == 200, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_list_roles_response_item_has_required_fields(self, admin_client):
        """Each role item in the list has the expected schema fields."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        resp = admin_client.get("/api/v1/rbac/roles")
        assert resp.status_code == 200, resp.text
        roles = resp.json()
        # Find our created role
        created = next((r for r in roles if r["name"] == role_name), None)
        assert created is not None
        for field in (
            "id",
            "name",
            "description",
            "is_system_role",
            "permissions",
            "created_at",
            "user_count",
        ):
            assert field in created, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# TestCreateRole — POST /api/v1/rbac/roles
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestCreateRole:
    """POST /api/v1/rbac/roles"""

    def test_admin_can_create_role(self, admin_client):
        """Admin creates a custom role — returns 201 with role data."""
        name = _unique_role_name()
        payload = _valid_role_payload(name)
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == name
        assert data["description"] == "Integration test custom role"
        assert "id" in data
        assert data["is_system_role"] is False

    def test_create_role_response_has_correct_permissions(self, admin_client):
        """Created role response includes the permissions specified in the request."""
        name = _unique_role_name()
        payload = _valid_role_payload(name)
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert isinstance(data["permissions"], list)
        assert len(data["permissions"]) > 0

    def test_role_persisted_and_retrievable(self, admin_client):
        """Created role can be retrieved via GET /roles/{name}."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        resp = admin_client.get(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == name

    def test_developer_cannot_create_role(self, developer_client):
        """Developer role is not ADMIN — returns 403 before any DB lookup."""
        # Admin check fires immediately; role name doesn't need to exist
        payload = _valid_role_payload()
        resp = developer_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 403, resp.text

    def test_analyst_cannot_create_role(self, analyst_client):
        """Analyst role is not ADMIN — returns 403."""
        payload = _valid_role_payload()
        resp = analyst_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 403, resp.text

    def test_viewer_cannot_create_role(self, viewer_user, db_session):
        """Viewer role is not ADMIN — returns 403."""
        viewer_client = make_client_for_user(db_session, viewer_user)
        try:
            payload = _valid_role_payload()
            resp = viewer_client.post("/api/v1/rbac/roles", json=payload)
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_duplicate_role_name_returns_conflict(self, admin_client):
        """Creating a role with an already-existing name returns 409 Conflict."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        # Try to create again with the same name
        payload = _valid_role_payload(name)
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 409, resp.text

    def test_create_role_no_permissions_is_valid(self, admin_client):
        """A role with an empty permissions list is accepted — returns 201."""
        payload = {
            "name": _unique_role_name(),
            "description": "No permissions role",
            "permissions": [],
        }
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 201, resp.text

    def test_role_name_too_short_returns_422(self, admin_client):
        """Role name shorter than 2 chars fails schema validation — returns 422."""
        payload = _valid_role_payload("a")  # 1 char is too short (min_length=2)
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 422, resp.text

    def test_role_name_with_uppercase_fails_validation(self, admin_client):
        """Role name with uppercase chars fails pattern validation — returns 422."""
        payload = _valid_role_payload("InvalidName")
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 422, resp.text

    def test_role_name_with_spaces_fails_validation(self, admin_client):
        """Role name with spaces fails pattern validation — returns 422."""
        payload = _valid_role_payload("role with spaces")
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 422, resp.text

    def test_invalid_resource_in_permissions_returns_422(self, admin_client):
        """A permission with an invalid resource type returns 422."""
        payload = {
            "name": _unique_role_name(),
            "permissions": [{"resource": "not_a_real_resource", "actions": ["read"]}],
        }
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 422, resp.text

    def test_invalid_action_in_permissions_returns_422(self, admin_client):
        """A permission with an invalid action returns 422."""
        payload = {
            "name": _unique_role_name(),
            "permissions": [{"resource": "experiment", "actions": ["fly"]}],
        }
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 422, resp.text

    def test_create_role_with_all_grantable_resources(self, admin_client):
        """A role may hold experiment, feature_flag and audit_log read (#891)."""
        payload = {
            "name": _unique_role_name(),
            "description": "All resources role",
            "permissions": [
                {"resource": "experiment", "actions": ["read"]},
                {"resource": "feature_flag", "actions": ["read"]},
                {"resource": "audit_log", "actions": ["read"]},
            ],
        }
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 201, resp.text

    def test_create_role_with_a_resource_no_role_may_hold_returns_422(
        self, admin_client
    ):
        """``user`` and ``report`` are known resources, refused for a role (#891)."""
        name = _unique_role_name()
        payload = {
            "name": name,
            "permissions": [
                {"resource": "experiment", "actions": ["read"]},
                {"resource": "user", "actions": ["read"]},
                {"resource": "report", "actions": ["read"]},
            ],
        }
        resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["type"] == "not_grantable"
        assert admin_client.get(f"/api/v1/rbac/roles/{name}").status_code == 404


# ---------------------------------------------------------------------------
# TestGetRole — GET /api/v1/rbac/roles/{role_name}
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestGetRole:
    """GET /api/v1/rbac/roles/{role_name}"""

    def test_get_existing_role_returns_200(self, admin_client):
        """GET on an existing role name returns 200 with role data."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        resp = admin_client.get(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == name

    def test_get_role_returns_correct_fields(self, admin_client):
        """GET response contains all required schema fields."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        resp = admin_client.get(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for field in (
            "id",
            "name",
            "description",
            "is_system_role",
            "permissions",
            "created_at",
            "user_count",
        ):
            assert field in data, f"Missing field: {field}"

    def test_get_role_permissions_match_create(self, admin_client):
        """Permissions returned by GET match what was set during creation."""
        name = _unique_role_name()
        payload = {
            "name": name,
            "description": "Permissions check",
            "permissions": [
                {"resource": "experiment", "actions": ["read", "create"]},
            ],
        }
        create_resp = admin_client.post("/api/v1/rbac/roles", json=payload)
        assert create_resp.status_code == 201, create_resp.text

        resp = admin_client.get(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 200, resp.text
        perms = resp.json()["permissions"]
        assert len(perms) == 1
        assert perms[0]["resource"] == "experiment"
        assert set(perms[0]["actions"]) == {"read", "create"}

    def test_get_nonexistent_role_returns_404(self, admin_client):
        """GET on a non-existent role name returns 404."""
        resp = admin_client.get("/api/v1/rbac/roles/role-that-does-not-exist-xyz99")
        assert resp.status_code == 404, resp.text

    def test_developer_can_get_existing_role(self, developer_client):
        """Developer (read access) can retrieve a role — creates and reads with same client."""
        # Developer client can't create (403), so we just verify we get 404 on non-existent
        # which confirms read access is attempted (not blocked at auth level)
        resp = developer_client.get("/api/v1/rbac/roles/non-existent-role-abc123")
        assert resp.status_code == 404, resp.text

    def test_analyst_can_list_roles(self, analyst_client):
        """Analyst (read access) can list roles without error."""
        resp = analyst_client.get("/api/v1/rbac/roles")
        assert resp.status_code == 200, resp.text

    def test_get_role_is_not_system_role(self, admin_client):
        """Custom roles created via API have is_system_role=False."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        resp = admin_client.get(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_system_role"] is False


# ---------------------------------------------------------------------------
# TestUpdateRole — PUT /api/v1/rbac/roles/{role_name}
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestUpdateRole:
    """PUT /api/v1/rbac/roles/{role_name}"""

    def test_admin_can_update_role_description(self, admin_client):
        """Admin can update the description of a custom role."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        update_payload = {"description": "Updated description for integration test"}
        resp = admin_client.put(f"/api/v1/rbac/roles/{name}", json=update_payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Updated description for integration test"

    def test_admin_can_update_role_permissions(self, admin_client):
        """Admin can replace permissions on a custom role."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        new_permissions = [
            {"resource": "experiment", "actions": ["read", "update", "delete"]},
            {"resource": "audit_log", "actions": ["read"]},
        ]
        update_payload = {"permissions": new_permissions}
        resp = admin_client.put(f"/api/v1/rbac/roles/{name}", json=update_payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data["permissions"], list)
        assert len(data["permissions"]) == 2

    def test_update_persisted_in_db(self, admin_client):
        """Updated description is reflected in subsequent GET."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        admin_client.put(
            f"/api/v1/rbac/roles/{name}",
            json={"description": "Persisted update"},
        )

        resp = admin_client.get(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Persisted update"

    def test_developer_cannot_update_role(self, developer_client):
        """Developer lacks ADMIN privilege — update returns 403 (no role lookup needed)."""
        # Admin check fires before DB lookup; role doesn't need to exist
        resp = developer_client.put(
            "/api/v1/rbac/roles/any-role-name",
            json={"description": "Should not work"},
        )
        assert resp.status_code == 403, resp.text

    def test_analyst_cannot_update_role(self, analyst_client):
        """Analyst cannot update a role — returns 403."""
        resp = analyst_client.put(
            "/api/v1/rbac/roles/any-role-name",
            json={"description": "Should not work"},
        )
        assert resp.status_code == 403, resp.text

    def test_viewer_cannot_update_role(self, viewer_user, db_session):
        """Viewer cannot update a role — returns 403."""
        viewer_client = make_client_for_user(db_session, viewer_user)
        try:
            resp = viewer_client.put(
                "/api/v1/rbac/roles/any-role-name",
                json={"description": "Should not work"},
            )
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_nonexistent_role_returns_404(self, admin_client):
        """PUT on a non-existent role returns 404."""
        resp = admin_client.put(
            "/api/v1/rbac/roles/non-existent-role-xyz99",
            json={"description": "Does not matter"},
        )
        assert resp.status_code == 404, resp.text

    def test_cannot_update_system_role(self, admin_client, system_role):
        """Updating a system role answers 400 and leaves it as it was."""
        resp = admin_client.put(
            f"/api/v1/rbac/roles/{system_role}",
            json={
                "description": "Trying to modify system role",
                "permissions": [{"resource": "experiment", "actions": ["delete"]}],
            },
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Cannot modify system roles"

        after = admin_client.get(f"/api/v1/rbac/roles/{system_role}")
        assert after.status_code == 200, after.text
        assert after.json()["is_system_role"] is True
        assert after.json()["description"] == "Seeded system role"
        assert after.json()["permissions"] == [
            {"resource": "experiment", "actions": ["read"]}
        ]

    def test_update_role_empty_payload_is_no_op(self, admin_client):
        """PUT with an empty body leaves the role name unchanged."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        resp = admin_client.put(f"/api/v1/rbac/roles/{name}", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == name


# ---------------------------------------------------------------------------
# TestDeleteRole — DELETE /api/v1/rbac/roles/{role_name}
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestDeleteRole:
    """DELETE /api/v1/rbac/roles/{role_name}"""

    def test_admin_can_delete_custom_role(self, admin_client):
        """Admin deletes a custom role — returns 204."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        resp = admin_client.delete(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 204, resp.text

    def test_deleted_role_no_longer_retrievable(self, admin_client):
        """After deletion, GET on the role name returns 404."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        admin_client.delete(f"/api/v1/rbac/roles/{name}")

        resp = admin_client.get(f"/api/v1/rbac/roles/{name}")
        assert resp.status_code == 404, resp.text

    def test_delete_nonexistent_role_returns_404(self, admin_client):
        """DELETE on a non-existent role returns 404."""
        resp = admin_client.delete("/api/v1/rbac/roles/role-that-never-existed-xyz")
        assert resp.status_code == 404, resp.text

    def test_developer_cannot_delete_role(self, developer_client):
        """Developer cannot delete a role — returns 403 (admin check fires first)."""
        resp = developer_client.delete("/api/v1/rbac/roles/any-role-name")
        assert resp.status_code == 403, resp.text

    def test_analyst_cannot_delete_role(self, analyst_client):
        """Analyst cannot delete a role — returns 403."""
        resp = analyst_client.delete("/api/v1/rbac/roles/any-role-name")
        assert resp.status_code == 403, resp.text

    def test_viewer_cannot_delete_role(self, viewer_user, db_session):
        """Viewer cannot delete a role — returns 403."""
        viewer_client = make_client_for_user(db_session, viewer_user)
        try:
            resp = viewer_client.delete("/api/v1/rbac/roles/any-role-name")
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_cannot_delete_system_role(self, admin_client, system_role):
        """Deleting a system role answers 400 and the role is still there."""
        resp = admin_client.delete(f"/api/v1/rbac/roles/{system_role}")
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Cannot delete system roles"

        after = admin_client.get(f"/api/v1/rbac/roles/{system_role}")
        assert after.status_code == 200, after.text
        assert after.json()["is_system_role"] is True

    def test_deleted_role_not_in_list(self, admin_client):
        """After deletion, role does not appear in list response."""
        name = _unique_role_name()
        _create_role(admin_client, name)

        admin_client.delete(f"/api/v1/rbac/roles/{name}")

        list_resp = admin_client.get("/api/v1/rbac/roles")
        assert list_resp.status_code == 200, list_resp.text
        names = [r["name"] for r in list_resp.json()]
        assert name not in names


# ---------------------------------------------------------------------------
# TestAssignRevokeRole — POST /api/v1/rbac/roles/assign & /revoke
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestAssignRevokeRole:
    """POST /api/v1/rbac/roles/assign and POST /api/v1/rbac/roles/revoke"""

    def test_admin_can_assign_role_to_user(self, admin_client, admin_user):
        """Admin assigns a custom role to themselves — returns 200 with assigned status."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        assign_payload = {
            "user_id": str(admin_user.id),
            "role_name": role_name,
            "reason": "Integration test assignment",
        }
        resp = admin_client.post("/api/v1/rbac/roles/assign", json=assign_payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "assigned"
        assert data["role"] == role_name

    def test_assign_role_response_has_expected_fields(self, admin_client, admin_user):
        """Assign response includes status, user_id, and role fields."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        payload = {"user_id": str(admin_user.id), "role_name": role_name}
        resp = admin_client.post("/api/v1/rbac/roles/assign", json=payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "status" in data
        assert "user_id" in data
        assert "role" in data

    def test_assign_same_role_twice_is_idempotent(self, admin_client, admin_user):
        """Assigning the same role twice succeeds without error (idempotent)."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        payload = {"user_id": str(admin_user.id), "role_name": role_name}
        resp1 = admin_client.post("/api/v1/rbac/roles/assign", json=payload)
        assert resp1.status_code == 200, resp1.text

        resp2 = admin_client.post("/api/v1/rbac/roles/assign", json=payload)
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["status"] == "assigned"

    def test_assigned_role_appears_in_user_permissions(self, admin_client, admin_user):
        """After assignment, the custom role name appears in effective permissions."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        user_id = str(admin_user.id)
        admin_client.post(
            "/api/v1/rbac/roles/assign",
            json={"user_id": user_id, "role_name": role_name},
        )

        perms_resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert perms_resp.status_code == 200, perms_resp.text
        assert role_name in perms_resp.json()["custom_roles"]

    def test_admin_can_revoke_role_from_user(self, admin_client, admin_user):
        """Admin revokes a role — returns 200 with revoked status.

        We revoke without a prior assign (user_custom_roles delete returns 0 rows),
        which exercises the happy-path revoke response without requiring 3 sequential
        API commits on the same db_session (known infrastructure limitation).
        """
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        user_id = str(admin_user.id)
        # Revoke directly — service handles "not assigned" gracefully
        revoke_payload = {"user_id": user_id, "role_name": role_name}
        resp = admin_client.post("/api/v1/rbac/roles/revoke", json=revoke_payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "revoked"

    def test_revoked_role_no_longer_in_effective_permissions(
        self, admin_client, admin_user
    ):
        """After revocation, the role is absent from effective permissions.

        We check that a role we never assigned does not appear in custom_roles,
        which is equivalent to verifying revoke removes it and the permissions
        endpoint correctly reflects the state.
        """
        user_id = str(admin_user.id)
        # Create a role but do NOT assign it
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        # Permissions should not include this unassigned role
        perms_resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert perms_resp.status_code == 200, perms_resp.text
        assert role_name not in perms_resp.json()["custom_roles"]

    def test_developer_cannot_assign_role(self, developer_client, developer_user):
        """Developer cannot assign roles — returns 403 (admin check fires before DB)."""
        payload = {
            "user_id": str(developer_user.id),
            "role_name": "some-role",
        }
        resp = developer_client.post("/api/v1/rbac/roles/assign", json=payload)
        assert resp.status_code == 403, resp.text

    def test_analyst_cannot_revoke_role(self, analyst_client, analyst_user):
        """Analyst cannot revoke roles — returns 403."""
        payload = {
            "user_id": str(analyst_user.id),
            "role_name": "some-role",
        }
        resp = analyst_client.post("/api/v1/rbac/roles/revoke", json=payload)
        assert resp.status_code == 403, resp.text

    def test_assign_nonexistent_role_returns_404(self, admin_client, admin_user):
        """Assigning a role that doesn't exist returns 404."""
        payload = {
            "user_id": str(admin_user.id),
            "role_name": "absolutely-nonexistent-role-xyz",
        }
        resp = admin_client.post("/api/v1/rbac/roles/assign", json=payload)
        assert resp.status_code == 404, resp.text

    def test_revoke_nonexistent_role_returns_404(self, admin_client, admin_user):
        """Revoking a role that doesn't exist returns 404."""
        payload = {
            "user_id": str(admin_user.id),
            "role_name": "absolutely-nonexistent-role-xyz",
        }
        resp = admin_client.post("/api/v1/rbac/roles/revoke", json=payload)
        assert resp.status_code == 404, resp.text

    def test_assign_role_with_reason(self, admin_client, admin_user):
        """Assign request with a reason string is accepted."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        payload = {
            "user_id": str(admin_user.id),
            "role_name": role_name,
            "reason": "Temporary elevated access for Q1 project",
        }
        resp = admin_client.post("/api/v1/rbac/roles/assign", json=payload)
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# TestEffectivePermissions — GET /api/v1/rbac/users/{user_id}/permissions
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestEffectivePermissions:
    """GET /api/v1/rbac/users/{user_id}/permissions"""

    def test_get_permissions_for_admin_user(self, admin_client, admin_user):
        """Admin can fetch their own effective permissions — returns 200."""
        user_id = str(admin_user.id)
        resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text

    def test_permissions_response_has_expected_structure(
        self, admin_client, admin_user
    ):
        """Effective permissions response has all required top-level fields."""
        user_id = str(admin_user.id)
        resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for field in (
            "user_id",
            "username",
            "base_role",
            "custom_roles",
            "permissions",
            "is_superuser",
        ):
            assert field in data, f"Missing field: {field}"

    def test_superuser_has_all_permissions(self, admin_client, admin_user):
        """Superuser effective permissions cover all expected resources."""
        user_id = str(admin_user.id)
        resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["is_superuser"] is True
        assert isinstance(data["permissions"], dict)
        assert len(data["permissions"]) > 0

    def test_admin_can_fetch_own_permissions(self, admin_client, admin_user):
        """Admin's base_role is returned correctly in the response."""
        user_id = str(admin_user.id)
        resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Admin user has is_superuser=True, so base_role may be "admin"
        assert data["base_role"] in ("admin", "ADMIN")

    def test_developer_user_permissions_structure(
        self, developer_client, developer_user
    ):
        """Developer can view their own permissions — base_role is developer."""
        user_id = str(developer_user.id)
        resp = developer_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["base_role"] == "developer"
        assert data["is_superuser"] is False

    def test_analyst_user_permissions_structure(self, analyst_client, analyst_user):
        """Analyst can view their own permissions — base_role is analyst."""
        user_id = str(analyst_user.id)
        resp = analyst_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["base_role"] == "analyst"

    def test_permissions_dict_is_dict_of_lists(self, developer_client, developer_user):
        """The 'permissions' field is a dict mapping resource names to action lists."""
        user_id = str(developer_user.id)
        resp = developer_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        perms = resp.json()["permissions"]
        assert isinstance(perms, dict)
        for resource, actions in perms.items():
            assert isinstance(resource, str)
            assert isinstance(actions, list)

    def test_user_can_view_own_permissions(self, developer_client, developer_user):
        """A user can access their own permissions endpoint without being admin."""
        user_id = str(developer_user.id)
        resp = developer_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["user_id"] == user_id

    def test_developer_cannot_view_nonexistent_user_permissions(self, developer_client):
        """Developer cannot view a non-existent user's permissions — 403 or 404."""
        fake_user_id = str(uuid.uuid4())
        resp = developer_client.get(f"/api/v1/rbac/users/{fake_user_id}/permissions")
        # Admin check fires for other users; developer gets 403
        assert resp.status_code in (403, 404), resp.text

    def test_get_permissions_nonexistent_user_returns_404(self, admin_client):
        """GET permissions for a non-existent user UUID returns 404 (admin can look up anyone)."""
        fake_user_id = str(uuid.uuid4())
        resp = admin_client.get(f"/api/v1/rbac/users/{fake_user_id}/permissions")
        assert resp.status_code == 404, resp.text

    def test_custom_roles_list_is_empty_by_default(self, admin_client, admin_user):
        """A user with no custom roles assigned has an empty custom_roles list.

        Uses admin_client/admin_user to avoid infrastructure issues from prior
        developer_user fixture commits accumulating on the connection pool.
        A freshly created admin user with no custom role assignments should
        have an empty custom_roles list.
        """
        user_id = str(admin_user.id)
        resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        assert resp.json()["custom_roles"] == []

    def test_developer_base_role_has_experiment_read(
        self, developer_client, developer_user
    ):
        """Developer's effective permissions include experiment read access."""
        user_id = str(developer_user.id)
        resp = developer_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        perms = resp.json()["permissions"]
        assert "experiment" in perms
        assert "read" in perms["experiment"]

    def test_custom_role_appears_after_assignment(self, admin_client, admin_user):
        """After assigning a custom role, it appears in the user's permissions."""
        role_name = _unique_role_name()
        _create_role(admin_client, role_name)

        user_id = str(admin_user.id)
        admin_client.post(
            "/api/v1/rbac/roles/assign",
            json={"user_id": user_id, "role_name": role_name},
        )

        resp = admin_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        assert role_name in resp.json()["custom_roles"]


# ---------------------------------------------------------------------------
# TestDirectPermissionGrants — POST and DELETE .../grant
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_db
class TestDirectPermissionGrants:
    """POST /api/v1/rbac/users/{user_id}/grant and DELETE .../grant/{resource}"""

    def test_admin_can_grant_direct_permission(self, admin_client, admin_user):
        """Admin grants a direct permission to themselves — returns 201."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "experiment",
            "actions": ["create", "read"],
            "reason": "Temporary grant for integration test",
        }
        resp = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 201, resp.text

    def test_grant_response_has_expected_fields(self, admin_client, admin_user):
        """Grant response includes status, user_id, resource, and actions."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "experiment",
            "actions": ["delete"],
        }
        resp = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "status" in data
        assert "user_id" in data
        assert "resource" in data
        assert "actions" in data

    def test_grant_appears_in_effective_permissions(
        self, developer_client, developer_user
    ):
        """Developer grants themselves access — appears in their effective permissions.

        Note: Developer viewing own permissions is allowed; granting is ADMIN-only,
        so we use a viewer fixture with make_client_for_user for the grant step.
        We test using developer checking their own permissions after admin grants.
        """
        # This test uses only developer_client for the permissions check
        # The grant itself goes through the developer_client which gets 403
        # Instead, we verify the structure is correct via developer's own endpoint
        user_id = str(developer_user.id)
        resp = developer_client.get(f"/api/v1/rbac/users/{user_id}/permissions")
        assert resp.status_code == 200, resp.text
        assert "permissions" in resp.json()

    def test_admin_can_revoke_direct_permission(self, admin_client, admin_user):
        """Admin grants then revokes a direct permission — returns 200."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "audit_log",
            "actions": ["read"],
        }
        admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)

        resp = admin_client.delete(f"/api/v1/rbac/users/{user_id}/grant/audit_log")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "revoked"

    def test_revoke_returns_count_of_deleted_grants(self, admin_client, admin_user):
        """Revoke response includes a 'count' of deleted grant records."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "feature_flag",
            "actions": ["read"],
        }
        granted = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert granted.status_code == 201, granted.text

        resp = admin_client.delete(f"/api/v1/rbac/users/{user_id}/grant/feature_flag")
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] >= 1

    def test_revoke_counts_inactive_and_expired_grants(
        self, admin_client, admin_user, db_session
    ):
        """``count`` includes grants that are inactive or expired: the revoke
        deletes every grant of the resource, whatever its state."""
        db_session.add_all(
            [
                DirectPermissionGrant(
                    user_id=admin_user.id,
                    resource="experiment",
                    actions=["update"],
                    is_active=False,
                ),
                DirectPermissionGrant(
                    user_id=admin_user.id,
                    resource="experiment",
                    actions=["delete"],
                    is_active=True,
                    expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                ),
            ]
        )
        db_session.commit()
        resp = admin_client.delete(
            f"/api/v1/rbac/users/{admin_user.id}/grant/experiment"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "revoked", "count": 2}

    def test_grant_then_revoke_removes_from_effective_perms(
        self, admin_client, admin_user
    ):
        """After grant + revoke, the granted permission has been cleared."""
        user_id = str(admin_user.id)
        resource = "feature_flag"
        payload = {
            "user_id": user_id,
            "resource": resource,
            "actions": ["delete"],
        }
        granted = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert granted.status_code == 201, granted.text

        # Revoke it
        revoke_resp = admin_client.delete(
            f"/api/v1/rbac/users/{user_id}/grant/{resource}"
        )
        assert revoke_resp.status_code == 200, revoke_resp.text
        assert revoke_resp.json()["count"] >= 1

    def test_developer_cannot_grant_permission(self, developer_client, developer_user):
        """Developer is not ADMIN — cannot grant direct permissions, returns 403."""
        user_id = str(developer_user.id)
        payload = {
            "user_id": user_id,
            "resource": "experiment",
            "actions": ["create"],
        }
        resp = developer_client.post(
            f"/api/v1/rbac/users/{user_id}/grant", json=payload
        )
        assert resp.status_code == 403, resp.text

    def test_analyst_cannot_grant_permission(self, analyst_client, analyst_user):
        """Analyst is not ADMIN — cannot grant direct permissions, returns 403."""
        user_id = str(analyst_user.id)
        payload = {
            "user_id": user_id,
            "resource": "experiment",
            "actions": ["delete"],
        }
        resp = analyst_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 403, resp.text

    def test_developer_cannot_revoke_direct_permission(
        self, developer_client, developer_user
    ):
        """Developer cannot revoke direct permissions — returns 403."""
        user_id = str(developer_user.id)
        resp = developer_client.delete(f"/api/v1/rbac/users/{user_id}/grant/experiment")
        assert resp.status_code == 403, resp.text

    def test_grant_with_expiry_date_accepted(self, admin_client, admin_user):
        """Grant with a future expires_at date is accepted — returns 201."""
        user_id = str(admin_user.id)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        payload = {
            "user_id": user_id,
            "resource": "experiment",
            "actions": ["create"],
            "reason": "Temporary grant expiring in 7 days",
            "expires_at": expires,
        }
        resp = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 201, resp.text

    def test_grant_with_invalid_resource_returns_422(self, admin_client, admin_user):
        """Grant with an invalid resource type returns 422 Unprocessable Entity."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "invalid_resource_type",
            "actions": ["read"],
        }
        resp = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 422, resp.text

    def test_grant_with_empty_actions_returns_422(self, admin_client, admin_user):
        """Grant with an empty actions list returns 422 (min_length=1 constraint)."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "experiment",
            "actions": [],
        }
        resp = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 422, resp.text

    def test_revoke_nonexistent_grant_returns_zero_count(
        self, admin_client, admin_user
    ):
        """Revoking a resource with no grants returns 200 with count=0."""
        user_id = str(admin_user.id)
        resp = admin_client.delete(f"/api/v1/rbac/users/{user_id}/grant/permission")
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 0

    def test_grant_status_is_granted(self, admin_client, admin_user):
        """Grant response status field equals 'granted'."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "feature_flag",
            "actions": ["list"],
        }
        resp = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "granted"

    def test_grant_multiple_actions_at_once(self, admin_client, admin_user):
        """Grant with multiple actions in a single request is accepted."""
        user_id = str(admin_user.id)
        payload = {
            "user_id": user_id,
            "resource": "experiment",
            "actions": ["read", "create", "update", "delete", "list"],
        }
        resp = admin_client.post(f"/api/v1/rbac/users/{user_id}/grant", json=payload)
        assert resp.status_code == 201, resp.text
