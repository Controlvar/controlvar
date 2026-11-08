"""Every request-body property of the served documents has a type (#1571).

``FeatureFlagCreate`` and ``FeatureFlagUpdate`` declared ``on_value`` and
``off_value`` as ``Any``. Both served documents therefore rendered the four
with a title and a description and nothing else: a schema every JSON value
passes, ``null`` included. The API refuses ``null`` for them with 422
(``"type": "null_not_allowed"``, #1497), so a client or a fuzzer that trusted
the document sent requests the API refused. Each of the four is now an
``anyOf`` of the five JSON types a flag value can be (boolean, string, number,
object and array). The validation is unchanged.

A property is *untyped* when its schema has none of ``type``, ``anyOf``,
``oneOf``, ``allOf``, ``$ref``, ``enum`` and ``const``. That is said
positively, so a property that gains only a description, or a 3.0 keyword such
as ``nullable: false``, still counts. The read-only properties a flag request
accepts and ignores (``READ_ONLY_FIELDS``, marked ``readOnly``) are left out,
and pinned: the names left out are exactly those.

The documents are dumped in a fresh interpreter, as the snapshot test does,
and every component schema a request body reaches is walked, transitively.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest
from jsonschema import Draft202012Validator

from backend.app.schemas.feature_flag import READ_ONLY_FIELDS
from backend.tests.smoke.test_openapi_snapshot import HTTP_METHODS, _refs, dump_document

pytestmark = pytest.mark.smoke

#: The keywords that say what a value may be.
TYPING_KEYWORDS = ("type", "anyOf", "oneOf", "allOf", "$ref", "enum", "const")

#: The four properties #1571 typed.
FLAG_VALUES = [
    ("FeatureFlagCreate", "on_value"),
    ("FeatureFlagCreate", "off_value"),
    ("FeatureFlagUpdate", "on_value"),
    ("FeatureFlagUpdate", "off_value"),
]

#: A flag value is any JSON value but null.
VALID_VALUES = [True, False, "s", 1, 1.5, {}, []]

#: The operations whose request bodies take the four.
FLAG_WRITES = {"POST /api/v1/feature-flags/", "PUT /api/v1/feature-flags/{flag_id}"}


@pytest.fixture(
    scope="module",
    params=[
        pytest.param("core", id="core"),
        pytest.param("full", id="full", marks=pytest.mark.modules),
    ],
)
def document(request) -> Dict[str, Any]:
    return dump_document(request.param)


def _reached(document: Dict[str, Any], start: Any) -> Set[str]:
    """The component schemas *start* references, transitively."""
    schemas = document.get("components", {}).get("schemas", {})
    pending: Set[str] = set()
    _refs(start, pending)
    seen: Set[str] = set()
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            _refs(schemas.get(name), pending)
    return seen


def _request_bodies(document: Dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    for path, item in document.get("paths", {}).items():
        for method, operation in item.items():
            if method in HTTP_METHODS and "requestBody" in operation:
                yield f"{method.upper()} {path}", operation["requestBody"]


def _properties(node: Any, where: str) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Every property schema under *node*, nested ones included."""
    if isinstance(node, dict):
        for name, schema in (node.get("properties") or {}).items():
            if isinstance(schema, dict):
                yield where, name, schema
                yield from _properties(schema, f"{where}.{name}")
        for key, value in node.items():
            if key != "properties":
                yield from _properties(value, where)
    elif isinstance(node, list):
        for value in node:
            yield from _properties(value, where)


def untyped_request_properties(
    document: Dict[str, Any],
) -> Tuple[List[str], Set[str]]:
    """(the untyped request-body properties, the read-only names left out)."""
    schemas = document["components"]["schemas"]
    reached: Set[str] = set()
    for _, body in _request_bodies(document):
        reached |= _reached(document, body)
    found: List[str] = []
    read_only: Set[str] = set()
    for schema_name in sorted(reached):
        for where, name, schema in _properties(schemas[schema_name], schema_name):
            if any(keyword in schema for keyword in TYPING_KEYWORDS):
                continue
            if schema.get("readOnly") is True:
                read_only.add(name)
            else:
                found.append(f"{where}.{name}")
    return found, read_only


@pytest.mark.regression
def test_no_request_body_property_is_untyped(document):
    found, read_only = untyped_request_properties(document)
    assert found == [], f"untyped request-body properties: {found}"
    assert read_only == READ_ONLY_FIELDS


@pytest.mark.regression
@pytest.mark.parametrize(("schema_name", "field"), FLAG_VALUES)
def test_a_flag_value_allows_every_json_value_but_null(document, schema_name, field):
    schema = document["components"]["schemas"][schema_name]["properties"][field]
    assert "default" not in schema, schema
    validator = Draft202012Validator(schema)
    assert not validator.is_valid(None), f"{schema_name}.{field} allows null"
    refused = [value for value in VALID_VALUES if not validator.is_valid(value)]
    assert refused == [], f"{schema_name}.{field} refuses {refused}"


def test_only_the_two_flag_writes_take_the_four(document):
    """The stability row names exactly these two operations."""
    taking = {
        label
        for label, body in _request_bodies(document)
        if _reached(document, body) & {"FeatureFlagCreate", "FeatureFlagUpdate"}
    }
    assert taking == FLAG_WRITES


def test_the_walk_finds_an_untyped_property():
    """Positive control: a planted untyped property, nested in a referenced
    schema, and a read-only one are each found."""
    planted = {
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Outer"}
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Outer": {
                    "properties": {
                        "inner": {"$ref": "#/components/schemas/Inner"},
                        "id": {"readOnly": True, "title": "Id"},
                    }
                },
                "Inner": {
                    "properties": {
                        "loose": {"title": "Loose", "nullable": False},
                        "typed": {"type": "string"},
                    }
                },
            }
        },
    }
    assert untyped_request_properties(planted) == (["Inner.loose"], {"id"})
