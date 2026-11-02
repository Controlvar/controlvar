"""The flag list answers 403 to a role without LIST, and still filters by `status`.

`list_feature_flags` took its filter as a Python parameter named `status`,
which hides the `fastapi.status` module inside the handler. Its 403 branch
reads `status.HTTP_403_FORBIDDEN`, so a caller the LIST check refused got a
500 instead. No role reaches that branch today (all four carry LIST on feature
flags), so the refusal is forced here by patching the handler's
`check_permission`.

The parameter is now `status_filter` with `alias="status"`: the query name a
client sends is unchanged, which the second half of this file pins.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import feature_flags as ff
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.user import UserRole

pytestmark = [pytest.mark.unit, pytest.mark.regression]

LIST_URL = "/api/v1/feature-flags/"


@pytest.fixture
def signed_in():
    """A signed-in, non-superuser VIEWER and a database nothing may touch."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.is_superuser = False
    user.role = UserRole.VIEWER
    user.is_active = True

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: MagicMock()
    try:
        yield user
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


@pytest.mark.parametrize("query", ["", "?status=ACTIVE"], ids=["no-filter", "status"])
def test_a_role_without_list_gets_403_not_500(signed_in, query):
    get_multi = MagicMock(return_value=[])
    count = MagicMock(return_value=0)
    with (
        patch.object(ff, "check_permission", return_value=False) as checked,
        patch.object(ff.crud_feature_flag, "get_multi", get_multi),
        patch.object(ff.crud_feature_flag, "count", count),
    ):
        resp = TestClient(app, raise_server_exceptions=False).get(LIST_URL + query)

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "You don't have permission to list feature flags"
    checked.assert_called_once()
    assert checked.call_args.args[0] is signed_in
    get_multi.assert_not_called()
    count.assert_not_called()


def test_the_status_query_still_reaches_the_filter(signed_in):
    get_multi = MagicMock(return_value=[])
    count = MagicMock(return_value=0)
    with (
        patch.object(ff, "check_permission", return_value=True),
        patch.object(ff.crud_feature_flag, "get_multi", get_multi),
        patch.object(ff.crud_feature_flag, "count", count),
    ):
        resp = TestClient(app, raise_server_exceptions=False).get(
            LIST_URL + "?status=INACTIVE&search=chk"
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "total": 0, "skip": 0, "limit": 100}
    # The text reaches the filter as the enum member (#955), never as text.
    assert get_multi.call_args.kwargs["status"] is FeatureFlagStatus.INACTIVE
    assert get_multi.call_args.kwargs["search"] == "chk"
    assert count.call_args.kwargs == {
        "status": FeatureFlagStatus.INACTIVE,
        "search": "chk",
    }
