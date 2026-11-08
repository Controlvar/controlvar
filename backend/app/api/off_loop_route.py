"""The route class of every router: a synchronous handler gives up the
request's database connection before it waits for a worker thread.

FastAPI runs a synchronous (``def``) handler in a worker thread, and, when the
route has a response model, validates the handler's result in a second trip to
a worker. Both trips wait in the same queue as every other request's worker
work. By then the request's session usually holds a pooled connection, taken
by an earlier step (``deps.get_current_user`` runs a query), so a request
waiting its turn kept a connection out of the pool for as long as the queue
was long.

:class:`OffLoopRoute` replaces such a handler, before FastAPI sees it, with an
``async`` function that

1. on the event loop, ends the transaction of every session the request's
   ``get_db`` registered (``backend.app.db.request_sessions``) with
   ``rollback()``, which returns the connection to the pool. ``rollback()``
   rather than ``close()``: objects the dependencies loaded stay attached to
   the session, and are read again in the worker when the handler uses them;
2. takes one trip to a worker thread, in which it calls the handler (whose
   first query takes a connection again) and then validates the result
   against the route's response model, so that reading the result's
   attributes -- relationships load on first access -- happens in the worker
   on the connection the handler holds, never on the event loop.

FastAPI then validates the already-validated value again, on the event loop.
That pass reads nothing from the database: a validated model is returned as it
is. A result that does not validate is handed back as the handler returned it,
so FastAPI raises its own error exactly as before. A route without a response
model gets its result passed through ``jsonable_encoder`` in the worker, for
the same reason; FastAPI's own pass over the encoded result changes nothing.

A handler that returns a ``Response`` is unaffected beyond running after the
release. ``async`` handlers, generators and the routes of the WebSocket are not
wrapped. The wrapper carries the handler's signature, name, module and
docstring (``functools.wraps``), so dependencies, parameters, the inferred
response model and the OpenAPI document are the handler's own, and the module
function itself is unchanged for code and tests that call it directly.

A dependency must not leave uncommitted changes in the request's session: the
release discards them.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Dict, Tuple

from fastapi.encoders import jsonable_encoder
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from backend.app.db import request_sessions

#: Set on the replacement function, so that a walk over the routes can tell
#: a wrapped handler from one written ``async``.
OFF_LOOP_MARKER = "__off_loop__"


def release_request_sessions() -> None:
    """End the transaction of every session the request registered, which
    returns each session's connection to the pool."""
    for session in request_sessions.current():
        session.rollback()


def runs_in_a_worker(endpoint: Callable[..., Any]) -> bool:
    """Whether FastAPI would call ``endpoint`` in a worker thread: a plain
    function, not a coroutine function or a generator of either kind. As
    FastAPI does, the function and what it wraps are both looked at."""
    for target in (endpoint, inspect.unwrap(endpoint)):
        if (
            inspect.iscoroutinefunction(target)
            or inspect.isgeneratorfunction(target)
            or inspect.isasyncgenfunction(target)
        ):
            return False
    return True


def _call_and_validate(
    route: APIRoute,
    endpoint: Callable[..., Any],
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
) -> Any:
    """Run in a worker: the handler, then the validation of its result."""
    result = endpoint(*args, **kwargs)
    if isinstance(result, Response):
        return result
    field = route.response_field
    if field is None:
        return jsonable_encoder(result)
    value, errors = field.validate(result, {}, loc=("response",))
    if errors:
        return result
    return value


def off_loop(endpoint: Callable[..., Any], route: APIRoute) -> Callable[..., Any]:
    """``endpoint`` as an ``async`` function that releases the request's
    sessions, then calls it and validates its result in one worker trip."""

    @functools.wraps(endpoint)
    async def handler(*args: Any, **kwargs: Any) -> Any:
        release_request_sessions()
        return await run_in_threadpool(
            _call_and_validate, route, endpoint, args, kwargs
        )

    setattr(handler, OFF_LOOP_MARKER, True)
    return handler


class OffLoopRoute(APIRoute):
    """An ``APIRoute`` whose synchronous handler releases the request's
    database sessions before it waits for a worker thread (see the module
    docstring). Every ``APIRouter`` in the application is built with
    ``route_class=OffLoopRoute``, or a subclass of it."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any):
        if runs_in_a_worker(endpoint):
            endpoint = off_loop(endpoint, self)
        super().__init__(path, endpoint, **kwargs)
