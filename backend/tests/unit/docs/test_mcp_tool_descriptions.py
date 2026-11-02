"""The MCP server's tool descriptions name only fields the API has (C13).

A tool description is read by a model, which then looks for the fields it
names: one that names a field the API does not return is a wrong answer
waiting to happen. ``sdk/mcp/tools.json`` is what the server answers to
``tools/list``; ``docs/api/openapi-v1.full.json`` is the API's own document.
Every name a description puts in backticks must be one of that tool's input
properties, or a property of the response schema of the route the tool calls
(at any depth). A route whose 200 response is an open object (the evaluate
route) is read from the payload its operation's description documents,
``{"key", "enabled", ...}``.

Also, each tool calls a route the document has, with that method, and that
route requires a credential (Q3's unit cross-check).

It is a sweep, not a proof: a field named without backticks is not read.
Reads files only; no git, so it runs the same in ``scripts/core_build.sh``'s
copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOLS = REPO_ROOT / "sdk" / "mcp" / "tools.json"
OPENAPI = REPO_ROOT / "docs" / "api" / "openapi-v1.full.json"

#: The route each tool calls: method and path, as the document spells them.
ROUTES: Dict[str, Tuple[str, str]] = {
    "list_feature_flags": ("get", "/api/v1/feature-flags/"),
    "get_feature_flag": ("get", "/api/v1/feature-flags/"),
    "evaluate_feature_flag": ("post", "/api/v1/feature-flags/evaluate/{flag_key}"),
    "list_experiments": ("get", "/api/v1/experiments/"),
    "get_experiment": ("get", "/api/v1/experiments/{experiment_id}"),
    "get_experiment_results": ("get", "/api/v1/experiments/{experiment_id}/results"),
}

BACKTICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{0,60})`")
#: The payload an operation's description documents: {"a", "b", ...}.
PAYLOAD = re.compile(r'\{("[a-z_]{1,40}"(?:, "[a-z_]{1,40}"){0,30})\}')


def tools() -> List[dict]:
    return json.loads(TOOLS.read_text(encoding="utf-8"))["tools"]


def document() -> dict:
    return json.loads(OPENAPI.read_text(encoding="utf-8"))


def _properties(schema: object, doc: dict, seen: Set[str]) -> Iterator[str]:
    """Every property name in a schema, following $ref, compositions and items."""
    if isinstance(schema, list):
        for item in schema:
            yield from _properties(item, doc, seen)
        return
    if not isinstance(schema, dict):
        return
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        if name not in seen:
            seen.add(name)
            yield from _properties(doc["components"]["schemas"][name], doc, seen)
    for name, inner in (schema.get("properties") or {}).items():
        yield name
        yield from _properties(inner, doc, seen)
    for key in ("anyOf", "allOf", "oneOf", "items", "additionalProperties"):
        if key in schema:
            yield from _properties(schema[key], doc, seen)


def response_fields(tool: str, doc: dict) -> Set[str]:
    method, path = ROUTES[tool]
    operation = doc["paths"][path][method]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    fields = set(_properties(schema, doc, set()))
    if not fields:
        documented = PAYLOAD.search(operation.get("description", ""))
        assert documented, (
            f"{method.upper()} {path}'s response declares no property and its "
            "description documents no payload, so the fields cannot be checked"
        )
        fields = {name.strip('"') for name in documented.group(1).split(", ")}
    return fields


def problems(served: List[dict], doc: dict) -> List[str]:
    found = []
    for tool in served:
        name = tool["name"]
        allowed = set(tool["inputSchema"].get("properties") or {}) | response_fields(
            name, doc
        )
        for field in BACKTICKED.findall(tool["description"]):
            if field not in allowed:
                method, path = ROUTES[name]
                found.append(
                    f"{name} names `{field}`, which neither its input nor "
                    f"{method.upper()} {path}'s response has"
                )
    return found


def test_the_tools_are_the_six_this_file_reads():
    assert [tool["name"] for tool in tools()] == list(ROUTES)


def test_every_field_a_description_names_exists():
    found = problems(tools(), document())
    assert not found, "\n".join(found)


def test_the_check_reads_what_it_exists_for():
    served = tools()
    assert sum(len(BACKTICKED.findall(tool["description"])) for tool in served) >= 20
    results = next(tool for tool in served if tool["name"] == "get_experiment_results")
    results["description"] += " Check `xyz` too."
    assert problems(served, document()) == [
        "get_experiment_results names `xyz`, which neither its input nor "
        "GET /api/v1/experiments/{experiment_id}/results's response has"
    ]


def test_the_evaluate_fields_come_from_the_documented_payload():
    assert response_fields("evaluate_feature_flag", document()) == {
        "key",
        "enabled",
        "value",
        "value_type",
        "config",
        "reason",
    }


@pytest.mark.parametrize("tool", list(ROUTES))
def test_each_tool_calls_a_documented_route_that_needs_a_credential(tool):
    method, path = ROUTES[tool]
    operation = document()["paths"].get(path, {}).get(method)
    assert operation is not None, (
        f"{tool} calls {method.upper()} {path}, which the document lacks"
    )
    assert operation.get("security"), (
        f"{method.upper()} {path} has no security requirement"
    )
