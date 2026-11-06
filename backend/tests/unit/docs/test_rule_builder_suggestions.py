"""The rule builder suggests only attributes the docs tell an app to send (#1775).

The dashboard's targeting rule builder offers a list of attributes to pick
from (``COMMON_ATTRIBUTES`` in ``frontend/src/types/targeting.ts``). The
"Common Targeting Attributes" table in ``docs/getting-started/concepts.md``
tells an application which context keys to send. The builder suggested
``device.type`` where the table says ``device_type``, and ``app.version``
where it says ``app_version``, and the server maps neither to the other:
a rule picked from the list matched nobody whose app followed the docs, and
nothing said so. It also suggested ``session.new_user``, which no page names.

This holds the two files to each other, judged by the server's own reading
of a dashboard rule (``normalise_targeting_rules``, then
``match_targeting_rule`` over ``expand_context``), as a flag evaluation does:

* every suggestion, used as a rule with an operator the builder offers for
  its type and the text the value input sends, matches a context that sends
  one row of the table, in the table's own name, and matches no other row;
* that row's type is the suggestion's type, so the operators the builder
  offers for it fit what the application sends;
* every suggestion has its own row;
* a rule saved with the former suggestion ``device.type`` still matches an
  application that sends the nested ``{"device": {"type": "mobile"}}``: the
  fix changes what the dashboard suggests, not how the server evaluates.

Files only, no git and no network, so it runs the same in
``scripts/core_build.sh``'s copy and in the docs-only lane through its
directory, ``backend/tests/unit/docs/`` (pinned in test_docs_only_gate.py).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.app.core.targeting_adapter import (
    expand_context,
    match_targeting_rule,
    normalise_targeting_rules,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
TYPES = REPO_ROOT / "frontend" / "src" / "types" / "targeting.ts"
CONCEPTS = REPO_ROOT / "docs" / "getting-started" / "concepts.md"

SUGGESTIONS_START = "export const COMMON_ATTRIBUTES: AttributeSuggestion[] = ["
SUGGESTION = re.compile(r"\{ value: '([^']*)', label: '[^']*', type: '([a-z]+)'")
TABLE_HEADING = "### Common Targeting Attributes"
TABLE_ROW = re.compile(r"^\| `([^`]+)` \| ([a-z]+) \| [^|]+ \|$", re.MULTILINE)

#: Per type: an operator the builder offers for it (OPERATORS_BY_TYPE in
#: targeting.ts), the value as the builder's text input sends it, and the
#: value an application sends. One probe per type, so a rule on one row's
#: attribute meets the same value under every other row's name.
PROBES: Dict[str, Tuple[str, str, Any]] = {
    "string": ("equals", "mobile", "mobile"),
    "number": ("equals", "30", 30),
    "semver": ("semver_eq", "2.3.1", "2.3.1"),
    "array": ("array_contains", "beta", ["beta", "staff"]),
    "boolean": ("equals", "true", True),
}


def _suggestions() -> List[Tuple[str, str]]:
    text = TYPES.read_text("utf-8")
    assert SUGGESTIONS_START in text, f"{TYPES} has no COMMON_ATTRIBUTES list"
    block = text.split(SUGGESTIONS_START, 1)[1].split("];", 1)[0]
    found = SUGGESTION.findall(block)
    assert found, "no suggestion read from COMMON_ATTRIBUTES"
    assert len(found) == block.count("value:"), (
        f"an entry of COMMON_ATTRIBUTES is not in the form this test reads: {block}"
    )
    return found


def _documented() -> List[Tuple[str, str]]:
    text = CONCEPTS.read_text("utf-8")
    assert TABLE_HEADING in text, f"{CONCEPTS} has no {TABLE_HEADING!r}"
    section = text.split(TABLE_HEADING, 1)[1].split("\n#", 1)[0]
    rows = TABLE_ROW.findall(section)
    assert rows, "no row read from the Common Targeting Attributes table"
    assert len(rows) == section.count("\n| `"), (
        f"a row of the table is not in the form this test reads: {section}"
    )
    return rows


def _matches(attribute: str, operator: str, value: Any, context: Dict) -> bool:
    rules = normalise_targeting_rules(
        {
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {"attribute": attribute, "operator": operator, "value": value}
                    ],
                }
            ],
        },
        owner="test",
    )
    assert rules is not None, f"the server cannot read a rule on {attribute!r}"
    return match_targeting_rule(rules, expand_context(context)) is not None


def _rows_answering(attribute: str, kind: str) -> List[Tuple[str, str]]:
    """The table rows whose context a rule on ``attribute`` matches."""
    operator, rule_value, sent = PROBES[kind]
    return [
        (name, documented_type)
        for name, documented_type in _documented()
        if _matches(attribute, operator, rule_value, {name: sent})
    ]


def test_every_suggestion_matches_an_app_that_sends_what_the_docs_say():
    wrong = []
    for attribute, kind in _suggestions():
        rows = _rows_answering(attribute, kind)
        if len(rows) != 1:
            wrong.append(
                f"{attribute} ({kind}) matches {[n for n, _ in rows] or 'no'} "
                "row(s) of the docs table"
            )
        elif rows[0][1] != kind:
            wrong.append(
                f"{attribute} is a {kind} in the builder and {rows[0][0]} is a "
                f"{rows[0][1]} in the docs table"
            )
    assert not wrong, "the rule builder suggests:\n" + "\n".join(wrong)


def test_no_two_suggestions_answer_the_same_row():
    answered = [
        rows[0][0]
        for rows in (_rows_answering(a, k) for a, k in _suggestions())
        if len(rows) == 1
    ]
    assert len(answered) == len(set(answered)), answered


def test_a_rule_saved_with_device_type_still_matches_the_nested_form():
    """The builder no longer suggests ``device.type``; the server is unchanged."""
    assert _matches("device.type", "equals", "mobile", {"device": {"type": "mobile"}})
    assert not _matches("device.type", "equals", "mobile", {"device_type": "mobile"})
    assert _matches("device_type", "equals", "mobile", {"device_type": "mobile"})
