"""Every ``mode="before"`` field validator of a request body model survives a
value of the wrong type (#957).

A ``before`` validator sees the raw value before pydantic checks its type. One
that compares or iterates it (``1 <= v``, ``for s in v``) raises ``TypeError``
for a value of another type, and pydantic does not turn a ``TypeError`` into a
422, so the request answered 500. Five fields did: ``holdout_percentage`` and
``traffic_allocation`` on create and update, and an API key's ``scopes``.

This walks every request body model the application serves, nested models
included, takes each field a ``before`` validator covers, and validates
``{field: probe}`` for each of ``{}``, ``[]``, ``false`` and ``null``: anything
raised but a ``ValidationError`` fails, naming the model, the field and the
probe.

Routes are read through ``effective_route_contexts`` (FastAPI keeps
``include_router`` lazy: ``app.routes`` alone shows no body), and the counts are
pinned per profile, so a walk that sees fewer models fails instead of passing on
fewer. A new ``before`` validator changes the count: it is probed like the
others, and the number is bumped. The profile is decided as the application
decides it: whether ``modules`` imports.
"""

from __future__ import annotations

import importlib.util
import typing
from typing import Iterator, List, Set, Tuple, Type

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from backend.app.main import app

pytestmark = [pytest.mark.unit]

#: Request body models reachable from the routes, per profile. #1545 added 11
#: to both: /start's body, VisualConfig with its page and two goal kinds, and
#: VisualChanges with its five change operations.
CORE_MODEL_COUNT = 90
FULL_MODEL_COUNT = 132

#: ``(model, field)`` pairs that a ``mode="before"`` field validator covers.
CORE_BEFORE_COUNT = 46
FULL_BEFORE_COUNT = 47

PROBES = ({}, [], False, None)


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _contexts() -> Iterator[object]:
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        yield from contexts() if callable(contexts) else contexts


def _models_in(annotation, seen: Set[type]) -> List[Type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return []
        seen.add(annotation)
        found = [annotation]
        for field in annotation.model_fields.values():
            found.extend(_models_in(field.annotation, seen))
        return found
    found = []
    for argument in typing.get_args(annotation):
        found.extend(_models_in(argument, seen))
    return found


def _request_models() -> List[Type[BaseModel]]:
    seen: Set[type] = set()
    models: List[Type[BaseModel]] = []
    for ctx in _contexts():
        body_field = getattr(ctx, "body_field", None)
        if body_field is not None:
            models.extend(_models_in(body_field.field_info.annotation, seen))
    return models


def _before_fields(models) -> List[Tuple[Type[BaseModel], str]]:
    pairs = []
    for model in models:
        validators = model.__pydantic_decorators__.field_validators.values()
        for validator in validators:
            if validator.info.mode != "before":
                continue
            fields = validator.info.fields
            if "*" in fields:
                fields = tuple(model.model_fields)
            for field in fields:
                if field in model.model_fields:
                    pairs.append((model, field))
    return sorted(set(pairs), key=lambda p: (p[0].__module__, p[0].__name__, p[1]))


MODELS = _request_models()
BEFORE = _before_fields(MODELS)


def _named(pairs) -> List[str]:
    return [f"{model.__module__}.{model.__name__}.{field}" for model, field in pairs]


def test_the_request_models_are_counted_per_profile():
    expected = FULL_MODEL_COUNT if _full_profile() else CORE_MODEL_COUNT
    assert len(MODELS) == expected, (
        f"{len(MODELS)} request body models reachable from the routes, "
        f"{expected} expected"
    )


def test_the_before_validated_fields_are_counted_per_profile():
    expected = FULL_BEFORE_COUNT if _full_profile() else CORE_BEFORE_COUNT
    assert len(BEFORE) == expected, (
        f"{len(BEFORE)} (model, field) pairs with a mode='before' validator, "
        f"{expected} expected: a new one is probed below like the others; bump "
        "the count. Found: " + ", ".join(_named(BEFORE))
    )


def test_no_before_validator_raises_anything_but_a_validation_error():
    crashes = []
    for model, field in BEFORE:
        for probe in PROBES:
            try:
                model.model_validate({field: probe})
            except ValidationError:
                pass
            except Exception as exc:  # the defect: this would answer 500
                crashes.append(
                    f"{model.__module__}.{model.__name__}.{field} = {probe!r}: "
                    f"{type(exc).__name__}"
                )
    assert crashes == []
