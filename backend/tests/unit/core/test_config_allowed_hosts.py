# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""ALLOWED_HOSTS: parsing, derivation, and the fail-closed rule (#220).

Three properties, each of which the previous attempt at this fix got wrong in
a different review round:

  * a pattern that can never match is REFUSED, not accepted. `*example.com`
    (no dot) is a literal hostname containing an asterisk. Accepted, it
    refuses every request while the health probes -- which are exempt -- stay
    green: an outage monitoring calls fine.
  * the allow-list DERIVES from PUBLIC_BASE_URL, which is the URL users
    actually reach the service at. The earlier attempt defaulted it to the
    load balancer's own DNS name, which no user ever sends.
  * staging and production refuse to start without one, and there is exactly
    ONE thing to set to satisfy that.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings

HARDENED = {
    "SECRET_KEY": "a" * 64,
    "FIRST_SUPERUSER_PASSWORD": "aV3ry-Str0ng-Passw0rd!",
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """`_hardening_required` is a no-op while TESTING is set, and it is set."""
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


@pytest.mark.unit
class TestParsing:
    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_HOSTS", "api.example.com, *.example.net")
        assert Settings(_env_file=None).ALLOWED_HOSTS == [
            "api.example.com",
            "*.example.net",
        ]

    def test_json_list(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_HOSTS", '["a.example.com","b.example.com"]')
        assert Settings(_env_file=None).ALLOWED_HOSTS == [
            "a.example.com",
            "b.example.com",
        ]

    def test_empty_is_empty_not_a_list_containing_an_empty_string(self, monkeypatch):
        """`"".split(",")` is `[""]`, and `[""]` is a host named "".

        The middleware enforces whenever the list is non-empty, so this
        distinction decides whether an empty environment variable turns the
        check ON against a list matching nothing -- refusing everything.
        """
        monkeypatch.setenv("ALLOWED_HOSTS", "")
        assert Settings(_env_file=None).ALLOWED_HOSTS == []


@pytest.mark.unit
class TestPatternsThatCanNeverMatch:
    @pytest.mark.parametrize(
        "pattern,why",
        [
            ("*example.com", "the classic: a wildcard without its dot"),
            (".example.com", "a bare suffix is not a pattern"),
            ("ex*ample.com", "an asterisk in the middle"),
            ("*.*.com", "two wildcards"),
            ("*.", "a wildcard with nothing after it"),
        ],
    )
    def test_refused(self, pattern, why):
        with pytest.raises(ValidationError, match="would match nothing"):
            Settings(_env_file=None, ALLOWED_HOSTS=pattern), why

    @pytest.mark.parametrize("pattern", ["example.com", "*.example.com", "localhost"])
    def test_accepted(self, pattern):
        assert Settings(_env_file=None, ALLOWED_HOSTS=pattern).ALLOWED_HOSTS == [
            pattern
        ]


@pytest.mark.unit
class TestDerivation:
    def test_it_derives_from_public_base_url(self):
        s = Settings(_env_file=None, PUBLIC_BASE_URL="https://api.example.com")
        assert s.ALLOWED_HOSTS == [], "the operator set nothing, and that shows"
        assert s.effective_allowed_hosts == ["api.example.com"]

    def test_the_port_is_not_part_of_the_derived_host(self):
        """`urlparse().hostname` drops it; `.netloc` would not.

        The middleware strips the port from the incoming Host before matching,
        so a derived entry carrying one would match nothing.
        """
        s = Settings(_env_file=None, PUBLIC_BASE_URL="http://localhost:8000")
        assert s.effective_allowed_hosts == ["localhost"]

    def test_explicit_wins(self):
        s = Settings(
            _env_file=None,
            PUBLIC_BASE_URL="https://api.example.com",
            ALLOWED_HOSTS="other.example.com",
        )
        assert s.effective_allowed_hosts == ["other.example.com"]

    def test_neither_is_empty(self):
        assert Settings(_env_file=None).effective_allowed_hosts == []


@pytest.mark.unit
class TestFailClosedWhenHardened:
    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_neither_set_is_refused(self, environment):
        with pytest.raises(ValidationError, match="hostname it answers on"):
            Settings(_env_file=None, ENVIRONMENT=environment, **HARDENED)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_public_base_url_alone_satisfies_it(self, environment):
        """The point of deriving: ONE setting, and one a deployment needs anyway."""
        s = Settings(
            _env_file=None,
            ENVIRONMENT=environment,
            PUBLIC_BASE_URL="https://api.example.com",
            **HARDENED,
        )
        assert s.effective_allowed_hosts == ["api.example.com"]

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_wildcard_is_refused(self, environment):
        """`*` satisfies "is it set" while configuring no check at all."""
        with pytest.raises(ValidationError, match=r"ALLOWED_HOSTS=\* defeats"):
            Settings(
                _env_file=None,
                ENVIRONMENT=environment,
                ALLOWED_HOSTS="*",
                **HARDENED,
            )

    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_unhardened_is_not_refused(self, environment):
        assert Settings(_env_file=None, ENVIRONMENT=environment).ALLOWED_HOSTS == []
