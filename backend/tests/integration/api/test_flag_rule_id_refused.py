"""A native flag rule id longer than 255 characters is refused on save (#2179).

A production evaluation records the id of the rule it matched in
``raw_metrics.targeting_rule_id``, a String(255). A longer ``id`` on a native
rule or ``default_rule`` used to be saved, and every production evaluation
that reached that rule answered 500. Every write that carries flag rules now
answers 422 at ``targeting_rules`` and saves nothing. A flag stored with such
an id before keeps its rules, and its matching production evaluations answer
500 until the id is shortened; ``python -m backend.scripts.check_targeting_rules``
lists it.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)
from backend.app.models.metrics.metric import RawMetric
from backend.scripts import check_targeting_rules

pytestmark = [pytest.mark.integration, pytest.mark.regression]

FLAGS = "/api/v1/feature-flags"
US = {"country": "US"}


def native(n: int, where: str = "rule") -> dict:
    """Native rules whose rule id, or ``default_rule`` id, is ``n`` long."""
    rule = {
        "id": "r" * n if where == "rule" else "r1",
        "rule": {
            "operator": "and",
            "conditions": [{"attribute": "country", "operator": "eq", "value": "US"}],
        },
        "rollout_percentage": 100,
        "priority": 0,
    }
    rules = {"version": "1.0", "rules": [rule]}
    if where == "default":
        rule["rule"]["conditions"][0]["value"] = "ZZ"
        rules["default_rule"] = {
            "id": "d" * n,
            "rule": {"operator": "and", "conditions": []},
            "rollout_percentage": 100,
        }
    return rules


def _refused(path: str) -> dict:
    return {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "targeting_rules"],
                "msg": f"Value error, {path}: must be at most 255 characters",
                "ctx": {"error": {}},
            }
        ]
    }


@pytest.fixture
def keys(db_session):
    made = []
    yield made
    db_session.rollback()
    for key in made:
        flag = db_session.query(FeatureFlag).filter(FeatureFlag.key == key).first()
        if flag is not None:
            db_session.query(RawMetric).filter(
                RawMetric.feature_flag_id == flag.id
            ).delete()
            db_session.query(FlagEnvironmentSetting).filter(
                FlagEnvironmentSetting.feature_flag_id == flag.id
            ).delete()
            db_session.delete(flag)
    db_session.commit()


def _create(client, keys, rules) -> tuple:
    key = f"rid2179-{uuid.uuid4().hex[:8]}"
    keys.append(key)
    body = {"key": key, "name": "Rule id", "is_active": True, "rollout_percentage": 0}
    if rules is not None:
        body["targeting_rules"] = rules
    return client.post(f"{FLAGS}/", json=body), key


def _saved(db_session, key: str) -> int:
    db_session.rollback()
    return db_session.query(FeatureFlag).filter(FeatureFlag.key == key).count()


def _evaluate(client, key: str, context=US) -> int:
    return client.get(
        f"{FLAGS}/evaluate/{key}",
        params={"user_id": "u1", "context": json.dumps(context)},
    ).status_code


@pytest.mark.parametrize(
    "where,path", [("rule", "rules[0].id"), ("default", "default_rule.id")]
)
def test_a_create_refuses_an_id_over_255_characters(
    developer_client, db_session, keys, where, path
):
    response, key = _create(developer_client, keys, native(256, where))
    assert response.status_code == 422, response.text
    assert response.json() == _refused(path)
    assert _saved(db_session, key) == 0


@pytest.mark.parametrize("where", ["rule", "default"])
def test_an_id_of_255_characters_is_saved_evaluated_and_recorded(
    developer_client, db_session, keys, where
):
    response, key = _create(developer_client, keys, native(255, where))
    assert response.status_code == 201, response.text
    context = US if where == "rule" else {"country": "FR"}
    assert _evaluate(developer_client, key, context) == 200
    flag_id = uuid.UUID(response.json()["id"])
    db_session.rollback()
    recorded = {
        row.targeting_rule_id
        for row in db_session.query(RawMetric).filter(
            RawMetric.feature_flag_id == flag_id
        )
    }
    assert ("r" if where == "rule" else "d") * 255 in recorded


def test_an_update_refuses_it_and_keeps_the_stored_rules(
    developer_client, db_session, keys
):
    response, key = _create(developer_client, keys, native(20))
    flag_id = response.json()["id"]
    response = developer_client.put(
        f"{FLAGS}/{flag_id}", json={"targeting_rules": native(256)}
    )
    assert response.status_code == 422, response.text
    assert response.json() == _refused("rules[0].id")
    assert developer_client.get(f"{FLAGS}/{flag_id}").json()["targeting_rules"] == (
        native(20)
    )


@pytest.mark.parametrize("environment", ["production", "staging", "development"])
def test_an_environment_update_refuses_it(
    developer_client, db_session, keys, environment
):
    response, _ = _create(developer_client, keys, None)
    flag_id = response.json()["id"]
    response = developer_client.put(
        f"{FLAGS}/{flag_id}/environments/{environment}",
        json={
            "is_active": True,
            "rollout_percentage": 0,
            "targeting_rules": native(256),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json() == _refused("rules[0].id")
    db_session.rollback()
    assert (
        db_session.query(FlagEnvironmentSetting)
        .filter(
            FlagEnvironmentSetting.feature_flag_id == uuid.UUID(flag_id),
            FlagEnvironmentSetting.environment == environment,
        )
        .count()
        == 0
    )


def test_a_promotion_of_a_stored_long_id_is_refused(developer_client, db_session, keys):
    response, _ = _create(developer_client, keys, None)
    flag_id = response.json()["id"]
    db_session.add(
        FlagEnvironmentSetting(
            feature_flag_id=uuid.UUID(flag_id),
            environment="staging",
            is_active=True,
            rollout_percentage=0,
            targeting_rules=native(300),
        )
    )
    db_session.commit()
    response = developer_client.post(
        f"{FLAGS}/{flag_id}/environments/production/promote", json={"from": "staging"}
    )
    assert response.status_code == 422, response.text
    assert response.json() == _refused("rules[0].id")


def test_a_flag_stored_with_a_long_id_is_listed_and_can_be_fixed(
    developer_client, db_session, keys, developer_user, capsys
):
    """A row stored before the check: its matching evaluations answer 500
    until the id is shortened, the check script lists it, and a PUT with a
    shorter id fixes it."""
    key = f"rid2179-{uuid.uuid4().hex[:8]}"
    keys.append(key)
    flag = FeatureFlag(
        key=key,
        name="Stored before the check",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        owner_id=developer_user.id,
        targeting_rules=native(256),
    )
    db_session.add(flag)
    db_session.commit()
    flag_id = str(flag.id)

    assert check_targeting_rules.main([]) == 0
    assert (
        f"feature_flag\t{flag_id}\t{key}\trules[0].id\tmust be at most 255 characters"
        in capsys.readouterr().out.splitlines()
    )

    # Its matching evaluations still fail until the id is shortened.
    unhandled = TestClient(app, raise_server_exceptions=False)
    assert _evaluate(unhandled, key) == 500
    user_flags = unhandled.get(f"{FLAGS}/user/u1", params={"context": json.dumps(US)})
    assert user_flags.status_code == 500

    # Its other fields can still be changed; its rules sent back are refused.
    assert (
        developer_client.put(f"{FLAGS}/{flag_id}", json={"name": "x"}).status_code
        == 200
    )
    stored = developer_client.get(f"{FLAGS}/{flag_id}").json()["targeting_rules"]
    refused = developer_client.put(
        f"{FLAGS}/{flag_id}", json={"targeting_rules": stored}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json() == _refused("rules[0].id")
    fixed = developer_client.put(
        f"{FLAGS}/{flag_id}", json={"targeting_rules": native(20)}
    )
    assert fixed.status_code == 200, fixed.text
    assert _evaluate(developer_client, key) == 200


def test_experiment_rules_are_unaffected():
    """An experiment records no rule id, so its rules keep any id length."""
    from backend.app.core.targeting_adapter import validate_experiment_targeting

    assert validate_experiment_targeting(native(300)) is not None
