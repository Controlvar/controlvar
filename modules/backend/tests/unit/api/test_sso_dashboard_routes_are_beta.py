# SPDX-License-Identifier: Apache-2.0
"""The two dashboard sign-in routes of the SSO module are beta (#1837).

``docs/auth/sso.md`` says ``GET /api/v1/auth/sso/login`` and
``POST /api/v1/auth/sso/exchange`` are beta. This reads the OpenAPI document
the application builds, so the page cannot keep saying it after a route loses
its ``x-stability`` marker.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def openapi():
    from backend.app.main import app

    return app.openapi()


def test_sso_login_is_beta(openapi):
    assert openapi["paths"]["/api/v1/auth/sso/login"]["get"]["x-stability"] == "beta"


def test_sso_exchange_is_beta(openapi):
    assert (
        openapi["paths"]["/api/v1/auth/sso/exchange"]["post"]["x-stability"] == "beta"
    )
