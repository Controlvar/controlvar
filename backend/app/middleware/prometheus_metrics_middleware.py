"""
FastAPI/Starlette middleware that automatically records Prometheus metrics
for every HTTP request.

A request is labelled by the route it matched, as that route's template
(``/api/v1/feature-flags/user/{user_id}``), never by the path the client
sent, and ``UNMATCHED_ENDPOINT`` when it matched no route. The method label
is mapped to a fixed set by :func:`backend.app.core.metrics.record_request`.
"""

import time
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Scope

from backend.app.core.metrics import record_request

#: The ``endpoint`` label of a request that reached no route: a 404, the
#: redirect to a path's trailing-slash form, and a response a middleware gave
#: before routing (a refused ``Host``, a 429, a body whose ``Content-Length``
#: is over the size limit). A body over the limit that arrives without a
#: ``Content-Length`` is refused while its route reads it, so it is labelled
#: with that route's template.
UNMATCHED_ENDPOINT = "unmatched"


def _template(route: object) -> str | None:
    template = getattr(route, "path_format", None)
    return template if isinstance(template, str) and template else None


def route_template(scope: Scope) -> str:
    """The template of the route this request matched, or ``UNMATCHED_ENDPOINT``.

    Read once the request has been handled: routing records what it matched
    in the scope, which every layer of the stack shares. Nothing here reads
    the request path, so the label takes only the values the application's
    routes define, plus ``UNMATCHED_ENDPOINT``.

    * A route inside an included router (every ``/api/v1`` route except the
      documentation pages and ``/api/v1/openapi.json``, and the health
      routes): FastAPI keeps that route's full template in
      ``scope["fastapi"]["effective_route_context"].path_format``. The key is
      not public FastAPI API. If a FastAPI upgrade drops it, these routes are
      labelled ``UNMATCHED_ENDPOINT`` and
      ``test_metric_labels_stay_bounded_route_templates`` fails, naming this
      function. ``scope["route"].path`` is not a substitute: for an included
      route it is relative to its router (``/user/{user_id}``, and ``/`` for
      every router's list endpoint).
    * A route registered on the application itself (the API documentation
      pages and ``/api/v1/openapi.json``): its own template, found among the
      root router's routes. The root router's paths are already full ones.
    """
    fastapi_scope = scope.get("fastapi")
    if isinstance(fastapi_scope, dict):
        template = _template(fastapi_scope.get("effective_route_context"))
        if template is not None:
            return template

    route = scope.get("route")
    endpoint = scope.get("endpoint")
    if route is None and endpoint is None:
        return UNMATCHED_ENDPOINT
    for candidate in getattr(scope.get("router"), "routes", ()):
        if candidate is route or (
            endpoint is not None and getattr(candidate, "endpoint", None) is endpoint
        ):
            template = _template(candidate)
            if template is not None:
                return template
    return UNMATCHED_ENDPOINT


class PrometheusMetricsMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that records Prometheus HTTP metrics for all requests.

    Uses :func:`backend.app.core.metrics.record_request` to increment the
    ``http_requests_total`` counter and observe the
    ``http_request_duration_seconds`` histogram, with the ``endpoint`` label
    from :func:`route_template`.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start_time: float = time.time()
        response: Response = await call_next(request)
        duration: float = time.time() - start_time

        record_request(
            method=request.method,
            endpoint=route_template(request.scope),
            status_code=response.status_code,
            duration=duration,
        )
        return response
