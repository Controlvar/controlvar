"""The list routes' status-like filters: the helper and the inventory (#955).

``backend/app/api/enum_filters.py`` is the one casing rule for the four routes
#955 named. These tests need no database:

- the helper: an absent or empty value is no filter, any case matches, and
  anything else is the 422 at ``["query", <param>]`` with a fixed message;
- each route's spellings (the message's list) name exactly its enum's values,
  so a member added to the enum cannot be missing from the 422's message;
- the inventory (gate 9d): every status-like query parameter in the served
  OpenAPI document is classified here, exactly. A new ``status`` filter, or a
  ``task_type``, fails until someone says how it answers an unknown value.

The behaviour through the routes, on PostgreSQL, is
``backend/tests/integration/api/test_list_status_filters.py``'s.
"""

from enum import Enum

import pytest
from fastapi.exceptions import RequestValidationError

from backend.app.api.enum_filters import enum_filter, enum_filter_message
from backend.app.api.v1.endpoints import (
    feature_flags,
    llm_experiments,
    mutual_exclusion_groups,
    notifications,
)
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.llm_experiment import LLMExperimentStatus, LLMTaskType
from backend.app.models.mutual_exclusion_group import MutualExclusionGroupStatus
from backend.app.models.notification import NotificationStatus

pytestmark = pytest.mark.unit


class _Color(Enum):
    RED = "RED"
    LIGHT_BLUE = "light_blue"


SPELLINGS = ("red", "light_blue")


@pytest.mark.parametrize("value", [None, ""])
def test_no_value_is_no_filter(value):
    assert enum_filter(value, _Color, "color", SPELLINGS) is None


@pytest.mark.parametrize(
    ("value", "member"),
    [
        ("red", _Color.RED),
        ("RED", _Color.RED),
        ("Red", _Color.RED),
        ("light_blue", _Color.LIGHT_BLUE),
        ("LIGHT_BLUE", _Color.LIGHT_BLUE),
        ("Light_Blue", _Color.LIGHT_BLUE),
    ],
)
def test_any_case_names_the_member(value, member):
    assert enum_filter(value, _Color, "color", SPELLINGS) is member


@pytest.mark.parametrize(
    "value", ["green", " red", "red ", "LIGHT-BLUE", "lightblue", "null", "RED,red"]
)
def test_anything_else_is_the_422(value):
    with pytest.raises(RequestValidationError) as caught:
        enum_filter(value, _Color, "color", SPELLINGS)
    assert caught.value.errors() == [
        {
            "type": "enum",
            "loc": ("query", "color"),
            "msg": "color must be one of: red, light_blue (any case)",
        }
    ]


def test_the_message_never_carries_the_value():
    with pytest.raises(RequestValidationError) as caught:
        enum_filter("s3cr3t-value", _Color, "color", SPELLINGS)
    assert "s3cr3t" not in repr(caught.value.errors())


#: Each route's filter: (its spellings, the enum they must name), with the
#: message the integration test pins, written out.
ROUTE_FILTERS = [
    pytest.param(
        feature_flags.FLAG_STATUS_SPELLINGS,
        FeatureFlagStatus,
        "status must be one of: active, inactive, archived (any case)",
        id="flags-status",
    ),
    pytest.param(
        llm_experiments.LLM_STATUS_SPELLINGS,
        LLMExperimentStatus,
        "status must be one of: DRAFT, ACTIVE, PAUSED, COMPLETED (any case)",
        id="llm-status",
    ),
    pytest.param(
        llm_experiments.LLM_TASK_TYPE_SPELLINGS,
        LLMTaskType,
        "task_type must be one of: chat_completion, text_generation, "
        "classification, summarization, code_generation, embedding (any case)",
        id="llm-task_type",
    ),
    pytest.param(
        mutual_exclusion_groups.GROUP_STATUS_SPELLINGS,
        MutualExclusionGroupStatus,
        "status must be one of: active, archived (any case)",
        id="groups-status",
    ),
    pytest.param(
        notifications.DELIVERY_STATUS_SPELLINGS,
        NotificationStatus,
        "status must be one of: sent, failed, skipped (any case)",
        id="delivery-status",
    ),
]


@pytest.mark.parametrize(("spellings", "enum_cls", "message"), ROUTE_FILTERS)
def test_each_routes_spellings_name_exactly_its_enum(spellings, enum_cls, message):
    assert sorted(s.lower() for s in spellings) == sorted(
        str(m.value).lower() for m in enum_cls
    )
    assert len(set(spellings)) == len(spellings)
    param = message.split(" ", 1)[0]
    assert enum_filter_message(param, spellings) == message


# ---------------------------------------------------------------------------
# Gate 9d: every status-like query parameter, classified
# ---------------------------------------------------------------------------

#: How each status-like query parameter answers a value outside its enum.
ENUM_FILTER = "enum_filter: any case; anything else 422 at the parameter (#955)"
SCHEMA_ENUM = "schema enum: exact values only; FastAPI answers 422 for anything else"
EXPERIMENTS = (
    "status_filter: resolve_experiment_status; an unknown value answers 200 "
    "with no rows (not #955: it never answered 500)"
)

CLASSIFIED = {
    ("GET", "/api/v1/feature-flags/", "status"): ENUM_FILTER,
    ("GET", "/api/v1/llm-experiments/", "status"): ENUM_FILTER,
    ("GET", "/api/v1/llm-experiments/", "task_type"): ENUM_FILTER,
    ("GET", "/api/v1/mutual-exclusion-groups", "status"): ENUM_FILTER,
    ("GET", "/api/v1/notifications/delivery-log", "status"): ENUM_FILTER,
    ("GET", "/api/v1/rollout-schedules/", "status"): SCHEMA_ENUM,
    ("GET", "/api/v1/segments", "status"): SCHEMA_ENUM,
    ("GET", "/api/v1/experiments/", "status_filter"): EXPERIMENTS,
}


def _status_like(name: str) -> bool:
    """A query parameter that filters on a state: any name with ``status`` in
    it, and ``task_type``, the one other enum filter #955 named."""
    return "status" in name or name == "task_type"


@pytest.fixture(scope="module")
def document():
    from backend.app.main import app

    return app.openapi()


def _found(document):
    found = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "query" and _status_like(parameter["name"]):
                    found[(method.upper(), path, parameter["name"])] = parameter
    return found


def test_every_status_like_query_parameter_is_classified(document):
    assert set(_found(document)) == set(CLASSIFIED)
    assert len(CLASSIFIED) == 8


def test_the_enum_filters_are_plain_strings_that_list_their_values(document):
    """An enum filter keeps a plain-string schema (an OpenAPI ``enum`` would
    promise one exact case), and its description names every allowed value."""
    found = _found(document)
    spellings = {
        (
            "GET",
            "/api/v1/feature-flags/",
            "status",
        ): feature_flags.FLAG_STATUS_SPELLINGS,
        (
            "GET",
            "/api/v1/llm-experiments/",
            "status",
        ): llm_experiments.LLM_STATUS_SPELLINGS,
        (
            "GET",
            "/api/v1/llm-experiments/",
            "task_type",
        ): llm_experiments.LLM_TASK_TYPE_SPELLINGS,
        (
            "GET",
            "/api/v1/mutual-exclusion-groups",
            "status",
        ): mutual_exclusion_groups.GROUP_STATUS_SPELLINGS,
        (
            "GET",
            "/api/v1/notifications/delivery-log",
            "status",
        ): notifications.DELIVERY_STATUS_SPELLINGS,
    }
    assert set(spellings) == {k for k, v in CLASSIFIED.items() if v == ENUM_FILTER}
    for key, values in spellings.items():
        parameter = found[key]
        schema = parameter["schema"]
        assert "enum" not in repr(schema), key
        assert "string" in repr(schema), key
        description = schema.get("description") or parameter.get("description", "")
        for value in values:
            assert f"`{value}`" in description, (key, value)
        assert "422" in description and "any case" in description, key
