"""The routes that can write a visual experiment's design are exactly these (#1545, V4).

A route whose request body can carry ``visual_config``, ``visual_changes`` or
a goal stores it, so each must hold it to the schema and to the write rules:
create and PUT do it in ``ExperimentCreate`` and in ``_refuse_visual_update``,
the visual editor's save in its ``VisualChanges`` body and then PUT's own
steps, the visual editor's design save in its goal models and then PUT's own
steps, and the clone (which copies stored values with no body) does it in
``clone_visual_problem``. The integration tests in
``backend/tests/integration/api/test_visual_experiment_api.py`` send each
refused document of the corpus through create, PUT and the editor's save,
and a planted one through clone; those in
``backend/tests/integration/api/test_visual_editor_session_design.py`` send a
goal PUT refuses through the design save. This
pins the list: a new route that accepts any of these models fails here until
it is added to those tests' routes.
"""

from __future__ import annotations

import typing
from typing import Iterator, Set, Tuple

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from backend.app.main import app
from backend.app.schemas.visual import VisualChanges, VisualConfig, VisualGoal

pytestmark = pytest.mark.smoke

#: The operations whose body can carry a visual design, in every profile. The
#: visual editor's two saves send their parts to PUT /{experiment_id}'s own
#: steps (``_apply_experiment_update``).
EXPECTED = {
    ("POST", "/api/v1/experiments/"),
    ("PUT", "/api/v1/experiments/{experiment_id}"),
    ("PUT", "/api/v1/visual-editor/sessions/current/variants/{variant_id}/changes"),
    ("PATCH", "/api/v1/visual-editor/sessions/current/experiment"),
}


def _models(annotation, seen) -> Iterator[type]:
    """Every pydantic model reachable from *annotation*."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        yield annotation
        for field in annotation.model_fields.values():
            yield from _models(field.annotation, seen)
        return
    for arg in typing.get_args(annotation):
        yield from _models(arg, seen)


def _goal_models() -> Tuple[type, ...]:
    """The models a ``VisualGoal`` is one of (it is an annotated union)."""
    union = typing.get_args(VisualGoal)[0]
    members = typing.get_args(union)
    assert members and all(issubclass(m, BaseModel) for m in members), members
    return members


def _contexts() -> Iterator[object]:
    """Every operation the app serves, through included routers too."""
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        yield from contexts() if callable(contexts) else contexts


def _operations_carrying(*targets: type) -> Set[Tuple[str, str]]:
    found = set()
    for ctx in _contexts():
        body_field = getattr(ctx, "body_field", None)
        if body_field is None:
            continue
        reachable = set(_models(body_field.field_info.annotation, set()))
        if reachable & set(targets):
            for method in getattr(ctx, "methods", None) or ():
                found.add((method, ctx.path))
    return found


def test_only_create_and_put_take_a_visual_design():
    targets = (VisualChanges, VisualConfig, *_goal_models())
    assert _operations_carrying(*targets) == EXPECTED


def test_the_walker_finds_a_model_it_should():
    """Positive control: the walker reaches a nested model at all."""
    from backend.app.schemas.experiment import MetricBase

    assert ("POST", "/api/v1/experiments/") in _operations_carrying(MetricBase)
