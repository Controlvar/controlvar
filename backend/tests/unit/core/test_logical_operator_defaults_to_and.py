"""A targeting rule's ``logical_operator`` left out is ``AND``, at the top
level and on each group, as in the dashboard shape the API stores."""

import pytest

from backend.app.core.targeting_adapter import (
    expand_context,
    match_targeting_rule,
    normalise_targeting_rules,
)

pytestmark = [pytest.mark.unit]

US = {"attribute": "country", "operator": "equals", "value": "US"}
PRO = {"attribute": "plan", "operator": "equals", "value": "pro"}
FREE = {"attribute": "plan", "operator": "equals", "value": "free"}


def _matches(rules, context) -> bool:
    return (
        match_targeting_rule(normalise_targeting_rules(rules), expand_context(context))
        is not None
    )


def test_a_group_without_an_operator_needs_every_condition():
    rules = {"groups": [{"conditions": [US, PRO]}]}
    assert _matches(rules, {"country": "US", "plan": "pro"})
    assert not _matches(rules, {"country": "US", "plan": "free"})


def test_groups_without_a_top_level_operator_must_all_match():
    rules = {"groups": [{"conditions": [US]}, {"conditions": [FREE]}]}
    assert _matches(rules, {"country": "US", "plan": "free"})
    assert not _matches(rules, {"country": "US", "plan": "pro"})
