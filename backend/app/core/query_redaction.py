"""Query values that are never written to a log line.

uvicorn writes every request to its access log as
``"GET /path?query HTTP/1.1"``, and every WebSocket handshake to
``uvicorn.error`` the same way, query string and all. The API's own request
logging middlewares log a request's query parameters too. Some query values
must not end up in a log:

* every value on the OIDC callback route
  (``/api/v1/auth/sso/oidc/{provider}/callback``): the provider's ``code`` and
  ``state``, and whatever else it appends;
* on any route, the value of a parameter whose name is in
  :data:`REDACTED_QUERY_NAMES` (the OAuth parameters ``code``, ``state``,
  ``token`` and the like, common spellings such as ``api_key``,
  ``password`` and ``jwt``, and ``context``, the end user's attributes that
  the SDKs send to ``GET /api/v1/feature-flags/evaluate/{key}`` and
  ``GET /api/v1/feature-flags/user/{user_id}``). The name is compared without
  regard to case, after percent-decoding, so ``CODE`` and ``%63ode`` are
  matched too.

Each such value is replaced with :data:`REDACTED`; the parameter names are
kept, so a log line still shows which parameters a request carried. Only the
first :data:`MAX_QUERY_CHARS` characters of a query string are looked at, and
the rest is replaced with :data:`TRUNCATED`. The parsing is a split on ``&``
and ``=``: no regular expression, linear in the length of the input.

:func:`configure_logging` (``backend/app/core/logger.py``) installs
:data:`QUERY_VALUE_REDACTOR` on the ``uvicorn.access`` and ``uvicorn.error``
loggers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping
from urllib.parse import unquote_plus

#: What a value that is not logged is written as.
REDACTED = "[redacted]"

#: Parameters whose value is not logged on any route (lower case).
REDACTED_QUERY_NAMES = frozenset(
    {
        "code",
        "state",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "client_secret",
        "api_key",
        "apikey",
        "password",
        "secret",
        "jwt",
        # The end user's attributes on the flag-evaluation routes.
        "context",
    }
)

#: The longest query string that is logged; anything after it is dropped.
MAX_QUERY_CHARS = 2048

#: Appended to a query string cut at :data:`MAX_QUERY_CHARS`.
TRUNCATED = "[truncated]"


def is_oidc_callback(path: str) -> bool:
    """Whether *path* is the OIDC callback, ``.../auth/sso/oidc/{provider}/callback``.

    Matched loosely (any prefix, any case, a trailing slash) so that a path
    that is not quite the route still has its values left out.
    """
    lowered = path.lower().rstrip("/")
    return "/auth/sso/oidc/" in lowered and lowered.endswith("/callback")


def _is_redacted_name(name: str) -> bool:
    return unquote_plus(name).lower() in REDACTED_QUERY_NAMES


def redact_query(path: str, query: str) -> str:
    """*query*, the raw query string of a request to *path*, fit to log.

    ``redact_query("/api/v1/x", "code=abc&page=2")`` gives
    ``"code=[redacted]&page=2"``. On the OIDC callback every value is
    replaced. A pair with no ``=`` has no value and is kept as it is.
    """
    truncated = len(query) > MAX_QUERY_CHARS
    if truncated:
        query = query[:MAX_QUERY_CHARS]
    every_value = is_oidc_callback(path)
    pairs = []
    for pair in query.split("&"):
        name, equals, _value = pair.partition("=")
        if equals and (every_value or _is_redacted_name(name)):
            pair = f"{name}={REDACTED}"
        pairs.append(pair)
    redacted = "&".join(pairs)
    return redacted + TRUNCATED if truncated else redacted


def redact_target(target: str) -> str:
    """A request target, ``path?query``, with :func:`redact_query` applied.

    A target with no ``?`` is returned unchanged.
    """
    path, question_mark, query = target.partition("?")
    if not question_mark:
        return target
    return f"{path}?{redact_query(path, query)}"


def redact_query_params(path: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    """Parsed query parameters of a request to *path*, fit to log.

    The same rule as :func:`redact_query`, for a mapping such as
    ``request.query_params``.
    """
    every_value = is_oidc_callback(path)
    return {
        name: REDACTED
        if every_value or str(name).lower() in REDACTED_QUERY_NAMES
        else value
        for name, value in params.items()
    }


def _redact_arg(arg: Any) -> Any:
    if not isinstance(arg, str) or "?" not in arg:
        return arg
    try:
        return redact_target(arg)
    except Exception:
        # Never let a log call fail; drop the whole query instead.
        return f"{arg.partition('?')[0]}?{REDACTED}"


class QueryValueRedactor(logging.Filter):
    """Rewrites every ``%s`` argument of a record that holds a ``?`` query.

    uvicorn passes the request target as a separate argument
    (``'%s - "%s %s HTTP/%s" %d'``), so the target is rewritten before the
    line is formatted, by whichever handler formats it.

    Two uvicorn log levels are outside it: ``--log-level trace`` (the
    ``uvicorn.asgi`` logger writes the whole ASGI scope, query string
    included) and ``--log-level debug`` (the websockets library writes a
    handshake's ``Authorization`` header) can still write these values.
    Nothing in this repository sets either.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        return True


#: The one instance :func:`configure_logging` installs (``addFilter`` keeps
#: one copy of an instance, so configuring twice does not add it twice).
QUERY_VALUE_REDACTOR = QueryValueRedactor()
