"""Nothing writes a flag's split (#2012): the columns are stored and not used.

``t2012_flag_split`` adds ``feature_flags.split`` and
``flag_environment_settings.split``, before anything can set one:

* no request field sets a split or a rule's own value, and no response carries
  a split;
* in the application's code the only place a split is assigned is
  ``FeatureFlagService.update_feature_flag``, which no caller passes one to,
  and the only place one is read is the view of a flag in an environment;
* the generic writer that would copy any column, ``crud_feature_flag``, is
  used for reading only.

The scan reads every Python file of the application (core and modules), the
Lambda functions and the scripts, through ``ast``, and compares with an exact
list, so a writer added anywhere fails here until it is meant.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from pydantic import ValidationError

from backend.app.schemas.feature_flag import (
    FeatureFlagCreate,
    FeatureFlagListItem,
    FeatureFlagRead,
    FeatureFlagUpdate,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
#: Every directory whose code runs against the database, from parts: a core
#: tree has no modules.
ROOTS = (
    REPO_ROOT / "backend" / "app",
    REPO_ROOT / "backend" / "lambda",
    REPO_ROOT / "backend" / "scripts",
    REPO_ROOT / "modules" / "backend" / "app",
    REPO_ROOT / "modules" / "lambda",
)
#: The column's name.
SPLIT = "split"


@functools.lru_cache(maxsize=1)
def _sources() -> Dict[str, ast.AST]:
    """``path -> parsed source`` for every file, the migrations left out: they
    write the column's definition, not a flag's split."""
    found = {}
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")) if root.is_dir() else ():
            if "migrations" in path.parts:
                continue
            name = str(path.relative_to(REPO_ROOT))
            found[name] = ast.parse(path.read_text(encoding="utf-8"))
    return found


def uses(tree: ast.AST) -> List[str]:
    """Each way *tree* could write a column called ``split``: an attribute
    assigned, a keyword argument, the name as a string (``setattr``, a dict
    key of ``.values(...)``), and a class attribute or a parameter."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == SPLIT
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            found.append("attribute assigned")
        elif isinstance(node, ast.keyword) and node.arg == SPLIT:
            found.append("keyword argument")
        elif isinstance(node, ast.Constant) and node.value == SPLIT:
            found.append("string")
        elif isinstance(node, ast.Name) and node.id == SPLIT:
            if isinstance(node.ctx, ast.Store):
                found.append("name assigned")
        elif isinstance(node, ast.arg) and node.arg == SPLIT:
            found.append("parameter")
    return sorted(found)


#: Every use there is. ``models/feature_flag.py`` declares the two columns;
#: ``core/flag_environments.py`` names the field in the two lists and the
#: mapping of the read-only view; the service takes the internal parameter and
#: assigns it. ``core/balanced_blocks.py``'s keyword fills a message about an
#: experiment's traffic split, not a column.
EXPECTED: Dict[str, List[str]] = {
    "backend/app/core/balanced_blocks.py": ["keyword argument"],
    "backend/app/core/flag_environments.py": ["string"] * 3,
    "backend/app/models/feature_flag.py": ["name assigned"] * 2,
    "backend/app/services/feature_flag_service.py": ["attribute assigned", "parameter"],
}


def test_the_only_writer_is_the_services_unused_parameter():
    sources = _sources()
    assert len(sources) > 200, "the scan found too few files: it is broken"
    found = {name: uses(tree) for name, tree in sources.items()}
    assert {name: kinds for name, kinds in found.items() if kinds} == EXPECTED


@pytest.mark.parametrize(
    "planted, kinds",
    [
        ("flag.split = body.split\n", ["attribute assigned"]),
        ("flag.split: list = []\n", ["attribute assigned"]),
        ("del flag.split\n", ["attribute assigned"]),
        ("FeatureFlag(key='k', split=parts)\n", ["keyword argument"]),
        ("service.update_feature_flag(flag, data, split=None)\n", ["keyword argument"]),
        ("setattr(flag, 'split', parts)\n", ["string"]),
        ("update(FeatureFlag).values({'split': parts})\n", ["string"]),
        ("class Body:\n    split: list\n", ["name assigned"]),
        ("'a b'.split(' ')\nparts = flag.split\n", []),
    ],
)
def test_the_scan_sees_each_way_of_writing_one(planted, kinds):
    assert uses(ast.parse(planted)) == kinds


def _calls(sources: Dict[str, ast.AST], method: str) -> List[Tuple[str, ast.Call]]:
    return [
        (name, node)
        for name, tree in sources.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    ]


def test_no_caller_passes_the_service_a_split():
    calls = _calls(_sources(), "update_feature_flag")
    assert calls, "the scan found no caller: it is broken"
    # A bare ``**`` is a keyword whose name is None: it could carry one.
    passed = [
        (name, [keyword.arg for keyword in call.keywords]) for name, call in calls
    ]
    assert all(keywords in ([], ["record"]) for _, keywords in passed), passed


def test_the_generic_writer_is_used_for_reading_only():
    used = {
        node.attr
        for tree in _sources().values()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "crud_feature_flag"
    }
    assert used == {"count", "get_multi"}


PARTS = [{"value": "a", "weight": 50}, {"value": "b", "weight": 50}]


@pytest.mark.parametrize("sent", [PARTS, [], None], ids=["parts", "empty", "null"])
@pytest.mark.parametrize("model", [FeatureFlagCreate, FeatureFlagUpdate])
def test_a_request_that_sends_a_split_is_refused(model, sent):
    body = {
        "key": "k",
        "name": "n",
        "value_type": "string",
        "on_value": "a",
        "off_value": "b",
        SPLIT: sent,
    }
    with pytest.raises(ValidationError) as refused:
        model.model_validate(body)
    assert [(e["type"], e["loc"]) for e in refused.value.errors()] == [
        ("extra_forbidden", (SPLIT,))
    ]


@pytest.mark.parametrize(
    "model",
    [FeatureFlagCreate, FeatureFlagUpdate, FeatureFlagRead, FeatureFlagListItem],
)
def test_no_request_or_response_model_has_the_field(model):
    assert SPLIT not in model.model_fields


CONDITION = {"attribute": "plan", "operator": "eq", "value": "pro"}
RULE = {"id": "pro", "rule": {"operator": "and", "conditions": [CONDITION]}}
DASHBOARD = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [{**CONDITION, "operator": "equals"}],
        }
    ],
}
#: Each place a rule's own value would be sent, beside the rules without one.
RULE_VALUES = {
    "dashboard": (DASHBOARD, {**DASHBOARD, "value": "B"}),
    "native": ({"rules": [RULE]}, {"rules": [{**RULE, "value": "B"}]}),
    "native-default": (
        {"rules": [RULE], "default_rule": {**RULE, "id": "all"}},
        {"rules": [RULE], "default_rule": {**RULE, "id": "all", "value": "B"}},
    ),
}


@pytest.mark.parametrize("shape", sorted(RULE_VALUES))
@pytest.mark.parametrize("model", [FeatureFlagCreate, FeatureFlagUpdate])
def test_a_request_that_sends_a_rule_value_is_refused(model, shape):
    accepted, refused = RULE_VALUES[shape]
    body = {
        "key": "k",
        "name": "n",
        "value_type": "string",
        "on_value": "a",
        "off_value": "b",
    }
    model.model_validate({**body, "targeting_rules": accepted})
    with pytest.raises(ValidationError, match="unknown key") as raised:
        model.model_validate({**body, "targeting_rules": refused})
    assert [e["loc"] for e in raised.value.errors()] == [("targeting_rules",)]


def reads(tree: ast.AST) -> int:
    """How many times *tree* reads an attribute called ``split`` that it does
    not call (``"a b".split(...)`` is a call)."""
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    return sum(
        isinstance(node, ast.Attribute)
        and node.attr == SPLIT
        and isinstance(node.ctx, ast.Load)
        and id(node) not in called
        for node in ast.walk(tree)
    )


def test_the_only_reader_is_the_environment_view():
    found = {name: reads(tree) for name, tree in _sources().items()}
    assert {name: count for name, count in found.items() if count} == {
        "backend/app/core/flag_environments.py": 1
    }
    assert reads(ast.parse("value = flag.split\nwords = 'a b'.split(' ')\n")) == 1
