"""The six deprecated ``/api/v1/wizard`` operations are gone (#537, #530).

They were deprecated in 0.15.0 (#530) in favour of ``POST /api/v1/experiments/``,
which the dashboard's guided setup sends, and are now removed. This pins the
removal four ways, because each one alone can pass while a route still answers:

* the mounted route set, read through ``effective_route_contexts`` (FastAPI
  keeps ``include_router`` as a lazy entry, so plain ``app.routes`` would see
  nothing at all), with a positive control that the reader sees a route that is
  still mounted;
* the OpenAPI document, since a route mounted with ``include_in_schema=False``
  is absent there and still answers: no path, no "Experiment Wizard" tag, no
  operation carrying it and no ``Wizard*`` component;
* an anonymous request to each of the six method/path pairs, which must get
  exactly 404. A mounted wizard route answered 401 to an anonymous caller, so a
  401 or 403 would mean it is still mounted; the control is an anonymous
  ``GET /api/v1/experiments/``, which still answers 401;
* the three modules (router, service, schemas) no longer exist.
"""

from __future__ import annotations

import importlib.util

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

REMOVED = (
    ("POST", "/api/v1/wizard/drafts"),
    ("GET", "/api/v1/wizard/drafts"),
    ("GET", "/api/v1/wizard/drafts/{draft_id}"),
    ("PUT", "/api/v1/wizard/drafts/{draft_id}/step"),
    ("POST", "/api/v1/wizard/drafts/{draft_id}/submit"),
    ("POST", "/api/v1/wizard/validate"),
)
PREFIX = "/api/v1/wizard"
TAG = "Experiment Wizard"
MODULES = (
    "backend.app.api.v1.endpoints.experiment_wizard",
    "backend.app.services.experiment_wizard_service",
    "backend.app.schemas.experiment_wizard",
)

# A route that is still mounted, core, and signed-in only.
STILL_MOUNTED = "/api/v1/experiments/"


def _route_paths(application) -> set:
    """Every mounted HTTP route path, lazy ``include_router`` entries included."""
    found = set()
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            path = getattr(route, "path", None)
            if path is not None:
                found.add(path)
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            found.add(ctx.path)
    return found


def _under_prefix(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def test_the_reader_sees_a_route_that_is_still_mounted():
    """Positive control: without it, a reader that saw nothing would pass."""
    assert STILL_MOUNTED in _route_paths(app)


def test_no_route_is_mounted_under_the_wizard_prefix():
    assert sorted(p for p in _route_paths(app) if _under_prefix(p)) == []


def test_the_document_has_no_wizard_path_tag_or_component():
    document = app.openapi()
    assert STILL_MOUNTED in document["paths"]
    assert sorted(p for p in document["paths"] if _under_prefix(p)) == []
    assert TAG not in [t["name"] for t in document.get("tags", [])]
    tagged = sorted(
        f"{method.upper()} {path}"
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if isinstance(operation, dict) and TAG in operation.get("tags", [])
    )
    assert tagged == []
    schemas = document.get("components", {}).get("schemas", {})
    assert sorted(n for n in schemas if n.startswith("Wizard")) == []


@pytest.fixture(scope="module")
def anonymous_client():
    return TestClient(app, raise_server_exceptions=False)


def test_an_anonymous_call_to_a_route_that_is_still_mounted_answers_401(
    anonymous_client,
):
    """Positive control: an anonymous caller is refused, not routed to a 404."""
    assert anonymous_client.get(STILL_MOUNTED).status_code == 401


@pytest.mark.parametrize("method,path", REMOVED)
def test_a_removed_operation_answers_404(anonymous_client, method, path):
    url = path.replace("{draft_id}", "00000000-0000-0000-0000-000000000000")
    response = anonymous_client.request(method, url, json={})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("module", MODULES)
def test_the_wizard_modules_are_deleted(module):
    assert importlib.util.find_spec(module) is None
