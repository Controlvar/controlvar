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

import io
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.tests.unit.infrastructure import _proc_net_fixtures as fx

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

    def test_the_trust_list_comes_from_the_helper(self):
        """No static list: the private ranges as a default believed every
        client on a private address. The list is what
        ``backend.app.core.proxy_trust`` prints, used as it is."""
        text = executable_lines()
        literals = [
            c for c in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16") if c in text
        ]
        assert not literals, (
            f"the entrypoint still trusts {', '.join(literals)} by a literal; "
            "the list must come from backend.app.core.proxy_trust"
        )
        flags = re.findall(r"--forwarded-allow-ips=(\S+)", text)
        assert len(flags) == 1, f"--forwarded-allow-ips appears {len(flags)} times"
        match = re.fullmatch(r'"\$\{?([a-z_]+)\}?"', flags[0].rstrip("\\").strip())
        assert match, (
            f"--forwarded-allow-ips={flags[0]} is a static list; the list must "
            "come from backend.app.core.proxy_trust"
        )
        variable = match.group(1)
        assert re.search(
            rf'^\s*{variable}="\$\(python -m backend\.app\.core\.proxy_trust\)"\s*$',
            text,
            re.M,
        ), f"${variable} is not the output of python -m backend.app.core.proxy_trust"

    def test_an_operator_can_add_to_it(self, tmp_path):
        """FORWARDED_ALLOW_IPS reaches the list, after loopback."""
        from backend.app.core import proxy_trust

        out, err = io.StringIO(), io.StringIO()
        code = proxy_trust.main(
            {"FORWARDED_ALLOW_IPS": "10.1.0.0/16"},
            route=str(tmp_path / "no-route"),
            if_inet6=str(tmp_path / "no-if_inet6"),
            stdout=out,
            stderr=err,
        )
        assert code == 0, err.getvalue()
        assert out.getvalue() == "127.0.0.1,::1,10.1.0.0/16\n"


#: ``python``: the trusted-proxy helper prints $HELPER_OUTPUT and exits
#: $HELPER_EXIT; anything else (the start-up check) succeeds.
#: ``uvicorn``: writes each of its arguments on a line of $ARGS_LOG.
_PYTHON = """#!/usr/bin/env bash
if [ "$*" = "-m backend.app.core.proxy_trust" ]; then
    [ -n "$HELPER_OUTPUT" ] && printf '%s\\n' "$HELPER_OUTPUT"
    exit "${HELPER_EXIT:-0}"
fi
exit 0
"""
_UVICORN = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$ARGS_LOG"
"""
BASH = shutil.which("bash")


def _start(tmp_path: Path, *command: str, **env_vars: str):
    """The real entrypoint with *command*: (status, stderr, uvicorn's
    arguments or None when it was not started)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in (("python", _PYTHON), ("uvicorn", _UVICORN)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    args_log = tmp_path / "uvicorn-args"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "ARGS_LOG": str(args_log),
        "APP_DIR_FOR_TEST": str(tmp_path),
        "WAIT_FOR_DB": "false",
        "RUN_MIGRATIONS": "false",
        "OPENSSL_armcap": "0",
        "ENVIRONMENT": "development",
        **env_vars,
    }
    driver = (
        'cd() { builtin cd "$APP_DIR_FOR_TEST"; }; export -f cd; exec bash "$0" "$@"'
    )
    result = subprocess.run(
        [BASH, "-c", driver, str(ENTRYPOINT), *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    started = args_log.read_text().splitlines() if args_log.exists() else None
    return result.returncode, result.stderr, started


def _allow_ips(arguments) -> list:
    return [
        a.split("=", 1)[1] for a in arguments if a.startswith("--forwarded-allow-ips=")
    ]


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.skipif(BASH is None, reason="needs bash")
class TestTheEntrypointStarts:
    """The entrypoint run, with ``python`` and ``uvicorn`` stubbed."""

    SENTINEL = "127.0.0.1,::1,192.0.2.0/24"

    def test_uvicorn_gets_the_helpers_list_last_and_once(self, tmp_path):
        code, stderr, started = _start(
            tmp_path / "plain",
            "uvicorn",
            "backend.app.main:app",
            HELPER_OUTPUT=self.SENTINEL,
        )
        assert code == 0, stderr
        assert started is not None, stderr
        assert _allow_ips(started) == [self.SENTINEL], started
        assert "--proxy-headers" in started
        # A caller's own flag comes first; the helper's is the one uvicorn keeps.
        code, stderr, started = _start(
            tmp_path / "caller",
            "uvicorn",
            "backend.app.main:app",
            "--forwarded-allow-ips=10.1.0.0/16",
            HELPER_OUTPUT=self.SENTINEL,
        )
        assert code == 0, stderr
        assert _allow_ips(started) == ["10.1.0.0/16", self.SENTINEL], started

    def test_an_empty_list_refuses_to_start(self, tmp_path):
        code, stderr, started = _start(
            tmp_path, "uvicorn", "backend.app.main:app", HELPER_OUTPUT=""
        )
        assert started is None, (
            "expected the entrypoint to refuse an empty trust list; it started "
            f"uvicorn with --forwarded-allow-ips={_allow_ips(started)}"
        )
        assert code == 78, stderr
        assert "the trusted-proxy list came back empty; refusing to start" in stderr

    def test_a_refusing_helper_stops_the_start(self, tmp_path):
        code, stderr, started = _start(
            tmp_path,
            "uvicorn",
            "backend.app.main:app",
            HELPER_OUTPUT="",
            HELPER_EXIT="78",
        )
        assert (code, started) == (78, None), stderr

    def test_a_command_other_than_uvicorn_does_not_build_the_list(self, tmp_path):
        code, stderr, started = _start(tmp_path, "true", HELPER_EXIT="1")
        assert code == 0, stderr


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


#: Per shipped topology: (FORWARDED_ALLOW_IPS, /proc/net/route, the proxies
#: in front of the API, an address that must not be believed as a proxy).
#: Compose: `web` and the gateway a same-host TLS proxy arrives from, and a
#: LAN client. AWS: the load balancer in a public subnet of the VPC the stack
#: names, and a client on the internet. Helm: an ingress controller pod on
#: another node, under the chart's default, and a client on the internet.
TOPOLOGIES = {
    "Compose": (
        "",
        fx.DOCKER_BRIDGE_ROUTE,
        ("172.19.0.3", "172.19.0.1"),
        "192.168.1.50",
    ),
    "AWS": (
        "10.0.0.0/16",
        fx.FARGATE_ROUTE,
        ("10.0.0.37", "10.0.1.200"),
        "203.0.113.9",
    ),
    "Helm": (
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        fx.FLANNEL_ROUTE,
        ("10.244.2.7",),
        "203.0.113.9",
    ),
}


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("topology", sorted(TOPOLOGIES))
def test_the_list_each_topology_starts_with_believes_its_proxies_only(topology):
    """Membership in the list the helper builds, by the pinned uvicorn's own
    parser: the proxies in front are in it, the client is not."""
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    from backend.app.core import proxy_trust

    raw, route, proxies, client = TOPOLOGIES[topology]
    own = proxy_trust.own_networks(route, "", byteorder="little")
    hosts = _TrustedHosts(proxy_trust.build(raw, own))
    for proxy in proxies:
        assert proxy in hosts, f"{proxy} is not believed in the {topology} topology"
    assert client not in hosts, (
        f"{client} is trusted as a proxy in the {topology} topology"
    )


# ---------------------------------------------------------------------------
# The dashboard's nginx appends the address it received
# ---------------------------------------------------------------------------

NGINX = REPO_ROOT / "frontend" / "nginx.conf"

#: The locations that proxy to the API.
API_LOCATIONS = ["/api/", "= /api/v1/docs", "= /api/v1/redoc", "/ws/", "/health"]
APPEND = "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;"


def _locations(text: str) -> dict:
    """``location <spec> {`` -> the lines of its block (one level deep)."""
    blocks, current, depth = {}, None, 0
    for line in text.splitlines():
        stripped = line.strip()
        if current is None:
            match = re.fullmatch(r"location\s+(.+?)\s*\{", stripped)
            if match:
                current, depth = match.group(1), 1
                blocks[current] = []
            continue
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            current = None
            continue
        blocks[current].append(stripped)
    return blocks


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.skipif(not NGINX.is_file(), reason="this tree has no frontend/nginx.conf")
def test_every_location_that_proxies_to_the_api_appends_x_forwarded_for():
    """The API's list believes `web`; `web` must hand on what it received,
    appended, or a TLS proxy's client entry is lost."""
    proxied = {
        spec: lines
        for spec, lines in _locations(NGINX.read_text(encoding="utf-8")).items()
        if "proxy_pass ${API_UPSTREAM};" in lines
    }
    new = sorted(set(proxied) - set(API_LOCATIONS))
    assert not new, f"a new location proxies to the API; classify it: {new}"
    assert sorted(proxied) == sorted(API_LOCATIONS), sorted(proxied)
    for spec, lines in proxied.items():
        forwarded = [line for line in lines if "X-Forwarded-For" in line]
        assert forwarded, f"location {spec} sets no X-Forwarded-For"
        for line in forwarded:
            assert "$remote_addr" not in line, (
                f"location {spec} sets X-Forwarded-For to $remote_addr"
            )
        assert forwarded == [APPEND], f"location {spec}: {forwarded}"


@pytest.mark.unit
@pytest.mark.skipif(not NGINX.is_file(), reason="this tree has no frontend/nginx.conf")
def test_the_dashboard_access_log_ends_with_the_x_forwarded_for_it_received():
    """`docker compose logs web` shows what the dashboard was handed: its
    access-log format ends with the X-Forwarded-For it received (nginx writes
    `-` for none)."""
    text = NGINX.read_text(encoding="utf-8")
    match = re.search(r"log_format\s+noquery\s+((?:'[^']*'\s*)+);", text)
    assert match, "frontend/nginx.conf defines no `log_format noquery`"
    fmt = "".join(re.findall(r"'([^']*)'", match.group(1)))
    assert fmt.endswith(' "$http_x_forwarded_for"'), fmt
