"""A workspace's and a custom role's request models refuse NUL and unpaired
surrogates in their text (#2181).

The routes are measured in
``modules/backend/tests/integration/api/test_workspace_role_text_refused.py``;
this checks the models alone: each field, both characters, at the field and
with a message that never repeats the value. A workspace's ``slug`` and a
role's ``name`` refused both already, with their own errors.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple, Type

import pytest
from pydantic import BaseModel, ValidationError

from modules.backend.app.schemas.rbac import CustomRoleCreate, CustomRoleUpdate
from modules.backend.app.schemas.workspaces import (
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

MARK = "zq2181mark"
CHARACTERS = {"nul": "\x00", "surrogate": "\ud800"}
EMOJI = "\U0001f600"

WORKSPACE = {"name": "w", "slug": "team-one"}

#: (model, a valid body, the field).
CASES: Dict[str, Tuple[Type[BaseModel], Dict[str, Any], str]] = {
    "workspace-create-name": (CreateWorkspaceRequest, WORKSPACE, "name"),
    "workspace-create-description": (CreateWorkspaceRequest, WORKSPACE, "description"),
    "workspace-update-name": (UpdateWorkspaceRequest, {}, "name"),
    "workspace-update-description": (UpdateWorkspaceRequest, {}, "description"),
    "role-create-description": (CustomRoleCreate, {"name": "auditor"}, "description"),
    "role-update-description": (CustomRoleUpdate, {}, "description"),
}


def _body(case: str, text: str) -> Tuple[Type[BaseModel], Dict[str, Any]]:
    model, base, field = CASES[case]
    return model, {**base, field: "t " + text}


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("case", CASES)
def test_the_text_is_refused_at_its_field(case, character):
    model, body = _body(case, MARK + CHARACTERS[character])
    field = CASES[case][2]

    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)

    errors = caught.value.errors(include_url=False, include_input=False)
    assert [(e["type"], e["loc"], e["msg"]) for e in errors] == [
        (
            "value_error",
            (field,),
            f"Value error, {field} must not contain NUL or unpaired surrogate characters",
        )
    ]
    assert MARK not in str(errors)


@pytest.mark.parametrize("case", CASES)
def test_the_same_body_without_the_character_is_accepted(case):
    model, body = _body(case, "")
    model.model_validate(body)


@pytest.mark.parametrize("case", CASES)
def test_an_emoji_is_accepted_as_sent(case):
    model, body = _body(case, EMOJI)
    validated = model.model_validate(body)
    assert getattr(validated, CASES[case][2]) == "t " + EMOJI


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_slug_and_a_role_name_answer_their_own_422(character):
    for model, body, field, kinds in (
        (
            CreateWorkspaceRequest,
            {**WORKSPACE, "slug": "team" + CHARACTERS[character]},
            "slug",
            ("value_error",),
        ),
        (
            CustomRoleCreate,
            {"name": "auditor" + CHARACTERS[character]},
            "name",
            ("string_pattern_mismatch", "string_unicode"),
        ),
    ):
        with pytest.raises(ValidationError) as caught:
            model.model_validate(body)
        errors = caught.value.errors()
        assert [e["loc"] for e in errors] == [(field,)]
        assert errors[0]["type"] in kinds
        assert "unpaired surrogate" not in errors[0]["msg"]


def test_a_null_is_answered_as_before():
    """An update's name and description, and a role's description, take null."""
    assert UpdateWorkspaceRequest.model_validate(
        {"name": None, "description": None}
    ).model_dump() == {"name": None, "description": None}
    assert CustomRoleUpdate.model_validate({"description": None}).description is None
    assert (
        CustomRoleCreate.model_validate(
            {"name": "auditor", "description": None}
        ).description
        is None
    )
