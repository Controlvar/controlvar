"""``check_targeting_rules`` lists experiments, on a real database (#1159).

Two reports over rows written straight to ``experiments``, as stored data can
be (no create validation):

* the default report gives each experiment whose rules a create or an update
  now refuses a line ``experiment <id> <key> <status> <path> <reason>``;
* ``--enrolment-changes`` gives each experiment whose rules hold a condition
  experiment targeting now reads as flag targeting does (``array_length``, or
  a condition carrying ``attribute_type``) one line, at any depth and inside
  ``NOT`` too, whatever its status, and nothing for the decoys.

The database is shared with other tests, so only the lines naming a row
planted here are compared, and they are compared exactly.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import pytest

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.scripts import check_targeting_rules

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _condition(attribute: str, operator: str, value: Any, **extra) -> Dict[str, Any]:
    return {"attribute": attribute, "operator": operator, "value": value, **extra}


def _group(operator: str, *conditions, groups=()) -> Dict[str, Any]:
    return {
        "operator": operator,
        "conditions": list(conditions),
        "groups": list(groups),
    }


def _native(*groups, default_rule=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "version": "1.0",
        "rules": [
            {"id": f"r{index}", "rule": group, "rollout_percentage": 100}
            for index, group in enumerate(groups)
        ],
    }
    if default_rule is not None:
        out["default_rule"] = {"id": "default", "rule": default_rule}
    return out


US = _condition("country", "eq", "US")
LONGER = _condition("tags", "array_length", 2, additional_value="gt")
TYPED = _condition(
    "location",
    "geo_distance",
    [48.8566, 2.3522],
    additional_value=50,
    attribute_type="geo_coordinate",
)

#: name -> (status, stored rules, the --enrolment-changes line's path and what)
LISTED = {
    "top-level": (
        ExperimentStatus.ACTIVE,
        _native(_group("and", US, LONGER)),
        ("rules[0].rule.conditions[1].operator", "array_length"),
    ),
    "nested": (
        ExperimentStatus.PAUSED,
        _native(
            _group("and", US),
            _group(
                "and",
                US,
                groups=[
                    _group("or", US, groups=[_group("and", US), _group("and", LONGER)])
                ],
            ),
        ),
        ("rules[1].rule.groups[0].groups[1].conditions[0].operator", "array_length"),
    ),
    "not-wrapped": (
        ExperimentStatus.ACTIVE,
        _native(_group("not", LONGER)),
        ("rules[0].rule.conditions[0].operator", "array_length"),
    ),
    "not-nested": (
        ExperimentStatus.DRAFT,
        _native(_group("and", US, groups=[_group("not", US, LONGER)])),
        ("rules[0].rule.groups[0].conditions[1].operator", "array_length"),
    ),
    "typed-in-default": (
        ExperimentStatus.DRAFT,
        _native(_group("and", US), default_rule=_group("and", TYPED)),
        ("default_rule.rule.conditions[0].attribute_type", "attribute_type"),
    ),
    "both-as-text": (
        ExperimentStatus.COMPLETED,
        json.dumps(_native(_group("and", TYPED), _group("not", LONGER))),
        (
            "rules[0].rule.conditions[0].attribute_type",
            "array_length, attribute_type",
        ),
    ),
    "unreadable": (
        ExperimentStatus.ARCHIVED,
        {"rules": [{"id": "r", "rule": {"operator": "and", "conditions": [{}]}}]},
        ("targeting_rules", None),
    ),
}

#: Rules that hold no condition read differently: never in the
#: ``--enrolment-changes`` report (``dashboard-length`` is in the default one).
DECOYS = {
    "plain-native": _native(_group("and", US)),
    "plain-dashboard": {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ],
            }
        ],
    },
    # A default_rule is returned without its conditions being read.
    "default-only": _native(_group("and", US), default_rule=_group("and", LONGER)),
    # The dashboard shape does not convert array_length: no rule is evaluated.
    "dashboard-length": {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "tags", "operator": "array_length", "value": 2}
                ],
            }
        ],
    },
    # The words, as an attribute and a value, are not the operator or the key.
    "words-only": _native(
        _group("and", _condition("array_length", "eq", "attribute_type"))
    ),
    "null": None,
}


@pytest.fixture
def planted(make_experiment, db_session):
    made: List[uuid.UUID] = []

    def plant(name: str, status: ExperimentStatus, rules: Any) -> Experiment:
        suffix = uuid.uuid4().hex[:8]
        experiment = make_experiment(
            name=f"Check {name} {suffix}",
            key=f"check-{name}-{suffix}",
            status=status,
            targeting_rules=rules,
        )
        made.append(experiment.id)
        return experiment

    yield plant
    db_session.rollback()
    if made:
        db_session.query(Experiment).filter(Experiment.id.in_(made)).delete(
            synchronize_session=False
        )
        db_session.commit()


def _lines_for(out: str, experiments: List[Experiment]) -> List[str]:
    ids = {str(e.id) for e in experiments}
    return [line for line in out.splitlines() if line.split("\t")[1] in ids]


@pytest.fixture
def rows(planted):
    listed = {
        name: planted(name, status, rules)
        for name, (status, rules, _) in LISTED.items()
    }
    decoys = {
        name: planted(name, ExperimentStatus.ACTIVE, rules)
        for name, rules in DECOYS.items()
    }
    return listed, decoys


def test_enrolment_changes_lists_exactly_the_planted_experiments(rows, capsys):
    listed, decoys = rows
    assert check_targeting_rules.main(["--enrolment-changes"]) == 0
    out = capsys.readouterr().out

    expected = []
    for name, (status, _, (path, what)) in LISTED.items():
        experiment = listed[name]
        reason = (
            "could not be checked"
            if what is None
            else f"enrolment of new users may change: {what}"
        )
        expected.append(
            "\t".join(
                (
                    "experiment",
                    str(experiment.id),
                    experiment.key,
                    status.name,
                    path,
                    reason,
                )
            )
        )
    # The report is ordered by id.
    expected.sort(key=lambda line: line.split("\t")[1])
    everything = list(listed.values()) + list(decoys.values())
    assert _lines_for(out, everything) == expected
    # Experiments only: no flag or segment is in this report.
    assert not [
        line
        for line in out.splitlines()
        if line.startswith(("feature_flag\t", "segment\t"))
    ]


def test_the_default_report_lists_experiments_the_api_now_refuses(rows, capsys):
    listed, decoys = rows
    assert check_targeting_rules.main([]) == 0
    out = capsys.readouterr().out

    def line(experiment: Experiment, status: str, path: str, reason: str) -> str:
        return "\t".join(
            ("experiment", str(experiment.id), experiment.key, status, path, reason)
        )

    expected = sorted(
        [
            line(
                listed["unreadable"],
                "ARCHIVED",
                "rules[0].rule.conditions[0].attribute",
                "not valid",
            ),
            line(
                listed["both-as-text"],
                "COMPLETED",
                "targeting_rules",
                "must be an object",
            ),
            line(
                decoys["dashboard-length"],
                "ACTIVE",
                "groups[0].conditions[0].operator",
                "unknown operator",
            ),
        ],
        key=lambda text: text.split("\t")[1],
    )
    everything = list(listed.values()) + list(decoys.values())
    assert _lines_for(out, everything) == expected
