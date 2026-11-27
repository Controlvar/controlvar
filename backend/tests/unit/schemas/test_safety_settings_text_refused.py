"""The global safety settings' request model refuses NUL and unpaired
surrogates in a metric's name or its ``comparison_type`` (#2209).

The route is measured in
``backend/tests/integration/api/test_safety_settings_text_refused.py``; this
checks the model alone: each character in each place, at the field and with a
message that never repeats the value; and that nothing else moves, because
the check reads the validated value: a key a threshold does not have is still
dropped, and a body another check refuses still gets that check's error. The
response model, which shares the base, is not changed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.schemas.safety import SafetySettingsResponse, SafetySettingsUpdate

pytestmark = [pytest.mark.unit, pytest.mark.regression]

MARK = "zqsafetysetmark"
CHARACTERS = {"nul": "\x00", "surrogate": "\ud800", "low-surrogate": "\udc00"}
EMOJI = "\U0001f600"

#: The ``default_metrics`` sent, given the character.
PLACES = {
    "a-metric-name": lambda c: {"error_rate" + c: {"warning_threshold": 0.05}},
    "a-comparison-type": lambda c: {"error_rate": {"comparison_type": "gt" + c}},
}


def _errors(caught):
    return [
        {k: e[k] for k in ("type", "loc", "msg")}
        for e in caught.value.errors(include_input=False, include_url=False)
    ]


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", PLACES)
def test_default_metrics_refuses_the_character(place, character):
    metrics = PLACES[place](MARK + CHARACTERS[character])

    with pytest.raises(ValidationError) as caught:
        SafetySettingsUpdate.model_validate(
            {"enable_automatic_rollbacks": True, "default_metrics": metrics}
        )

    errors = _errors(caught)
    assert errors == [
        {
            "type": "value_error",
            "loc": ("default_metrics",),
            "msg": (
                "Value error, default_metrics must not contain NUL or unpaired "
                "surrogate characters"
            ),
        }
    ]
    assert MARK not in errors[0]["msg"]


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_key_a_threshold_does_not_have_is_still_dropped(character):
    stored = SafetySettingsUpdate.model_validate(
        {
            "default_metrics": {
                "error_rate": {
                    "warning_threshold": 0.05,
                    "note" + CHARACTERS[character]: 1,
                }
            }
        }
    )

    assert stored.model_dump()["default_metrics"] == {
        "error_rate": {
            "warning_threshold": 0.05,
            "critical_threshold": None,
            "comparison_type": "greater_than",
        }
    }


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize(
    "metrics,error",
    (
        (
            lambda c: {"error_rate": {"warning_threshold": "0.05" + c}},
            ("default_metrics", "error_rate", "warning_threshold"),
        ),
        (lambda c: ["error_rate" + c], ("default_metrics",)),
    ),
    ids=("a-threshold-sent-as-a-string", "a-list-sent-for-the-field"),
)
def test_a_body_another_check_refuses_gets_that_error(metrics, error, character):
    with pytest.raises(ValidationError) as caught:
        SafetySettingsUpdate.model_validate(
            {"default_metrics": metrics(CHARACTERS[character])}
        )

    errors = _errors(caught)
    assert [e["loc"] for e in errors] == [error]
    assert errors[0]["type"] != "value_error"


def test_a_null_a_storable_body_and_an_emoji_are_accepted_as_before():
    assert (
        SafetySettingsUpdate.model_validate({"default_metrics": None}).default_metrics
        is None
    )
    stored = SafetySettingsUpdate.model_validate(
        {"default_metrics": {"error_rate" + EMOJI: {"comparison_type": "gt" + EMOJI}}}
    )
    assert stored.model_dump()["default_metrics"] == {
        "error_rate" + EMOJI: {
            "warning_threshold": None,
            "critical_threshold": None,
            "comparison_type": "gt" + EMOJI,
        }
    }


def test_the_response_model_does_not_check():
    """Only the request is checked: the response model, which shares the base,
    has no validator of its own for the field."""
    validators = SafetySettingsResponse.__pydantic_decorators__.field_validators
    assert not any("default_metrics" in v.info.fields for v in validators.values())
