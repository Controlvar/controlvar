"""``check_targeting_rules --time-window-changes`` on a real database (#1527).

Rows written straight to ``feature_flags`` and ``experiments``, as stored data
can be (no create validation), hold native rules with a ``time_window`` that
has no ``start`` or ``end``. The ``Condition`` schema refused such a window
until #1527, so the readers ignored the whole ruleset; now they read it, and
the report lists each such flag and experiment with how its answer changes. It
lists nothing for the decoys, writes nothing, and exits 0 (EMB3-4).

The database is shared with other tests, so only the lines naming a row
planted here are compared, and they are compared exactly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import text

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.scripts import check_targeting_rules

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

AT = "rules[0].rule.conditions[0].value"

#: The reasons, written out so a changed text fails.
FLAG_NOW_APPLY = (
    "answer changes on upgrade: the flag's rules were ignored (it served its "
    "global rollout) because this time_window has no start or end; they now apply"
)
FLAG_ANSWERS_ERROR = (
    "answer changes on upgrade: the flag's rules were ignored (it served its "
    "global rollout); now this time_window cannot be read, so the flag answers "
    "off, reason error, for each user whose evaluation reaches it"
)
EXPERIMENT_NOW_APPLY = (
    "answer changes on upgrade: the experiment's rules made no new user eligible "
    "because this time_window has no start or end; they now apply"
)


def _native(window: Any) -> Dict[str, Any]:
    return {
        "rules": [
            {
                "id": "r",
                "rule": {
                    "operator": "and",
                    "conditions": [
                        {"attribute": "now", "operator": "time_window", "value": window}
                    ],
                },
                "rollout_percentage": 100,
            }
        ]
    }


DAYS_ONLY = _native({"days": [2]})
NO_KEY = _native({})
#: Not listed: it has start and end (it already answered error), or it is in
#: the dashboard shape.
DECOYS = {
    "start-end": _native({"start": "09:00", "end": "17:00"}),
    "dashboard": {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {
                        "attribute": "now",
                        "operator": "time_window",
                        "value": {"start": "09:00", "end": "17:00"},
                    }
                ],
            }
        ],
    },
}


@pytest.fixture
def planted(make_feature_flag, make_experiment, db_session):
    flags: List[uuid.UUID] = []
    experiments: List[uuid.UUID] = []

    def flag(rules: Any, status: FeatureFlagStatus = FeatureFlagStatus.ACTIVE):
        made = make_feature_flag(
            targeting_rules=rules, rollout_percentage=100, status=status
        )
        flags.append(made.id)
        return made

    def experiment(rules: Any) -> Experiment:
        suffix = uuid.uuid4().hex[:8]
        made = make_experiment(
            name=f"Window {suffix}",
            key=f"window-{suffix}",
            status=ExperimentStatus.ACTIVE,
            targeting_rules=rules,
        )
        experiments.append(made.id)
        return made

    yield SimpleNamespace(flag=flag, experiment=experiment)
    db_session.rollback()
    if flags:
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(flags)).delete(
            synchronize_session=False
        )
    if experiments:
        db_session.query(Experiment).filter(Experiment.id.in_(experiments)).delete(
            synchronize_session=False
        )
    db_session.commit()


@pytest.mark.regression
def test_time_window_changes_lists_the_planted_flags_and_experiment(
    planted, db_session, capsys
):
    days = planted.flag(DAYS_ONLY)
    empty = planted.flag(NO_KEY)
    dormant = planted.flag(DAYS_ONLY, FeatureFlagStatus.INACTIVE)
    decoys = [planted.flag(rules) for rules in DECOYS.values()]
    experiment = planted.experiment(DAYS_ONLY)
    planted_ids = {str(row.id) for row in (days, empty, dormant, experiment, *decoys)}

    def stored():
        db_session.expire_all()
        return [
            tuple(r)
            for r in db_session.execute(
                text(
                    "SELECT 'f', id::text, targeting_rules::text FROM feature_flags "
                    "UNION ALL SELECT 'e', id::text, targeting_rules::text FROM "
                    "experiments ORDER BY 1, 2"
                )
            ).all()
        ]

    before = stored()
    assert check_targeting_rules.main(["--time-window-changes"]) == 0
    assert stored() == before, "the report wrote"
    captured = capsys.readouterr()

    lines = [
        line for line in captured.out.splitlines() if line.split("\t")[1] in planted_ids
    ]
    flag_lines = sorted(
        [
            f"feature_flag\t{days.id}\t{days.key}\tACTIVE\t{AT}\t{FLAG_NOW_APPLY}",
            f"feature_flag\t{empty.id}\t{empty.key}\tACTIVE\t{AT}"
            f"\t{FLAG_ANSWERS_ERROR}",
            f"feature_flag\t{dormant.id}\t{dormant.key}\tINACTIVE\t{AT}"
            f"\t{FLAG_NOW_APPLY} (inactive: no change while it stays inactive)",
        ],
        key=lambda line: line.split("\t")[1],
    )
    # Flags first, then experiments, each ordered by id.
    assert lines == flag_lines + [
        f"experiment\t{experiment.id}\t{experiment.key}\tACTIVE\t{AT}"
        f"\t{EXPERIMENT_NOW_APPLY}"
    ]
    assert "listed whose stored rules hold a time_window without start or end" in (
        captured.err
    )
