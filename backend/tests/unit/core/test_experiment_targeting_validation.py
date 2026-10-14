"""Experiment targeting rules are validated on write (#523, PR 1a).

``validate_experiment_targeting`` decides the shape with the predicate
experiment assignment uses (``_is_dashboard_rules_shape``), converts the rules
the way assignment does, and refuses what assignment would not apply as
written. Before it existed, ``PUT /experiments/{id}`` stored any dict: an
unknown operator or a blank condition made assignment ignore the rules and
admit everyone, and a flat ``{"country": ["US"]}`` was read as "no rules"
without a word in the log.

Three things are pinned here:

* the accept/refuse table, with the exact message for each refusal;
* parity: for every accepted value, what the validator returns is what
  ``AssignmentService._coerce_targeting_rules`` evaluates;
* the message never repeats the submitted value.

And, beside them, the one logging change on the read path: a non-empty dict
that yields no rules on the non-dashboard path warns once per experiment.
"""

from __future__ import annotations

import copy
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.core.targeting_adapter import (
    MAX_LIST_ITEMS,
    TargetingRulesError,
    validate_experiment_targeting,
)
from backend.app.core.validation_errors import render_validation_errors
from backend.app.schemas.experiment import ExperimentUpdate
from backend.app.services import assignment_service
from backend.app.services.assignment_service import AssignmentService

pytestmark = [pytest.mark.unit]

US = {"attribute": "country", "operator": "equals", "value": "US"}
GB = {"attribute": "country", "operator": "equals", "value": "GB"}


def dash(*groups, top="AND", **extra):
    """Dashboard rules from groups given as lists of conditions."""
    return {
        "logical_operator": top,
        "groups": [{"logical_operator": "AND", "conditions": list(g)} for g in groups],
        **extra,
    }


def cond(attribute, operator, value=None):
    return {"attribute": attribute, "operator": operator, "value": value}


NATIVE_US = {
    "version": "1.0",
    "rules": [
        {
            "id": "us_only",
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "country", "operator": "eq", "value": "US"}
                ],
            },
            "rollout_percentage": 100,
            "priority": 1,
        }
    ],
}

#: What the dashboard's rule builder sends: ``id`` keys on groups and conditions.
BUILDER = {
    "logical_operator": "AND",
    "groups": [
        {
            "id": "id-1759000000000-1",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "id-1759000000000-2",
                    "attribute": "country",
                    "operator": "in",
                    "value": "US, CA",
                },
                {
                    "id": "id-1759000000000-3",
                    "attribute": "app.version",
                    "operator": "semver_gte",
                    "value": "3.2",
                },
            ],
        }
    ],
}

ACCEPTED = {
    "None": None,
    "empty dict": {},
    "empty groups": {"groups": []},
    "empty groups with operator": {"logical_operator": "AND", "groups": []},
    "native, no rules": {"rules": []},
    "native, no rules, version": {"version": "1.0", "rules": []},
    "one group": dash([US]),
    "no top operator": {"groups": [{"conditions": [US]}]},
    "lower-case operators": {
        "logical_operator": "or",
        "groups": [{"logical_operator": "not", "conditions": [US]}],
    },
    "two groups OR": dash([US], [GB], top="OR"),
    "builder payload with id keys": BUILDER,
    "rollout_percentage 0": dash([US], rollout_percentage=0),
    "rollout_percentage 50": dash([US], rollout_percentage=50),
    "rollout_percentage 33.5": dash([US], rollout_percentage=33.5),
    "rollout_percentage 100": dash([US], rollout_percentage=100),
    "top-level id": dash([US], id="checkout-rule", rollout_percentage=50),
    "is_null": dash([cond("plan", "is_null")]),
    "in, 1000 values": dash(
        [cond("user_id", "in", [str(i) for i in range(MAX_LIST_ITEMS)])]
    ),
    "regex": dash([cond("email", "regex", "^[a-z]+@example\\.com$")]),
    "numeric string": dash([cond("age", "greater_than", "17")]),
    "native": NATIVE_US,
}

#: value -> the exact message
REFUSED = {
    "list shape": (
        [{"type": "context", "conditions": [US]}],
        "a list of rules is not supported for experiments",
    ),
    "groups and rules together": (
        {"groups": [{"conditions": [US]}], "rules": []},
        "groups: cannot be combined with rules",
    ),
    "logical_operator without groups": (
        {"logical_operator": "AND"},
        "logical_operator: given without groups",
    ),
    "groups not a list": ({"groups": "nonsense"}, "groups: must be a list"),
    "groups a dict": ({"groups": {"a": US}}, "groups: must be a list"),
    "group not an object": ({"groups": ["x"]}, "groups[0]: must be an object"),
    "group with no conditions": (
        {"groups": [{"logical_operator": "AND", "conditions": []}]},
        "groups[0].conditions: at least one condition is required",
    ),
    "group with no conditions key": (
        {"groups": [{"conditions": [US]}, {"id": "g"}]},
        "groups[1].conditions: at least one condition is required",
    ),
    "top operator XOR": (
        dash([US], top="XOR"),
        "logical_operator: must be and, or or not",
    ),
    "group operator XOR": (
        {"groups": [{"logical_operator": "xor", "conditions": [US]}]},
        "groups[0].logical_operator: must be and, or or not",
    ),
    "operator not text": (
        {"logical_operator": 1, "groups": [{"conditions": [US]}]},
        "logical_operator: must be and, or or not",
    ),
    "flat dict (the old documented example)": (
        {"country": ["US", "CA"]},
        "unknown key",
    ),
    "unknown key": ({"foo": 1}, "unknown key"),
    "dashboard with unknown key": (dash([US], extra_key=1), "unknown key"),
    "top-level name": (
        dash([US], name="US only"),
        "name: not supported for experiment rules",
    ),
    "unknown group key": (
        {"groups": [{"conditions": [US], "negate": True}]},
        "groups[0]: unknown key",
    ),
    "a group nested in a group": (
        {"groups": [{"conditions": [US], "groups": [{"conditions": [US]}]}]},
        "groups[0]: unknown key",
    ),
    "unknown condition key": (
        dash([{**US, "values": ["US"]}]),
        "groups[0].conditions[0]: unknown key",
    ),
    "blank attribute (+ Add Group)": (
        dash([cond("", "equals", "")]),
        "groups[0].conditions[0].attribute: attribute is required",
    ),
    "unknown operator": (
        dash([US, cond("plan", "bogus_op", "x")]),
        "groups[0].conditions[1].operator: unknown operator",
    ),
    "bad semver": (
        dash([cond("app.version", "semver_gte", "not-a-version")]),
        "groups[0].conditions[0].value: value is not valid for the operator",
    ),
    "not a number": (
        dash([cond("age", "greater_than", "old")]),
        "groups[0].conditions[0].value: value is not valid for the operator",
    ),
    "empty list": (
        dash([cond("plan", "in", "")]),
        "groups[0].conditions[0].value: value is not valid for the operator",
    ),
    "attribute with a hyphen": (
        dash([cond("user-tier", "equals", "gold")]),
        "groups[0].conditions[0].attribute: "
        "attribute may contain only letters, digits, underscores and dots",
    ),
    "regex that is not RE2": (
        dash([cond("email", "regex", "(unclosed")]),
        "groups[0].conditions[0]: pattern is not valid",
    ),
    "in, 1001 values": (
        dash(
            [US], [cond("user_id", "in", [str(i) for i in range(MAX_LIST_ITEMS + 1)])]
        ),
        "groups[1].conditions[0].value: at most 1000 values are allowed",
    ),
    "rollout_percentage text": (
        dash([US], rollout_percentage="abc"),
        "rollout_percentage: must be a number from 0 to 100",
    ),
    "rollout_percentage 101": (
        dash([US], rollout_percentage=101),
        "rollout_percentage: must be a number from 0 to 100",
    ),
    "rollout_percentage true": (
        dash([US], rollout_percentage=True),
        "rollout_percentage: must be a number from 0 to 100",
    ),
    "top-level id not text": (
        dash([US], id=7),
        "id: must be text of 1 to 100 characters",
    ),
    "native rules not a list": ({"rules": "x"}, "rules: not valid"),
    "native upper-case operator": (
        {"rules": [{"id": "r", "rule": {"operator": "AND", "conditions": []}}]},
        "rules[0].rule.operator: not valid",
    ),
    "native without rules": ({"version": "1.0"}, "rules: required"),
    "native regex that is not RE2": (
        {
            "rules": [
                {
                    "id": "r",
                    "rule": {
                        "operator": "and",
                        "conditions": [
                            {"attribute": "e", "operator": "match_regex", "value": "(a"}
                        ],
                    },
                }
            ]
        },
        "rules[0].rule.conditions[0]: pattern is not valid",
    ),
    "native duplicate rule ids": (
        {"rules": [NATIVE_US["rules"][0], NATIVE_US["rules"][0]]},
        "rule ids must be unique",
    ),
}


def engine_view(raw):
    """What experiment assignment evaluates for a stored value
    (``_evaluate_experiment_targeting``: falsy is "no rules", then coerce)."""
    if not raw:
        return None
    rules, _why = AssignmentService._coerce_targeting_rules(copy.deepcopy(raw))
    return rules


@pytest.mark.regression
@pytest.mark.parametrize("name", list(ACCEPTED))
def test_accepted(name):
    value = copy.deepcopy(ACCEPTED[name])
    first = validate_experiment_targeting(value)
    assert value == ACCEPTED[name], "the validator changed the value"
    # Idempotent: the create route validates the same payload twice.
    assert validate_experiment_targeting(value) == first
    assert ExperimentUpdate(targeting_rules=value).targeting_rules == ACCEPTED[name]


@pytest.mark.regression
@pytest.mark.parametrize("name", list(REFUSED))
def test_refused(name):
    value, message = REFUSED[name]
    with pytest.raises(TargetingRulesError) as caught:
        validate_experiment_targeting(copy.deepcopy(value))
    assert str(caught.value) == message


@pytest.mark.regression
@pytest.mark.parametrize("name", list(ACCEPTED))
def test_parity_with_what_assignment_evaluates(name):
    """The validator's reading of an accepted value is the engine's reading."""
    value = ACCEPTED[name]
    assert validate_experiment_targeting(copy.deepcopy(value)) == engine_view(value)


def test_parity_rows_include_real_rules():
    """Guards the parity test: were every accepted row "no rules", it could
    not tell two readings apart."""
    with_rules = [n for n, v in ACCEPTED.items() if engine_view(v) is not None]
    assert len(with_rules) >= 10, with_rules


def test_the_dashboard_rows_are_read_as_dashboard_rules():
    """``{"groups": [...]}`` without ``logical_operator`` is dashboard to the
    engine; a classifier that disagreed would refuse it."""
    rules = validate_experiment_targeting(ACCEPTED["no top operator"])
    assert (
        rules is not None and rules.rules[0].rule.groups[0].conditions[0].value == "US"
    )


DISTINCT_OPERATOR = "zq_op_7731"
DISTINCT_VALUE = "zq-9.x-7731"


@pytest.mark.regression
@pytest.mark.parametrize(
    "value",
    [
        dash([cond("country", DISTINCT_OPERATOR, "US")]),
        dash([cond("app.version", "semver_gte", DISTINCT_VALUE)]),
        dash([cond("age", "greater_than", DISTINCT_VALUE)]),
        dash([cond("email", "regex", "(" + DISTINCT_VALUE)]),
        {DISTINCT_VALUE: ["US"]},
        {"rules": [{"id": DISTINCT_VALUE, "rule": {"operator": DISTINCT_OPERATOR}}]},
    ],
    ids=["operator", "semver", "number", "regex", "key", "native"],
)
def test_the_message_never_repeats_the_submitted_value(value):
    with pytest.raises(ValidationError) as caught:
        ExperimentUpdate(targeting_rules=value)
    # What the 422 carries: the application's handler renders exactly this.
    text = render_validation_errors(caught.value.errors()).decode()
    assert "targeting_rules" in text
    assert DISTINCT_OPERATOR not in text
    assert "7731" not in text, text


# --- the read path: rules that yield nothing are named once ----------------


@pytest.fixture
def warnings(monkeypatch):
    """Every warning the assignment service logs, formatted."""
    EVALUATION_NOTES.clear()
    logged = []
    recorder = Mock()
    recorder.warning.side_effect = lambda msg, *args: logged.append(msg % args)
    monkeypatch.setattr(assignment_service, "logger", recorder)
    yield logged
    EVALUATION_NOTES.clear()


@pytest.mark.regression
def test_a_flat_dict_that_yields_no_rules_is_logged_once_per_experiment(warnings):
    raw = {"country": ["US", "zq7731"]}
    for _ in range(3):
        rules, _ = AssignmentService._coerce_targeting_rules(
            raw, owner="experiment:e-1"
        )
        assert rules is None
    AssignmentService._coerce_targeting_rules(raw, owner="experiment:e-2")
    assert len(warnings) == 2, warnings
    assert "experiment:e-1" in warnings[0] and "experiment:e-2" in warnings[1]
    assert not any("zq7731" in w or "country" in w for w in warnings), warnings


@pytest.mark.parametrize(
    "raw",
    [{"rules": []}, {"version": "1.0", "rules": []}, NATIVE_US],
    ids=["rules empty", "rules empty with version", "native rules"],
)
def test_native_values_are_not_logged(warnings, raw):
    AssignmentService._coerce_targeting_rules(raw, owner="experiment:e-3")
    assert warnings == []
