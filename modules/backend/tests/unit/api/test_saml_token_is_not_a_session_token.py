"""The SAML sign-in's token is not accepted as a local session token (#501).

``sso._issue_jwt`` signs with ``SECRET_KEY`` and writes no ``iss``, so
``security.decode_local_token`` refuses it. A local access token must come
from ``security.create_local_access_token``, which writes the account's
password stamp (``pwv``); a token accepted as a local one without a stamp
would outlive a password change. The core test
``backend/tests/unit/core/test_local_token_mint_sites.py`` pins which functions
may encode with ``SECRET_KEY`` or write the local issuer; this pins what the
one module encoder's tokens are worth.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from backend.app.core import security
from backend.app.core.config import settings
from modules.backend.app.api.v1.endpoints import sso

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def test_a_saml_token_is_refused_as_a_local_access_token(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "s" * 64)
    user = SimpleNamespace(
        id=uuid.uuid4(), email="saml@example.com", username="saml", role=None
    )
    token = sso._issue_jwt(user)
    with pytest.raises(security.InvalidTokenError):
        security.decode_local_token(token)
