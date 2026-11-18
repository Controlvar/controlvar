"""Every route that can take away a superuser's own access is accounted for.

``PUT /api/v1/admin/users/{id}`` and ``PUT /api/v1/users/{id}`` once let a
superuser set their own ``is_superuser`` or ``is_active`` to false (#652); the
refusal only protects the routes that apply it. This pins the exact set of
mutating routes under ``/api/v1/users`` and ``/api/v1/admin/users``, and, for
each one whose request body can carry ``is_superuser`` or ``is_active``, what
refuses an own-account change. A new route there fails until it is classified.

Routes are read through ``effective_route_contexts``, as in
``test_flag_change_routes_guarded.py``: plain ``app.routes`` holds
``include_router`` as a lazy entry and would see none of them.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute

from backend.app.main import app

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

PREFIXES = ("/api/v1/users", "/api/v1/admin/users")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
FLAGS = {"is_superuser", "is_active"}

# Every mutating route under PREFIXES. The two DELETEs of an account refuse
# deleting your own account in the handler. The four ``/users/me/2fa`` actions
# change only the caller's own two-step sign-in, and the superuser's
# ``/admin/users/{user_id}/2fa`` reset refuses the caller's own account
# (#1812); none of their bodies carries FLAGS.
INVENTORY = {
    ("POST", "/api/v1/users/"),
    ("POST", "/api/v1/users/me/password"),
    ("POST", "/api/v1/users/me/2fa/enroll"),
    ("POST", "/api/v1/users/me/2fa/confirm"),
    ("POST", "/api/v1/users/me/2fa/disable"),
    ("POST", "/api/v1/users/me/2fa/recovery-codes"),
    ("PUT", "/api/v1/users/{user_id}"),
    ("DELETE", "/api/v1/users/{user_id}"),
    ("PUT", "/api/v1/admin/users/{user_id}"),
    ("PATCH", "/api/v1/admin/users/{user_id}"),
    ("DELETE", "/api/v1/admin/users/{user_id}"),
    ("DELETE", "/api/v1/admin/users/{user_id}/2fa"),
}

# How each route whose body can carry FLAGS refuses an own-account change.
SHARED_REFUSAL = "calls refuse_if_own_access_removed"
PATCH_REFUSAL = "refuses OWN_DEACTIVATION_REFUSED (AdminUserPatch has no is_superuser)"
CREATE = "exempt: creates an account, so cannot remove existing access"

FLAG_CARRIERS = {
    ("POST", "/api/v1/users/"): CREATE,
    ("PUT", "/api/v1/users/{user_id}"): SHARED_REFUSAL,
    ("PUT", "/api/v1/admin/users/{user_id}"): SHARED_REFUSAL,
    ("PATCH", "/api/v1/admin/users/{user_id}"): PATCH_REFUSAL,
}


def _under_prefixes(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PREFIXES)


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
        if method in MUTATING and _under_prefixes(path)
    }


def _body_fields(ctx) -> set:
    """Every field name of every body model the route accepts."""
    names = set()
    for param in ctx.dependant.body_params:
        model = param.field_info.annotation
        names |= set(getattr(model, "model_fields", {}) or {})
        names.add(param.name)
    return names


def test_app_routes_alone_would_see_nothing():
    """Why the flattening: the unflattened list holds none of these routes."""
    plain = {getattr(r, "path", None) for r in app.routes}
    assert not any(p and _under_prefixes(p) for p in plain)


def test_the_inventory_is_exact():
    found = set(_mutating_routes(app))
    assert found == INVENTORY, (
        f"unclassified: {sorted(found - INVENTORY)}; gone: {sorted(INVENTORY - found)}"
    )


def test_the_routes_that_can_carry_the_flags_are_exactly_these():
    carriers = {
        key for key, ctx in _mutating_routes(app).items() if _body_fields(ctx) & FLAGS
    }
    assert carriers == set(FLAG_CARRIERS), (
        f"unclassified: {sorted(carriers - set(FLAG_CARRIERS))}; "
        f"gone: {sorted(set(FLAG_CARRIERS) - carriers)}"
    )


@pytest.mark.parametrize("key", sorted(FLAG_CARRIERS), ids=lambda k: f"{k[0]} {k[1]}")
def test_each_carrier_refuses_as_classified(key):
    ctx = _mutating_routes(app)[key]
    guard = FLAG_CARRIERS[key]
    source = inspect.getsource(ctx.endpoint)
    if guard == SHARED_REFUSAL:
        assert "refuse_if_own_access_removed(user, current_user, update_data)" in (
            source
        )
    elif guard == PATCH_REFUSAL:
        assert "detail=OWN_DEACTIVATION_REFUSED" in source
        assert "is_superuser" not in _body_fields(ctx)
    elif guard == CREATE:
        assert ctx.endpoint.__name__ == "create_user"
    else:  # pragma: no cover - a typo in FLAG_CARRIERS
        pytest.fail(f"unknown guard {guard!r}")
