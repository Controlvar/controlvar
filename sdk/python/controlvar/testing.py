"""Test helpers: an in-memory :class:`FakeTransport` for exercising the client offline.

Example::

    from controlvar import ExperimentationClient
    from controlvar.testing import FakeTransport

    transport = FakeTransport().route(
        "GET", "/api/v1/feature-flags/evaluate/new_search",
        json={"key": "new_search", "enabled": True, "config": None},
    )
    client = ExperimentationClient("http://test", "key", transport=transport)
    assert client.is_feature_enabled("new_search", "user-1")
    assert transport.last.query == {"user_id": "user-1"}
"""

from __future__ import annotations

import json as _json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlsplit

from .transport import Response

__all__ = ["FakeTransport", "RecordedRequest", "make_response"]


def make_response(
    status: int = 200,
    json: Any = None,
    body: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> Response:
    """Build a :class:`Response`; ``json`` (if given) is serialised as the body."""
    merged: Dict[str, str] = dict(headers or {})
    if json is not None:
        body = _json.dumps(json).encode("utf-8")
        merged.setdefault("Content-Type", "application/json")
    return Response(status, merged, body or b"")


@dataclass
class RecordedRequest:
    """One request seen by :class:`FakeTransport`."""

    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    timeout: float = 0.0

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def query(self) -> Dict[str, str]:
        return dict(parse_qsl(urlsplit(self.url).query, keep_blank_values=True))

    def json(self) -> Any:
        return None if self.body is None else _json.loads(self.body.decode("utf-8"))


_Outcome = Union[Response, Exception]


class FakeTransport:
    """Records requests and serves canned responses (thread-safe).

    Responses are resolved in this order:

    1. a queued outcome from :meth:`respond` / :meth:`fail` (FIFO);
    2. a route registered with :meth:`route` / :meth:`route_error`, matched on
       method + path (query string ignored);
    3. otherwise a ``404`` with a JSON ``detail``.
    """

    def __init__(self) -> None:
        self.requests: List[RecordedRequest] = []
        self._queue: List[_Outcome] = []
        self._routes: Dict[Tuple[str, str], _Outcome] = {}
        self._lock = threading.Lock()

    # -- configuration -------------------------------------------------------

    def respond(
        self,
        status: int = 200,
        json: Any = None,
        body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> "FakeTransport":
        """Queue one response for the next request."""
        with self._lock:
            self._queue.append(make_response(status, json, body, headers))
        return self

    def fail(self, exc: Exception) -> "FakeTransport":
        """Queue an exception to be raised by the next request."""
        with self._lock:
            self._queue.append(exc)
        return self

    def route(
        self,
        method: str,
        path: str,
        status: int = 200,
        json: Any = None,
        body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> "FakeTransport":
        """Serve a persistent response for ``method path``."""
        with self._lock:
            self._routes[(method.upper(), path)] = make_response(status, json, body, headers)
        return self

    def route_error(self, method: str, path: str, exc: Exception) -> "FakeTransport":
        with self._lock:
            self._routes[(method.upper(), path)] = exc
        return self

    # -- inspection ----------------------------------------------------------

    @property
    def last(self) -> RecordedRequest:
        if not self.requests:
            raise AssertionError("FakeTransport received no requests")
        return self.requests[-1]

    def calls(self) -> List[str]:
        """``["POST /api/v1/tracking/assign", ...]`` in order."""
        return [f"{r.method} {r.path}" for r in self.requests]

    # -- Transport protocol --------------------------------------------------

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> Response:
        recorded = RecordedRequest(method.upper(), url, dict(headers), body, timeout)
        with self._lock:
            self.requests.append(recorded)
            if self._queue:
                outcome: Optional[_Outcome] = self._queue.pop(0)
            else:
                outcome = self._routes.get((recorded.method, recorded.path))
        if outcome is None:
            return make_response(404, {"detail": f"no fake route for {recorded.method} {recorded.path}"})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
