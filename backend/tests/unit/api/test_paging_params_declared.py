"""Every paging parameter has a minimum and a maximum (#1001).

``skip``, ``offset`` and ``page`` reached ``OFFSET`` with no maximum, and the
flag list's ``skip`` and ``limit`` had no bound at all, so a value the database
cannot take answered 500. Each ``skip`` and ``offset`` now takes 0 to
9223372036854775807 (2**63 - 1, the largest bigint), each ``page`` 1 to 2**63,
and the flag list's ``limit`` 0 to 2**63 - 1; every ``limit`` and
``page_size`` has a minimum and a maximum.

The parameters are a literal list, diffed against what the application
declares: every query parameter named ``skip``, ``offset``, ``page``,
``limit``, ``page_size`` or ``per_page`` of every route context, a
dependency's included. ``app.routes`` alone lists no route with a parameter
(FastAPI keeps ``include_router`` lazy), so the walk reads
``effective_route_contexts``, has a floor, and its counts are pinned per
profile. A new paging parameter fails the diff until it is listed here, and
then it is held to the bounds. The served document describes them:
``"format": "int64"`` and the minimum where the bound is the database's
(never a ``maximum`` above 2**53, which a release would rewrite), a minimum
and a maximum elsewhere. The rollout schedule list's ``status``, whose Python
name changed, is held to the document main served.

No database: these are declarations. That the database then answers is
``backend/tests/integration/api/test_offset_bounds_api.py``'s. The numbers are
written out rather than imported, so the test pins them.
"""

from __future__ import annotations

import importlib.util
from typing import Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.routing import APIRoute

from backend.app.main import app

pytestmark = [pytest.mark.unit]

#: The largest OFFSET PostgreSQL takes, written out.
LARGEST_BIGINT = 9223372036854775807

NAMES = ("skip", "offset", "page", "limit", "page_size", "per_page")

#: (param, method, path): the core profile's paging parameters, sorted. The
#: flag list is there twice: its slash-less twin is a route context too (not
#: in the document).
CORE_PARAMS: List[Tuple[str, str, str]] = [
    ("limit", "GET", "/api/v1/admin/users"),
    ("limit", "GET", "/api/v1/audit-logs/"),
    ("limit", "GET", "/api/v1/audit-logs/entity/{entity_type}/{entity_id}"),
    ("limit", "GET", "/api/v1/audit-logs/stream"),
    ("limit", "GET", "/api/v1/audit-logs/user/{user_id}"),
    ("limit", "GET", "/api/v1/compliance/audit-events"),
    ("limit", "GET", "/api/v1/experiments/"),
    ("limit", "GET", "/api/v1/feature-flags"),
    ("limit", "GET", "/api/v1/feature-flags/"),
    ("limit", "GET", "/api/v1/feature-flags/{flag_id}/history"),
    ("limit", "GET", "/api/v1/holdout/all"),
    ("limit", "GET", "/api/v1/metrics/aggregated"),
    ("limit", "GET", "/api/v1/metrics/errors"),
    ("limit", "GET", "/api/v1/mutual-exclusion-groups"),
    ("limit", "GET", "/api/v1/notifications/delivery-log"),
    ("limit", "GET", "/api/v1/rollout-schedules/"),
    ("limit", "GET", "/api/v1/scheduler/{name}/history"),
    ("limit", "GET", "/api/v1/segments"),
    ("limit", "GET", "/api/v1/users/"),
    ("offset", "GET", "/api/v1/feature-flags/{flag_id}/history"),
    ("offset", "GET", "/api/v1/segments"),
    ("page", "GET", "/api/v1/audit-logs/"),
    ("page", "GET", "/api/v1/compliance/audit-events"),
    ("page", "GET", "/api/v1/llm-experiments/"),
    ("page", "GET", "/api/v1/notifications/delivery-log"),
    ("page_size", "GET", "/api/v1/llm-experiments/"),
    ("skip", "GET", "/api/v1/admin/users"),
    ("skip", "GET", "/api/v1/experiments/"),
    ("skip", "GET", "/api/v1/feature-flags"),
    ("skip", "GET", "/api/v1/feature-flags/"),
    ("skip", "GET", "/api/v1/holdout/all"),
    ("skip", "GET", "/api/v1/metrics/aggregated"),
    ("skip", "GET", "/api/v1/metrics/errors"),
    ("skip", "GET", "/api/v1/mutual-exclusion-groups"),
    ("skip", "GET", "/api/v1/rollout-schedules/"),
    ("skip", "GET", "/api/v1/users/"),
]

#: The full profile's own (the HIPAA module), sorted.
MODULE_PARAMS: List[Tuple[str, str, str]] = [
    ("page", "GET", "/api/v1/hipaa/audit-logs"),
    ("page_size", "GET", "/api/v1/hipaa/audit-logs"),
]

CORE_COUNT = 36
FULL_COUNT = 38

#: The routes not in the document: the flag list's slash-less twin, whose
#: skip and limit are the list's own.
HIDDEN = {
    ("skip", "GET", "/api/v1/feature-flags"),
    ("limit", "GET", "/api/v1/feature-flags"),
}

#: The flag list's two routes: skip and limit both 0 to the largest bigint,
#: one rule.
FLAG_LIST = ("/api/v1/feature-flags/", "/api/v1/feature-flags")


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _expected() -> List[Tuple[str, str, str]]:
    return sorted(CORE_PARAMS + (MODULE_PARAMS if _full_profile() else []))


def _bounds(param: str, path: str) -> Optional[Tuple[int, int]]:
    """The exact (minimum, maximum) a parameter must have, or None where any
    minimum and maximum will do (a route's own limit or page size)."""
    if param in ("skip", "offset"):
        return (0, LARGEST_BIGINT)
    if param == "page":
        return (1, LARGEST_BIGINT + 1)
    if path in FLAG_LIST:
        return (0, LARGEST_BIGINT)
    return None


def _contexts() -> Iterator[Tuple[str, str, object]]:
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _query_params(dependant) -> Iterator[object]:
    yield from dependant.query_params
    for sub in dependant.dependencies:
        yield from _query_params(sub)


def _declared() -> Dict[Tuple[str, str, str], Tuple[Optional[int], Optional[int]]]:
    found = {}
    for method, path, ctx in _contexts():
        dependant = getattr(ctx, "dependant", None)
        if dependant is None:
            continue
        for param in _query_params(dependant):
            if param.alias not in NAMES:
                continue
            ge = le = None
            for meta in param.field_info.metadata:
                ge = getattr(meta, "ge", ge)
                le = getattr(meta, "le", le)
            found[(param.alias, method, path)] = (ge, le)
    return found


def test_the_lists_are_sorted_and_counted():
    assert CORE_PARAMS == sorted(CORE_PARAMS)
    assert MODULE_PARAMS == sorted(MODULE_PARAMS)
    assert len(CORE_PARAMS) == CORE_COUNT
    assert len(CORE_PARAMS) + len(MODULE_PARAMS) == FULL_COUNT
    counts = dict.fromkeys(NAMES, 0)
    for param, _, _ in CORE_PARAMS + MODULE_PARAMS:
        counts[param] += 1
    # The counts the plan measured: the twin adds one skip and one limit.
    assert counts == {
        "skip": 10,
        "offset": 2,
        "page": 5,
        "limit": 19,
        "page_size": 2,
        "per_page": 0,
    }


def test_every_paging_param_is_listed():
    """The walk finds exactly the listed parameters, per profile.

    A paging parameter added without being listed here (and so without its
    bounds being checked) is named by the diff."""
    declared = set(_declared())
    assert len(declared) >= CORE_COUNT, (
        f"the walk found {len(declared)} paging parameters: it reads fewer "
        "routes than the application serves"
    )
    expected = set(_expected())
    assert declared == expected, (
        f"not listed: {sorted(declared - expected)}; "
        f"listed but not declared: {sorted(expected - declared)}"
    )
    assert len(declared) == (FULL_COUNT if _full_profile() else CORE_COUNT)


@pytest.mark.regression
def test_1001_every_paging_param_has_a_minimum_and_a_maximum():
    failures = []
    for (param, method, path), (ge, le) in sorted(_declared().items()):
        where = f"{method} {path} {param}"
        if ge is None:
            failures.append(f"{where} has no minimum")
        if le is None:
            failures.append(f"{where} has no maximum")
        exact = _bounds(param, path)
        if exact is not None and (ge, le) != exact:
            failures.append(f"{where}: bounds {(ge, le)}, expected {exact}")
    assert failures == [], "\n".join(failures)


@pytest.mark.regression
def test_1001_the_served_document_gives_every_paging_param_its_bounds():
    """What a client reads. A parameter bounded at the database's limit is
    ``"format": "int64"`` with its minimum and no ``maximum``: a release
    rewrites the committed snapshots through JavaScript, which prints an
    integer above 2**53 as a different number
    (``backend/tests/smoke/test_openapi_release_rewrite.py``). Every other
    one (a route's own ``limit`` or ``page_size``) has a ``minimum`` and a
    ``maximum``."""
    failures = []
    seen = 0
    for path, item in app.openapi()["paths"].items():
        for method, operation in item.items():
            for param in operation.get("parameters", []):
                name = param["name"]
                if param["in"] != "query" or name not in NAMES:
                    continue
                seen += 1
                schema = param["schema"]
                where = f"{method.upper()} {path} {name}"
                exact = _bounds(name, path)
                if exact is not None:
                    found = (
                        schema.get("format"),
                        schema.get("minimum"),
                        "maximum" in schema,
                    )
                    if found != ("int64", exact[0], False):
                        failures.append(
                            f"{where}: (format, minimum, has maximum) {found}, "
                            f"expected ('int64', {exact[0]}, False)"
                        )
                elif None in (schema.get("minimum"), schema.get("maximum")):
                    failures.append(
                        f"{where}: minimum and maximum "
                        f"{(schema.get('minimum'), schema.get('maximum'))}"
                    )
    assert seen == len(set(_expected()) - HIDDEN)
    assert failures == [], "\n".join(failures)


@pytest.mark.regression
def test_1001_the_flag_list_document_gains_only_the_bounds():
    """The flag list's ``skip`` and ``limit`` were plain integers in the
    document; they gain ``"format": "int64"`` and ``minimum: 0`` and nothing
    else (no description, no smaller cap)."""
    operation = app.openapi()["paths"]["/api/v1/feature-flags/"]["get"]
    params = {p["name"]: p for p in operation["parameters"]}
    assert params["skip"] == {
        "in": "query",
        "name": "skip",
        "required": False,
        "schema": {
            "default": 0,
            "format": "int64",
            "minimum": 0,
            "title": "Skip",
            "type": "integer",
        },
    }
    assert params["limit"] == {
        "in": "query",
        "name": "limit",
        "required": False,
        "schema": {
            "default": 100,
            "format": "int64",
            "minimum": 0,
            "title": "Limit",
            "type": "integer",
        },
    }


def test_the_rollout_schedule_list_keeps_its_status_parameter():
    """A no-behaviour-change pin. The Python parameter is ``status_filter``
    with ``alias="status"`` (a parameter called ``status`` hid the
    ``fastapi.status`` module in the route). The document's parameters are
    the ones main served: same names, same order, ``status`` byte for byte."""
    operation = app.openapi()["paths"]["/api/v1/rollout-schedules/"]["get"]
    names = [p["name"] for p in operation["parameters"]]
    assert names == ["feature_flag_id", "status", "skip", "limit"]
    assert operation["parameters"][1] == {
        "description": "Filter by status",
        "in": "query",
        "name": "status",
        "required": False,
        "schema": {
            "anyOf": [
                {"$ref": "#/components/schemas/RolloutScheduleStatus"},
                {"type": "null"},
            ],
            "description": "Filter by status",
            "title": "Status",
        },
    }
