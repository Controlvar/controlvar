"""The invitation preview stays anonymous when Cognito does not answer.

``_invite_viewer`` catches the ``HTTPException`` that ``get_current_user``
raises, so the 503 for an unavailable Cognito behaves as the 401 always did:
no account, and the preview answers the same with or without a sign-in.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ReadTimeoutError

from backend.app.core.config import settings
from backend.app.services.auth_service import CognitoAuthService
from modules.backend.app.api.v1.endpoints import workspaces

pytestmark = pytest.mark.unit


def test_the_invite_preview_stays_anonymous(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    service = CognitoAuthService()
    service.client = MagicMock()
    service.client.get_user.side_effect = ReadTimeoutError(
        endpoint_url="http://127.0.0.1:1"
    )
    monkeypatch.setattr("backend.app.api.deps.auth_service", service)
    assert workspaces._invite_viewer(bearer="a-token", db=MagicMock()) is None
