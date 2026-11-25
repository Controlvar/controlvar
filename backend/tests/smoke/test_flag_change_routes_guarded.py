"""Every route that can change a feature flag is accounted for.

The per-flag rule (``can_act_on_feature_flag``) only protects the routes that
call it.  Two of the routes that change flags once called nothing at all, so
this pins the exact set of mutating routes under ``/feature-flags``,
``/environments``, ``/rollout-schedules`` and ``/safety`` and says, for each,
what guards it.  A new mutating route there fails ``test_the_inventory_is_exact``
until it is classified here -- that is the point.

Routes are read through ``effective_route_contexts``: FastAPI >= 0.141 keeps
``include_router`` as a lazy entry, so plain ``app.routes`` holds only a handful
of entries and would see none of these (``test_app_routes_alone_would_see_nothing``).
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from backend.app.api import deps
from backend.app.main import app

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

PREFIXES = (
    "/api/v1/feature-flags",
    "/api/v1/environments",
    "/api/v1/rollout-schedules",
    "/api/v1/safety",
)
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# How each route is guarded.
FLAG_RULE = "calls can_act_on_feature_flag"
ROLLOUT_RULE = "calls _require_update_on_flag (UPDATE on the schedule's flag)"
CREATE_GATE = "role-gated via Depends(can_create_feature_flag)"
SUPERUSER = "exempt: superuser-only (Depends(get_current_superuser))"
API_KEY = "exempt: an API-key evaluation that changes nothing"

INVENTORY = {
    ("POST", "/api/v1/feature-flags/"): CREATE_GATE,
    # The no-slash twin of the create route (#94), hidden from the OpenAPI
    # document; the same endpoint function, so the same gate.
    ("POST", "/api/v1/feature-flags"): CREATE_GATE,
    ("POST", "/api/v1/feature-flags/bulk-toggle"): FLAG_RULE,
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"): API_KEY,
    ("PUT", "/api/v1/feature-flags/{flag_id}"): FLAG_RULE,
    ("DELETE", "/api/v1/feature-flags/{flag_id}"): FLAG_RULE,
    ("POST", "/api/v1/feature-flags/{flag_id}/activate"): FLAG_RULE,
    ("POST", "/api/v1/feature-flags/{flag_id}/deactivate"): FLAG_RULE,
    ("POST", "/api/v1/feature-flags/{flag_id}/toggle"): FLAG_RULE,
    ("POST", "/api/v1/feature-flags/{flag_id}/enable"): FLAG_RULE,
    ("POST", "/api/v1/feature-flags/{flag_id}/disable"): FLAG_RULE,
    ("POST", "/api/v1/feature-flags/{flag_id}/unarchive"): FLAG_RULE,
    # A flag's settings per environment (#2011).
    ("PUT", "/api/v1/feature-flags/{flag_id}/environments/{environment}"): FLAG_RULE,
    (
        "POST",
        "/api/v1/feature-flags/{flag_id}/environments/{target}/promote",
    ): FLAG_RULE,
    ("POST", "/api/v1/rollout-schedules/"): ROLLOUT_RULE,
    ("PUT", "/api/v1/rollout-schedules/{schedule_id}"): ROLLOUT_RULE,
    ("DELETE", "/api/v1/rollout-schedules/{schedule_id}"): ROLLOUT_RULE,
    ("POST", "/api/v1/rollout-schedules/{schedule_id}/activate"): ROLLOUT_RULE,
    ("POST", "/api/v1/rollout-schedules/{schedule_id}/pause"): ROLLOUT_RULE,
    ("POST", "/api/v1/rollout-schedules/{schedule_id}/cancel"): ROLLOUT_RULE,
    ("POST", "/api/v1/rollout-schedules/{schedule_id}/stages"): ROLLOUT_RULE,
    ("PUT", "/api/v1/rollout-schedules/stages/{stage_id}"): ROLLOUT_RULE,
    ("DELETE", "/api/v1/rollout-schedules/stages/{stage_id}"): ROLLOUT_RULE,
    ("POST", "/api/v1/rollout-schedules/stages/{stage_id}/advance"): ROLLOUT_RULE,
    ("POST", "/api/v1/safety/settings"): SUPERUSER,
    ("POST", "/api/v1/safety/feature-flags/{feature_flag_id}/config"): SUPERUSER,
    ("POST", "/api/v1/safety/feature-flags/{feature_flag_id}/rollback"): SUPERUSER,
}


def _contexts(application):
    """(method, path, route context) for every HTTP route, flattened."""
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            # A route declared on the app itself (``@app.post``) is a plain
            # APIRoute, with no contexts: it is its own context.
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _mutating_routes(application):
    return {
        (method, path): ctx
        for method, path, ctx in _contexts(application)
        if method in MUTATING and path.startswith(PREFIXES)
    }


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependency_calls(dep)


def test_app_routes_alone_would_see_nothing():
    """Why the flattening: the unflattened list holds none of these routes."""
    plain = {getattr(r, "path", None) for r in app.routes}
    assert not any(p and p.startswith(PREFIXES) for p in plain)


def test_the_inventory_is_exact():
    found = set(_mutating_routes(app))
    assert found == set(INVENTORY), (
        f"unclassified: {sorted(found - set(INVENTORY))}; "
        f"gone: {sorted(set(INVENTORY) - found)}"
    )
    assert len(INVENTORY) == 27


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/feature-flags/{flag_id}/environments/{environment}",
        "/api/v1/environments/{environment}",
        "/api/v1/environments/{environment}/promote",
        "/api/v1/rollout-schedules/{schedule_id}",
        "/api/v1/safety/settings",
    ],
)
def test_a_mutating_route_under_each_prefix_is_seen(path):
    """On an application with one such route, the walk finds it."""
    router = APIRouter()
    router.add_api_route(path, lambda: None, methods=["POST"])
    probe = FastAPI()
    probe.include_router(router)
    assert set(_mutating_routes(probe)) == {("POST", path)}


@pytest.mark.parametrize("key", sorted(INVENTORY), ids=lambda k: f"{k[0]} {k[1]}")
def test_each_route_is_guarded_as_classified(key):
    ctx = _mutating_routes(app)[key]
    guard = INVENTORY[key]
    source = inspect.getsource(ctx.endpoint)
    calls = set(_dependency_calls(ctx.dependant))
    if guard == FLAG_RULE:
        assert "can_act_on_feature_flag(" in source
    elif guard == ROLLOUT_RULE:
        assert "_require_update_on_flag(db, current_user" in source
    elif guard == CREATE_GATE:
        assert deps.can_create_feature_flag in calls
    elif guard == SUPERUSER:
        assert deps.get_current_superuser in calls
    elif guard == API_KEY:
        assert deps.get_api_key in calls
    else:  # pragma: no cover - a typo in INVENTORY
        pytest.fail(f"unknown guard {guard!r}")
