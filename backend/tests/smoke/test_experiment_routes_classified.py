"""Every route that acts on experiments, and the rule that admits a caller.

The inventory is every HTTP and WebSocket route of the application whose path
is ``/api/v1/experiments`` or under it, is under ``/api/v1/results/`` or
``/api/v1/ws/experiments/``, or contains ``{experiment_id}``; plus
``POST /api/v1/mutual-exclusion-groups/{group_id}/experiments``,
``GET /api/v1/export/experiments`` and ``GET /api/v1/ws/active-experiments``.
A new route there fails ``test_the_inventory_is_exactly_this`` until it is
given a class below.

This pins the inventory and its labels, not behaviour. What each label claims
for the experiment routes is proved by
``backend/tests/integration/api/test_experiment_access_by_role.py``, and for the
results, export, interaction, bandit, counters and stream routes by
``backend/tests/integration/api/test_experiment_data_read_rule.py``. The export
and interaction routes have a second exact table,
``EXPORT_AND_INTERACTION_ROUTES``, since the selection rule sees only two.

HTTP routes are read through ``effective_route_contexts`` (FastAPI keeps
``include_router`` lazy), as in ``test_sdk_scoped_routes.py``. Those contexts
contain no WebSocket route, so WebSockets are found by walking the included
routers themselves and adding up their prefixes; a probe of the one stream
proves the arithmetic against the real router.

The profile is decided as the application decides it: whether ``modules``
imports. The full profile adds five module routes.
"""

from __future__ import annotations

import importlib.util
import re

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from backend.app.main import app

pytestmark = [pytest.mark.smoke]

# --- classes -----------------------------------------------------------------

READ = "READ"
CHANGE_UPDATE = "CHANGE-UPDATE"
CLONE = "CLONE"
CHANGE_DELETE = "CHANGE-DELETE"
ROLE_ONLY = "ROLE-ONLY"
SUPERUSER = "SUPERUSER"
AUTHENTICATED = "AUTHENTICATED"
NO_ROLE_CHECK = "no role check, request body only"
ANONYMOUS = "anonymous, T65"

#: One line per class: what admits a caller.
CLASS_REASONS = {
    READ: "a superuser, or a role holding READ on experiments; any experiment",
    CHANGE_UPDATE: "a superuser, or a role holding READ and UPDATE on experiments; "
    "any experiment, whoever created it",
    CLONE: "READ on the source experiment, and CREATE on experiments",
    CHANGE_DELETE: "a superuser, or a role holding READ and DELETE on experiments; "
    "DRAFT only, whoever created it",
    ROLE_ONLY: "a role check only (LIST, CREATE, UPDATE, READ or a named role); "
    "who created the experiment is not considered",
    SUPERUSER: "superusers only",
    AUTHENTICATED: "authentication only: /experiments/analysis/sample-size",
    NO_ROLE_CHECK: "any signed-in user: the route reads only its request body",
    ANONYMOUS: "no token required; tracked in T65",
}

E = "/api/v1/experiments"
R = "/api/v1/results/{experiment_id}"

CORE_ROUTES = {
    # experiments.py
    ("GET", f"{E}/"): ROLE_ONLY,
    ("POST", f"{E}/"): ROLE_ONLY,
    ("GET", f"{E}/analysis/sample-size"): AUTHENTICATED,
    ("POST", f"{E}/schedules/process"): SUPERUSER,
    ("GET", f"{E}/{{experiment_id}}"): READ,
    ("GET", f"{E}/{{experiment_id}}/results"): READ,
    ("GET", f"{E}/{{experiment_id}}/daily-results"): READ,
    ("GET", f"{E}/{{experiment_id}}/segmented-results/{{segment_by}}"): READ,
    ("PUT", f"{E}/{{experiment_id}}"): CHANGE_UPDATE,
    ("POST", f"{E}/{{experiment_id}}/start"): CHANGE_UPDATE,
    ("POST", f"{E}/{{experiment_id}}/pause"): CHANGE_UPDATE,
    ("POST", f"{E}/{{experiment_id}}/complete"): CHANGE_UPDATE,
    ("POST", f"{E}/{{experiment_id}}/archive"): CHANGE_UPDATE,
    ("POST", f"{E}/{{experiment_id}}/metadata"): CHANGE_UPDATE,
    ("POST", f"{E}/{{experiment_id}}/clone"): CLONE,
    ("PUT", f"{E}/{{experiment_id}}/schedule"): CHANGE_UPDATE,
    ("DELETE", f"{E}/{{experiment_id}}"): CHANGE_DELETE,
    ("GET", f"{E}/{{experiment_id}}/split-url/preview"): ROLE_ONLY,
    # results.py and post_stratification.py
    ("GET", R): READ,
    ("GET", f"{R}/daily"): READ,
    ("GET", f"{R}/sample-size"): READ,
    ("GET", f"{R}/bayesian"): READ,
    ("GET", f"{R}/sequential"): READ,
    ("GET", f"{R}/cuped"): READ,
    ("GET", f"{R}/balanced-groups"): READ,
    ("POST", f"{R}/post-stratification"): READ,
    ("POST", f"{R}/fdr-correction"): READ,
    ("POST", f"{R}/invalidate-cache"): SUPERUSER,
    # websocket_results.py
    ("WS", "/api/v1/ws/experiments/{experiment_id}/results"): READ,
    ("GET", "/api/v1/ws/experiments/{experiment_id}/results/subscribers"): ANONYMOUS,
    ("GET", "/api/v1/ws/active-experiments"): ANONYMOUS,
    # export, AI interpretation, bandits
    ("GET", "/api/v1/export/experiments"): ROLE_ONLY,
    ("GET", "/api/v1/export/reports/experiments/{experiment_id}"): READ,
    ("POST", "/api/v1/ai/interpret/{experiment_id}"): NO_ROLE_CHECK,
    ("GET", "/api/v1/bandit/{experiment_id}"): READ,
    ("POST", "/api/v1/bandit/{experiment_id}/update"): ROLE_ONLY,
    ("PUT", "/api/v1/bandit/{experiment_id}/weights"): ROLE_ONLY,
    # mutual-exclusion groups
    ("POST", "/api/v1/mutual-exclusion-groups/{group_id}/experiments"): ROLE_ONLY,
    (
        "DELETE",
        "/api/v1/mutual-exclusion-groups/{group_id}/experiments/{experiment_id}",
    ): ROLE_ONLY,
    # LLM experiments: a separate resource, checked by role
    ("GET", "/api/v1/llm-experiments/{experiment_id}"): ROLE_ONLY,
    ("PUT", "/api/v1/llm-experiments/{experiment_id}"): ROLE_ONLY,
    ("GET", "/api/v1/llm-experiments/{experiment_id}/results"): ROLE_ONLY,
    ("POST", "/api/v1/llm-experiments/{experiment_id}/start"): ROLE_ONLY,
    ("POST", "/api/v1/llm-experiments/{experiment_id}/pause"): ROLE_ONLY,
    ("POST", "/api/v1/llm-experiments/{experiment_id}/complete"): ROLE_ONLY,
    ("POST", "/api/v1/llm-experiments/{experiment_id}/evaluate"): ROLE_ONLY,
    ("POST", "/api/v1/llm-experiments/{experiment_id}/judge"): ROLE_ONLY,
    ("POST", "/api/v1/llm-experiments/{experiment_id}/variants"): ROLE_ONLY,
    (
        "PUT",
        "/api/v1/llm-experiments/{experiment_id}/variants/{variant_id}",
    ): ROLE_ONLY,
}

#: Added by the full profile.
MODULE_ROUTES = {
    ("GET", "/api/v1/counters/{experiment_id}"): READ,
    ("POST", "/api/v1/counters/{experiment_id}/increment"): ROLE_ONLY,
    ("POST", "/api/v1/counters/{experiment_id}/reset"): ROLE_ONLY,
    ("GET", "/api/v1/warehouse/analysis/experiments/{experiment_id}/runs"): ROLE_ONLY,
    ("POST", "/api/v1/warehouse/analysis/experiments/{experiment_id}/runs"): ROLE_ONLY,
}

#: Not matched by the selection rule; listed so it stays pinned.
EXPLICIT = {("GET", "/api/v1/ws/active-experiments")}

#: Every route under ``/api/v1/export/`` and ``/api/v1/interactions/``, most of
#: which the selection rule above does not see: the list exports ask the LIST
#: permission of their list pages, the report the experiment's READ, and the
#: interaction routes name the roles they admit.
EXPORT_AND_INTERACTION_ROUTES = {
    ("GET", "/api/v1/export/experiments"): ROLE_ONLY,
    ("GET", "/api/v1/export/variants"): ROLE_ONLY,
    ("GET", "/api/v1/export/feature-flags"): ROLE_ONLY,
    ("GET", "/api/v1/export/reports/overview"): ROLE_ONLY,
    ("GET", "/api/v1/export/reports/experiments/{experiment_id}"): READ,
    ("GET", "/api/v1/interactions/scan"): ROLE_ONLY,
    ("GET", "/api/v1/interactions/{exp_a_id}/{exp_b_id}"): ROLE_ONLY,
}
EXPORT_AND_INTERACTION_PREFIXES = ("/api/v1/export/", "/api/v1/interactions/")

CORE_COUNT = 49  # 47 HTTP + 1 WebSocket selected, and the explicit entry
FULL_COUNT = 54


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _selected(path: str) -> bool:
    return (
        path == E
        or path.startswith((f"{E}/", "/api/v1/results/", "/api/v1/ws/experiments/"))
        or "{experiment_id}" in path
        or path == "/api/v1/mutual-exclusion-groups/{group_id}/experiments"
        or path == "/api/v1/export/experiments"
    )


def _http_routes(application):
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
                yield method, ctx.path


def _websocket_paths(router, prefix: str = ""):
    """Every WebSocket path under ``router``, prefixes added up by hand."""
    routes = list(router.routes) + list(getattr(router, "_low_priority_routes", []))
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            child_prefix = prefix + (route.include_context.prefix or "")
            yield from _websocket_paths(included, child_prefix)
        elif isinstance(route, APIWebSocketRoute):
            yield prefix + route.path


def _inventory(application) -> set[tuple[str, str]]:
    http = set(_http_routes(application))
    found = {(m, p) for m, p in http if _selected(p)}
    found |= {("WS", p) for p in _websocket_paths(application.router) if _selected(p)}
    found |= EXPLICIT & http
    return found


def _expected() -> dict[tuple[str, str], str]:
    expected = dict(CORE_ROUTES)
    if _full_profile():
        expected.update(MODULE_ROUTES)
    return expected


def test_the_walks_see_the_routes():
    """Guard the guard: an empty or short walk would make the inventory vacuous."""
    assert len(set(_http_routes(app))) > 100
    websockets = set(_websocket_paths(app.router))
    assert "/api/v1/ws/experiments/{experiment_id}/results" in websockets
    assert EXPLICIT <= set(_http_routes(app))


def test_the_websocket_walk_names_a_path_the_application_serves():
    """The prefix arithmetic, proved against the real router: the walked path
    answers, and without a token it closes with 4401 (the test session runs
    with authentication on; see backend/tests/conftest.py)."""
    (path,) = [
        p
        for p in _websocket_paths(app.router)
        if p.startswith("/api/v1/ws/experiments/")
    ]
    url = path.replace("{experiment_id}", "00000000-0000-0000-0000-000000000001")
    with TestClient(app).websocket_connect(url) as ws:
        message = ws.receive()
    assert message["type"] == "websocket.close"
    assert message["code"] == 4401


def test_the_inventory_is_exactly_this():
    found = _inventory(app)
    expected = _expected()
    unclassified = sorted(found - expected.keys())
    gone = sorted(expected.keys() - found)
    assert not unclassified, f"unclassified: {unclassified}"
    assert not gone, f"classified routes the application no longer serves: {gone}"
    assert {k: expected[k] for k in found} == expected
    assert len(found) == (FULL_COUNT if _full_profile() else CORE_COUNT)


def test_export_and_interaction_routes_are_exactly_these():
    found = {
        (m, p)
        for m, p in _http_routes(app)
        if p.startswith(EXPORT_AND_INTERACTION_PREFIXES)
    }
    unclassified = sorted(found - EXPORT_AND_INTERACTION_ROUTES.keys())
    gone = sorted(EXPORT_AND_INTERACTION_ROUTES.keys() - found)
    assert not unclassified, f"unclassified: {unclassified}"
    assert not gone, f"classified routes the application no longer serves: {gone}"
    # A route in both tables carries the same label in each.
    both = EXPORT_AND_INTERACTION_ROUTES.keys() & CORE_ROUTES.keys()
    assert both == {
        ("GET", "/api/v1/export/experiments"),
        ("GET", "/api/v1/export/reports/experiments/{experiment_id}"),
    }
    assert {k: CORE_ROUTES[k] for k in both} == {
        k: EXPORT_AND_INTERACTION_ROUTES[k] for k in both
    }
    assert set(EXPORT_AND_INTERACTION_ROUTES.values()) <= set(CLASS_REASONS)


def test_every_class_has_a_reason():
    labels = set(CORE_ROUTES.values()) | set(MODULE_ROUTES.values())
    assert labels == set(CLASS_REASONS)
    for label, reason in CLASS_REASONS.items():
        assert reason and "\n" not in reason, label


def test_the_counts_match_the_tables():
    assert len(CORE_ROUTES) == CORE_COUNT
    assert len(CORE_ROUTES) + len(MODULE_ROUTES) == FULL_COUNT
    assert not CORE_ROUTES.keys() & MODULE_ROUTES.keys()


def test_the_selection_rule_is_what_the_docstring_says():
    """A rule that selected nothing would make every table above trivially met."""
    assert _selected(f"{E}/{{experiment_id}}/anything")
    assert _selected("/api/v1/results/{experiment_id}/anything")
    assert _selected("/api/v1/anything/{experiment_id}")
    assert not _selected("/api/v1/feature-flags/{flag_id}")
    assert not re.search(r"\{experiment_id\}", "/api/v1/ws/active-experiments")
