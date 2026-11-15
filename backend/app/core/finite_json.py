"""Refuses a JSON request body that holds a number that is not finite (#1630).

Python's JSON parser, which FastAPI uses for every JSON request body, reads
``NaN``, ``Infinity`` and ``-Infinity`` as floats, and a number too large for
a double (``1e400``) as an infinity. The first three are not JSON at all, and
no field of this API means any of the four. So a request whose body FastAPI parses as JSON
is answered 422 when that body holds one, anywhere: at the top level, inside an
object or an array, in a free-form field (``metadata``, ``context``) as in a
typed one. The error names the first such number in document order::

    {"detail": [{"type": "finite_number", "loc": ["body", "events", 1, "value"],
                 "msg": "Input should be a finite number"}]}

What is accepted: every finite number (``1e308`` included), ``-0.0``, ``null``,
JSON integers of any length (they stay integers), and the *string* ``"NaN"``.

:func:`refuse_non_finite_json` is an application dependency: it is passed to
the ``FastAPI`` constructor in ``backend/app/main.py``, so every route the
application includes, core and module alike, runs it before its own
dependencies (authentication among them) and before its body is validated.
It acts only when FastAPI itself parsed the body as JSON:

* the route declares a body, and that body is not a form (a form body is read
  with ``request.form()``, never parsed as JSON: the same ``is_body_form``
  test FastAPI makes);
* the body is not empty;
* the content type is one FastAPI parses: ``application/json`` or
  ``application/<anything>+json``, tested as FastAPI tests it. With no content
  type the body is parsed only on a route whose ``strict_content_type`` is
  off, as FastAPI does.

Anything else (a form, ``text/plain``, a route that reads its own body) is
left as it is. A body that is not valid JSON is never answered here: FastAPI
has already answered it with its own 422 (``json_invalid``), and should this
check meet one anyway it does nothing.

FastAPI keeps the parsed body on the request, so the body is not parsed a
second time. The walk is iterative (an explicit stack, never recursion), and
builds the path to the offending number only once it knows there is one.
"""

from __future__ import annotations

import email.message
from math import isfinite
from typing import Any, List, Optional, Tuple, Union

from fastapi import params
from fastapi.datastructures import DefaultPlaceholder
from fastapi.exceptions import RequestValidationError
from starlette.requests import HTTPConnection, Request

#: The ``type`` of the 422 error, as pydantic names the same refusal.
FINITE_NUMBER_TYPE = "finite_number"
#: Its ``msg``, pydantic's own wording for that type.
FINITE_NUMBER_MESSAGE = "Input should be a finite number"

PathPart = Union[str, int]


def has_non_finite(document: Any) -> bool:
    """Whether *document*, a parsed JSON value, holds a float that is NaN or
    infinite anywhere. Iterative; the stack holds containers only."""
    kind = type(document)
    if kind is float:
        return not isfinite(document)
    if kind is not dict and kind is not list:
        return False
    stack: List[Any] = [document]
    while stack:
        node = stack.pop()
        for child in node.values() if type(node) is dict else node:
            kind = type(child)
            if kind is float:
                if not isfinite(child):
                    return True
            elif kind is dict or kind is list:
                stack.append(child)
    return False


def first_non_finite(document: Any) -> Optional[Tuple[PathPart, ...]]:
    """The path to the first NaN or infinite float in *document*, in document
    order (object keys as strings, array positions as integers), or ``None``
    when there is none. ``()`` is the document itself."""
    if not has_non_finite(document):
        return None
    # Each entry carries a link to its parent's, (parent link, key or index),
    # so a push costs the same at any depth; the path is read back on a hit.
    stack: List[Tuple[Any, Any]] = [(document, None)]
    while stack:
        node, link = stack.pop()
        kind = type(node)
        if kind is float:
            if not isfinite(node):
                parts: List[PathPart] = []
                while link is not None:
                    link, part = link
                    parts.append(part)
                return tuple(reversed(parts))
        elif kind is dict:
            stack.extend((child, (link, key)) for key, child in reversed(node.items()))
        elif kind is list:
            stack.extend(
                (node[index], (link, index)) for index in range(len(node) - 1, -1, -1)
            )
    return None  # pragma: no cover - has_non_finite found one


def _resolved(value: Any) -> Any:
    return value.value if isinstance(value, DefaultPlaceholder) else value


def parses_as_json(content_type: Optional[str], strict_content_type: bool) -> bool:
    """Whether FastAPI parses a body sent with *content_type* as JSON, by the
    test ``fastapi.routing.get_request_handler`` makes."""
    if not content_type:
        return not strict_content_type
    message = email.message.Message()
    message["content-type"] = content_type
    if message.get_content_maintype() != "application":
        return False
    subtype = message.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


async def refuse_non_finite_json(conn: HTTPConnection) -> None:
    """Raise a 422 (``finite_number``) when the request's JSON body holds a
    number that is not finite; otherwise do nothing. See the module
    docstring for when it acts."""
    if not isinstance(conn, Request):
        return  # a websocket has no request body
    route = conn.scope.get("route")
    body_field = getattr(route, "body_field", None)
    if body_field is None:
        return
    if isinstance(body_field.field_info, params.Form):
        return
    strict = _resolved(getattr(route, "strict_content_type", True))
    if not parses_as_json(conn.headers.get("content-type"), bool(strict)):
        return
    if not await conn.body():
        return
    try:
        document = await conn.json()
    except (ValueError, RecursionError):
        # Not JSON (a JSONDecodeError or a UnicodeDecodeError, both
        # ValueErrors) or nested past the parser's limit: FastAPI answers it.
        return
    path = first_non_finite(document)
    if path is None:
        return
    raise RequestValidationError(
        [
            {
                "type": FINITE_NUMBER_TYPE,
                "loc": ("body", *path),
                "msg": FINITE_NUMBER_MESSAGE,
            }
        ]
    )
