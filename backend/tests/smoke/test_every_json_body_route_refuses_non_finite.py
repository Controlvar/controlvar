"""Every route with a JSON request body refuses a number that is not finite (#1630).

Each operation of ``backend.app.main.app`` whose route declares a body is
sent ``{"__probe": NaN}`` as ``application/json``, with no credentials and a
UUID in every path parameter, and must answer 422 ``finite_number`` at
``["body", "__probe"]``. The check runs before every other dependency, so no
database, key or sign-in is needed: an operation that answers 401, 403, 404
or anything else has a route the check does not reach.

Routes are read through ``effective_route_contexts`` (FastAPI keeps
``include_router`` lazy: ``app.routes`` lists 3 routes and none has a body),
and the counts are pinned per profile, so a walk that sees fewer routes fails
instead of passing on fewer. The profile is decided as the application decides
it: whether ``modules`` imports.

* JSON-body operations: 77 in the core profile, 107 in the full one (#1545
  gave ``POST /api/v1/experiments/{experiment_id}/start`` an optional one, and
  added ``POST /api/v1/tracking/visual``, ``POST /api/v1/tracking/visual/preview``
  and ``POST /api/v1/experiments/{experiment_id}/visual/preview-links``). A new
  route with a JSON body changes the count here: it is refused like the others
  (this test sends it the probe), and the number is bumped.
* Form-body operations, named: FastAPI reads them with ``request.form()``, so
  the check leaves them alone (``POST /api/v1/auth/token``, and in the full
  profile the SAML ACS).
* Routes that declare no body and read it themselves (the full profile's SSO
  exchange and three webhooks) are outside the check; they are named so that
  a new one is a decision.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from typing import Iterator, Set, Tuple

import pytest
from fastapi import params
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.app.core.finite_json import FINITE_NUMBER_MESSAGE, FINITE_NUMBER_TYPE
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

CORE_JSON_BODY_COUNT = 77
FULL_JSON_BODY_COUNT = 107

FORM_OPERATIONS = {("POST", "/api/v1/auth/token")}
MODULE_FORM_OPERATIONS = {("POST", "/api/v1/auth/sso/saml/{config_id}/acs")}

MODULE_SELF_PARSING = {
    ("POST", "/api/v1/auth/sso/exchange"),
    ("POST", "/api/v1/integrations/webhooks/github"),
    ("POST", "/api/v1/integrations/webhooks/jira"),
    ("POST", "/api/v1/integrations/webhooks/salesforce"),
}

UUID = "00000000-0000-4000-8000-000000000001"
PARAM = re.compile(r"\{[^}]+\}")
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
READS_ITS_BODY = re.compile(r"request\.(json|body|form|stream)\(")

Operation = Tuple[str, str]


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


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


def _with_body(form: bool) -> Set[Operation]:
    found = set()
    for method, path, ctx in _contexts():
        body_field = getattr(ctx, "body_field", None)
        if body_field is None:
            continue
        if isinstance(body_field.field_info, params.Form) is form:
            found.add((method, path))
    return found


def _self_parsing() -> Set[Operation]:
    found = set()
    for method, path, ctx in _contexts():
        if method not in BODY_METHODS or getattr(ctx, "body_field", None) is not None:
            continue
        endpoint = getattr(ctx, "endpoint", None)
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        if READS_ITS_BODY.search(source):
            found.add((method, path))
    return found


JSON_BODY = sorted(_with_body(form=False))


def test_the_json_body_operations_are_counted_per_profile():
    expected = FULL_JSON_BODY_COUNT if _full_profile() else CORE_JSON_BODY_COUNT
    assert len(JSON_BODY) == expected, (
        f"{len(JSON_BODY)} operations with a JSON body, {expected} expected: a new "
        "one is refused like the others (the test below sends it the probe); "
        "bump the count"
    )


def test_the_form_operations_are_these():
    expected = FORM_OPERATIONS | (MODULE_FORM_OPERATIONS if _full_profile() else set())
    assert _with_body(form=True) == expected


def test_the_routes_that_read_their_own_body_are_these():
    expected = MODULE_SELF_PARSING if _full_profile() else set()
    assert _self_parsing() == expected


def _strict(value) -> bool:
    return bool(getattr(value, "value", value))


def test_every_json_body_route_parses_by_its_own_content_type_setting():
    """With no content type FastAPI parses a body only on a route whose
    ``strict_content_type`` is off. The check reads the matched route's own
    setting (``scope["route"]``), so the setting each route is served with,
    which an including router can change, must be the route's own: today
    every one is strict. A router that turns it off fails here."""
    differ = []
    for method, path, ctx in _contexts():
        if getattr(ctx, "body_field", None) is None:
            continue
        own = getattr(ctx, "original_route", ctx)
        served = _strict(ctx.strict_content_type)
        if not served or served != _strict(own.strict_content_type):
            differ.append(f"{method} {path}")
    assert differ == []


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.regression
@pytest.mark.parametrize(
    "method, path", JSON_BODY, ids=[f"{m} {p}" for m, p in JSON_BODY]
)
def test_every_json_body_operation_refuses_a_non_finite_number(client, method, path):
    response = client.request(
        method,
        PARAM.sub(UUID, path),
        content=b'{"__probe": NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, f"{method} {path}: {response.text}"
    assert response.json()["detail"] == [
        {
            "type": FINITE_NUMBER_TYPE,
            "loc": ["body", "__probe"],
            "msg": FINITE_NUMBER_MESSAGE,
        }
    ], f"{method} {path}"
