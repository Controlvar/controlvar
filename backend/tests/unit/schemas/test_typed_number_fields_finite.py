"""Every typed number field of a request refuses a value that is not finite (#1742).

A ``float`` field reads the *strings* ``"NaN"``, ``"Infinity"``,
``"-Infinity"`` and ``"1e400"`` as numbers that are not finite. The JSON check
(#1630, ``backend/app/core/finite_json.py``) does not see them, because in the
JSON they are strings. So each such field is ``FiniteFloat``
(``backend/app/schemas/finite_number.py``) and each such query parameter is
declared with ``allow_inf_nan=False``: all four answer 422 ``finite_number`` at
the field's own place.

The fields are not listed here. They are found from the application's own
route table: every operation's body (through ``effective_route_contexts``:
FastAPI keeps ``include_router`` lazy) and every query parameter of the
operation and of its dependencies, walked down the validation schema
(definitions, models, lists, sets, tuples, dict values, nullable, defaults,
validators, unions, tagged unions) to each ``float`` node. A new route or field
is swept like the others, and the counts are pinned per profile, so a walk that
sees fewer fails instead of passing on fewer. The profile is decided as the
application decides it: whether ``modules`` imports.

* A strict field (``StrictFloat``) refuses every string with ``float_type``,
  before and after: it is swept with that type.
* A tracking event's ``value`` is exempt, by exact equality, and each exempt
  field must still be found by the walk (a stale exemption fails) and still
  refuses the four strings its own way (``BoundedEventValue``).
* The positive control: an in-range number, and the same number as a string
  where the field takes strings, validates at every field. It is what catches
  ``Field(allow_inf_nan=False)`` put on a container instead of its items, which
  raises ``TypeError`` (a 500) on a valid list or dict, while the sweep stays
  green.
"""

from __future__ import annotations

import importlib.util
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Annotated,
    Any,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Union,
)

import pytest
from fastapi import params
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import (
    BaseModel,
    Field,
    GetPydanticSchema,
    StrictFloat,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_core import SchemaValidator, core_schema

from backend.app.api import deps
from backend.app.core.finite_json import FINITE_NUMBER_MESSAGE, FINITE_NUMBER_TYPE
from backend.app.main import app
from backend.app.schemas.tracking import VALUE_BOUND_MESSAGE
from backend.app.services.event_service import EventService
from backend.tests.unit.api.test_non_finite_numbers_refused import _Session

pytestmark = [pytest.mark.unit]

#: The strings a ``float`` field reads as a number that is not finite.
NON_FINITE = ("NaN", "Infinity", "-Infinity", "1e400")

# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

#: Schema nodes that hold no float (a float can only be found below a branch).
TERMINALS = {
    "any",
    "bool",
    "bytes",
    "date",
    "datetime",
    "decimal",
    "enum",
    "int",
    "is-instance",
    "is-subclass",
    "literal",
    "missing-sentinel",
    "none",
    "str",
    "time",
    "timedelta",
    "url",
    "multi-host-url",
    "uuid",
}
#: Nodes whose one inner schema is validated in place (no step in the loc).
IN_PLACE = {"nullable", "default", "function-before", "function-after", "function-wrap"}

Step = Tuple[Any, ...]


@dataclass(frozen=True)
class Leaf:
    """One ``float`` node of one operation's request."""

    method: str
    path: str
    where: str  # "body" or "query"
    steps: Tuple[Step, ...]
    owner: str  # the nearest enclosing model, ``module.qualname``
    owner_from: int  # the step the owner's own fields start at
    strict: bool
    bounds: Tuple[Tuple[str, float], ...]
    annotation: Any  # the body's annotation (the query parameter's for a query)

    @property
    def loc(self) -> Tuple[Any, ...]:
        """The ``loc`` of an error at this float, as FastAPI reports it."""
        return (self.where,) + tuple(step[-1] for step in self.steps)

    @property
    def owner_loc(self) -> Tuple[Any, ...]:
        return tuple(step[-1] for step in self.steps[self.owner_from :])

    @property
    def name(self) -> str:
        return f"{self.method} {self.path}." + ".".join(str(p) for p in self.loc)


def walk(
    schema: Dict[str, Any],
) -> Iterator[Tuple[Tuple[Step, ...], Dict[str, Any], Dict]]:
    """Yield ``(steps, float node, context)`` for every float below *schema*,
    a pydantic core schema. Fails on a node type it does not know, so a new
    construct is a decision, not a silent skip."""
    definitions: Dict[str, Dict[str, Any]] = {}
    yield from _walk(schema, (), definitions, frozenset(), None, 0, False)


def _walk(node, steps, definitions, refs, owner, owner_from, strict):
    kind = node["type"]
    if "ref" in node and kind != "definition-ref":
        definitions.setdefault(node["ref"], node)
    if kind == "float":
        yield (
            steps,
            node,
            {
                "owner": owner,
                "owner_from": owner_from,
                "strict": node.get("strict", strict),
            },
        )
        return
    if kind in TERMINALS:
        return
    if kind == "definitions":
        for definition in node["definitions"]:
            definitions[definition["ref"]] = definition
        yield from _walk(
            node["schema"], steps, definitions, refs, owner, owner_from, strict
        )
    elif kind == "definition-ref":
        ref = node["schema_ref"]
        if ref in refs:  # a model that holds itself: its floats are walked once
            return
        yield from _walk(
            definitions[ref],
            steps,
            definitions,
            refs | {ref},
            owner,
            owner_from,
            strict,
        )
    elif kind == "model":
        config = node.get("config") or {}
        cls = node["cls"]
        yield from _walk(
            node["schema"],
            steps,
            definitions,
            refs,
            f"{cls.__module__}.{cls.__qualname__}",
            len(steps),
            config.get("strict", False),
        )
    elif kind == "model-fields":
        for name, field in node["fields"].items():
            alias = field.get("validation_alias", name)
            if not isinstance(alias, str):
                raise AssertionError(f"a field alias the walk does not read: {alias!r}")
            yield from _walk(
                field["schema"],
                steps + (("field", alias),),
                definitions,
                refs,
                owner,
                owner_from,
                strict,
            )
    elif kind in IN_PLACE:
        if "schema" in node:  # a plain function-before/after has one
            yield from _walk(
                node["schema"], steps, definitions, refs, owner, owner_from, strict
            )
    elif kind == "function-plain":
        return
    elif kind in ("list", "set", "frozenset"):
        if "items_schema" in node:
            yield from _walk(
                node["items_schema"],
                steps + (("item", 0),),
                definitions,
                refs,
                owner,
                owner_from,
                strict,
            )
    elif kind == "tuple":
        for index, item in enumerate(node["items_schema"]):
            yield from _walk(
                item,
                steps + (("item", index),),
                definitions,
                refs,
                owner,
                owner_from,
                strict,
            )
    elif kind == "dict":
        if "values_schema" in node:
            yield from _walk(
                node["values_schema"],
                steps + (("value", "k"),),
                definitions,
                refs,
                owner,
                owner_from,
                strict,
            )
    elif kind == "union":
        for choice in node["choices"]:
            schema, label = choice if isinstance(choice, tuple) else (choice, None)
            if label is None:  # pydantic names the member as its validator does
                label = SchemaValidator(
                    core_schema.definitions_schema(schema, list(definitions.values()))
                ).title
            yield from _walk(
                schema,
                steps + (("member", label),),
                definitions,
                refs,
                owner,
                owner_from,
                strict,
            )
    elif kind == "tagged-union":
        discriminator = node["discriminator"]
        if not isinstance(discriminator, str):
            raise AssertionError(
                f"a discriminator the walk does not read: {discriminator!r}"
            )
        for tag, schema in node["choices"].items():
            yield from _walk(
                schema,
                steps + (("tag", discriminator, tag),),
                definitions,
                refs,
                owner,
                owner_from,
                strict,
            )
    elif kind == "chain":
        for step in node["steps"]:
            yield from _walk(step, steps, definitions, refs, owner, owner_from, strict)
    elif kind == "lax-or-strict":
        # A request is validated in lax mode.
        yield from _walk(
            node["lax_schema"], steps, definitions, refs, owner, owner_from, strict
        )
    elif kind == "json-or-python":
        # FastAPI validates the parsed body with validate_python.
        yield from _walk(
            node["python_schema"], steps, definitions, refs, owner, owner_from, strict
        )
    else:
        raise AssertionError(f"a schema node the walk does not know: {kind!r}")


BOUND_KEYS = ("ge", "gt", "le", "lt")


def _contexts() -> Iterator[Tuple[str, str, Any]]:
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _query_fields(dependant) -> Iterator[Any]:
    yield from dependant.query_params
    for sub in dependant.dependencies:
        yield from _query_fields(sub)


def _leaves() -> Tuple[List[Leaf], Dict[Tuple[str, str], Any]]:
    leaves: List[Leaf] = []
    bodies: Dict[Tuple[str, str], Any] = {}
    for method, path, ctx in _contexts():
        body_field = getattr(ctx, "body_field", None)
        if body_field is not None and not isinstance(
            body_field.field_info, params.Form
        ):
            bodies[(method, path)] = body_field
            for steps, node, context in walk(body_field._type_adapter.core_schema):
                leaves.append(
                    _leaf(method, path, "body", steps, node, context, body_field)
                )
        seen = set()
        for field in _query_fields(ctx.dependant):
            if field.alias in seen:
                continue
            seen.add(field.alias)
            for steps, node, context in walk(field._type_adapter.core_schema):
                leaves.append(
                    _leaf(
                        method,
                        path,
                        "query",
                        (("field", field.alias),) + steps,
                        node,
                        context,
                        field,
                    )
                )
    return leaves, bodies


def _leaf(method, path, where, steps, node, context, field) -> Leaf:
    return Leaf(
        method=method,
        path=path,
        where=where,
        steps=steps,
        owner=context["owner"] or f"{where}:{method} {path}",
        owner_from=context["owner_from"],
        strict=bool(context["strict"]),
        bounds=tuple((k, node[k]) for k in BOUND_KEYS if k in node),
        annotation=field.field_info.annotation,
    )


LEAVES, BODIES = _leaves()
BODY_LEAVES = [leaf for leaf in LEAVES if leaf.where == "body"]
QUERY_LEAVES = [leaf for leaf in LEAVES if leaf.where == "query"]
QUERY_FIELDS = {
    (method, path, field.alias): field
    for method, path, ctx in _contexts()
    for field in _query_fields(ctx.dependant)
}


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _value_at(steps, value) -> Any:
    """The smallest request part that holds *value* at *steps*."""
    node = value
    for step in reversed(steps):
        kind = step[0]
        if kind == "field":
            node = {step[1]: node}
        elif kind == "item":
            node = [None] * step[1] + [node]
        elif kind == "value":
            node = {step[1]: node}
        elif kind == "tag":
            node = {step[1]: step[2], **node}
    return node


def validate(leaf: Leaf, value: Any) -> List[Dict[str, Any]]:
    """The errors at *leaf*'s place when *value* is sent there, as FastAPI
    reports them (``loc`` prefixed with ``body`` or ``query``). Anything raised
    but a ValidationError propagates: it would be a 500."""
    if leaf.where == "body":
        field = BODIES[(leaf.method, leaf.path)]
        sent = _value_at(leaf.steps, value)
        prefix: Tuple[Any, ...] = ("body",)
    else:
        name = leaf.steps[0][1]
        field = QUERY_FIELDS[(leaf.method, leaf.path, name)]
        sent = _value_at(leaf.steps[1:], value)
        prefix = ("query", name)
    try:
        field._type_adapter.validate_python(sent, from_attributes=True)
    except ValidationError as exc:
        errors = [
            dict(e, loc=prefix + tuple(e["loc"])) for e in exc.errors(include_url=False)
        ]
        return [e for e in errors if e["loc"] == leaf.loc]
    return []


# ---------------------------------------------------------------------------
# What the walk finds, per profile
# ---------------------------------------------------------------------------

#: Pinned per profile. Keyed per operation and place (method, path, loc), never
#: by the body annotation's name: both warehouse source bodies are named
#: ``typing.Union``. "classes" counts each field once in the nearest model that
#: declares it (a nested model shared by two bodies counts once). The full
#: profile adds the warehouse's three strict fields; the query is the same.
COUNTS = {
    "core": {
        "body fields": 62,
        "body operations": 22,
        "body annotations": 22,
        "strict": 0,
        "fields by class": 47,
        "classes": 24,
        "query parameters": 20,
        "query operations": 7,
    },
    "full": {
        "body fields": 65,
        "body operations": 25,
        "body annotations": 25,
        "strict": 3,
        "fields by class": 50,
        "classes": 27,
        "query parameters": 20,
        "query operations": 7,
    },
}


def _counts() -> Dict[str, int]:
    by_class = {(leaf.owner, leaf.owner_loc) for leaf in BODY_LEAVES}
    return {
        "body fields": len(
            {(leaf.method, leaf.path, leaf.loc) for leaf in BODY_LEAVES}
        ),
        "body operations": len({(leaf.method, leaf.path) for leaf in BODY_LEAVES}),
        "body annotations": len({id(leaf.annotation) for leaf in BODY_LEAVES}),
        "strict": sum(leaf.strict for leaf in LEAVES),
        "fields by class": len(by_class),
        "classes": len({owner for owner, _ in by_class}),
        "query parameters": len(
            {(leaf.method, leaf.path, leaf.loc) for leaf in QUERY_LEAVES}
        ),
        "query operations": len({(leaf.method, leaf.path) for leaf in QUERY_LEAVES}),
    }


def test_the_number_fields_are_counted_per_profile():
    expected = COUNTS["full" if _full_profile() else "core"]
    assert _counts() == expected, (
        "a new number field is swept like the others (the test below sends it "
        "the four strings); bump the count. Found: "
        + ", ".join(leaf.name for leaf in LEAVES)
    )


def test_every_field_is_found_once():
    names = [leaf.name for leaf in LEAVES]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# The exemptions: a tracking event's value keeps its own refusal
# ---------------------------------------------------------------------------

#: (method, path, loc) -> the body model, by exact equality. Each must be a field
#: the walk finds, with that model, or the test below fails as stale.
EXEMPT = {
    (
        "POST",
        "/api/v1/tracking/track",
        ("body", "value"),
    ): "backend.app.schemas.tracking.EventRequest",
    (
        "POST",
        "/api/v1/tracking/batch",
        ("body", "events", 0, "value"),
    ): "backend.app.schemas.tracking.EventBatchRequest",
    (
        "POST",
        "/api/v1/tracking/events",
        ("body", "value"),
    ): "backend.app.schemas.tracking.EventCreate",
}


def _key(leaf: Leaf) -> Tuple[str, str, Tuple[Any, ...]]:
    return (leaf.method, leaf.path, leaf.loc)


def _model(leaf: Leaf) -> str:
    return f"{leaf.annotation.__module__}.{leaf.annotation.__qualname__}"


def _exempt(leaf: Leaf) -> bool:
    return leaf.where == "body" and EXEMPT.get(_key(leaf)) == _model(leaf)


SWEPT = [leaf for leaf in LEAVES if not _exempt(leaf)]


def test_the_exemptions_are_exactly_fields_the_walk_finds():
    found = {_key(leaf): _model(leaf) for leaf in BODY_LEAVES if _key(leaf) in EXEMPT}
    assert found == EXEMPT, "a stale exemption: no such field on that body model"


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

#: pydantic's text for a strict float given a string.
FLOAT_TYPE_MESSAGE = "Input should be a valid number"


@pytest.mark.regression
@pytest.mark.parametrize("leaf", SWEPT, ids=lambda leaf: leaf.name)
def test_typed_number_fields_refuse_non_finite_strings(leaf):
    """Each of the four strings answers exactly one error at the field:
    ``finite_number`` with pydantic's own text, or ``float_type`` for a field
    that takes only a JSON number."""
    if leaf.strict:
        expected = [("float_type", FLOAT_TYPE_MESSAGE)]
    else:
        expected = [(FINITE_NUMBER_TYPE, FINITE_NUMBER_MESSAGE)]
    answers = {
        text: [(e["type"], e["msg"]) for e in validate(leaf, text)]
        for text in NON_FINITE
    }
    assert answers == dict.fromkeys(NON_FINITE, expected)


# ---------------------------------------------------------------------------
# The positive control: a number in range is accepted at every field
# ---------------------------------------------------------------------------

#: A value in range for a field whose own validator allows less than its bounds
#: say, keyed by the nearest model and the place in it. Every other field takes
#: the middle of its bounds (0.5 with none, half a unit inside a one-sided bound).
VALIDATOR_RANGE = {
    # validate_rating: 1.0 to 5.0
    (
        "backend.app.schemas.llm_experiments.SubmitEvaluationRequest",
        ("human_rating",),
    ): 3.0,
}


def _in_range(leaf: Leaf) -> float:
    if (leaf.owner, leaf.owner_loc) in VALIDATOR_RANGE:
        return VALIDATOR_RANGE[(leaf.owner, leaf.owner_loc)]
    bounds = dict(leaf.bounds)
    low = bounds.get("ge", bounds.get("gt"))
    high = bounds.get("le", bounds.get("lt"))
    if low is None and high is None:
        return 0.5
    if high is None:
        return low + 0.5
    if low is None:
        return high - 0.5
    return (low + high) / 2


def test_each_validator_range_names_a_field():
    fields = {(leaf.owner, leaf.owner_loc) for leaf in LEAVES}
    assert set(VALIDATOR_RANGE) <= fields, "a stale entry: no such field"


@pytest.mark.parametrize("leaf", LEAVES, ids=lambda leaf: leaf.name)
def test_a_number_in_range_is_accepted_at_every_field(leaf):
    """In a non-empty list or dict, as a number and (where the field takes
    strings) as a numeric string: no error at the field, and nothing raised but
    a ValidationError. ``Field(allow_inf_nan=False)`` on a container raises
    ``TypeError`` here, which a request would answer 500."""
    value = _in_range(leaf)
    sent = [value] if leaf.strict else [value, repr(value)]
    answers = {repr(v): validate(leaf, v) for v in sent}
    assert answers == dict.fromkeys(answers, [])


# ---------------------------------------------------------------------------
# The walk itself, on a model holding each construct
# ---------------------------------------------------------------------------


class _Inner(BaseModel):
    x: float


class _TagA(BaseModel):
    kind: Literal["a"]
    a: float


class _TagB(BaseModel):
    kind: Literal["b"]
    b: StrictFloat


def _replaced(make):
    return GetPydanticSchema(lambda source, handler: make())


_LAX_OR_STRICT = _replaced(
    lambda: core_schema.lax_or_strict_schema(
        lax_schema=core_schema.float_schema(),
        strict_schema=core_schema.float_schema(strict=True),
    )
)
_JSON_OR_PYTHON = _replaced(
    lambda: core_schema.json_or_python_schema(
        json_schema=core_schema.float_schema(),
        python_schema=core_schema.float_schema(),
    )
)
_CHAIN = _replaced(
    lambda: core_schema.chain_schema(
        [core_schema.any_schema(), core_schema.float_schema()]
    )
)


class _Fixture(BaseModel):
    plain: float
    optional: Optional[float] = None
    listed: List[float] = []
    a_set: Set[float] = set()
    pair: Tuple[float, int, float]
    mapped: Dict[str, float] = {}
    nested: _Inner
    nested_list: List[_Inner] = []
    either: Union[_Inner, int] = 0
    tagged: Annotated[Union[_TagA, _TagB], Field(discriminator="kind")]
    before: float
    wrapped: float
    after: float
    lax: Annotated[float, _LAX_OR_STRICT]
    json_or_python: Annotated[float, _JSON_OR_PYTHON]
    chained: Annotated[float, _CHAIN]
    parent: Optional[_Fixture] = None
    count: int = 0
    text: str = ""

    @field_validator("before", mode="before")
    @classmethod
    def _before(cls, value):
        return value

    @field_validator("wrapped", mode="wrap")
    @classmethod
    def _wrapped(cls, value, handler):
        return handler(value)

    @field_validator("after")
    @classmethod
    def _after(cls, value):
        return value


#: (loc, nearest model, strict) of every float the walk must find in _Fixture.
FIXTURE_FLOATS = [
    (("plain",), "_Fixture", False),
    (("optional",), "_Fixture", False),
    (("listed", 0), "_Fixture", False),
    (("a_set", 0), "_Fixture", False),
    (("pair", 0), "_Fixture", False),
    (("pair", 2), "_Fixture", False),
    (("mapped", "k"), "_Fixture", False),
    (("nested", "x"), "_Inner", False),
    (("nested_list", 0, "x"), "_Inner", False),
    (("either", "_Inner", "x"), "_Inner", False),
    (("tagged", "a", "a"), "_TagA", False),
    (("tagged", "b", "b"), "_TagB", True),
    (("before",), "_Fixture", False),
    (("wrapped",), "_Fixture", False),
    (("after",), "_Fixture", False),
    (("lax",), "_Fixture", False),
    (("json_or_python",), "_Fixture", False),
    (("chained",), "_Fixture", False),
]


def test_the_walk_finds_every_float_of_each_construct_once():
    found = [
        (
            tuple(step[-1] for step in steps),
            context["owner"].rsplit(".", 1)[-1],
            bool(context["strict"]),
        )
        for steps, _node, context in walk(TypeAdapter(_Fixture).core_schema)
    ]
    assert found == FIXTURE_FLOATS


def test_the_walk_builds_a_body_that_reaches_each_float():
    """A list sent at each place the walk names is refused at exactly that
    loc: the body it builds (a dict key ``k``, item 0, the tag of a tagged
    union) and the loc it expects (a union member's name) agree."""
    adapter = TypeAdapter(_Fixture)
    missed = []
    for steps, _node, _context in walk(adapter.core_schema):
        loc = tuple(step[-1] for step in steps)
        try:
            adapter.validate_python(_value_at(steps, []))
        except ValidationError as exc:
            if loc in {tuple(e["loc"]) for e in exc.errors()}:
                continue
        missed.append(loc)
    assert missed == []


# ---------------------------------------------------------------------------
# Through the application: the anonymous power calculator (no sign-in)
# ---------------------------------------------------------------------------

PLAN = {
    "experiment_name": "Checkout test",
    "metric_description": "conversion rate",
    "baseline_rate": 0.1,
    "mde": 0.1,
    "runtime_days": 14,
}


@pytest.fixture
def anonymous() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _refused_at(*loc) -> Dict[str, Any]:
    return {
        "detail": [
            {"type": FINITE_NUMBER_TYPE, "loc": list(loc), "msg": FINITE_NUMBER_MESSAGE}
        ]
    }


@pytest.mark.regression
def test_the_planner_refuses_an_infinite_runtime_at_the_field(anonymous):
    """200, "The estimated runtime of inf days ...", before #1742."""
    response = anonymous.post(
        "/api/v1/power/plan", json=dict(PLAN, runtime_days="Infinity")
    )
    assert response.status_code == 422, response.text
    assert response.json() == _refused_at("body", "runtime_days")
    assert anonymous.post("/api/v1/power/plan", json=PLAN).status_code == 200


@pytest.mark.regression
def test_the_power_curve_refuses_a_nan_effect_at_the_parameter(anonymous):
    """200, a curve, before #1742."""
    query = {"baseline": 0.1, "mde": "NaN"}
    response = anonymous.get("/api/v1/power/curve", params=query)
    assert response.status_code == 422, response.text
    assert response.json() == _refused_at("query", "mde")
    query["mde"] = "0.05"
    assert anonymous.get("/api/v1/power/curve", params=query).status_code == 200


# ---------------------------------------------------------------------------
# Through the application: each exempt field still refuses the four strings
# ---------------------------------------------------------------------------

UUID = "00000000-0000-4000-8000-000000000001"
INVALID_VALUE = "Invalid event: value not valid"


@pytest.fixture
def stored(monkeypatch) -> List[Any]:
    """The events the tracking routes store, in order."""
    events: List[Any] = []

    def track_event_once(self, event_data, event_id, *, allow_untagged):
        event = EventService.build_event(event_data, allow_untagged=allow_untagged)
        event.id = uuid.uuid4()
        event.updated_at = datetime.now(timezone.utc)
        events.append(event)
        return event.id, True, event

    monkeypatch.setattr(EventService, "track_event_once", track_event_once)
    monkeypatch.setattr(EventService, "unknown_id_fields", lambda self, data: [])
    return events


@pytest.fixture
def tracking(stored) -> Iterator[TestClient]:
    def session() -> Iterator[_Session]:
        yield _Session()

    app.dependency_overrides[deps.get_db] = session
    app.dependency_overrides[deps.get_api_key] = lambda: None
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def _event(value: Any, user: str = "u-1742") -> Dict[str, Any]:
    return {"event_type": "purchase", "user_id": user, "value": value}


def _track_refuses(client, stored, text):
    response = client.post("/api/v1/tracking/track", json=_event(text))
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": INVALID_VALUE}
    assert stored == []


def _batch_reports_it(client, stored, text):
    events = [_event(v, f"u-1742-{i}") for i, v in enumerate([5, text, 7])]
    response = client.post("/api/v1/tracking/batch", json={"events": events})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(e["index"], e["error"]) for e in body["errors"]] == [(1, INVALID_VALUE)]
    assert [e.value for e in stored] == [5.0, 7.0]


def _events_refuses(client, stored, text):
    body = dict(_event(text), event_name="purchase", experiment_id=UUID)
    response = client.post("/api/v1/tracking/events", json=body)
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert (error["loc"], error["type"]) == (["body", "value"], "value_error")
    assert VALUE_BOUND_MESSAGE in error["msg"]
    assert stored == []


OWN_REFUSAL = {
    "/api/v1/tracking/track": _track_refuses,
    "/api/v1/tracking/batch": _batch_reports_it,
    "/api/v1/tracking/events": _events_refuses,
}


def test_each_exemption_has_its_own_refusal():
    assert set(OWN_REFUSAL) == {path for _method, path, _loc in EXEMPT}


@pytest.mark.parametrize("text", NON_FINITE)
@pytest.mark.parametrize("path", sorted(OWN_REFUSAL))
def test_an_exempt_field_refuses_the_strings_its_own_way(tracking, stored, path, text):
    OWN_REFUSAL[path](tracking, stored, text)
