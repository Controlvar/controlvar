"""`python -m backend.scripts.check_targeting_rules` -- the post-upgrade report (#535)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.services import feature_flag_service as ffs
from backend.scripts import check_targeting_rules as check

pytestmark = pytest.mark.unit

US = {"attribute": "country", "operator": "equals", "value": "US"}


def _dash(*conditions, **extra):
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
        **extra,
    }


def _legacy(operator: Any = "eq", value: Any = "pro") -> List[Dict[str, Any]]:
    condition: Dict[str, Any] = {"attribute": "plan", "value": value}
    if operator is not None:
        condition["operator"] = operator
    return [
        {
            "id": "pro-only",
            "type": "context",
            "conditions": [condition],
            "percentage": 100,
        }
    ]


def test_one_line_per_listed_flag():
    rows = [
        ("id-1", "typo", _dash({**US, "operator": "equal"})),
        ("id-2", "clean", _dash(US)),
        ("id-3", "flat", {"country": ["US"]}),
        ("id-4", "old-seed", {"operator": "and", "rules": []}),
        ("id-5", "legacy-eq", _legacy("eq")),
        ("id-6", "legacy-typo", _legacy("equal")),
        ("id-7", "scalar", 42),
        ("id-8", "empty", {}),
        (
            "id-9",
            "lone-default",
            {"default_rule": {"id": "d", "rule": {"operator": "and"}}},
        ),
    ]
    lines = [f.line() for f in check.findings_for(rows)]
    assert lines == [
        "feature_flag\tid-1\ttypo\tgroups[0].conditions[0].operator\tunknown operator",
        "feature_flag\tid-3\tflat\ttargeting_rules\tunknown key",
        "feature_flag\tid-4\told-seed\ttargeting_rules\tunknown key",
        "feature_flag\tid-5\tlegacy-eq\ttargeting_rules"
        "\ta list of rules is not supported; use the groups shape",
        "feature_flag\tid-6\tlegacy-typo\t[0].conditions[0].operator"
        "\tlegacy condition whose operator is not eq, ne, gt, lt, contains or in; "
        "its rule matches no user",
        "feature_flag\tid-7\tscalar\ttargeting_rules\tmust be an object",
        "feature_flag\tid-9\tlone-default\trules\trequired",
    ]


@pytest.mark.parametrize(
    "operator", ["equal", "equals", "gte", "", None, ["eq"], {"op": "eq"}]
)
def test_a_legacy_condition_that_cannot_match_has_its_own_line(operator):
    found = check.finding_for("id", "k", _legacy(operator))
    assert found is not None and found.matches_no_user
    assert found.path == "[0].conditions[0].operator"


@pytest.mark.parametrize("operator", ["eq", "ne", "gt", "lt", "contains", "in"])
def test_a_legacy_condition_with_a_known_operator_gets_the_list_line(operator):
    found = check.finding_for("id", "k", _legacy(operator))
    assert found is not None and not found.matches_no_user
    assert found.reason == "a list of rules is not supported; use the groups shape"


def test_the_first_condition_that_cannot_match_is_named():
    rules = _legacy("eq") + [
        {"type": "user_id", "user_ids": ["u"]},
        {
            "type": "context",
            "conditions": [
                {"attribute": "a", "operator": "eq", "value": 1},
                {"attribute": "b", "operator": "equal", "value": 1},
            ],
        },
    ]
    assert check.finding_for("id", "k", rules).path == "[2].conditions[1].operator"


def _served(rules: Any, context: Dict[str, Any], monkeypatch) -> Dict[str, Any]:
    monkeypatch.setattr(ffs, "MetricsService", MagicMock())
    flag = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000535",
        key="legacy",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        targeting_rules=rules,
    )
    service = ffs.FeatureFlagService(db=MagicMock())
    return service.evaluate_flag_detailed(flag, "user-1", context)


@pytest.mark.regression
@pytest.mark.parametrize(
    "rules",
    [
        _legacy("equal"),
        _legacy(None),
        _legacy("gte", 3),
        _legacy("eq"),
        _legacy("in", ["pro", "free"]),
        _legacy("contains", "pr"),
    ],
    ids=["equal", "missing", "gte", "eq", "in", "contains"],
)
def test_the_legacy_label_is_what_the_evaluator_does(rules, monkeypatch):
    """EM C2: "matches no user" is said of exactly the rows whose rule the flag
    evaluator matches for no user. Each row is evaluated at global 0% for users
    who send the attribute with the rule's value and with others."""
    found = check.finding_for("id", "k", rules)
    value = rules[0]["conditions"][0]["value"]
    contexts = [{"plan": v} for v in ("pro", "free", "p", 3, 4, "")]
    if not isinstance(value, list):
        contexts.append({"plan": value})
    matched = [
        c
        for c in contexts
        if _served(rules, c, monkeypatch)["reason"] == "targeting_rule"
    ]
    assert found.matches_no_user == (matched == [])
    assert ("matches no user" in found.line()) == found.matches_no_user


def test_a_value_the_check_cannot_read_is_listed(monkeypatch):
    def boom(_value):
        raise RecursionError

    monkeypatch.setattr(check, "validate_flag_targeting", boom)
    found = check.finding_for("id", "k", {"groups": []})
    assert found.line() == "feature_flag\tid\tk\ttargeting_rules\tcould not be checked"


def test_the_feature_flags_experiments_and_segments_tables_are_read(monkeypatch):
    seen = []
    rows = {
        "feature_flags": [("row", "key", {"country": ["US"]})],
        "experiments": [
            ("exp", "exp-key", "ACTIVE", {"country": ["US"]}),
            ("fine", "fine-key", "DRAFT", _dash(US)),
        ],
        "segments": [
            ("seg", "Legacy", {"operator": "and", "conditions": []}),
            ("ok", "Fine", _dash(US)),
        ],
    }

    class Result:
        def __init__(self, table):
            self.table = table

        def all(self):
            return rows[self.table]

    class Connection:
        def execute(self, statement):
            seen.append(str(statement))
            table = next(name for name in rows if f'"{name}"' in str(statement))
            return Result(table)

    found = check.scan(Connection(), "exp")
    assert [f.line() for f in found] == [
        "feature_flag\trow\tkey\ttargeting_rules\tunknown key",
        "experiment\texp\texp-key\tACTIVE\ttargeting_rules\tunknown key",
        "segment\tseg\tLegacy\trules\trules not valid: unknown key",
    ]
    assert seen == [
        'SELECT id, "key", "targeting_rules" FROM "exp"."feature_flags" '
        'WHERE "targeting_rules" IS NOT NULL ORDER BY id',
        'SELECT id, "key", "status", "targeting_rules" FROM "exp"."experiments" '
        'WHERE "targeting_rules" IS NOT NULL ORDER BY id',
        'SELECT id, "name", "rules" FROM "exp"."segments" '
        "WHERE \"status\" <> 'ARCHIVED' ORDER BY id",
    ]


def test_an_experiment_line_carries_its_status():
    found = check.experiment_finding_for("id", None, "PAUSED", 42)
    assert (
        found.line() == "experiment\tid\t\tPAUSED\ttargeting_rules\tmust be an object"
    )
    assert check.experiment_finding_for("id", "k", "DRAFT", _dash(US)) is None
    assert check.experiment_finding_for("id", "k", "DRAFT", None) is None


def _native(group, default=None):
    out = {"rules": [{"id": "r", "rule": group}]}
    if default is not None:
        out["default_rule"] = {"id": "d", "rule": default}
    return out


LONGER = {
    "attribute": "tags",
    "operator": "array_length",
    "value": 2,
    "additional_value": "gt",
}
TYPED = {
    "attribute": "plan",
    "operator": "eq",
    "value": "pro",
    "attribute_type": "string",
}
EQ = {"attribute": "plan", "operator": "eq", "value": "pro"}


@pytest.mark.parametrize(
    "rules, path, what",
    [
        (
            _native({"operator": "and", "conditions": [EQ, LONGER]}),
            "rules[0].rule.conditions[1].operator",
            "array_length",
        ),
        (
            _native({"operator": "not", "conditions": [LONGER]}),
            "rules[0].rule.conditions[0].operator",
            "array_length",
        ),
        (
            _native(
                {
                    "operator": "and",
                    "conditions": [EQ],
                    "groups": [{"operator": "not", "conditions": [EQ, TYPED]}],
                }
            ),
            "rules[0].rule.groups[0].conditions[1].attribute_type",
            "attribute_type",
        ),
        (
            _native(
                {"operator": "and", "conditions": [EQ]}, default={"conditions": [TYPED]}
            ),
            "default_rule.rule.conditions[0].attribute_type",
            "attribute_type",
        ),
        (
            _native(
                {
                    "operator": "and",
                    "conditions": [{**LONGER, "attribute_type": "array"}],
                }
            ),
            "rules[0].rule.conditions[0].operator",
            "array_length, attribute_type",
        ),
    ],
    ids=["top", "not", "nested-not", "default-typed", "both"],
)
def test_an_enrolment_change_names_the_first_condition(rules, path, what):
    found = check.enrolment_change_for("id", "k", "ACTIVE", rules)
    assert found.line() == (
        f"experiment\tid\tk\tACTIVE\t{path}\tenrolment of new users may change: {what}"
    )


@pytest.mark.parametrize(
    "rules",
    [
        None,
        {},
        _native({"operator": "and", "conditions": [EQ]}),
        _native(
            {"operator": "and", "conditions": [EQ]}, default={"conditions": [LONGER]}
        ),
        _dash({"attribute": "tags", "operator": "array_length", "value": 2}),
        [{"type": "context", "conditions": [LONGER]}],
    ],
    ids=["null", "empty", "plain", "length-in-default", "dashboard", "legacy-list"],
)
def test_rules_assignment_reads_alike_are_not_enrolment_changes(rules):
    assert check.enrolment_change_for("id", "k", "ACTIVE", rules) is None


def test_rules_assignment_cannot_read_are_listed():
    found = check.enrolment_change_for("id", "k", "DRAFT", "{not json")
    assert found.line() == (
        "experiment\tid\tk\tDRAFT\ttargeting_rules\tcould not be checked"
    )


@pytest.mark.parametrize(
    "rules, path, reason",
    [
        ({"operator": "and", "conditions": []}, "rules", "unknown key"),
        ({"rules": []}, "rules", "unknown key"),
        (None, "rules", "must be an object"),
        (
            {"groups": [{"conditions": [{**US, "operator": "eq"}]}]},
            "groups[0].conditions[0].operator",
            "unknown operator",
        ),
        ({"groups": []}, "groups", "at least one group is required"),
        (
            {"groups": [{"conditions": []}]},
            "groups[0].conditions",
            "at least one condition is required",
        ),
        ({"groups": "x"}, "groups", "must be a list"),
    ],
    ids=["legacy", "native", "null", "pe-eq", "pe-no-groups", "pe-empty", "pe-x"],
)
def test_a_segment_whose_rules_are_not_valid_has_a_line(rules, path, reason):
    found = check.segment_finding_for("id", "Name", rules)
    assert found.line() == f"segment\tid\tName\t{path}\trules not valid: {reason}"
    assert not found.matches_no_user


def test_a_valid_segment_has_no_line_and_a_name_stays_on_one_line():
    assert check.segment_finding_for("id", "Name", _dash(US)) is None
    found = check.segment_finding_for("id", "a\tb\n c", {"groups": []})
    assert found.line().split("\t")[2] == "a b c"


def test_a_segment_the_check_cannot_read_is_listed(monkeypatch):
    def boom(_value):
        raise RecursionError

    monkeypatch.setattr(check, "validate_segment_rules", boom)
    found = check.segment_finding_for("id", "n", {"groups": []})
    assert (
        found.line() == "segment\tid\tn\trules\trules not valid: could not be checked"
    )


@pytest.mark.regression
def test_an_unreadable_database_exits_2(monkeypatch, capsys):
    """Not 0: a report that cannot read the database must not look clean."""
    monkeypatch.setenv("POSTGRES_SERVER", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "1")
    assert check.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not read the database" in captured.err
