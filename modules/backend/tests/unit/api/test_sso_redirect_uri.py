# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The OIDC redirect_uri must not move when the Host header does (#220).

This is the value an authorization code is returned to. Built from
`request.base_url` it was built from the `Host` header and a scheme that
`X-Forwarded-Proto` can set, so a crafted request steered the code to an
attacker's host -- authorization-code interception.

`ALLOWED_HOSTS` refuses a foreign Host before this function runs, so these pin
the second of two locks. Worth having on its own terms even with no attacker:
an OIDC `redirect_uri` must match the IdP's registration EXACTLY, so deriving
it from whichever accepted host a given request used is a bug, and behind a
TLS-terminating proxy the request's own scheme is `http` and every URL built
from it is wrong.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "modules.backend.app.api.v1.endpoints.sso",
    reason="modules profile only",
)

from modules.backend.app.api.v1.endpoints.sso import (
    _get_redirect_uri,
)


class _Request:
    """Only the attribute `_get_redirect_uri` reads."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


HOSTILE = "https://evil.example.net/"
OURS = "https://api.example.com"


@pytest.mark.unit
def test_public_base_url_wins_over_the_request(monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", OURS, raising=False)
    uri = _get_redirect_uri(_Request(HOSTILE), "okta")
    assert uri == f"{OURS}/api/v1/auth/sso/oidc/okta/callback"
    assert "evil.example.net" not in uri


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.example.net/",
        "http://127.0.0.1:1337/",
        "https://api.example.com.evil.net/",
    ],
)
def test_the_uri_is_identical_whatever_the_request_claims(monkeypatch, base_url):
    """One configured value, so every request produces the same registered URI."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", OURS, raising=False)
    assert _get_redirect_uri(_Request(base_url), "okta") == _get_redirect_uri(
        _Request(OURS + "/"), "okta"
    )


@pytest.mark.unit
def test_without_the_setting_it_still_falls_back_to_the_request(monkeypatch):
    """Unset is the development default, and development has no proxy.

    Pinned so that the fallback is a DECISION rather than an accident, and so
    that removing it later is a visible change. In staging and production
    `ALLOWED_HOSTS` is mandatory, so even this path can only ever see a host we
    accept.
    """
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None, raising=False)
    assert (
        _get_redirect_uri(_Request("http://localhost:8000/"), "okta")
        == "http://localhost:8000/api/v1/auth/sso/oidc/okta/callback"
    )
