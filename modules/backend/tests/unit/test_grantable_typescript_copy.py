"""The Roles grid offers exactly what a custom role or grant can hold (#891, check #43).

``GRANTABLE_GROUPS`` in
``modules/frontend/src/components/admin/roles/grantable.ts`` is the
dashboard's copy of ``backend.app.core.permissions.GRANTABLE``: the grid offers
exactly its pairs, and the request body is built from them. This test reads
the TypeScript literal as text and compares the pairs with the backend's
set, so a pair added or dropped on one side only fails here instead of
offering a checkbox the API refuses with a 422 (or hiding one it accepts).

It lives with the modules' unit tests because it reads ``modules/``; a core
test may not name a module's path.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import pytest

from backend.app.core.permissions import GRANTABLE

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
GRANTABLE_TS = (
    REPO
    / "modules"
    / "frontend"
    / "src"
    / "components"
    / "admin"
    / "roles"
    / "grantable.ts"
)

Pair = Tuple[str, str]

_LITERAL = re.compile(r"export const GRANTABLE_GROUPS\b[^=]*=\s*\[(.*?)\n\];", re.S)
_GROUP = re.compile(r"\{([^{}]*)\}")
_RESOURCE = re.compile(r"\bresource:\s*'([^']*)'")
_ACTIONS = re.compile(r"\bactions:\s*\[([^\]]*)\]")
_STRING = re.compile(r"'([^']*)'")


def typescript_pairs(text: str) -> List[Pair]:
    """Every (resource, action) of the ``GRANTABLE_GROUPS`` literal, in order."""
    literal = _LITERAL.search(text)
    assert literal, f"GRANTABLE_GROUPS not found in {GRANTABLE_TS.relative_to(REPO)}"
    pairs: List[Pair] = []
    for group in _GROUP.finditer(literal.group(1)):
        resource = _RESOURCE.search(group.group(1))
        actions = _ACTIONS.search(group.group(1))
        assert resource and actions, (
            f"a group without resource or actions: {group.group(0)!r}"
        )
        pairs.extend((resource.group(1), a) for a in _STRING.findall(actions.group(1)))
    return pairs


def _diff(found: List[Pair]) -> str:
    extra = sorted(set(found) - GRANTABLE)
    missing = sorted(GRANTABLE - set(found))
    return f"in the TypeScript copy only: {extra}; in GRANTABLE only: {missing}"


def test_the_typescript_copy_equals_grantable():
    found = typescript_pairs(GRANTABLE_TS.read_text("utf-8"))
    assert set(found) == GRANTABLE, _diff(found)
    assert len(found) == len(set(found)), f"a pair is listed twice: {found}"


def test_eleven_pairs_on_both_sides():
    # The control: an empty parse and an empty set would compare equal.
    assert len(GRANTABLE) == 11
    assert len(typescript_pairs(GRANTABLE_TS.read_text("utf-8"))) == 11


def test_a_pair_added_to_the_copy_is_named():
    # The positive control, on the file's own text with one pair planted.
    text = GRANTABLE_TS.read_text("utf-8")
    planted = text.replace("actions: ['read'] }", "actions: ['read', 'list'] }", 1)
    assert planted != text, (
        "the audit_log group's literal changed shape; update the plant"
    )
    found = typescript_pairs(planted)
    assert set(found) != GRANTABLE
    assert "('audit_log', 'list')" in _diff(found)
