"""Nothing sets a flag's split (#2012): a promotion copies one, and that is all.

``t2012_flag_split`` adds ``feature_flags.split`` and
``flag_environment_settings.split``, before anything can set one:

* a flag create, update, environment or promotion request refuses a split, and
  the first three a rule's own value; the flag responses have no such field;
* of what the scan sees, a split is written by a promotion alone, which copies
  it with the rest of an environment's settings: into production through
  ``FeatureFlagService.update_feature_flag``, which only ``_save`` of
  ``flag_environments.py`` passes one to, and into staging or development as
  one of ``flag_environment_service``'s ``SETTING_FIELDS``. A split comes from
  ``effective_settings``, which only the promotion (and its audit entry) calls;
* the only attribute reads of one are the view of a flag in an environment,
  ``effective_settings``, the column the ruleset's query loads and the
  stale-flag hint; ``flag_values`` reads it by name in one function, for what a
  flag serves and for its ruleset entry. No response carries one;
* the generic writer that would copy any column, ``crud_feature_flag``, is
  used for reading only.

The scan parses every Python file under :data:`ROOTS` but the migrations, and
compares what it sees, function by function, with an exact list, so a writer of
a kind it sees fails here until it is meant. It sees the attribute assigned or
deleted, the keyword argument, the name as a string, a variable, class attribute
or parameter of that name, and SQL whose text the source shows (a string, an
f-string but for its fields, or ``+`` of them) when an UPDATE assigns the column
or an INSERT's list of columns names it.

It does not see a name built at run time (``setattr(flag, "spl" + "it", parts)``),
SQL built at run time (the column in a field, a variable or a ``join``), an
INSERT that lists no columns, code outside :data:`ROOTS`, or one of these
functions called under another name.
"""

from __future__ import annotations

import ast
import functools
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import pytest
from pydantic import ValidationError

from backend.app.schemas.feature_flag import (
    FeatureFlagCreate,
    FeatureFlagListItem,
    FeatureFlagRead,
    FeatureFlagUpdate,
)
from backend.app.schemas.flag_environment import (
    FlagEnvironmentSettings,
    FlagEnvironmentUpdate,
    FlagPromotion,
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


#: SQL that writes a column called ``split`` by its name: an UPDATE's SET list,
#: up to its WHERE, that assigns it, alone or in ``(a, b) = (...)``; an INSERT
#: whose list of columns names it. ``UPDATE`` and ``SET`` may have a table
#: between them, or nothing (``ON CONFLICT ... DO UPDATE SET``).
_SQL_WRITE = re.compile(
    r"""
    \bupdate\b [^;]{0,200}? \bset\b
        (?: (?!\bwhere\b) [^;] ){0,2000}?
        (?<![\w.]) (?: "?split"? \s* =(?!=) | \( [^()=]{0,500} \bsplit\b [^()=]{0,500} \) \s* = )
    | \binsert\b \s* (?: into\b (?: (?!\b(?:values|select|default)\b) [^();] ){0,200} )?
        \( [^()]{0,2000} (?<![\w.]) "?split"? \b [^()]{0,2000} \)
    """,
    re.I | re.S | re.X,
)


def _text(node: ast.AST) -> Optional[str]:
    """The text of *node* as the source shows it, or None if it shows none: a
    string, an f-string with ``?`` for each field, or ``+`` of them with ``?``
    for an operand that is not one."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "?"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        parts = [_text(node.left), _text(node.right)]
        if parts != [None, None]:
            return "".join("?" if part is None else part for part in parts)
    return None


def sql_writes(tree: ast.AST) -> int:
    """How many of *tree*'s texts are SQL that writes the column: a text that
    does is counted once, and not again for the parts it is made of."""
    count, nodes = 0, [tree]
    while nodes:
        node = nodes.pop()
        text = _text(node)
        if text is not None and _SQL_WRITE.search(text):
            count += 1
        else:
            nodes.extend(ast.iter_child_nodes(node))
    return count


def uses(tree: ast.AST) -> List[str]:
    """Each way *tree* could write a column called ``split``: an attribute
    assigned, a keyword argument, the name as a string (``setattr``, a dict
    key of ``.values(...)``), a variable, class attribute or parameter, and SQL
    (``sql_writes``)."""
    found = ["SQL"] * sql_writes(tree)
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


#: What is outside every function of :func:`functions`.
MODULE = "<module>"


def functions(tree: ast.AST) -> Iterator[Tuple[str, ast.AST]]:
    """Each top-level function of *tree*, and each method as ``Class.method``."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{item.name}", item


def by_function(tree: ast.AST, count) -> Dict[str, List[str]]:
    """``{function: what count finds there}``, and what it finds elsewhere."""
    rest = Counter(count(tree))
    found = {}
    for name, node in functions(tree):
        if kinds := count(node):
            found[name] = kinds
            rest -= Counter(kinds)
    if rest:
        found[MODULE] = sorted(rest.elements())
    return found


ENVIRONMENTS = "backend/app/core/flag_environments.py"
VALUES = "backend/app/core/flag_values.py"
RULESET_ROUTE = "backend/app/api/v1/endpoints/sdk.py"
STALE = "backend/app/services/stale_flags.py"
ROUTES = "backend/app/api/v1/endpoints/flag_environments.py"
SETTINGS = "backend/app/services/flag_environment_service.py"
SERVICE = "backend/app/services/feature_flag_service.py"

#: Every use there is. ``models/feature_flag.py`` declares the two columns;
#: ``core/flag_environments.py`` names the field in the two lists and the
#: mapping of the read-only view. A promotion copies it: ``SETTING_FIELDS``,
#: ``NOT_CONFIGURED`` and ``effective_settings`` name it, the route passes it to
#: the service beside the request (which leaves it out, ``_as_requested``), and
#: the service takes the parameter and assigns it. ``audit_setting`` and
#: ``AUDIT_NAME_ONLY_FIELDS`` name it in an entry. ``core/balanced_blocks.py``'s
#: keyword fills a message about an experiment's traffic split, not a column.
#: Serving one writes none: ``core/flag_values.py`` reads it by name in one
#: function, and its variables and parameters are called that; the two vector
#: generators build a transient row of their own corpus, never saved.
EXPECTED: Dict[str, Dict[str, List[str]]] = {
    "backend/app/core/balanced_blocks.py": {"balance_problem": ["keyword argument"]},
    VALUES: {
        "_split_of": ["string"],
        "_unservable": ["name assigned"],
        "serve": ["name assigned"],
        "split_errors": ["parameter"],
        "split_value": ["parameter"],
    },
    "backend/scripts/generate_ruleset_vectors.py": {
        "Corpus.flag": ["parameter", "string"],
        "_multi_way_flags": ["keyword argument", "string"],
        "stored_flag": ["keyword argument", "string"],
    },
    "backend/scripts/generate_value_vectors.py": {
        "stored_flag": ["keyword argument", "string"],
        MODULE: ["string"] * 3,
    },
    ENVIRONMENTS: {
        "FlagInEnvironment.__init__": ["string"],
        MODULE: ["string", "string"],
    },
    "backend/app/models/feature_flag.py": {MODULE: ["name assigned"] * 2},
    "backend/app/services/audit_service.py": {MODULE: ["string"]},
    SERVICE: {
        "FeatureFlagService.update_feature_flag": ["attribute assigned", "parameter"]
    },
    SETTINGS: {
        "audit_setting": ["string"],
        "effective_settings": ["string"],
        MODULE: ["string", "string"],
    },
    ROUTES: {
        "_as_requested": ["string"],
        "_save": ["keyword argument", "string"],
    },
}


def test_the_only_writer_is_a_promotion():
    sources = _sources()
    assert len(sources) > 200, "the scan found too few files: it is broken"
    found = {name: by_function(tree, uses) for name, tree in sources.items()}
    assert {name: kinds for name, kinds in found.items() if kinds} == EXPECTED


def test_what_is_found_is_told_by_function():
    planted = ast.parse(
        "flag.split = 1\n"
        "def f(split):\n    def g():\n        flag.split = 2\n"
        "class C:\n    split = 3\n    async def m(self):\n        setattr(x, 'split', 4)\n"
    )
    assert by_function(planted, uses) == {
        "f": ["attribute assigned", "parameter"],
        "C.m": ["string"],
        MODULE: ["attribute assigned", "name assigned"],
    }


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
        pytest.param(
            """db.execute(text("UPDATE feature_flags SET split = '[]'"))\n""",
            ["SQL"],
            id="update",
        ),
        pytest.param(
            'sql = f"""update only {schema}.feature_flags as f\n'
            "SET rollout_percentage = 5, split='{parts}'\n"
            'WHERE f.key = {key}"""\n',
            ["SQL"],
            id="update-f-string",
        ),
        pytest.param(
            "sql = 'UPDATE ' + table + ' SET ' + '\"split\" = NULL'\n",
            ["SQL"],
            id="update-added",
        ),
        pytest.param(
            "sql = 'UPDATE t ' 'SET a = 1, ' 'split = 2'\n",
            ["SQL"],
            id="update-adjacent",
        ),
        pytest.param(
            "sql = 'UPDATE t SET split = %s' % parts\n", ["SQL"], id="update-percent"
        ),
        pytest.param(
            "sql = 'UPDATE t SET (a, split) = (1, 2)'\n", ["SQL"], id="update-list"
        ),
        pytest.param(
            "sql = 'INSERT INTO flags (id, split) VALUES (1, 2)'\n",
            ["SQL"],
            id="insert",
        ),
        pytest.param(
            "sql = 'INSERT INTO t (id) VALUES (1) ON CONFLICT (id) '\\\n"
            "    'DO UPDATE SET split = EXCLUDED.split'\n",
            ["SQL"],
            id="upsert",
        ),
        # Read, not written.
        pytest.param(
            "sql = 'SELECT split FROM feature_flags WHERE split = 1'\n",
            [],
            id="select",
        ),
        pytest.param(
            "sql = 'UPDATE t SET a = split_part(b, c, 1) WHERE split = 2'\n",
            [],
            id="update-where",
        ),
        pytest.param(
            "sql = \"INSERT INTO t (a) VALUES ('split')\"\n", [], id="insert-value"
        ),
        # Not seen: the name, or the SQL, is built at run time.
        pytest.param(
            "setattr(flag, 'spl' + 'it', parts)\n", [], id="unseen-computed-setattr"
        ),
        pytest.param(
            "sql = f'UPDATE feature_flags SET {column} = 1'\n",
            [],
            id="unseen-column-in-a-field",
        ),
        pytest.param(
            "sql = 'INSERT INTO feature_flags VALUES (1, 2, 3)'\n",
            [],
            id="unseen-insert-without-columns",
        ),
    ],
)
def test_the_scan_sees_each_way_of_writing_one(planted, kinds):
    assert uses(ast.parse(planted)) == kinds


def calls(called: str) -> Dict[Tuple[str, str], List[ast.Call]]:
    """Each call of *called*, as a name or an attribute, by file and function."""
    found: Dict[Tuple[str, str], List[ast.Call]] = {}
    for name, tree in _sources().items():
        inside = {
            id(node): function
            for function, body in functions(tree)
            for node in ast.walk(body)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if called in (
                getattr(node.func, "id", None),
                getattr(node.func, "attr", None),
            ):
                where = (name, inside.get(id(node), MODULE))
                found.setdefault(where, []).append(node)
    return found


def test_only_a_promotion_passes_the_service_a_split():
    """``_save`` passes one: the split in *changes*, and only a promotion's
    changes have one (the test after this)."""
    # A bare ``**`` is a keyword whose name is None: it could carry one.
    passed = {
        where: [[keyword.arg for keyword in call.keywords] for call in found]
        for where, found in calls("update_feature_flag").items()
    }
    assert passed == {
        ("backend/app/api/v1/endpoints/feature_flags.py", "update_feature_flag"): [
            ["record"]
        ],
        (ROUTES, "_save"): [["record", "split"]],
    }


def callers(called: str) -> Set[Tuple[str, str]]:
    return set(calls(called))


def test_a_split_reaches_a_writer_from_a_promotion_alone():
    """The writers of a flag's settings are given a request's fields, which have
    no split, or a promotion's, which ``effective_settings`` reads."""
    assert callers("effective_settings") == {
        (ROUTES, "promote_flag_environment"),
        (SETTINGS, "audit_setting"),
    }
    assert callers("_save") == {
        (ROUTES, "promote_flag_environment"),
        (ROUTES, "update_flag_environment"),
    }
    assert callers("save_environment_setting") == {(ROUTES, "_save")}


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


@pytest.mark.parametrize("sent", [PARTS, [], None], ids=["parts", "empty", "null"])
@pytest.mark.parametrize(
    "model, body",
    [
        (FlagEnvironmentUpdate, {"rollout_percentage": 5}),
        (FlagPromotion, {"from": "staging"}),
    ],
    ids=["environment", "promotion"],
)
def test_an_environment_request_that_sends_a_split_is_refused(model, body, sent):
    model.model_validate(body)
    with pytest.raises(ValidationError) as refused:
        model.model_validate({**body, SPLIT: sent})
    assert [(e["type"], e["loc"]) for e in refused.value.errors()] == [
        ("extra_forbidden", (SPLIT,))
    ]


@pytest.mark.parametrize(
    "model",
    [
        FeatureFlagCreate,
        FeatureFlagUpdate,
        FeatureFlagRead,
        FeatureFlagListItem,
        FlagEnvironmentUpdate,
        FlagEnvironmentSettings,
        FlagPromotion,
    ],
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


@pytest.mark.parametrize("shape", sorted(RULE_VALUES))
def test_an_environment_request_that_sends_a_rule_value_is_refused(shape):
    accepted, refused = RULE_VALUES[shape]
    FlagEnvironmentUpdate.model_validate({"targeting_rules": accepted})
    with pytest.raises(ValidationError, match="unknown key") as raised:
        FlagEnvironmentUpdate.model_validate({"targeting_rules": refused})
    assert [e["loc"] for e in raised.value.errors()] == [("targeting_rules",)]


def reads(tree: ast.AST) -> List[str]:
    """``read``, once for each attribute called ``split`` that *tree* reads and
    does not call (``"a b".split(...)`` is a call)."""
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    return [
        "read"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == SPLIT
        and isinstance(node.ctx, ast.Load)
        and id(node) not in called
    ]


def test_the_only_readers_are_the_environment_view_a_promotion_and_serving():
    """Serving one: the ruleset's query loads the column, the stale-flag hint
    asks whether there is one, and ``flag_values._split_of`` (in EXPECTED above,
    read by name) is the rest."""
    found = {name: by_function(tree, reads) for name, tree in _sources().items()}
    assert {name: places for name, places in found.items() if places} == {
        ENVIRONMENTS: {"FlagInEnvironment.__init__": ["read"]},
        SETTINGS: {"effective_settings": ["read"]},
        RULESET_ROUTE: {"_ruleset_response": ["read"]},
        STALE: {"_one_answer": ["read"]},
    }
    assert reads(ast.parse("value = flag.split\nwords = 'a b'.split(' ')\n")) == [
        "read"
    ]
