"""Unit tests for RBAC API endpoints."""

from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.api.deps import get_current_active_user, get_db
from backend.app.main import app
from backend.app.models.user import UserRole


def _make_mock_db():
    """Return a MagicMock representing a DB session."""
    return MagicMock()


def _override_get_db(mock_db):
    def _get_db():
        try:
            yield mock_db
        finally:
            pass

    return _get_db


@pytest.fixture
def admin_user():
    mock_user = MagicMock()
    mock_user.id = uuid4()
    mock_user.username = "admin"
    mock_user.email = "admin@example.com"
    mock_user.is_active = True
    mock_user.is_superuser = True
    mock_user.role = UserRole.ADMIN
    return mock_user


@pytest.fixture
def viewer_user():
    mock_user = MagicMock()
    mock_user.id = uuid4()
    mock_user.username = "viewer"
    mock_user.email = "viewer@example.com"
    mock_user.is_active = True
    mock_user.is_superuser = False
    mock_user.role = UserRole.VIEWER
    return mock_user


@pytest.fixture
def admin_client(admin_user):
    mock_db = _make_mock_db()
    app.dependency_overrides[get_db] = _override_get_db(mock_db)
    app.dependency_overrides[get_current_active_user] = lambda: admin_user
    with TestClient(app) as client:
        yield client, admin_user, mock_db
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client(viewer_user):
    mock_db = _make_mock_db()
    app.dependency_overrides[get_db] = _override_get_db(mock_db)
    app.dependency_overrides[get_current_active_user] = lambda: viewer_user
    with TestClient(app) as client:
        yield client, viewer_user, mock_db
    app.dependency_overrides.clear()


def _make_mock_role(name="test-role", is_system=False, permissions=None):
    role = MagicMock()
    role.id = uuid4()
    role.name = name
    role.description = None
    role.is_system_role = is_system
    role.permissions = permissions or []
    role.created_at = datetime.now()
    return role


class TestListRoles:
    def test_list_returns_200(self, admin_client):
        client, _, mock_db = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.list_custom_roles.return_value = []
            response = client.get("/api/v1/rbac/roles")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_list_requires_auth(self):
        with TestClient(app) as unauth:
            response = unauth.get("/api/v1/rbac/roles")
        assert response.status_code in (401, 403)

    def test_list_returns_roles(self, admin_client):
        client, _, mock_db = admin_client
        mock_role = _make_mock_role("analyst-plus")
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.list_custom_roles.return_value = [mock_role]
            response = client.get("/api/v1/rbac/roles")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "analyst-plus"


class TestCreateRole:
    def test_create_returns_201(self, admin_client):
        client, _, mock_db = admin_client
        mock_role = _make_mock_role("data-scientist")
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.create_custom_role.return_value = mock_role
            response = client.post(
                "/api/v1/rbac/roles",
                json={
                    "name": "data-scientist",
                    "permissions": [],
                },
            )
        assert response.status_code == 201

    def test_viewer_cannot_create_role(self, viewer_client):
        client, _, _ = viewer_client
        response = client.post(
            "/api/v1/rbac/roles", json={"name": "newrole", "permissions": []}
        )
        assert response.status_code == 403

    def test_duplicate_role_returns_409(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.create_custom_role.side_effect = ValueError("already exists")
            response = client.post(
                "/api/v1/rbac/roles", json={"name": "existing", "permissions": []}
            )
        assert response.status_code == 409

    def test_invalid_role_name_returns_422(self, admin_client):
        client, _, _ = admin_client
        response = client.post(
            "/api/v1/rbac/roles", json={"name": "1Invalid", "permissions": []}
        )
        assert response.status_code == 422


class TestGetRole:
    def test_get_existing_role_returns_200(self, admin_client):
        client, _, mock_db = admin_client
        mock_role = _make_mock_role("analyst-plus")
        mock_role.description = "Extended analyst"
        mock_db.query.return_value.filter.return_value.count.return_value = 3
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.get_custom_role.return_value = mock_role
            response = client.get("/api/v1/rbac/roles/analyst-plus")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "analyst-plus"
        assert data["user_count"] == 3

    def test_get_nonexistent_role_returns_404(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.get_custom_role.return_value = None
            response = client.get("/api/v1/rbac/roles/ghost-role")
        assert response.status_code == 404


class TestUpdateRole:
    def test_update_returns_200(self, admin_client):
        client, _, mock_db = admin_client
        mock_role = _make_mock_role("my-role")
        mock_role.description = "Updated desc"
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.update_custom_role.return_value = mock_role
            response = client.put(
                "/api/v1/rbac/roles/my-role", json={"description": "Updated desc"}
            )
        assert response.status_code == 200

    def test_viewer_cannot_update(self, viewer_client):
        client, _, _ = viewer_client
        response = client.put("/api/v1/rbac/roles/some-role", json={"description": "x"})
        assert response.status_code == 403

    def test_update_system_role_returns_400(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.update_custom_role.side_effect = ValueError(
                "Cannot modify system roles"
            )
            response = client.put("/api/v1/rbac/roles/admin", json={"description": "x"})
        assert response.status_code == 400


class TestDeleteRole:
    def test_delete_returns_204(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.delete_custom_role.return_value = None
            response = client.delete("/api/v1/rbac/roles/my-role")
        assert response.status_code == 204

    def test_viewer_cannot_delete(self, viewer_client):
        client, _, _ = viewer_client
        response = client.delete("/api/v1/rbac/roles/my-role")
        assert response.status_code == 403

    def test_delete_system_role_returns_400(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.delete_custom_role.side_effect = ValueError(
                "Cannot delete system roles"
            )
            response = client.delete("/api/v1/rbac/roles/admin")
        assert response.status_code == 400


class TestAssignRevoke:
    def test_assign_role_returns_200(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.assign_role.return_value = MagicMock()
            response = client.post(
                "/api/v1/rbac/roles/assign",
                json={
                    "user_id": str(uuid4()),
                    "role_name": "data-scientist",
                },
            )
        assert response.status_code == 200
        assert response.json()["status"] == "assigned"

    def test_revoke_role_returns_200(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.revoke_role.return_value = None
            response = client.post(
                "/api/v1/rbac/roles/revoke",
                json={
                    "user_id": str(uuid4()),
                    "role_name": "data-scientist",
                },
            )
        assert response.status_code == 200
        assert response.json()["status"] == "revoked"

    def test_viewer_cannot_assign(self, viewer_client):
        client, _, _ = viewer_client
        response = client.post(
            "/api/v1/rbac/roles/assign",
            json={"user_id": str(uuid4()), "role_name": "any"},
        )
        assert response.status_code == 403

    def test_assign_unknown_role_returns_404(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.assign_role.side_effect = ValueError("not found")
            response = client.post(
                "/api/v1/rbac/roles/assign",
                json={"user_id": str(uuid4()), "role_name": "ghost"},
            )
        assert response.status_code == 404


class TestEffectivePermissions:
    def test_get_own_permissions_returns_200(self, admin_client):
        client, user, mock_db = admin_client
        from modules.backend.app.schemas.rbac import EffectivePermissionsResponse

        mock_db.query.return_value.filter.return_value.first.return_value = user
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.get_effective_permissions.return_value = (
                EffectivePermissionsResponse(
                    user_id=str(user.id),
                    username="admin",
                    base_role="admin",
                    custom_roles=[],
                    permissions={"experiment": ["create", "read"]},
                    is_superuser=True,
                )
            )
            response = client.get(f"/api/v1/rbac/users/{user.id}/permissions")
        assert response.status_code == 200
        data = response.json()
        assert "permissions" in data
        assert "base_role" in data
        assert data["is_superuser"] is True

    def test_get_unknown_user_returns_404(self, admin_client):
        client, _, mock_db = admin_client
        mock_db.query.return_value.filter.return_value.first.return_value = None
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService"):
            response = client.get(f"/api/v1/rbac/users/{uuid4()}/permissions")
        assert response.status_code == 404

    def test_viewer_can_get_own_permissions(self, viewer_client):
        client, user, mock_db = viewer_client
        from modules.backend.app.schemas.rbac import EffectivePermissionsResponse

        mock_db.query.return_value.filter.return_value.first.return_value = user
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.get_effective_permissions.return_value = (
                EffectivePermissionsResponse(
                    user_id=str(user.id),
                    username="viewer",
                    base_role="viewer",
                    custom_roles=[],
                    permissions={"experiment": ["read", "list"]},
                    is_superuser=False,
                )
            )
            response = client.get(f"/api/v1/rbac/users/{user.id}/permissions")
        assert response.status_code == 200


class TestGrantRevokePermission:
    def test_grant_permission_returns_201(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.grant_direct_permission.return_value = MagicMock()
            response = client.post(
                f"/api/v1/rbac/users/{uuid4()}/grant",
                json={
                    "user_id": str(uuid4()),
                    "resource": "experiment",
                    "actions": ["update"],
                },
            )
        assert response.status_code == 201
        assert response.json()["status"] == "granted"

    def test_viewer_cannot_grant(self, viewer_client):
        client, _, _ = viewer_client
        response = client.post(
            f"/api/v1/rbac/users/{uuid4()}/grant",
            json={
                "user_id": str(uuid4()),
                "resource": "experiment",
                "actions": ["update"],
            },
        )
        assert response.status_code == 403

    def test_a_pair_no_role_or_grant_may_hold_is_refused_first(self, admin_client):
        """Validation runs before the route: 422, and the service is not
        called (#891)."""
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            response = client.post(
                f"/api/v1/rbac/users/{uuid4()}/grant",
                json={
                    "user_id": str(uuid4()),
                    "resource": "report",
                    "actions": ["read"],
                },
            )
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "not_grantable"
        mock_svc.grant_direct_permission.assert_not_called()

    def test_revoke_permission_returns_200(self, admin_client):
        client, _, _ = admin_client
        with patch("modules.backend.app.api.v1.endpoints.rbac.RBACService") as mock_svc:
            mock_svc.revoke_direct_permission.return_value = 2
            response = client.delete(f"/api/v1/rbac/users/{uuid4()}/grant/report")
        assert response.status_code == 200
        assert response.json()["count"] == 2
