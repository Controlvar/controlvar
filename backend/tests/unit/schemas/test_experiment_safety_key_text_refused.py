"""The request models of an experiment, a flag's safety settings and an API key
refuse NUL and unpaired surrogates in their text (#2180).

The routes are measured in
``backend/tests/integration/api/test_experiment_safety_key_text_refused.py``;
this checks the models alone: each field, both characters, a key or a value,
nested or not, at the field and with a message that never repeats the value.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Tuple, Type

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.schemas.api_key import APIKeyCreate
from backend.app.schemas.experiment import (
    ExperimentCreate,
    ExperimentUpdate,
    MetricBase,
    VariantBase,
)
from backend.app.schemas.safety import FeatureFlagSafetyConfigUpdate

pytestmark = [pytest.mark.unit, pytest.mark.regression]

MARK = "zq2180mark"
CHARACTERS = {"nul": "\x00", "surrogate": "\ud800"}
EMOJI = "\U0001f600"

VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
]
METRICS = [{"name": "Purchase", "event_name": "purchase", "is_primary": True}]
RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
}
CREATE = {"name": "n", "variants": VARIANTS, "metrics": METRICS}


def _deep(text: str, depth: int = 50) -> Dict[str, Any]:
    value: Any = text
    for _ in range(depth):
        value = {"k": [value]}
    return value


#: (model, a valid body, the field, the value holding ``{}`` for the
#: character, the error's loc).
CASES: Dict[str, Tuple[Type[BaseModel], Dict[str, Any], str, Any, Tuple[Any, ...]]] = {
    "create-name": (ExperimentCreate, CREATE, "name", "n{}", ("name",)),
    "create-description": (
        ExperimentCreate,
        CREATE,
        "description",
        "d{}",
        ("description",),
    ),
    "create-hypothesis": (
        ExperimentCreate,
        CREATE,
        "hypothesis",
        "h{}",
        ("hypothesis",),
    ),
    "create-a-tag": (ExperimentCreate, CREATE, "tags", ["ok", "t{}"], ("tags",)),
    "create-a-rule-value": (
        ExperimentCreate,
        CREATE,
        "targeting_rules",
        {
            **RULES,
            "groups": [
                {
                    **RULES["groups"][0],
                    "conditions": [
                        {"attribute": "a", "operator": "equals", "value": "v{}"}
                    ],
                }
            ],
        },
        ("targeting_rules",),
    ),
    "create-a-variant": (
        ExperimentCreate,
        CREATE,
        "variants",
        [VARIANTS[0], {**VARIANTS[1], "description": "d{}"}],
        ("variants", 1, "description"),
    ),
    "create-a-metric": (
        ExperimentCreate,
        CREATE,
        "metrics",
        [{**METRICS[0], "event_value_path": "v{}"}],
        ("metrics", 0, "event_value_path"),
    ),
    "update-name": (ExperimentUpdate, {}, "name", "n{}", ("name",)),
    "update-description": (
        ExperimentUpdate,
        {},
        "description",
        "d{}",
        ("description",),
    ),
    "update-hypothesis": (ExperimentUpdate, {}, "hypothesis", "h{}", ("hypothesis",)),
    "update-a-tag": (ExperimentUpdate, {}, "tags", ["t{}"], ("tags",)),
    "update-a-rule-list-item": (
        ExperimentUpdate,
        {},
        "targeting_rules",
        {
            **RULES,
            "groups": [
                {
                    **RULES["groups"][0],
                    "conditions": [
                        {"attribute": "a", "operator": "in", "value": ["US", "CA{}"]}
                    ],
                }
            ],
        },
        ("targeting_rules",),
    ),
    "update-a-variant": (
        ExperimentUpdate,
        {},
        "variants",
        [{**VARIANTS[0], "name": "C{}"}, VARIANTS[1]],
        ("variants", 0, "name"),
    ),
    "update-a-metric": (
        ExperimentUpdate,
        {},
        "metrics",
        [{**METRICS[0], "aggregation_method": "a{}"}],
        ("metrics", 0, "aggregation_method"),
    ),
    "variant-name": (VariantBase, {"name": "v"}, "name", "v{}", ("name",)),
    "variant-description": (
        VariantBase,
        {"name": "v"},
        "description",
        "d{}",
        ("description",),
    ),
    "variant-configuration-value": (
        VariantBase,
        {"name": "v"},
        "configuration",
        {"color": "blue{}"},
        ("configuration",),
    ),
    "variant-configuration-key": (
        VariantBase,
        {"name": "v"},
        "configuration",
        {"color{}": "blue"},
        ("configuration",),
    ),
    "variant-configuration-50-deep": (
        VariantBase,
        {"name": "v"},
        "configuration",
        _deep("x{}"),
        ("configuration",),
    ),
    "metric-name": (MetricBase, METRICS[0], "name", "m{}", ("name",)),
    "metric-description": (
        MetricBase,
        METRICS[0],
        "description",
        "d{}",
        ("description",),
    ),
    "metric-event-name": (MetricBase, METRICS[0], "event_name", "e{}", ("event_name",)),
    "metric-aggregation-method": (
        MetricBase,
        METRICS[0],
        "aggregation_method",
        "a{}",
        ("aggregation_method",),
    ),
    "metric-event-value-path": (
        MetricBase,
        METRICS[0],
        "event_value_path",
        "p{}",
        ("event_value_path",),
    ),
    "safety-metric-name": (
        FeatureFlagSafetyConfigUpdate,
        {},
        "metrics",
        {"error_rate{}": {"critical_threshold": 0.1}},
        ("metrics",),
    ),
    "safety-comparison-type": (
        FeatureFlagSafetyConfigUpdate,
        {},
        "metrics",
        {"error_rate": {"comparison_type": "greater_than{}"}},
        ("metrics",),
    ),
    "key-name": (APIKeyCreate, {}, "name", "k{}", ("name",)),
    "key-description": (
        APIKeyCreate,
        {"name": "k"},
        "description",
        "d{}",
        ("description",),
    ),
    "key-a-scope": (
        APIKeyCreate,
        {"name": "k"},
        "scopes",
        ["flags:read{}"],
        ("scopes",),
    ),
    "key-scopes-as-text": (APIKeyCreate, {"name": "k"}, "scopes", "a,b{}", ("scopes",)),
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
    """An update's ``name`` and the safety ``metrics`` still refuse null with
    their own error; an optional text field still takes it."""
    for model, field in (
        (ExperimentUpdate, "name"),
        (FeatureFlagSafetyConfigUpdate, "metrics"),
    ):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({field: None})
        assert [e["type"] for e in caught.value.errors()] == ["null_not_allowed"]
    assert ExperimentUpdate.model_validate({"description": None}).description is None
    assert (
        APIKeyCreate.model_validate({"name": "k", "description": None}).description
        is None
    )
