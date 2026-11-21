"""A call the tool makes to the take's own API, off camera (``send``).

The storyboard's ``setup`` (``run.make_setup``) makes video 9's exclusion
group with it, after gate 14 and before the first scene: the method, the
stack's own API origin and a path under ``/api/v1/``, the run's admin token in
a header, no redirect followed, and exactly the status the caller declares, or
refused (``CallRefused``) naming the status it got and the API's ``detail``,
cut short. The origin must be plain http on 127.0.0.1, with no user in it, at
a port that is not reserved. Nothing it sends or answers is drawn.

Video 11's setup also asks for flags as an app does (check fix 5 of the
V11-V13 plan): ``api_key=`` sends that key as ``X-API-Key`` instead of the
token's ``Authorization: Bearer``, never both.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional, Tuple
from urllib.parse import urlparse

from showcase.capture import config


class CallRefused(RuntimeError):
    """A call was not made, or was not answered as the caller declares."""


#: The only host a call goes to: the take's own stack (``guard.origin``).
STACK_HOST = "127.0.0.1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is the answer, never followed: urllib then raises it as HTTPError."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def no_redirect_opener() -> urllib.request.OpenerDirector:
    """``guard.http_opener``'s rule (no proxy from the environment), and no redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


Opener = Callable[[urllib.request.Request], Tuple[int, bytes]]


def _open(request: urllib.request.Request) -> Tuple[int, bytes]:
    try:
        with no_redirect_opener().open(request, timeout=60) as answer:
            return int(answer.status), answer.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()


def send(
    api_url: str,
    token: str,
    method: str,
    path: str,
    body: Optional[Mapping[str, Any]],
    status: int,
    *,
    opener: Opener = _open,
    api_key: Optional[str] = None,
) -> Tuple[int, Any]:
    """One call to the take's own API: ``(status, parsed answer)``, or refused.

    *api_url* is the stack's API origin; *path* starts ``/api/v1/``. GET, POST
    and PUT only; no redirect is followed; any status but *status* is refused,
    naming the status (and the API's ``detail``, cut short). With *api_key*,
    the call carries ``X-API-Key: <api_key>`` and no ``Authorization``: the
    token is not sent."""
    if method not in ("GET", "POST", "PUT"):
        raise CallRefused(f"{method} {path}: only GET, POST and PUT are sent")
    if not path.startswith("/api/v1/") or "://" in path:
        raise CallRefused(f"{method} {path}: a call's path is under /api/v1/")
    if api_key is not None and (
        not api_key or any(ch.isspace() or not ch.isprintable() for ch in api_key)
    ):
        raise CallRefused(f"{method} {path}: an API key is one printable word")
    url = api_url.rstrip("/") + path
    origin = urlparse(url)
    try:
        port = origin.port
    except ValueError:
        port = None
    if (
        origin.scheme != "http"
        or origin.hostname != STACK_HOST
        or origin.username is not None
        or origin.password is not None
        or port is None
        or port in config.RESERVED_PORTS
    ):
        raise CallRefused(
            f"{method} {path}: the API's address is not plain http on {STACK_HOST},"
            " with no user, at a port that is not reserved"
        )
    data = None if body is None else json.dumps(body).encode("utf-8")
    credential = (
        {"Authorization": f"Bearer {token}"}
        if api_key is None
        else {"X-API-Key": api_key}
    )
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **credential},
    )
    got, raw = opener(request)
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    if got != status:
        detail = parsed.get("detail") if isinstance(parsed, dict) else None
        said = f": {str(detail)[:200]}" if detail else ""
        raise CallRefused(f"{method} {path} was answered {got}, not {status}{said}")
    return got, parsed
