"""Demo mode on the real application: an exact allow-list, and every other write refused.

With ``DEMO_MODE=true`` a request whose method is not GET, HEAD or OPTIONS is
answered 403 ``{"detail": {"code": "demo_read_only", "message": ...}}`` unless
it is one of eight POSTs (``backend/app/core/demo_mode.py``). These tests hold,
in each profile:

* the allow-list is exactly the eight pairs pinned here, and each is a live
  route that demo mode lets through;
* every mutating route of the application -- read through
  ``effective_route_contexts``, as ``test_flag_change_routes_guarded.py`` does,
  so a route added later is in the sweep -- is refused for a signed-in VIEWER,
  before its handler or any database session; refused equals mutating minus
  allowed, and mutating is at least the profile's floor, so a sweep that sees
  nothing fails;
* a test-only route, ``POST /api/v1/tracking/zz-demo-plant``, mounted for the
  sweep, is refused like any other: a prefix in the allow-list would let it
  through;
* variants of each refused request -- a query string, a toggled trailing
  slash, a doubled slash, an upper-case prefix, a percent-encoded letter, a
  method-override header, and HEAD or OPTIONS -- never reach a handler;
* the refusal is readable by a dashboard on another origin: it carries
  ``Access-Control-Allow-Origin`` and ``X-Request-ID``.

No database is needed: the refusal comes before any session is opened, and a
``get_db`` that records each call proves it.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from typing import Dict, List, Set, Tuple

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.demo_mode import REFUSAL_BODY as MODULE_REFUSAL_BODY
from backend.app.main import app
from backend.app.middleware.demo_mode_middleware import DemoModeMiddleware
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.smoke]

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
PLACEHOLDER = "00000000-0000-4000-8000-000000000000"
ORIGIN = "http://localhost:3100"  # a development default in CORS_ORIGINS

#: The allow-list, pinned here rather than read from the module.
PINNED_ALLOWED = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/token"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/track"),
    ("POST", "/api/v1/tracking/batch"),
    ("POST", "/api/v1/tracking/errors"),
    ("POST", "/api/v1/tracking/errors/batch"),
}

#: Mutating routes the application serves, measured at runtime (107 core,
#: 157 full). A floor: an enumeration that finds fewer has stopped seeing them.
FLOOR = {"core": 107, "full": 157}

PLANT = ("POST", "/api/v1/tracking/zz-demo-plant")

#: The refusal, as a literal: exactly what a client compares against (the
#: dashboard keeps the same body as a fixture). Not read from the module, so a
#: change to the wording or the shape there fails here.
REFUSAL_BODY = {
    "detail": {
        "code": "demo_read_only",
        "message": (
            "This is a read-only demo: creating and changing things is turned off."
        ),
    }
}
REFUSED = (403, REFUSAL_BODY)


def _profile() -> str:
    return "full" if importlib.util.find_spec("modules") is not None else "core"


def _live_routes() -> Set[Tuple[str, str]]:
    """Every (method, path) the application routes, flattened."""
    found: Set[Tuple[str, str]] = set()
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                found.add((method, ctx.path))
    return found


def _mutating_routes() -> List[Tuple[str, str]]:
    return sorted(pair for pair in _live_routes() if pair[0] in MUTATING)


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", PLACEHOLDER, path)


def _answer(response) -> Tuple[int, object]:
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = response.text
    return response.status_code, body


def _viewer() -> User:
    return User(
        id=uuid.UUID(PLACEHOLDER),
        username="demo_viewer",
        email="viewer@demo.com",
        hashed_password="!",
        role=UserRole.VIEWER,
        is_superuser=False,
        is_active=True,
    )


@pytest.fixture
def db_calls(monkeypatch) -> List[int]:
    """A signed-in VIEWER, and a ``get_db`` that records every session asked for."""
    calls: List[int] = []
    viewer = _viewer()

    def recorded_db():
        calls.append(1)
        yield None

    async def current_user():
        return viewer

    def current_active_user():
        return viewer

    overrides = {
        deps.get_db: recorded_db,
        deps.get_current_user: current_user,
        deps.get_current_active_user: current_active_user,
    }
    assert app.dependency_overrides == {}
    app.dependency_overrides.update(overrides)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    try:
        yield calls
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


@pytest.fixture
def plant() -> List[int]:
    """Mount the test-only route for one test; record each time its handler runs."""
    reached: List[int] = []

    def zz_demo_plant() -> Dict[str, bool]:
        reached.append(1)
        return {"reached": True}

    app.router.add_api_route(
        PLANT[1], zz_demo_plant, methods=[PLANT[0]], include_in_schema=False
    )
    route = app.router.routes[-1]
    assert getattr(route, "path", None) == PLANT[1]
    try:
        yield reached
    finally:
        app.router.routes.remove(route)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)


# --- P2: the allow-list is exact ---------------------------------------------------


def test_the_refusal_is_exactly_the_published_body(client, demo_on):
    assert MODULE_REFUSAL_BODY == REFUSAL_BODY
    response = client.post("/api/v1/experiments/", json={})
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/json"
    assert response.json() == REFUSAL_BODY
    assert response.content == (
        b'{"detail":{"code":"demo_read_only","message":"This is a read-only demo: '
        b'creating and changing things is turned off."}}'
    )


def test_the_allow_list_is_exact():
    allowed = DemoModeMiddleware(app=None).allowed
    assert allowed == PINNED_ALLOWED, (
        f"unexpected allowed: {sorted(allowed - PINNED_ALLOWED)}; "
        f"missing: {sorted(PINNED_ALLOWED - allowed)}"
    )
    assert len(allowed) == 8


def test_every_allowed_entry_is_a_live_route():
    """Both the pinned list and the one the layer applies name routes that exist."""
    allowed = DemoModeMiddleware(app=None).allowed
    gone = (PINNED_ALLOWED | allowed) - _live_routes()
    assert not gone, f"gone: {sorted(gone)}"


def test_each_allowed_post_reaches_its_route(client, demo_on):
    """The control for the sweep: demo mode lets exactly these through."""
    stopped = {}
    for method, path in sorted(PINNED_ALLOWED):
        answer = _answer(client.request(method, path, json={}))
        if answer == REFUSED or answer[0] == 404:
            stopped[f"{method} {path}"] = answer
    assert not stopped, f"allowed but not reached: {stopped}"


def test_the_dashboards_two_writes_pass(client, demo_on):
    """In demo mode the dashboard writes only these two, at exactly these paths."""
    assert {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
    } <= DemoModeMiddleware(app=None).allowed
    assert client.post("/api/v1/auth/logout").status_code == 204
    login = client.post("/api/v1/auth/login", json={})
    assert login.status_code == 422, login.text  # reached the route, not refused


# --- P3: every other mutating route is refused -------------------------------------


def test_every_mutating_route_is_refused_for_a_signed_in_viewer(
    client, demo_on, db_calls, plant
):
    mutating = _mutating_routes()
    assert PLANT in mutating, "the test-only route is not mounted"
    assert len(mutating) - 1 >= FLOOR[_profile()], (len(mutating) - 1, _profile())
    refused, through, sessions = set(), {}, []
    for method, path in mutating:
        before = len(db_calls)
        answer = _answer(client.request(method, _url(path), json={}))
        if answer == REFUSED:
            refused.add((method, path))
        else:
            through[(method, path)] = answer
        if len(db_calls) != before and (method, path) not in PINNED_ALLOWED:
            sessions.append(f"{method} {path}")
    expected = set(mutating) - PINNED_ALLOWED
    assert refused == expected, (
        f"allowed but not in DEMO_ALLOWED: {sorted(expected - refused)}; "
        f"refused but allowed: {sorted(refused - expected)}; "
        f"answers: { {k: v for k, v in through.items() if k not in PINNED_ALLOWED} }"
    )
    assert plant == [], "the test-only route's handler ran"
    assert sessions == [], f"a database session was opened for {sessions}"


def test_with_demo_mode_off_the_routes_answer(client, monkeypatch, plant):
    """The control: the refusal is demo mode's, and the test-only route is live.

    No identity is given, so the signed-in route answers 401 before its
    handler; the test-only route has no dependency and runs.
    """
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    assert app.dependency_overrides == {}
    assert _answer(client.post("/api/v1/experiments/", json={})) == (
        401,
        {"detail": "Not authenticated"},
    )
    assert _answer(client.post(PLANT[1])) == (200, {"reached": True})
    assert plant == [1]


# --- P4: variants never reach a handler ----------------------------------------------


def _percent_encoded(url: str) -> str:
    """The first letter after the API prefix, percent-encoded."""
    prefix = "/api/v1/"
    letter = url[len(prefix)]
    return f"{prefix}%{ord(letter):02X}{url[len(prefix) + 1 :]}"


def _variants(method: str, url: str):
    toggled = url[:-1] if url.endswith("/") else url + "/"
    yield "query", method, url + "?x=1", {}
    yield "slash", method, toggled, {}
    yield "double-slash", method, "/" + url, {}
    yield "upper-case", method, "/API/V1/" + url[len("/api/v1/") :], {}
    yield "percent-encoded", method, _percent_encoded(url), {}
    for header in ("X-HTTP-Method-Override", "X-Method-Override", "X-HTTP-Method"):
        yield header, method, url, {header: "GET"}


def test_variants_of_a_refused_request_never_reach_a_handler(
    client, demo_on, db_calls, plant
):
    wrong = {}
    for method, path in _mutating_routes():
        if (method, path) in PINNED_ALLOWED:
            continue
        url = _url(path)
        for name, verb, target, headers in _variants(method, url):
            answer = _answer(client.request(verb, target, json={}, headers=headers))
            if answer != REFUSED:
                wrong[f"{name}: {verb} {target}"] = answer
    assert not wrong, f"{len(wrong)} variants answered otherwise: {wrong}"
    assert plant == [], "the test-only route's handler ran"
    assert db_calls == [], "a database session was opened"


def test_head_and_options_on_a_mutating_path_reach_no_handler(
    client, demo_on, db_calls, plant
):
    wrong = {}
    for method, path in _mutating_routes():
        if (method, path) in PINNED_ALLOWED:
            continue
        url = _url(path)
        for verb in ("HEAD", "OPTIONS"):
            status = client.request(verb, url).status_code
            if 200 <= status < 300:
                wrong[f"{verb} {url}"] = status
    assert not wrong, wrong
    assert plant == [], "the test-only route's handler ran"


# --- P5: a dashboard on another origin can read the refusal ----------------------------


def test_the_refusal_carries_cors_and_request_id_headers(client, demo_on):
    response = client.post("/api/v1/experiments/", json={}, headers={"Origin": ORIGIN})
    assert _answer(response) == REFUSED
    assert response.headers.get("access-control-allow-origin") == ORIGIN, dict(
        response.headers
    )
    assert response.headers.get("x-request-id")
    assert response.headers.get("x-content-type-options") == "nosniff"
    # Credentials are never allowed cross-origin.
    assert "access-control-allow-credentials" not in response.headers


def test_a_preflight_for_a_refused_write_is_still_answered(client, demo_on):
    """The browser asks first; the refusal is the answer to the request itself."""
    response = client.options(
        "/api/v1/experiments/",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == ORIGIN
