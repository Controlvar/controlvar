# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""Whose X-Forwarded-* the server believes (#237).

``--forwarded-allow-ips="*"`` told uvicorn to believe everybody's. That is not
a small setting: it decides both ``scope["scheme"]`` and ``scope["client"]``,
and the rate limiter keys its buckets on the latter, so it decided whether a
client could mint itself an unlimited budget against the strict login limit.

The second test here drives the REAL uvicorn that is pinned in
``backend/requirements.txt`` rather than asserting what its documentation says
it does. That distinction has mattered in this repository more than once: a
flag does not do what its name suggests until it has been run and looked at.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"


def executable_lines() -> str:
    """The entrypoint with comments stripped.

    The assertions below are about the COMMAND uvicorn is started with, and a
    grep over the whole file is not that -- it also reads the prose explaining
    why the wildcard was wrong, which necessarily quotes the wildcard. Writing
    the explanation would then fail the check that exists to keep the
    explanation true. (That shape has now bitten this repository six times in
    various files; it is worth stripping comments rather than not explaining.)
    """
    return "\n".join(
        line
        for line in ENTRYPOINT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


pytestmark = pytest.mark.skipif(
    not ENTRYPOINT.is_file(), reason="this tree has no backend/docker-entrypoint.sh"
)


@pytest.mark.unit
@pytest.mark.regression
class TestTheEntrypoint:
    def test_it_does_not_trust_every_peer(self):
        """The literal that was there. A wildcard trusts the client itself."""
        text = executable_lines()
        assert '--forwarded-allow-ips="*"' not in text
        assert "--forwarded-allow-ips='*'" not in text
        assert not re.search(r"--forwarded-allow-ips=\*(\s|$)", text)

    def test_it_still_reads_proxy_headers_at_all(self):
        """Removing the wildcard by removing the flag would break the limiter.

        Without ``--proxy-headers`` uvicorn never consults X-Forwarded-For, so
        every request appears to come FROM the load balancer and the rate
        limiter collapses to one shared bucket -- a different way to get the
        same outage the wildcard risked.
        """
        assert "--proxy-headers" in executable_lines()

    def test_the_default_covers_the_private_ranges(self):
        """A load balancer is always inside the VPC, and so always RFC1918.

        Trusting the private ranges means this container never has to be told
        its own VPC's CIDR -- and a list that omitted the load balancer is
        exactly the silent single-bucket collapse above.
        """
        text = executable_lines()
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.1"):
            assert cidr in text, f"{cidr} is not trusted; the proxy may be in it"

    def test_an_operator_can_override_it(self):
        assert "FORWARDED_ALLOW_IPS" in executable_lines()


@pytest.mark.unit
@pytest.mark.regression
class TestUvicornItself:
    """The pinned uvicorn, driven. Not what the flag is called -- what it does."""

    # A client forges a hop; the ALB appends the address it really saw.
    FORGED = "9.9.9.9, 203.0.113.9, 10.0.3.10"
    REAL_CLIENT = "203.0.113.9"
    FORGERY = "9.9.9.9"

    def _resolve(self, trusted: str) -> str:
        from uvicorn.middleware.proxy_headers import _TrustedHosts

        host, _port = _TrustedHosts(trusted).get_trusted_client_address(self.FORGED)
        return host

    def test_the_wildcard_returns_the_forgery(self):
        """Why this issue exists. `*` makes uvicorn take the LEFTMOST entry."""
        assert self._resolve("*") == self.FORGERY

    def test_the_private_ranges_return_the_real_client(self):
        """Narrowed, it walks in reverse and skips the hops it trusts."""
        trusted = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
        assert self._resolve(trusted) == self.REAL_CLIENT

    def test_the_configured_default_is_the_one_that_works(self):
        """Read the entrypoint's own default and drive uvicorn with it.

        Typed separately above, these two could drift: the entrypoint could be
        edited to something that parses and resolves the wrong host while every
        assertion here still passed. This closes that by using the real value.
        """
        match = re.search(
            r'--forwarded-allow-ips="\$\{FORWARDED_ALLOW_IPS:-([^}]+)\}"',
            executable_lines(),
        )
        assert match, "could not read the default out of docker-entrypoint.sh"
        assert self._resolve(match.group(1)) == self.REAL_CLIENT
