"""The Redis subnet group name behind the CDK page's known-issue note.

Up to v0.45.0 the ElastiCache stack names its Redis subnet group with the
expression ``V0450`` below, cut to 40 characters. For ``prod`` and ``demo`` the cut
ends in a hyphen, which ElastiCache refuses, so ``cdk deploy`` fails at
``experimentation-redis-<env>``. The note gives a one-line change, ``FIXED``. Held
here, from the expressions themselves (the two forms are quoted from the note):

* ``V0450`` gives a name that ends in a hyphen for ``prod`` and ``demo`` and not
  for ``staging`` and ``dev``;
* ``FIXED`` gives a valid name for all four: it starts with a letter, holds ASCII
  letters, digits and hyphens, has no trailing or double hyphen, and is at most 255
  characters;
* the stack's current expression is ``V0450`` or ``FIXED``, so this passes before
  and after the fix ships, and fails when a third form is written without the note
  being updated;
* the installer builds ``staging``'s shape, whose names carry no trailing hyphen.

The stack is parsed, never imported, so no CDK library is needed and the test runs
in a copy of the tree with no git. Reads files only; no network, no AWS.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[4]
STACK = ROOT / "infrastructure/cdk/stacks/elasticache_redis_stack.py"
APP = ROOT / "infrastructure/cdk/app.py"
INSTALLER = ROOT / "deploy/aws-installer/controlvar-aws"

V0450 = 'f"{construct_id}-redis-subnet-group".lower().replace("_", "-")[:40]'
FIXED = (
    'f"{construct_id}-redis-subnet-group".lower().replace("_", "-")[:40].rstrip("-")'
)
ENVIRONMENTS = ("prod", "demo", "staging", "dev")


def _expr(code: str) -> ast.expr:
    return ast.parse(code, mode="eval").body


def _same(a: ast.expr, b: ast.expr) -> bool:
    return ast.dump(a) == ast.dump(b)


def _subnet_group_name_node(source: str) -> ast.expr:
    found = []
    for call in ast.walk(ast.parse(source)):
        if isinstance(call, ast.Call) and "CfnSubnetGroup" in ast.dump(call.func):
            found += [
                kw.value for kw in call.keywords if kw.arg == "cache_subnet_group_name"
            ]
    assert len(found) == 1, "expected one CfnSubnetGroup with a name"
    return found[0]


def _is_a_documented_form(node: ast.expr) -> bool:
    return _same(node, _expr(V0450)) or _same(node, _expr(FIXED))


def _name(node: ast.expr, environment: str) -> str:
    code = compile(ast.Expression(node), str(STACK), "eval")
    return eval(
        code,
        {"__builtins__": {}},
        {"construct_id": f"experimentation-redis-{environment}"},
    )


def test_the_stack_ids_are_experimentation_redis_env() -> None:
    assert 'f"experimentation-redis-{env_name}"' in APP.read_text(encoding="utf-8")


def test_v0450_ends_in_a_hyphen_for_prod_and_demo_only() -> None:
    names = {e: _name(_expr(V0450), e) for e in ENVIRONMENTS}
    assert {e: n.endswith("-") for e, n in names.items()} == {
        "prod": True,
        "demo": True,
        "staging": False,
        "dev": False,
    }
    assert all(len(n) == 40 for n in names.values())


def test_the_documented_change_gives_a_valid_name_in_every_environment() -> None:
    for environment in ENVIRONMENTS:
        name = _name(_expr(FIXED), environment)
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(-[A-Za-z0-9]+)*", name), name
        assert len(name) <= 255


def test_the_stacks_expression_is_the_v0450_form_or_the_fixed_form() -> None:
    node = _subnet_group_name_node(STACK.read_text(encoding="utf-8"))
    assert _is_a_documented_form(node), ast.unparse(node)


def test_a_third_form_is_refused() -> None:
    source = STACK.read_text(encoding="utf-8")
    node = _subnet_group_name_node(source)
    third = _expr(ast.unparse(node).replace("[:40]", "[:39]"))
    assert not _is_a_documented_form(third)
    assert not _is_a_documented_form(_expr(V0450.replace("[:40]", '[:40].strip("-")')))


def test_the_installer_builds_stagings_shape() -> None:
    assert "staging's shape" in INSTALLER.read_text(encoding="utf-8")
