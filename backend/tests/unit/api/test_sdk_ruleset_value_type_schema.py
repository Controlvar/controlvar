"""The ruleset's ``value_type`` is an open string in the published schema.

``GET /api/v1/sdk/ruleset`` returns a raw ``Response`` that is never validated
against ``SdkRulesetFlag``, so the model only documents the document. It listed
``string``, ``number`` and ``json`` as a closed enum, which a generated client
could enforce, while the field's own description tells an SDK that does not
know a type to ask the server, and the flag read model gives ``value_type`` as
a plain string, "never a Literal" (``schemas/feature_flag.py``). Plan F, F5b.
"""

import pytest

from backend.app.api.v1.endpoints.sdk import SdkRulesetFlag
from backend.app.main import app

pytestmark = [pytest.mark.unit]


def _is_open_nullable_string(prop: dict) -> bool:
    options = prop.get("anyOf", [prop])
    types = sorted(option.get("type") for option in options)
    return types == ["null", "string"] and not any("enum" in o for o in options)


@pytest.mark.regression
def test_model_schema_value_type_is_a_nullable_string_without_enum():
    prop = SdkRulesetFlag.model_json_schema()["properties"]["value_type"]
    assert _is_open_nullable_string(prop), prop
    assert "string, number, or json" in prop["description"]


@pytest.mark.regression
def test_published_openapi_value_type_is_a_nullable_string_without_enum():
    schema = app.openapi()["components"]["schemas"]["SdkRulesetFlag"]
    prop = schema["properties"]["value_type"]
    assert _is_open_nullable_string(prop), prop
