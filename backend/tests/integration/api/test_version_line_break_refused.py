"""A save refuses a native ``semantic_version`` value that is a version followed
by a newline, through the routes (#1836).

``POST`` and ``PUT`` of a flag and of an experiment answer 422 at
``["body", "targeting_rules"]`` with the condition's path, and write nothing.
A request that leaves the rules out is unaffected, and an experiment clone
copies stored rules as they are. The environment routes are in
``test_flag_environments_api.py``.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict

import pytest

from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag
from backend.tests.integration.api.test_targeting_vocabulary_refused import (
    EXPERIMENTS,
    FLAGS,
    _experiment_body,
    _refused,
    _remove_rows_created_here,
)
from backend.tests.integration.helpers import unique_flag_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

MSG = (
    "Value error, rules[0].rule.conditions[0].value: not a version; use a "
    "version such as 1.2.3 or 1.2.3-beta.1, with no line break at the end"
)


def _rules(value: Any) -> Dict[str, Any]:
    return {
        "rules": [
            {
                "id": "r",
                "rule": {
                    "operator": "and",
                    "conditions": [
                        {
                            "attribute": "v",
                            "operator": "semantic_version",
                            "value": value,
                            "additional_value": "gte",
                        }
                    ],
                },
                "rollout_percentage": 100,
                "priority": 0,
            }
        ]
    }


@pytest.mark.regression
def test_flag_create_refuses_a_version_and_a_newline(admin_client, db_session):
    key = unique_flag_key("u15")
    body = {"key": key, "name": "Version", "targeting_rules": _rules("1.2.3\n")}
    _refused(admin_client.post(f"{FLAGS}/", json=body), MSG)
    assert db_session.query(FeatureFlag).filter_by(key=key).count() == 0
    body["targeting_rules"] = _rules("1.2.3")
    assert admin_client.post(f"{FLAGS}/", json=body).status_code == 201


@pytest.mark.regression
def test_flag_update_refuses_it_and_resending_stored_rules_is_refused_too(
    admin_client, db_session, make_feature_flag
):
    stored = _rules("1.2.3\n")
    flag = make_feature_flag(targeting_rules=stored, rollout_percentage=10)
    # The stored rule is not sent back by a request without rules.
    ok = admin_client.put(f"{FLAGS}/{flag.id}", json={"rollout_percentage": 20})
    assert ok.status_code == 200, ok.text
    response = admin_client.put(
        f"{FLAGS}/{flag.id}",
        json={"targeting_rules": copy.deepcopy(stored), "rollout_percentage": 90},
    )
    _refused(response, MSG)
    db_session.expire_all()
    row = db_session.get(FeatureFlag, flag.id)
    assert (row.targeting_rules, row.rollout_percentage) == (stored, 20)


@pytest.mark.regression
def test_experiment_create_refuses_it(admin_client, db_session):
    title = f"Version {uuid.uuid4().hex[:8]}"
    response = admin_client.post(
        f"{EXPERIMENTS}/", json=_experiment_body(title, _rules("1.2.3-beta\n"))
    )
    _refused(response, MSG)
    assert db_session.query(Experiment).filter_by(name=title).count() == 0


@pytest.mark.regression
def test_experiment_update_refuses_it_and_clone_copies_a_stored_one(
    admin_client, db_session
):
    created = admin_client.post(
        f"{EXPERIMENTS}/",
        json=_experiment_body(f"Version {uuid.uuid4().hex[:8]}", None),
    )
    assert created.status_code == 201, created.text
    experiment_id = uuid.UUID(created.json()["id"])
    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}", json={"targeting_rules": _rules("0.0.0\n")}
    )
    _refused(response, MSG)
    db_session.expire_all()
    assert db_session.get(Experiment, experiment_id).targeting_rules is None

    stored = _rules("1.2.3\n")
    db_session.get(Experiment, experiment_id).targeting_rules = copy.deepcopy(stored)
    db_session.commit()
    cloned = admin_client.post(f"{EXPERIMENTS}/{experiment_id}/clone")
    assert cloned.status_code == 201, cloned.text
    assert cloned.json()["targeting_rules"] == stored
