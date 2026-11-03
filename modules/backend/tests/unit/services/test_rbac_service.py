"""Unit tests for RBACService — all using MagicMock DB."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backend.app.models.user import UserRole
from modules.backend.app.schemas.rbac import (
    CustomRoleCreate,
    CustomRoleUpdate,
    PermissionAction,
    PermissionGrant,
    PermissionResource,
)
from modules.backend.app.services.rbac_service import RBACService


class TestCreateCustomRole:
    def test_creates_role_with_name(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        data = CustomRoleCreate(name="data-scientist", permissions=[])
        role = RBACService.create_custom_role(db, data, uuid4())
        db.add.assert_called_once()
        db.flush.assert_called_once()

    def test_raises_if_name_exists(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock()
        data = CustomRoleCreate(name="existing-role", permissions=[])
        with pytest.raises(ValueError, match="already exists"):
            RBACService.create_custom_role(db, data, uuid4())

    def test_persists_permissions(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        perms = [
            PermissionGrant(
                resource=PermissionResource.EXPERIMENT, actions=[PermissionAction.READ]
            )
        ]
        data = CustomRoleCreate(name="readonly-experiments", permissions=perms)
        RBACService.create_custom_role(db, data, uuid4())
        added = db.add.call_args[0][0]
        assert len(added.permissions) == 1
        assert added.permissions[0]["resource"] == "experiment"


class TestUpdateCustomRole:
    def test_raises_on_system_role(self):
        db = MagicMock()
        mock_role = MagicMock()
        mock_role.is_system_role = True
        db.query.return_value.filter.return_value.first.return_value = mock_role
        with pytest.raises(ValueError, match="Cannot modify system roles"):
            RBACService.update_custom_role(
                db, "admin", CustomRoleUpdate(description="new")
            )

    def test_raises_if_not_found(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError, match="not found"):
            RBACService.update_custom_role(db, "ghost", CustomRoleUpdate())

    def test_updates_description(self):
        db = MagicMock()
        mock_role = MagicMock()
        mock_role.is_system_role = False
        db.query.return_value.filter.return_value.first.return_value = mock_role
        RBACService.update_custom_role(
            db, "myrole", CustomRoleUpdate(description="updated")
        )
        assert mock_role.description == "updated"


class TestDeleteCustomRole:
    def test_raises_on_system_role(self):
        db = MagicMock()
        mock_role = MagicMock()
        mock_role.is_system_role = True
        db.query.return_value.filter.return_value.first.return_value = mock_role
        with pytest.raises(ValueError, match="Cannot delete system roles"):
            RBACService.delete_custom_role(db, "admin")

    def test_deletes_role(self):
        db = MagicMock()
        mock_role = MagicMock()
        mock_role.is_system_role = False
        db.query.return_value.filter.return_value.first.return_value = mock_role
        RBACService.delete_custom_role(db, "myrole")
        db.delete.assert_called_with(mock_role)


class TestAssignRevoke:
    def test_assign_creates_assignment(self):
        db = MagicMock()
        mock_role = MagicMock()
        mock_role.id = uuid4()
        db.query.return_value.filter.return_value.first.side_effect = [mock_role, None]
        RBACService.assign_role(db, uuid4(), "myrole", uuid4())
        db.add.assert_called_once()

    def test_assign_idempotent(self):
        db = MagicMock()
        mock_role = MagicMock()
        mock_role.id = uuid4()
        mock_existing = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            mock_role,
            mock_existing,
        ]
        result = RBACService.assign_role(db, uuid4(), "myrole", uuid4())
        db.add.assert_not_called()
        assert result == mock_existing

    def test_assign_raises_if_role_not_found(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        with pytest.raises(ValueError, match="not found"):
            RBACService.assign_role(db, uuid4(), "ghost", uuid4())

    def test_revoke_calls_delete(self):
        db = MagicMock()
        mock_role = MagicMock()
        mock_role.id = uuid4()
        db.query.return_value.filter.return_value.first.return_value = mock_role
        RBACService.revoke_role(db, uuid4(), "myrole")
        db.query.return_value.filter.return_value.delete.assert_called_once()


class TestGetEffectivePermissions:
    """What ``GET /rbac/users/{id}/permissions`` lists is built on the checks
    themselves (#891): the actions ``check_permission`` admits on experiments
    and feature flags, and ``audit_log: ["read"]`` when
    ``can_read_all_audit_logs`` says yes. ``granted_permissions`` (what the
    user's custom roles and grants hold) is patched here; the provider and the
    route are tested on Postgres in ``test_rbac_effective_permissions_display.py``.
    """

    @staticmethod
    def _user(role, superuser=False):
        user = MagicMock()
        user.is_superuser = superuser
        user.id = uuid4()
        user.username = "u1"
        user.role = role
        return user

    @staticmethod
    def _granted(monkeypatch, pairs):
        monkeypatch.setattr(
            "backend.app.core.permissions.granted_permissions",
            lambda user: frozenset(pairs),
        )

    def test_superuser_gets_every_grantable_pair(self):
        result = RBACService.get_effective_permissions(
            MagicMock(), self._user(UserRole.ADMIN, superuser=True)
        )
        assert result.is_superuser is True
        assert result.permissions == {
            "experiment": ["create", "delete", "list", "read", "update"],
            "feature_flag": ["create", "delete", "list", "read", "update"],
            "audit_log": ["read"],
        }

    def test_viewer_gets_read_and_list_only(self):
        result = RBACService.get_effective_permissions(
            MagicMock(), self._user(UserRole.VIEWER)
        )
        assert result.permissions == {
            "experiment": ["list", "read"],
            "feature_flag": ["list", "read"],
        }

    def test_an_admin_who_is_not_a_superuser_is_shown_the_three_resources_only(self):
        """ADMIN holds user, role and permission actions in ROLE_PERMISSIONS, but
        no check admits them to a non-superuser; they are not shown."""
        result = RBACService.get_effective_permissions(
            MagicMock(), self._user(UserRole.ADMIN)
        )
        assert set(result.permissions) == {"experiment", "feature_flag", "audit_log"}

    def test_a_custom_role_or_grant_adds_the_pairs_the_checks_admit(self, monkeypatch):
        self._granted(
            monkeypatch,
            {
                ("experiment", "create"),
                ("feature_flag", "update"),
                ("audit_log", "read"),
            },
        )
        result = RBACService.get_effective_permissions(
            MagicMock(), self._user(UserRole.VIEWER)
        )
        assert result.permissions == {
            "experiment": ["create", "list", "read"],
            "feature_flag": ["list", "read", "update"],
            "audit_log": ["read"],
        }

    def test_a_pair_no_check_reads_is_never_shown(self, monkeypatch):
        # granted_permissions never returns these (it filters by GRANTABLE);
        # even if it did, the display asks the checks, which name no "export".
        self._granted(monkeypatch, {("export", "read"), ("report", "create")})
        result = RBACService.get_effective_permissions(
            MagicMock(), self._user(UserRole.DEVELOPER)
        )
        assert "export" not in result.permissions
        assert "report" not in result.permissions

    def test_custom_roles_are_read_from_the_activated_assignments(self):
        db = MagicMock()
        chain = db.query.return_value.join.return_value.filter.return_value
        chain.order_by.return_value.all.return_value = [("exporter",)]
        result = RBACService.get_effective_permissions(
            db, self._user(UserRole.DEVELOPER)
        )
        assert result.custom_roles == ["exporter"]
        (condition_user, condition_active) = (
            db.query.return_value.join.return_value.filter.call_args.args
        )
        assert "activated_at IS NOT NULL" in str(condition_active)
