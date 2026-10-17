# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""PUBLIC_BASE_URL parsing and validation (#220).

The shape rules are not cosmetic. This value is concatenated with
``/api/v1/auth/sso/oidc/{provider}/callback`` to form an OIDC ``redirect_uri``,
and an IdP matches that against its registration EXACTLY -- so a trailing
slash or a stray path is a callback the IdP refuses, which surfaces as "SSO is
broken" rather than as a configuration error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


@pytest.mark.unit
class TestPublicBaseUrl:
    def test_default_is_none(self):
        """Unset is the development default; nothing is required to set it."""
        assert Settings(_env_file=None).PUBLIC_BASE_URL is None

    @pytest.mark.parametrize(
        "raw,expected,why",
        [
            ("https://api.example.com", "https://api.example.com", "the plain form"),
            (
                "https://api.example.com/",
                "https://api.example.com",
                "trailing slash stripped",
            ),
            (
                "https://api.example.com///",
                "https://api.example.com",
                "several stripped",
            ),
            (
                "http://localhost:8000",
                "http://localhost:8000",
                "a port is part of the origin",
            ),
            (
                "  https://api.example.com  ",
                "https://api.example.com",
                "surrounding space",
            ),
            ("", None, "empty is unset, not an empty string"),
            ("   ", None, "whitespace is unset too"),
        ],
    )
    def test_accepted_forms(self, monkeypatch, raw, expected, why):
        monkeypatch.setenv("PUBLIC_BASE_URL", raw)
        assert Settings(_env_file=None).PUBLIC_BASE_URL == expected, why

    @pytest.mark.parametrize(
        "raw,why",
        [
            ("api.example.com", "no scheme -- urlparse gives it no netloc either"),
            ("//api.example.com", "protocol-relative is not absolute"),
            ("ftp://api.example.com", "not http(s)"),
            ("https://", "a scheme and nothing else"),
            ("https://api.example.com/api/v1", "a path would corrupt the redirect_uri"),
            ("https://api.example.com/x/", "a path, trailing slash notwithstanding"),
        ],
    )
    def test_refused_forms(self, monkeypatch, raw, why):
        monkeypatch.setenv("PUBLIC_BASE_URL", raw)
        with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
            Settings(_env_file=None), why

    def test_it_is_not_required_in_production_when_allowed_hosts_names_the_hosts(
        self, monkeypatch
    ):
        """Optional in production only when ALLOWED_HOSTS names the hosts.

        Since #220 a hardened start must know what hostname it answers on, and
        PUBLIC_BASE_URL is the usual way to say so; ALLOWED_HOSTS stands in
        for a service with several names. (This test used to build production
        settings with neither, which only passed because the suite's
        ``TESTING=true`` switched the host check off; ``Settings`` now refuses
        TESTING with a hardened environment.)
        """
        monkeypatch.delenv("TESTING", raising=False)
        s = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            SECRET_KEY="a" * 64,
            FIRST_SUPERUSER_PASSWORD="aV3ry-Str0ng-Passw0rd!",
            ALLOWED_HOSTS="a.example.com,b.example.com",
        )
        assert s.PUBLIC_BASE_URL is None
        assert s.is_production
