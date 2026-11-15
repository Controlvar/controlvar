"""Exactly which routes accept an API key that carries ``mcp:read``.

``get_current_reader`` (``backend/app/api/read_scope.py``) lets four read
operations -- five route registrations, because the flag list also answers
without its trailing slash -- take a key with the ``mcp:read`` scope as well as
a sign-in. This pins the set, over every route of the application: a route
that starts depending on it, or one that stops, fails
``test_the_mcp_read_routes_are_exactly_these`` until it is listed here. Every
one is a GET, so no route that changes anything takes such a key.

It also pins what the served OpenAPI document says about them: each of the
four operations offers the key under its own security scheme, and that scheme
names ``mcp:read`` (a generated client would otherwise read "any API key").

Routes are read through ``effective_route_contexts`` (FastAPI keeps
``include_router`` lazy), as in ``test_flag_change_routes_guarded.py``.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from backend.app.api.read_scope import MCP_READ_KEY_HEADER, get_current_reader
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

#: Every (method, path) whose user may come from an ``mcp:read`` key.
MCP_READ_ROUTES = {
    ("GET", "/api/v1/feature-flags/"),
    # The no-slash twin of the flag list (#94), hidden from the OpenAPI
    # document; the same endpoint function, so the same dependency.
    ("GET", "/api/v1/feature-flags"),
    ("GET", "/api/v1/experiments/"),
    ("GET", "/api/v1/experiments/{experiment_id}"),
    ("GET", "/api/v1/experiments/{experiment_id}/results"),
}

#: The same set as the OpenAPI document lists it (the twin is not in it).
MCP_READ_OPERATIONS = MCP_READ_ROUTES - {("GET", "/api/v1/feature-flags")}

SCHEME = MCP_READ_KEY_HEADER.scheme_name


def _contexts(application):
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependency_calls(dep)


def _readers(application) -> set[tuple[str, str]]:
    return {
        (method, path)
        for method, path, ctx in _contexts(application)
        if get_current_reader in set(_dependency_calls(ctx.dependant))
    }


def test_the_walk_sees_the_routes():
    """Guard the guard: an empty walk would make the inventory vacuous."""
    assert len(list(_contexts(app))) > 100


def test_the_mcp_read_routes_are_exactly_these():
    found = _readers(app)
    extra = sorted(found - MCP_READ_ROUTES)
    missing = sorted(MCP_READ_ROUTES - found)
    assert not extra, (
        f"{', '.join(f'{m} {p}' for m, p in extra)} accepts an mcp:read key but "
        "is not in the pinned set"
    )
    assert not missing, (
        f"{', '.join(f'{m} {p}' for m, p in missing)} no longer accepts an mcp:read key"
    )


def test_every_mcp_read_route_is_a_get():
    """No route that changes anything takes an mcp:read key."""
    not_get = sorted(r for r in _readers(app) if r[0] != "GET")
    assert not_get == [], not_get


@pytest.fixture(scope="module")
def document():
    return app.openapi()


def _operations(document):
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if isinstance(operation, dict) and "responses" in operation:
                yield method.upper(), path, operation


def test_the_document_offers_the_key_on_exactly_the_four_operations(document):
    offering = {
        (method, path)
        for method, path, operation in _operations(document)
        if any(SCHEME in requirement for requirement in operation.get("security", []))
    }
    assert offering == MCP_READ_OPERATIONS


def test_the_document_names_mcp_read_on_each_of_the_four_operations(document):
    """The document says which key these operations take, so a generated
    client does not read "any API key"."""
    scheme = document["components"]["securitySchemes"][SCHEME]
    assert (scheme["type"], scheme["in"], scheme["name"]) == (
        "apiKey",
        "header",
        "X-API-Key",
    )
    by_label = {(m, p): op for m, p, op in _operations(document)}
    for method, path in sorted(MCP_READ_OPERATIONS):
        operation = by_label[(method, path)]
        names = {name for requirement in operation["security"] for name in requirement}
        named = [
            document["components"]["securitySchemes"][name].get("description", "")
            for name in names
            if document["components"]["securitySchemes"][name]["type"] == "apiKey"
        ] + [operation.get("description", "")]
        assert any("mcp:read" in text for text in named), (
            f"{method} {path} accepts an API key but the document does not say it "
            "needs mcp:read"
        )
        # A sign-in is still the first way in, unchanged.
        assert operation["security"][0] == {"OAuth2PasswordBearer": []}
