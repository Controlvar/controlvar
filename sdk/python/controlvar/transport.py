"""HTTP transport abstraction.

The client talks to a :class:`Transport` with a single ``send`` method. The
default :class:`UrllibTransport` uses only the standard library; tests (and
callers who want a different HTTP stack) can inject their own.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Protocol

from .types import ExperimentationError

__all__ = ["Response", "Transport", "TransportError", "UrllibTransport"]


def without_query(url: str) -> str:
    """*url* up to its ``?``: what a request error message names.

    The query holds the user's id and attributes (``?user_id=...&context=...``).
    """
    return url.partition("?")[0]


def failure_message(method: str, url: str, exc: BaseException) -> str:
    """``"<method> <url without its query> failed: <exc>"``.

    Where *exc*'s own text repeats the URL, whole (urllib's ``unknown url type``
    does) or as the request target, path and query (http.client's ``InvalidURL``
    does), its query is removed too.
    """
    shown = without_query(url)
    detail = str(exc)
    query = url[len(shown) :]
    if len(query) > 1:
        detail = detail.replace(query, "")
    return f"{method} {shown} failed: {detail}"


class TransportError(ExperimentationError):
    """A request could not be completed (DNS/connection failure, timeout, ...).

    ``status`` is always ``None``; HTTP error responses are *not* transport
    errors — they come back as a :class:`Response` with a 4xx/5xx status.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status=None, body=None)


class Response:
    """Minimal HTTP response: status code, headers and the raw body."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: Optional[Mapping[str, str]] = None, body: bytes = b"") -> None:
        self.status = int(status)
        self.headers: Dict[str, str] = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body or b""

    def header(self, name: str) -> Optional[str]:
        """Case-insensitive header lookup."""
        return self.headers.get(name.lower())

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Response(status={self.status}, body={self.body[:80]!r})"


class Transport(Protocol):
    """Anything with ``send(method, url, headers, body, timeout) -> Response``.

    Implementations must return a :class:`Response` for *every* HTTP status
    (including 4xx/5xx) and raise :class:`TransportError` only when no
    response was received.
    """

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> Response:
        ...


class UrllibTransport:
    """Default transport built on :func:`urllib.request.urlopen` (no third-party deps)."""

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> Response:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as raw:  # noqa: S310 - caller-provided URL
                return Response(raw.status, dict(raw.headers.items()), raw.read())
        except urllib.error.HTTPError as exc:
            # A response *was* received; surface it so the client can inspect status/body.
            headers_out = dict(exc.headers.items()) if exc.headers is not None else {}
            try:
                payload = exc.read()
            except Exception:  # pragma: no cover - defensive
                payload = b""
            return Response(exc.code, headers_out, payload or b"")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # URLError, socket.timeout/TimeoutError and ConnectionError are OSError
            # subclasses; ValueError covers malformed URLs.
            raise TransportError(failure_message(method, url, exc)) from exc
