"""A synchronous handler answers the same under ``OffLoopRoute`` as under
FastAPI's own ``APIRoute``: status, headers and body, byte for byte.

``OffLoopRoute`` validates a synchronous handler's result in the worker that
ran the handler, and FastAPI then validates the validated value again and
serializes it. Each handler below is mounted twice on a scratch application,
once on a router of each class, and both answers are compared, for every way a
route here declares its response: a model, a list of models, a union, a dict
(``Any``), no response model, ``response_model_exclude_unset``,
``response_model_exclude_none``, a status code, a response class, a
``Response`` the handler builds itself, headers set on the injected
``Response``, an object read through its attributes (as an ORM row is), and a
result that fails validation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import pytest
from fastapi import APIRouter, FastAPI, Response
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from backend.app.api.off_loop_route import OFF_LOOP_MARKER, OffLoopRoute

pytestmark = [pytest.mark.smoke]


class Item(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    note: Optional[str] = None
    tags: List[str] = []


class Other(BaseModel):
    username: str


class Page(BaseModel):
    items: List[Item]
    total: int


class Row:
    """An object read through its attributes, as an ORM row is."""

    def __init__(self, id: int, name: str, note: Optional[str] = None):
        self.id = id
        self.name = name
        self.note = note
        self.tags = ["a", "b"]


def _register(router: APIRouter) -> None:
    @router.get("/model", response_model=Item)
    def model():
        return Row(1, "one")

    @router.get("/list", response_model=List[Item])
    def items():
        return [Row(1, "one"), Row(2, "two", "n")]

    @router.get("/page", response_model=Page)
    def page():
        return {"items": [Row(3, "three")], "total": 1}

    @router.get("/union", response_model=Union[Item, Other])
    def union():
        return Other(username="u")

    @router.get("/any")
    def anything() -> Any:
        return {"b": 2, "a": [1, 2.5, None], "c": {"d": True}}

    @router.get("/none")
    def no_model():
        return {"when": "2026-11-05T00:00:00", "rows": [{"x": 1}]}

    @router.get("/unset", response_model=Item, response_model_exclude_unset=True)
    def unset():
        return Item(id=4, name="four")

    @router.get("/unset-dict", response_model=Item, response_model_exclude_unset=True)
    def unset_dict():
        return {"id": 5, "name": "five", "note": None}

    @router.get("/no-none", response_model=Item, response_model_exclude_none=True)
    def no_none():
        return Row(6, "six")

    @router.post("/created", response_model=Item, status_code=201)
    def created():
        return Row(7, "seven")

    @router.delete("/gone", status_code=204, response_class=Response)
    def gone():
        return None

    @router.get("/text", response_class=PlainTextResponse)
    def text():
        return "plain"

    @router.get("/built")
    def built() -> Response:
        return Response(content=b"a,b\r\n1,2\r\n", media_type="text/csv")

    @router.get("/headers", response_model=Dict[str, int])
    def headers(response: Response):
        response.headers["Cache-Control"] = "private, no-store"
        response.status_code = 202
        return {"n": 1}

    @router.get("/invalid", response_model=Item)
    def invalid():
        return {"id": "not a number"}


def _app(route_class: type) -> FastAPI:
    application = FastAPI()
    router = APIRouter(route_class=route_class)
    _register(router)
    application.include_router(router)
    return application


PATHS = [
    ("GET", "/model"),
    ("GET", "/list"),
    ("GET", "/page"),
    ("GET", "/union"),
    ("GET", "/any"),
    ("GET", "/none"),
    ("GET", "/unset"),
    ("GET", "/unset-dict"),
    ("GET", "/no-none"),
    ("POST", "/created"),
    ("DELETE", "/gone"),
    ("GET", "/text"),
    ("GET", "/built"),
    ("GET", "/headers"),
    ("GET", "/invalid"),
]


def _answer(client: TestClient, method: str, path: str):
    response = client.request(method, path)
    return response.status_code, sorted(response.headers.items()), response.content


def test_every_handler_is_replaced():
    application = _app(OffLoopRoute)
    assert all(
        getattr(route.endpoint, OFF_LOOP_MARKER, False)
        for route in application.router.routes
        if getattr(route, "path", "") in {p for _, p in PATHS}
    )


@pytest.mark.parametrize("method, path", PATHS)
def test_the_answer_is_the_same_byte_for_byte(method, path):
    with (
        TestClient(_app(APIRoute), raise_server_exceptions=False) as plain,
        TestClient(_app(OffLoopRoute), raise_server_exceptions=False) as off_loop,
    ):
        assert _answer(off_loop, method, path) == _answer(plain, method, path)
