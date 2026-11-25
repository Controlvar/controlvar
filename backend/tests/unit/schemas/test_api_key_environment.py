"""An API key's environment in the create request and the responses (#2011).

Without a database: which values ``POST /api/v1/api-keys`` takes, the refusal
of every other one and the refusal of ``mcp:read`` outside production, each
word for word. The HTTP answers and the stored rows are pinned on PostgreSQL by
``backend/tests/integration/api/test_api_key_environment_api.py``, and the
OpenAPI documents by ``backend/tests/smoke/test_api_key_environment_in_snapshot.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.flag_environments import FLAG_ENVIRONMENTS
from backend.app.schemas.api_key import (
    MCP_READ_NEEDS_PRODUCTION,
    APIKeyCreate,
    APIKeyCreated,
    APIKeyRead,
)

pytestmark = [pytest.mark.unit]

#: The standard 422 for an environment that is not one of the three.
NOT_AN_ENVIRONMENT = "Input should be 'production', 'staging' or 'development'"

#: The refusal of ``mcp:read`` outside production, word for word.
MCP_READ_REFUSED = (
    "mcp:read needs a production key: the routes it opens read production only. "
    "Create the key for production, or without mcp:read."
)

REFUSED = [
    pytest.param("Production", id="capitalised"),
    pytest.param("STAGING", id="upper-case"),
    pytest.param(" staging ", id="padded"),
    pytest.param("prod", id="prod"),
    pytest.param("dev", id="dev"),
    pytest.param("qa", id="qa"),
    pytest.param("", id="empty"),
    pytest.param(None, id="null"),
    pytest.param(1, id="number"),
    pytest.param(True, id="boolean"),
    pytest.param(["staging"], id="list"),
    pytest.param({"staging": 1}, id="object"),
]


def _errors(**body):
    with pytest.raises(ValidationError) as refused:
        APIKeyCreate.model_validate({"name": "k", **body})
    return refused.value.errors()


def test_the_default_is_production():
    assert APIKeyCreate.model_validate({"name": "k"}).environment == "production"


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_each_environment_is_accepted_as_named(environment):
    body = APIKeyCreate.model_validate({"name": "k", "environment": environment})
    assert body.environment == environment


@pytest.mark.parametrize("sent", REFUSED)
def test_any_other_value_is_the_standard_422(sent):
    (error,) = _errors(environment=sent)
    assert error["loc"] == ("environment",)
    assert error["type"] == "literal_error"
    assert error["msg"] == NOT_AN_ENVIRONMENT
    assert error["ctx"] == {"expected": "'production', 'staging' or 'development'"}


def test_the_mcp_read_refusal_is_these_words():
    assert MCP_READ_NEEDS_PRODUCTION == MCP_READ_REFUSED


@pytest.mark.parametrize("environment", ["staging", "development"])
@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param(["mcp:read"], id="list"),
        pytest.param(["sdk:ruleset", "mcp:read"], id="beside-another"),
        pytest.param([" mcp:read "], id="padded"),
        pytest.param("read, mcp:read ,,", id="comma-string"),
        pytest.param({"mcp:read": 1}, id="object-key"),
    ],
)
def test_mcp_read_outside_production_is_refused(environment, scopes):
    (error,) = _errors(environment=environment, scopes=scopes)
    assert error["loc"] == ("scopes",)
    assert error["type"] == "value_error"
    assert error["msg"] == f"Value error, {MCP_READ_REFUSED}"


@pytest.mark.parametrize("environment", ["staging", "development"])
@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="none"),
        pytest.param(["sdk:ruleset"], id="sdk-ruleset"),
        pytest.param(["MCP:READ", "xmcp:read", "mcp:read-only"], id="near-misses"),
    ],
)
def test_every_other_scope_is_accepted_outside_production(environment, scopes):
    body = APIKeyCreate.model_validate(
        {"name": "k", "environment": environment, "scopes": scopes}
    )
    assert body.environment == environment
    assert body.scopes == scopes


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="by-default"),
        pytest.param({"environment": "production"}, id="named"),
    ],
)
def test_mcp_read_is_accepted_in_production(body):
    key = APIKeyCreate.model_validate({"name": "k", "scopes": ["mcp:read"], **body})
    assert (key.environment, key.scopes) == ("production", ["mcp:read"])


def test_a_refused_environment_does_not_skip_the_mcp_read_check():
    """The check reads the environment that was accepted before it. One that
    was refused is not there to read, and the check refuses then too."""
    errors = _errors(environment="qa", scopes=["mcp:read"])
    assert [(e["loc"], e["type"]) for e in errors] == [
        (("environment",), "literal_error"),
        (("scopes",), "value_error"),
    ]


@pytest.mark.parametrize("model", [APIKeyRead, APIKeyCreated])
def test_each_response_always_names_the_environment(model):
    assert model.model_fields["environment"].is_required()
