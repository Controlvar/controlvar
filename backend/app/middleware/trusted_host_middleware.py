# backend/app/middleware/trusted_host_middleware.py
# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""Refuse a request whose ``Host`` header is not one of ours.

``Host`` is attacker-controlled on any path that reaches the app, and the app
used to build absolute URLs from it -- the OIDC ``redirect_uri`` among them,
which makes a crafted ``Host`` an authorization-code interception path (#220).
``PUBLIC_BASE_URL`` closes that particular hole by not consulting the request
at all; this closes the general one, for every handler that has not been
audited and every one not yet written.

Why not ``starlette.middleware.trustedhost.TrustedHostMiddleware``: it has no
path exemption, and the ALB's health check sends the TARGET'S OWN IP as
``Host`` (``infrastructure/cdk/stacks/fargate_service_stack.py`` health-checks
``/health``). With Starlette's version and a real ``ALLOWED_HOSTS`` the probes
would 400, the target group would never turn healthy, and the deployment would
roll back -- with the app itself perfectly fine. The exemption below is the
whole reason this file exists, so it is written down rather than discovered.

Exempting the probes costs nothing: they are ``include_in_schema=False``
liveness/readiness endpoints that build no URLs and reveal nothing a port scan
would not. ``/metrics`` is exempt for the same reason and is separately
protected by its own token (``backend/app/core/health.py``).
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Sequence

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Paths an infrastructure probe reaches with the target's IP as ``Host``.
# Exact matches, not prefixes: a prefix allow-list is how a new route under an
# existing prefix silently inherits an exemption, which this repository has
# shipped before.
PROBE_PATHS: frozenset = frozenset(
    {"/health", "/health/live", "/health/ready", "/metrics"}
)

# How much of a refused ``Host`` reaches the log. The value is attacker-sized,
# so it is cut here; a longer one is only shortened in the log, it is refused
# the same way.
MAX_LOGGED_HOST = 200


def _host_matches(host: str, pattern: str) -> bool:
    """``host`` against one allow-list entry, with a leading ``*.`` wildcard.

    The port is already stripped by the caller. Matching is case-insensitive
    because ``Host`` is a DNS name and DNS is.
    """
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix) and len(host) > len(suffix)
    return host == pattern


class TrustedHostMiddleware(BaseHTTPMiddleware):
    """400 a request whose ``Host`` is not in ``allowed_hosts``."""

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Sequence[str] | None = None,
        probe_paths: Iterable[str] = PROBE_PATHS,
    ) -> None:
        super().__init__(app)
        self.allowed_hosts = [
            h.strip().lower() for h in (allowed_hosts or []) if h.strip()
        ]
        self.probe_paths = frozenset(probe_paths)
        # An empty list means "allow anything", which is the development
        # default. It is NOT reachable in staging or production: the settings
        # validator refuses to construct there with ALLOWED_HOSTS unset, so
        # this branch cannot be how a hardened deployment ends up open.
        self.enabled = bool(self.allowed_hosts) and "*" not in self.allowed_hosts

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled or request.url.path in self.probe_paths:
            return await call_next(request)

        # Starlette lower-cases header names, not values. Strip the port: a
        # ``Host`` legitimately carries one (``example.com:8000``) and the
        # allow-list is hostnames. An IPv6 literal is bracketed
        # (``[::1]:8000``), so split on the LAST colon only when it follows a
        # closing bracket or there is exactly one colon.
        raw = request.headers.get("host", "")
        host = raw.lower()
        if host.startswith("["):
            host = host.partition("]")[0] + "]"
        elif host.count(":") == 1:
            host = host.partition(":")[0]

        if any(_host_matches(host, p) for p in self.allowed_hosts):
            return await call_next(request)

        # The line an operator reads when every /api/* call fails while the
        # exempt probes stay green: it names the refused value, what this
        # deployment answers on, and the setting that changes it. The Host is
        # client-controlled and unbounded, so it is truncated BEFORE it is
        # formatted, and logged as a repr so any control character arrives
        # escaped. The allow-list is our own configuration, not request data.
        logger.warning(
            "rejected a request with Host %r; this deployment answers on %s. "
            "Set PUBLIC_BASE_URL (or ALLOWED_HOSTS, which takes precedence "
            "when set) to the URL clients use.",
            raw[:MAX_LOGGED_HOST],
            ", ".join(self.allowed_hosts),
        )
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid host header"},
        )
