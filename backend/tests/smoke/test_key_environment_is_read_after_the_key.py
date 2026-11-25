"""A dependency reads the presented key's environment only after the key (#2011).

``deps.get_api_key`` records the key's flag environment on ``request.state``,
and ``key_environment.presented_environment`` answers ``production`` when
nothing is recorded. FastAPI solves a route's dependencies in the order they
are declared, so a dependency that reads the environment and is declared
before the key dependency answers ``production`` for a staging key, and no
error says so. Handlers read it in their own body, where every dependency has
run; a dependency may read it only when ``get_api_key`` is below it, so that
it always runs after.

This walks every route of the application and reads the source of each
dependency in its tree. The scratch applications prove both halves: a
dependency that takes only the request is named, and what it answers does
depend on where it is declared.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Dict, List, Tuple

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.key_environment import (
    presented_environment,
    require_production_key,
)
from backend.app.main import app

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

#: What a function that reads the presented environment names.
READS = frozenset({"presented_environment", "flag_environment"})


def _contexts(application):
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            if getattr(ctx, "dependant", None) is None:
                continue
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _reads(call) -> bool:
    """Whether *call*'s source names the presented environment."""
    try:
        source = textwrap.dedent(inspect.getsource(call))
    except (OSError, TypeError):
        return False
    return any(
        (isinstance(node, ast.Name) and node.id in READS)
        or (isinstance(node, ast.Attribute) and node.attr in READS)
        for node in ast.walk(ast.parse(source))
    )


def _below(dependant) -> List:
    found = []
    for sub in dependant.dependencies:
        found += [sub.call, *_below(sub)]
    return found


def _early_readers(dependant) -> List[str]:
    """The dependencies that read the environment without ``get_api_key``
    below them (``get_api_key`` itself, which records it, apart)."""
    named = []
    for sub in dependant.dependencies:
        if sub.call is not deps.get_api_key and _reads(sub.call):
            if not any(call is deps.get_api_key for call in _below(sub)):
                named.append(f"{sub.call.__module__}.{sub.call.__qualname__}")
        named += _early_readers(sub)
    return named


def _walk(application) -> Dict[Tuple[str, str], List[str]]:
    return {
        (method, path): _early_readers(ctx.dependant)
        for method, path, ctx in _contexts(application)
    }


def test_the_walk_sees_what_it_must():
    """Guard the guard: the routes, the recorder and the one dependency that
    reads it (the split URL routes use it, in the full profile)."""
    assert len(_walk(app)) > 100
    assert _reads(deps.get_api_key)
    assert _reads(require_production_key)
    assert not _reads(deps.get_db)


def test_no_dependency_reads_the_environment_before_the_key():
    early = {route: names for route, names in _walk(app).items() if names}
    assert not early, (
        "these dependencies can run before deps.get_api_key records the key's "
        "environment, and then answer production for any key:\n"
        + "\n".join(f"{m} {p}: {names}" for (m, p), names in sorted(early.items()))
    )


# ---------------------------------------------------------------------------
# Guards, on scratch applications
# ---------------------------------------------------------------------------


async def _reads_the_request(request: Request) -> str:
    return presented_environment(request)


async def _layered(request: Request, _owner=Depends(deps.get_api_key)) -> str:
    return presented_environment(request)


def _scratch() -> FastAPI:
    application = FastAPI()
    router = APIRouter()

    @router.get("/before")
    async def before(
        environment: str = Depends(_reads_the_request),
        _owner=Depends(deps.get_api_key),
    ) -> str:
        return environment

    @router.get("/after")
    async def after(
        _owner=Depends(deps.get_api_key),
        environment: str = Depends(_reads_the_request),
    ) -> str:
        return environment

    @router.get("/layered")
    async def layered(environment: str = Depends(_layered)) -> str:
        return environment

    @router.get("/handler")
    async def handler(request: Request, _owner=Depends(deps.get_api_key)) -> str:
        return presented_environment(request)

    application.include_router(router)
    return application


def test_a_dependency_that_takes_only_the_request_is_named():
    named = f"{__name__}._reads_the_request"
    walked = _walk(_scratch())
    assert walked[("GET", "/before")] == [named]
    assert walked[("GET", "/after")] == [named]
    assert walked[("GET", "/layered")] == walked[("GET", "/handler")] == []


def test_what_it_answers_depends_on_where_it_is_declared():
    """Why it is named: declared before the key, it answers ``production``
    for a staging key. The handler and the layered dependency do not."""
    application = _scratch()

    def a_staging_key(request: Request) -> str:
        request.state.flag_environment = "staging"
        return "owner"

    application.dependency_overrides[deps.get_api_key] = a_staging_key
    client = TestClient(application)
    answers = {
        path: client.get(path).json()
        for path in ("/before", "/after", "/layered", "/handler")
    }
    assert answers == {
        "/before": "production",
        "/after": "staging",
        "/layered": "staging",
        "/handler": "staging",
    }
