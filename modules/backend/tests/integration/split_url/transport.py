"""Transports for the reference integration: its ``http`` argument, pointed at
the in-process API, or answering a fixed failure."""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

#: (method, url, headers, body, timeout) -> (status, body)
Http = Callable[..., Tuple[int, bytes]]


def _path(url: str) -> str:
    rest = url.split("://", 1)[1]
    return rest[rest.index("/") :]


def client_transport(client, calls: Optional[List[Tuple[str, str]]] = None) -> Http:
    """Send each request to *client* (a TestClient), recording it in *calls*."""

    def http(method, url, headers, body, timeout):
        if calls is not None:
            calls.append((method, _path(url)))
        response = client.request(method, _path(url), content=body, headers=headers)
        return response.status_code, response.content

    return http
