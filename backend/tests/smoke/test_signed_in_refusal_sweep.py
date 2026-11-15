"""Every signed-in route refuses a request that carries no token with the same
401, whatever else the request carries.

A route is signed-in when its dependency tree reaches ``deps.get_current_user``
(matched by identity). Each one is called with its path parameters filled with
a placeholder and, for a method that takes a body, an empty JSON object: it
must answer ``401 {"detail": "Not authenticated"}`` with
``WWW-Authenticate: Bearer``, which is decided before any database step, the
handler, or the validation of the path, the query or the body. No database is
needed: a session opens no connection until it runs a query.
"""

from __future__ import annotations

import importlib.util
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

NOT_AUTHENTICATED = (401, {"detail": "Not authenticated"}, "Bearer")
FLOOR = {"core": 150, "full": 220}
PLACEHOLDER = "00000000-0000-4000-8000-000000000000"


def _reaches(dependant, target) -> bool:
    return dependant.call is target or any(
        _reaches(sub, target) for sub in dependant.dependencies
    )


def _signed_in_routes():
    found = set()
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            dependant = getattr(ctx, "dependant", None)
            if dependant is None or not _reaches(dependant, deps.get_current_user):
                continue
            for method in getattr(ctx, "methods", None) or ():
                found.add((method, ctx.path))
    return sorted(found)


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", PLACEHOLDER, path)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    assert app.dependency_overrides == {}
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_every_signed_in_route_refuses_a_request_without_a_token(client):
    routes = _signed_in_routes()
    profile = "full" if importlib.util.find_spec("modules") is not None else "core"
    assert len(routes) >= FLOOR[profile], len(routes)
    wrong = {}
    for method, path in routes:
        body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
        response = client.request(method, _url(path), json=body)
        answer = (
            response.status_code,
            response.json() if response.content else None,
            response.headers.get("www-authenticate"),
        )
        if answer != NOT_AUTHENTICATED:
            wrong[f"{method} {path}"] = answer
    assert not wrong, f"answers other than {NOT_AUTHENTICATED}: {wrong}"
