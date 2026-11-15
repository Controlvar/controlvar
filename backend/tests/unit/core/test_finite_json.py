"""The finite-number check on JSON request bodies: ``backend/app/core/finite_json.py`` (#1630).

Pure functions and the dependency called directly, no database and no
application. What the application's routes answer is pinned in
``backend/tests/unit/api/test_non_finite_numbers_refused.py`` (the matrix) and
``backend/tests/smoke/test_every_json_body_route_refuses_non_finite.py`` (every
route, per profile).
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import Body, FastAPI, params
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocket

from backend.app.core.finite_json import (
    FINITE_NUMBER_MESSAGE,
    FINITE_NUMBER_TYPE,
    first_non_finite,
    has_non_finite,
    parses_as_json,
    refuse_non_finite_json,
)

pytestmark = [pytest.mark.unit]

NAN, INF = math.nan, math.inf

# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

#: (id, document as parsed, the path to its first non-finite number).
NON_FINITE = [
    ("top-level-nan", NAN, ()),
    ("top-level-inf", INF, ()),
    ("top-level-minus-inf", -INF, ()),
    ("object", {"value": NAN}, ("value",)),
    ("array", [1, 2, INF], (2,)),
    ("nested", {"a": {"b": [1, {"c": -INF}]}}, ("a", "b", 1, "c")),
    ("array-in-array", [[0.5], [1, [2, NAN]]], (1, 1, 1)),
    ("after-finite-siblings", {"x": 1e308, "y": -0.0, "z": INF}, ("z",)),
]

#: Documents with no non-finite number: what stays accepted.
FINITE = [
    ("largest-double", 1.7976931348623157e308),
    ("minus-zero", -0.0),
    ("huge-integer", 10**400),
    ("string-nan", "NaN"),
    ("string-infinity", {"value": "Infinity"}),
    ("null-and-bools", [None, True, False]),
    ("empty-containers", {"a": [], "b": {}}),
    ("nested-finite", {"a": [1, 2.5, {"b": [-1e300, "1e400"]}]}),
]


@pytest.mark.parametrize(
    "label, document, path", NON_FINITE, ids=[r[0] for r in NON_FINITE]
)
def test_the_first_non_finite_number_is_found_at_its_path(label, document, path):
    assert has_non_finite(document)
    assert first_non_finite(document) == path


@pytest.mark.parametrize("label, document", FINITE, ids=[r[0] for r in FINITE])
def test_a_document_of_finite_numbers_is_left_alone(label, document):
    assert not has_non_finite(document)
    assert first_non_finite(document) is None


def test_the_first_in_document_order_is_named():
    """Two offenders: the earlier in the document, in key and in array order."""
    assert first_non_finite({"b": INF, "a": NAN}) == ("b",)
    assert first_non_finite([[1, NAN], INF]) == (0, 1)
    assert first_non_finite({"z": [0, {"y": -INF}], "a": NAN}) == ("z", 1, "y")


def test_the_walk_does_not_recurse():
    """100,000 levels, far past the interpreter's recursion limit."""
    document: Any = NAN
    for _ in range(100_000):
        document = [document]
    path = first_non_finite(document)
    assert path == (0,) * 100_000


def test_detection_and_the_path_agree_on_random_documents():
    rng = random.Random(1630)
    leaves = [0, 1.5, -2.0, 1e308, "NaN", None, True, NAN, INF, -INF]

    def build(depth: int) -> Any:
        if depth == 0 or rng.random() < 0.3:
            return rng.choice(leaves)
        if rng.random() < 0.5:
            return [build(depth - 1) for _ in range(rng.randint(0, 4))]
        return {f"k{i}": build(depth - 1) for i in range(rng.randint(0, 4))}

    for _ in range(2_000):
        document = build(5)
        path = first_non_finite(document)
        assert has_non_finite(document) is (path is not None)
        if path is not None:
            node = document
            for part in path:
                node = node[part]
            assert isinstance(node, float) and not math.isfinite(node)


def test_a_number_too_large_for_a_double_is_infinite_once_parsed():
    """What Python's parser makes of 1e400, the reason the walk sees it."""
    assert first_non_finite(json.loads('{"v": 1e400}')) == ("v",)
    assert first_non_finite(json.loads('{"v": -1e400}')) == ("v",)
    assert first_non_finite(json.loads("[NaN]")) == (0,)


# ---------------------------------------------------------------------------
# The content types FastAPI parses
# ---------------------------------------------------------------------------

CONTENT_TYPES = [
    "application/json",
    "application/json; charset=utf-8",
    "Application/JSON",
    "application/vnd.api+json",
    "application/vnd.x+json; charset=utf-8",
    "application/problem+JSON",
    "text/json",
    "text/plain",
    "application/x-www-form-urlencoded",
    "application/jsonx",
    "application/json-patch",
    "multipart/form-data; boundary=x",
]


def _fastapi_parsed(content_type: str | None, strict: bool) -> bool:
    """Whether FastAPI itself parses a body sent with *content_type*: a route
    that takes any object answers 200 when it did, 422 when the bytes reached
    the model unparsed."""
    scratch = FastAPI()

    async def echo(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return body

    scratch.router.add_api_route(
        "/echo", echo, methods=["POST"], strict_content_type=strict
    )
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = TestClient(scratch).post("/echo", content=b'{"a": 1}', headers=headers)
    assert response.status_code in (200, 422), response.text
    return response.status_code == 200


@pytest.mark.parametrize("content_type", CONTENT_TYPES)
def test_the_content_type_test_is_fastapis(content_type):
    """The same decision as FastAPI's, measured on FastAPI: a FastAPI upgrade
    that parses another content type fails here."""
    assert parses_as_json(content_type, True) is _fastapi_parsed(content_type, True)


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "not-strict"])
def test_no_content_type_follows_the_routes_strictness(strict):
    assert parses_as_json(None, strict) is _fastapi_parsed(None, strict)
    assert parses_as_json("", strict) is (not strict)


# ---------------------------------------------------------------------------
# The dependency, called directly
# ---------------------------------------------------------------------------


def _route(field_info: Any = None, strict: Any = True) -> Any:
    body_field = None if field_info is None else SimpleNamespace(field_info=field_info)
    return SimpleNamespace(body_field=body_field, strict_content_type=strict)


def _request(body: bytes, route: Any, content_type: str | None = "application/json"):
    headers: List[tuple] = []
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": headers,
        "route": route,
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _run(conn) -> None:
    asyncio.run(refuse_non_finite_json(conn))


def test_a_non_finite_number_raises_the_validation_error_at_its_place():
    request = _request(
        b'{"events": [{"value": 1}, {"value": NaN}]}', _route(params.Body())
    )
    with pytest.raises(RequestValidationError) as raised:
        _run(request)
    assert raised.value.errors() == [
        {
            "type": FINITE_NUMBER_TYPE,
            "loc": ("body", "events", 1, "value"),
            "msg": FINITE_NUMBER_MESSAGE,
        }
    ]


def test_a_finite_body_passes():
    _run(_request(b'{"value": 1e308, "s": "NaN"}', _route(params.Body())))


def test_a_route_with_no_body_is_not_read():
    _run(_request(b'{"value": NaN}', _route(None)))


def test_a_form_body_is_not_parsed_as_json():
    """FastAPI reads a form body with ``request.form()``; parsing it here
    would answer for a body FastAPI never parses."""
    _run(_request(b'{"value": NaN}', _route(params.Form())))
    _run(_request(b"{bad", _route(params.Form())))


@pytest.mark.parametrize(
    "body",
    [b"{bad", b"\xff\xfe", b"[" * 100_000],
    ids=["not-json", "not-utf8", "too-deep"],
)
def test_a_body_that_does_not_parse_is_left_to_fastapi(body):
    """Reached only if this check and FastAPI disagree about parsing: it must
    do nothing rather than answer 500."""
    _run(_request(body, _route(params.Body())))


def test_an_empty_body_is_not_parsed():
    _run(_request(b"", _route(params.Body())))


def test_a_content_type_fastapi_does_not_parse_is_not_read():
    _run(_request(b'{"value": NaN}', _route(params.Body()), content_type="text/plain"))
    _run(_request(b'{"value": NaN}', _route(params.Body()), content_type=None))


def test_no_content_type_is_read_on_a_route_that_is_not_strict():
    request = _request(b"[NaN]", _route(params.Body(), strict=False), content_type=None)
    with pytest.raises(RequestValidationError):
        _run(request)


def test_a_websocket_is_left_alone():
    scope = {
        "type": "websocket",
        "path": "/ws",
        "headers": [],
        "route": _route(params.Body()),
    }

    async def receive():  # pragma: no cover - never awaited
        return {"type": "websocket.disconnect"}

    async def send(message):  # pragma: no cover - never awaited
        return None

    _run(WebSocket(scope, receive, send))
