"""Two 403 refusals say what is true, in words (#1974).

* ``DELETE /feature-flags/{id}`` claimed the caller "must be the owner or a
  superuser"; access is by role (D11), so it now names the roles that may.
* ``GET /api-keys?all=true`` printed an enum repr
  ("ResourceType.API_KEY") because the shared message builder had no name for
  that resource.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.permissions import (
    Action,
    ResourceType,
    feature_flag_roles_for,
    get_permission_error_message,
)
from backend.app.main import app
from backend.app.models.user import UserRole

pytestmark = [pytest.mark.unit, pytest.mark.regression]


@pytest.fixture
def as_role():
    """Sign in a non-superuser of the given role; the database is a mock."""
    saved = dict(app.dependency_overrides)

    def sign_in(role: UserRole) -> MagicMock:
        user = MagicMock()
        user.id = uuid.uuid4()
        user.is_superuser = False
        user.is_active = True
        user.role = role
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        db = MagicMock()
        # Any flag lookup finds a flag owned by someone else.
        db.query.return_value.filter.return_value.first.return_value = MagicMock(
            owner_id=uuid.uuid4()
        )
        app.dependency_overrides[deps.get_db] = lambda: db
        return user

    try:
        yield sign_in
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def test_flag_delete_refusal_names_the_roles_that_may_delete(as_role):
    as_role(UserRole.VIEWER)
    resp = TestClient(app, raise_server_exceptions=False).delete(
        f"/api/v1/feature-flags/{uuid.uuid4()}"
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail == "Deleting a feature flag needs the Admin or Developer role"
    assert "owner" not in detail
    assert "superuser" not in detail


def test_the_named_roles_are_the_ones_the_scope_table_allows():
    assert feature_flag_roles_for(Action.DELETE) == [
        UserRole.ADMIN,
        UserRole.DEVELOPER,
    ]


def test_api_keys_all_refusal_is_words_not_an_enum_repr(as_role):
    as_role(UserRole.DEVELOPER)
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/api-keys?all=true"
    )
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail == "You don't have permission to list API keys"
    assert "ResourceType" not in detail


@pytest.mark.parametrize("resource", list(ResourceType))
@pytest.mark.parametrize("action", list(Action))
def test_no_refusal_prints_an_enum_repr(resource, action):
    message = get_permission_error_message(resource, action)
    assert "ResourceType" not in message
    assert "Action." not in message
