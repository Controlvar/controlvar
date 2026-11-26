"""A native ``semantic_version`` value that is a version followed by a newline
is refused when rules are saved (#1836).

The request model's pattern ends in ``$``, which matches before one final
newline, so ``1.2.3\\n`` was saved; the engine's version parser refuses it, so
the condition never held (under ``NOT`` it held for every user, and a
``default_rule``'s conditions are not read at all). The write path now refuses
it with the condition's path. Pinned here:

* a save refuses it, on flags and experiments, at each position, and accepts
  the same value without the newline;
* ``banana``, two newlines and a leading ``v`` keep the model's message; an
  unknown comparison keeps its own reason first;
* the dashboard shape strips the value and is still accepted; segments are
  not affected;
* stored rules keep their answers on every reader, and the SDK ruleset entry
  is unchanged: the model's pattern is not tightened and the engine does not
  strip the value;
* the check script lists a stored one with the same text;
* only the request schemas, the environment routes' re-check and the check
  script reach the walker, never a read.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

import pytest

from backend.app.core import targeting_adapter
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.sdk_ruleset import build_flag_entry
from backend.scripts import check_targeting_rules
from backend.tests.unit.core.test_targeting_values import (
    NOT_MATCHED,
    ROLLOUT_OFF,
    _experiment,
)
from backend.tests.unit.core.test_targeting_vocabulary import (
    EXPERIMENT,
    FLAG,
    NATIVE_KINDS,
    SEGMENT,
    US,
    VERSION,
    cond,
    dashboard,
    native,
    refusal,
)
from backend.tests.unit.services.test_experiment_flag_targeting_parity import (
    flag_detailed,
)

pytestmark = pytest.mark.unit

REASON = (
    "not a version; use a version such as 1.2.3 or 1.2.3-beta.1, with no line "
    "break at the end"
)
VERSIONS = ("1.2.3", "1.2.3-beta", "1.2.3+b", "0.0.0")


def _semver(value: Any, comparison: Any = "gte") -> Dict[str, Any]:
    return cond("semantic_version", value, comparison, attribute="v")


def _only(condition):
    return native(condition)


def _second(condition):
    return {
        "rules": [
            {
                "id": "r",
                "rule": {"operator": "or", "conditions": [US, condition]},
                "rollout_percentage": 100,
                "priority": 0,
            }
        ]
    }


def _not(condition):
    return {
        "rules": [
            {
                "id": "r",
                "rule": {"operator": "not", "conditions": [condition]},
                "rollout_percentage": 100,
                "priority": 0,
            }
        ]
    }


def _nested(condition):
    return native(US, groups=[{"operator": "not", "conditions": [condition]}])


def _default_rule(condition):
    return native(
        US,
        default_rule={
            "id": "d",
            "rollout_percentage": 100,
            "rule": {"operator": "and", "conditions": [condition]},
        },
    )


#: name -> (builder, path of the condition).
POSITIONS = {
    "only": (_only, "rules[0].rule.conditions[0]"),
    "second": (_second, "rules[0].rule.conditions[1]"),
    "not": (_not, "rules[0].rule.conditions[0]"),
    "nested": (_nested, "rules[0].rule.groups[0].conditions[0]"),
    "default_rule": (_default_rule, "default_rule.rule.conditions[0]"),
}


def _rows() -> List[Tuple[str, str, str]]:
    return [
        (kind, version, position)
        for kind in NATIVE_KINDS
        for version in VERSIONS
        for position in POSITIONS
    ]


@pytest.mark.regression
@pytest.mark.parametrize("kind, version, position", _rows())
def test_a_version_followed_by_a_newline_is_refused_on_save(kind, version, position):
    build, path = POSITIONS[position]
    assert refusal(kind, build(_semver(version + "\n"))) == f"{path}.value: {REASON}"
    assert refusal(kind, build(_semver(version))) is None


@pytest.mark.regression
@pytest.mark.parametrize("kind", NATIVE_KINDS)
@pytest.mark.parametrize("comparison", [None, "eq", "gt", "gte", "lt", "lte"])
def test_every_comparison_is_refused_with_a_newline(kind, comparison):
    message = refusal(kind, _only(_semver("1.2.3\n", comparison)))
    assert message == f"rules[0].rule.conditions[0].value: {REASON}"


@pytest.mark.parametrize("kind", NATIVE_KINDS)
@pytest.mark.parametrize("value", ["banana\n", "1.2.3\n\n", "v1.2.3\n", "banana"])
def test_other_values_keep_the_models_message(kind, value):
    message = refusal(kind, _only(_semver(value)))
    assert message == "rules[0].rule.conditions[0]: not valid"


@pytest.mark.parametrize("kind", NATIVE_KINDS)
def test_an_unknown_comparison_keeps_its_own_reason_first(kind):
    message = refusal(kind, _only(_semver("1.2.3\n", "banana")))
    assert message == f"rules[0].rule.conditions[0].additional_value: {VERSION}"


@pytest.mark.regression
@pytest.mark.parametrize("kind", [FLAG, EXPERIMENT, SEGMENT])
@pytest.mark.parametrize("value", ["1.2.3\n", " 1.2.3", "17.4", "v1.2.3", "1.2.3"])
def test_the_dashboard_shape_is_unaffected(kind, value):
    condition = {"attribute": "v", "operator": "semver_gte", "value": value}
    assert refusal(kind, dashboard(condition)) is None


def test_the_reason_is_one_line_of_at_most_200_characters():
    assert targeting_adapter.VERSION_VALUE_REASON == REASON
    assert "\n" not in REASON and len(REASON) <= 200
    assert targeting_adapter.VERSION_VALUE_REASON in targeting_adapter.VALUE_REASONS


# ---------------------------------------------------------------------------
# Stored rules keep their answers
# ---------------------------------------------------------------------------

#: The version in the context is above the stored one under ``gte``: a fix
#: that strips the newline would make the condition hold.
CONTEXTS = {
    "US": {"country": "US", "v": "2.0.0"},
    "DE": {"country": "DE", "v": "2.0.0"},
}
MATCHED = {"enabled": True, "reason": "targeting_rule", "rule_id": "r"}
MATCHED_DEFAULT = {"enabled": True, "reason": "targeting_rule", "rule_id": "d"}
YES = (True, "Matched targeting rule: r")
YES_DEFAULT = (True, "Matched targeting rule: d")

#: position -> {country: (flag answer, experiment answer)}.
STORED_ANSWERS = {
    "only": {"US": (ROLLOUT_OFF, NOT_MATCHED), "DE": (ROLLOUT_OFF, NOT_MATCHED)},
    "second": {"US": (MATCHED, YES), "DE": (ROLLOUT_OFF, NOT_MATCHED)},
    "not": {"US": (MATCHED, YES), "DE": (MATCHED, YES)},
    "nested": {"US": (MATCHED, YES), "DE": (ROLLOUT_OFF, NOT_MATCHED)},
    "default_rule": {"US": (MATCHED, YES), "DE": (MATCHED_DEFAULT, YES_DEFAULT)},
}


@pytest.mark.regression
@pytest.mark.parametrize("country", ["US", "DE"])
@pytest.mark.parametrize("position", POSITIONS)
def test_a_stored_version_with_a_newline_keeps_its_answer(position, country):
    """Written without the save's check, as stored before this change."""
    stored = POSITIONS[position][0](_semver("1.0.0\n"))
    flag_expected, experiment_expected = STORED_ANSWERS[position][country]
    answer = flag_detailed(stored, CONTEXTS[country], rollout=0)
    assert {key: answer[key] for key in flag_expected} == flag_expected
    assert _experiment(stored, CONTEXTS[country]) == experiment_expected


def _entry(raw: Any) -> Dict[str, Any]:
    flag = FeatureFlag(
        key="u15",
        name="u15",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        targeting_rules=copy.deepcopy(raw),
    )
    return build_flag_entry(flag)


@pytest.mark.regression
@pytest.mark.parametrize("position", ["only", "second", "not", "nested"])
def test_a_stored_version_with_a_newline_is_served_remotely(position):
    entry = _entry(POSITIONS[position][0](_semver("1.0.0\n")))
    assert entry["evaluation"] == "remote"


@pytest.mark.regression
def test_a_stored_default_rule_with_a_newline_keeps_its_ruleset_entry():
    entry = _entry(_default_rule(_semver("1.0.0\n")))
    assert entry["evaluation"] == "local"
    assert entry["default_rule"]["id"] == "d"
    assert entry == _entry(_default_rule(_semver("1.0.0")))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_check_script_lists_a_stored_one():
    finding = check_targeting_rules.finding_for(
        "id", "key", _second(_semver("1.0.0\n"))
    )
    assert finding is not None
    assert (finding.path, finding.reason) == (
        "rules[0].rule.conditions[1].value",
        REASON,
    )
    assert (
        check_targeting_rules.finding_for("id", "key", _second(_semver("1.0.0")))
        is None
    )
