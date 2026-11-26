"""The request models of a holdout, a mutual exclusion group, a user, a
user's notification settings, a flag and a rollout schedule refuse NUL and
unpaired surrogates in their text (#2181).

The routes are measured in
``backend/tests/integration/api/test_names_text_refused.py``; this checks the
models alone: each field, both characters, a key or a value, nested or not, at
the field and with a message that never repeats the value. The workspace and
custom role models are checked in the modules suite.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, Tuple, Type

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.schemas.feature_flag import FeatureFlagCreate, FeatureFlagUpdate
from backend.app.schemas.global_holdout import GlobalHoldoutCreate, GlobalHoldoutUpdate
from backend.app.schemas.mutual_exclusion_group import (
    MutualExclusionGroupCreate,
    MutualExclusionGroupUpdate,
)
from backend.app.schemas.notification import NotificationPreferenceUpdate
from backend.app.schemas.rollout_schedule import (
    RolloutScheduleCreate,
    RolloutScheduleUpdate,
    RolloutStageCreate,
    RolloutStageUpdate,
)
from backend.app.schemas.user import UserCreate, UserUpdate

pytestmark = [pytest.mark.unit, pytest.mark.regression]

MARK = "zq2181mark"
CHARACTERS = {"nul": "\x00", "surrogate": "\ud800"}
EMOJI = "\U0001f600"

USER = {"username": "user1", "email": "user1@texttest.io"}
NEW_USER = {**USER, "password": "Passw0rdOk"}
STAGE = {
    "name": "s",
    "stage_order": 0,
    "target_percentage": 10,
    "trigger_type": "manual",
}
SCHEDULE = {"name": "s", "feature_flag_id": str(uuid.uuid4()), "stages": [STAGE]}


def _deep(text: str, depth: int = 50) -> Dict[str, Any]:
    value: Any = text
    for _ in range(depth):
        value = {"k": [value]}
    return value


#: (model, a valid body, the field, the value holding ``{}`` for the
#: character, the error's loc).
CASES: Dict[str, Tuple[Type[BaseModel], Dict[str, Any], str, Any, Tuple[Any, ...]]] = {
    "holdout-create-name": (GlobalHoldoutCreate, {}, "name", "h{}", ("name",)),
    "holdout-create-description": (
        GlobalHoldoutCreate,
        {"name": "h"},
        "description",
        "d{}",
        ("description",),
    ),
    "holdout-update-name": (GlobalHoldoutUpdate, {}, "name", "h{}", ("name",)),
    "holdout-update-description": (
        GlobalHoldoutUpdate,
        {},
        "description",
        "d{}",
        ("description",),
    ),
    "group-create-name": (MutualExclusionGroupCreate, {}, "name", "g{}", ("name",)),
    "group-create-description": (
        MutualExclusionGroupCreate,
        {"name": "g"},
        "description",
        "d{}",
        ("description",),
    ),
    "group-update-name": (MutualExclusionGroupUpdate, {}, "name", "g{}", ("name",)),
    "group-update-description": (
        MutualExclusionGroupUpdate,
        {},
        "description",
        "d{}",
        ("description",),
    ),
    "user-create-username": (UserCreate, NEW_USER, "username", "user{}", ("username",)),
    "user-create-full-name": (
        UserCreate,
        NEW_USER,
        "full_name",
        "A Name{}",
        ("full_name",),
    ),
    "user-update-username": (UserUpdate, USER, "username", "user{}", ("username",)),
    "user-update-full-name": (
        UserUpdate,
        USER,
        "full_name",
        "A Name{}",
        ("full_name",),
    ),
    "notification-slack-channel": (
        NotificationPreferenceUpdate,
        {},
        "slack_channel",
        "#c{}",
        ("slack_channel",),
    ),
    "notification-email-override": (
        NotificationPreferenceUpdate,
        {},
        "email_override",
        "a{}@texttest.io",
        ("email_override",),
    ),
    "flag-create-name": (FeatureFlagCreate, {"key": "k"}, "name", "n{}", ("name",)),
    "flag-create-description": (
        FeatureFlagCreate,
        {"key": "k", "name": "n"},
        "description",
        "d{}",
        ("description",),
    ),
    "flag-update-name": (FeatureFlagUpdate, {}, "name", "n{}", ("name",)),
    "flag-update-description": (
        FeatureFlagUpdate,
        {},
        "description",
        "d{}",
        ("description",),
    ),
    "schedule-create-name": (RolloutScheduleCreate, SCHEDULE, "name", "s{}", ("name",)),
    "schedule-create-description": (
        RolloutScheduleCreate,
        SCHEDULE,
        "description",
        "d{}",
        ("description",),
    ),
    "schedule-create-config-data-value": (
        RolloutScheduleCreate,
        SCHEDULE,
        "config_data",
        {"owner": "o{}"},
        ("config_data",),
    ),
    "schedule-create-config-data-key": (
        RolloutScheduleCreate,
        SCHEDULE,
        "config_data",
        {"owner{}": "o"},
        ("config_data",),
    ),
    "schedule-create-config-data-50-deep": (
        RolloutScheduleCreate,
        SCHEDULE,
        "config_data",
        _deep("x{}"),
        ("config_data",),
    ),
    "schedule-create-a-stage-name": (
        RolloutScheduleCreate,
        SCHEDULE,
        "stages",
        [{**STAGE, "name": "s{}"}],
        ("stages", 0, "name"),
    ),
    "schedule-create-a-stage-trigger-configuration": (
        RolloutScheduleCreate,
        SCHEDULE,
        "stages",
        [{**STAGE, "trigger_configuration": {"note": "n{}"}}],
        ("stages", 0, "trigger_configuration"),
    ),
    "stage-create-name": (RolloutStageCreate, STAGE, "name", "s{}", ("name",)),
    "stage-create-description": (
        RolloutStageCreate,
        STAGE,
        "description",
        "d{}",
        ("description",),
    ),
    "stage-create-trigger-configuration-value": (
        RolloutStageCreate,
        STAGE,
        "trigger_configuration",
        {"note": "n{}"},
        ("trigger_configuration",),
    ),
    "stage-create-trigger-configuration-key": (
        RolloutStageCreate,
        STAGE,
        "trigger_configuration",
        {"note{}": 1},
        ("trigger_configuration",),
    ),
    "schedule-update-name": (RolloutScheduleUpdate, {}, "name", "s{}", ("name",)),
    "schedule-update-description": (
        RolloutScheduleUpdate,
        {},
        "description",
        "d{}",
        ("description",),
    ),
    "schedule-update-config-data": (
        RolloutScheduleUpdate,
        {},
        "config_data",
        {"owner": ["a", "o{}"]},
        ("config_data",),
    ),
    "stage-update-name": (RolloutStageUpdate, {}, "name", "s{}", ("name",)),
    "stage-update-description": (
        RolloutStageUpdate,
        {},
        "description",
        "d{}",
        ("description",),
    ),
    "stage-update-trigger-configuration": (
        RolloutStageUpdate,
        {},
        "trigger_configuration",
        {"note": "n{}"},
        ("trigger_configuration",),
    ),
}


def _filled(value: Any, text: str) -> Any:
    """*value* with every ``{}`` in its strings, keys included, replaced."""
    if isinstance(value, str):
        return value.replace("{}", text)
    if isinstance(value, dict):
        return {_filled(k, text): _filled(v, text) for k, v in value.items()}
    if isinstance(value, list):
        return [_filled(v, text) for v in value]
    return value


def _body(case: str, text: str) -> Tuple[Type[BaseModel], Dict[str, Any]]:
    model, base, field, value, _ = CASES[case]
    body = copy.deepcopy(base)
    body[field] = _filled(value, text)
    return model, body


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("case", CASES)
def test_the_text_is_refused_at_its_field(case, character):
    model, body = _body(case, MARK + CHARACTERS[character])
    field = CASES[case][4][-1]

    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)

    errors = caught.value.errors(include_url=False, include_input=False)
    assert [(e["type"], e["loc"], e["msg"]) for e in errors] == [
        (
            "value_error",
            CASES[case][4],
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
    assert EMOJI in str(validated.model_dump())


def test_a_null_is_answered_as_before():
    """The fields that refused null still do, with their own error; an
    optional text field still takes it."""
    for model, field, kind in (
        (FeatureFlagUpdate, "name", "null_not_allowed"),
        (RolloutScheduleUpdate, "name", "value_error"),
        (RolloutStageUpdate, "name", "value_error"),
    ):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({field: None})
        errors = caught.value.errors()
        assert [(e["type"], e["loc"]) for e in errors] == [(kind, (field,))]
        assert "cannot be null" in errors[0]["msg"]
    for model, body in (
        (GlobalHoldoutUpdate, {"name": None, "description": None}),
        (MutualExclusionGroupUpdate, {"name": None, "description": None}),
        (UserUpdate, {**USER, "full_name": None}),
        (NotificationPreferenceUpdate, {"slack_channel": None, "email_override": None}),
        (FeatureFlagUpdate, {"description": None}),
        (RolloutScheduleUpdate, {"description": None, "config_data": None}),
        (RolloutStageUpdate, {"description": None, "trigger_configuration": None}),
    ):
        validated = model.model_validate(body)
        assert all(getattr(validated, k) is None for k in body if k not in USER)


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_password_and_an_email_answer_as_before(character):
    """A user's ``email`` refused both characters already, with its own error;
    a password is hashed, never stored as sent, so a NUL in it is accepted as
    before (a lone surrogate has its own refusal)."""
    with pytest.raises(ValidationError) as caught:
        UserCreate.model_validate(
            {**NEW_USER, "email": "user1" + CHARACTERS[character] + "@texttest.io"}
        )
    assert [e["loc"] for e in caught.value.errors()] == [("email",)]
    assert "unpaired surrogate" not in str(caught.value)

    body = {**NEW_USER, "password": "Passw0rdOk" + CHARACTERS[character]}
    if character == "nul":
        assert (
            UserCreate.model_validate(body).password.get_secret_value()
            == (body["password"])
        )
        return
    with pytest.raises(ValidationError) as caught:
        UserCreate.model_validate(body)
    assert [e["msg"] for e in caught.value.errors()] == [
        "Value error, Password must be valid text"
    ]
