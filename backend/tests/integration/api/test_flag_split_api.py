"""Flags with a split, or with a rule's own value, over the real routes (#2012).

Each flag is stored through the models (setting them through the API is
``test_flag_split_write_api.py``); then the routes an SDK calls answer it, with
real keys and ``deps.get_api_key`` not replaced:

* the bulk route answers true or false for every one of the contract vectors'
  such flags, the same ``enabled`` as ``/evaluate``;
* one evaluation of such a flag runs the statements a two-value flag's does;
* it counts as one evaluation and nothing else: no assignment, no event, and no
  new Prometheus label;
* the changes the API accepts of such a flag, the previous release's, keep its
  split and are answered;
* staging's split and production's are each served by their own keys, and a
  change of one leaves the other exactly as it was.

What every one of the vectors' cases is answered, over the production route and
over a staging key's, is ``test_sdk_ruleset_api.py`` and
``test_environment_evaluation.py``.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event

from backend.app.api import deps
from backend.app.core.metrics import feature_flag_evaluations_total
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import MetricType, RawMetric
from backend.app.models.user import UserRole
from backend.app.services.metrics_service import MetricsService
from backend.scripts.generate_ruleset_vectors import VECTORS_PATH
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from backend.tests.integration.key_environments import configure, environment_key

#: The ground-truth copy of the hash (standard library only).
_LIBRARY_ROOT = Path(__file__).resolve().parents[4] / "tests" / "ground_truth"
if str(_LIBRARY_ROOT) not in sys.path:
    sys.path.insert(0, str(_LIBRARY_ROOT))

from ground_truth import hashing

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

FLAGS = "/api/v1/feature-flags"
RULESET = "/api/v1/sdk/ruleset"
USERS = [f"user-{n}" for n in range(120)]
PRO = {"plan": "pro"}
FREE = {"plan": "free"}
RULE = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        }
    ],
}


def _split(*weights: int) -> List[Dict[str, Any]]:
    return [
        {"value": value, "weight": weight}
        for value, weight in zip(("red", "green", "blue"), weights)
    ]


SPLIT = _split(34, 33, 33)


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)
    assert deps.get_api_key not in app.dependency_overrides


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def owner(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="split")


@pytest.fixture
def keys(db_session, owner) -> Dict[str, Dict[str, str]]:
    return {
        environment: environment_key(db_session, owner, environment, "sdk:ruleset")
        for environment in ("production", "staging", "development")
    }


@pytest.fixture
def make_flag(db_session, owner):
    """A string flag on at 100% in production, unless told otherwise."""

    def make(**overrides) -> FeatureFlag:
        flag = FeatureFlag(
            **{
                "key": f"split-{uuid.uuid4().hex[:10]}",
                "name": "Split flag",
                "status": FeatureFlagStatus.ACTIVE,
                "owner_id": owner.id,
                "rollout_percentage": 100,
                "value_type": "string",
                "on_value": "on",
                "off_value": "off",
                **overrides,
            }
        )
        db_session.add(flag)
        db_session.commit()
        return flag

    return make


def _evaluate(client, headers, key: str, user: str, context=None) -> Dict[str, Any]:
    params = {"user_id": user}
    if context is not None:
        params["context"] = json.dumps(context)
    answer = client.get(f"{FLAGS}/evaluate/{key}", params=params, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _values(client, headers, key: str, users=USERS, context=None) -> Dict[str, Any]:
    return {u: _evaluate(client, headers, key, u, context)["value"] for u in users}


def _pick(user: str, key: str, split=SPLIT) -> Any:
    return hashing.split_value(user, key, split)


# ---------------------------------------------------------------------------
# The bulk route
# ---------------------------------------------------------------------------


@pytest.fixture
def stored_multi_way(db_session, owner):
    """The contract vectors' active flags with a split or a rule's own value,
    and their cases."""
    vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    remote = {
        entry["key"]
        for entry in vectors["ruleset"]["flags"]
        if entry.get("evaluation") == "remote"
    }
    stored = [
        spec
        for spec in vectors["stored_flags"]
        if spec["key"] in remote and spec["key"].startswith(("split-", "rule-value"))
    ]
    flag_keys = [spec["key"] for spec in stored]

    def remove() -> None:
        db_session.rollback()
        db_session.query(FeatureFlag).filter(FeatureFlag.key.in_(flag_keys)).delete(
            synchronize_session=False
        )
        db_session.commit()

    remove()
    for spec in stored:
        fields = {**spec, "status": FeatureFlagStatus[spec["status"]]}
        db_session.add(FeatureFlag(name=spec["key"], owner_id=owner.id, **fields))
    db_session.commit()
    yield [case for case in vectors["cases"] if case["flag"] in flag_keys]
    remove()


def _refused_at(response) -> list:
    """Each validation error of a 422, as (type, loc)."""
    return [(error["type"], error["loc"]) for error in response.json()["detail"]]


@pytest.mark.regression
def test_the_bulk_route_answers_true_or_false_and_agrees_with_evaluate(
    client, keys, stored_multi_way, monkeypatch
):
    """``/feature-flags/user/{id}`` stays ``{key: bool}``: a value sent there
    would be read as true by an SDK that turns it into a boolean, the off value
    among them. The per-evaluation metric rows are not what this checks, so that
    write is stubbed, as ``test_sdk_ruleset_api.py`` does for the same cases."""
    monkeypatch.setattr(
        MetricsService, "record_flag_evaluation", staticmethod(lambda **_: None)
    )
    asked = {}
    for case in stored_multi_way:
        asked.setdefault((case["user_id"], json.dumps(case["context"])), []).append(
            case
        )
    checked, refused, wrong = 0, 0, []
    for (user, context), cases in asked.items():
        params = {} if context == "null" else {"context": context}
        response = client.get(
            f"{FLAGS}/user/{user}", params=params, headers=keys["production"]
        )
        if len(user) > 255:
            # #2176: both routes refuse an id the evaluation cannot record.
            assert response.status_code == 422, response.text
            assert _refused_at(response) == [("string_too_long", ["path", "user_id"])]
            for case in cases:
                one = client.get(
                    f"{FLAGS}/evaluate/{case['flag']}",
                    params={"user_id": user, **params},
                    headers=keys["production"],
                )
                assert one.status_code == 422, one.text
                assert _refused_at(one) == [("string_too_long", ["query", "user_id"])]
                refused += 1
            continue
        assert response.status_code == 200, response.text
        bulk = response.json()
        assert all(value is True or value is False for value in bulk.values())
        for case in cases:
            one = _evaluate(
                client, keys["production"], case["flag"], user, case["context"]
            )
            checked += 1
            if bulk[case["flag"]] is not one["enabled"]:
                wrong.append((case["id"], bulk[case["flag"]], one["enabled"]))
            if one["enabled"] is not case["expected"]["enabled"]:
                wrong.append((case["id"], one, case["expected"]))
    assert wrong == []
    # Every case of the nine active flags; the inactive and the archived one
    # are left out of a bulk answer, as any such flag. The one with a
    # 300-character user_id is refused by both routes.
    assert (checked, refused) == (159, 1)


# ---------------------------------------------------------------------------
# What one evaluation costs and records
# ---------------------------------------------------------------------------


def _statements(client, db_session, headers, key: str) -> List[str]:
    """The statements one evaluation runs, after one unmeasured (the first
    request of a key writes when it was last used)."""
    _evaluate(client, headers, key, "user-1", PRO)
    engine = db_session.get_bind()
    statements: List[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()[:3]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        _evaluate(client, headers, key, "user-1", PRO)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return statements


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_one_evaluation_runs_the_statements_a_two_value_flags_does(
    client, db_session, keys, make_flag, environment
):
    two = make_flag(targeting_rules=RULE)
    split = make_flag(targeting_rules=RULE, split=SPLIT)
    ruled = make_flag(targeting_rules={**RULE, "value": "pro-copy"}, split=SPLIT)
    for flag in (two, split, ruled):
        configure(
            db_session,
            flag,
            "staging",
            targeting_rules=flag.targeting_rules,
            split=flag.split,
        )
    counted = {
        name: _statements(client, db_session, keys[environment], flag.key)
        for name, flag in (("two", two), ("split", split), ("ruled", ruled))
    }
    assert counted["split"] == counted["ruled"] == counted["two"], counted
    assert counted["two"], "nothing was recorded"


def _rows(db_session, flag) -> Dict[str, int]:
    db_session.expire_all()
    return {
        "assignments": db_session.query(Assignment).count(),
        "events": db_session.query(Event).count(),
        "evaluations": db_session.query(RawMetric)
        .filter(
            RawMetric.feature_flag_id == flag.id,
            RawMetric.metric_type == MetricType.FLAG_EVALUATION.value,
        )
        .count(),
        "metrics": db_session.query(RawMetric)
        .filter(RawMetric.feature_flag_id == flag.id)
        .count(),
    }


def test_an_evaluation_is_one_evaluation_and_no_part_of_an_experiment(
    client, db_session, keys, make_flag
):
    """No assignment, no exposure or other event: a split is not an experiment.
    The safety monitor counts each evaluation once, as any flag's."""
    flag = make_flag(
        split=SPLIT,
        rollout_percentage=60,
        targeting_rules={**RULE, "value": "pro-copy"},
    )
    configure(db_session, flag, "staging", split=SPLIT)
    two = make_flag(rollout_percentage=60, targeting_rules=RULE)
    before, two_before = _rows(db_session, flag), _rows(db_session, two)
    for user in USERS[:20]:
        for context in (PRO, FREE):
            _evaluate(client, keys["production"], flag.key, user, context)
            _evaluate(client, keys["production"], two.key, user, context)
            # Staging evaluations are not counted at all.
            _evaluate(client, keys["staging"], flag.key, user, context)
    after, two_after = _rows(db_session, flag), _rows(db_session, two)
    assert {name: after[name] - before[name] for name in after} == {
        "assignments": 0,
        "events": 0,
        "evaluations": 40,
        "metrics": two_after["metrics"] - two_before["metrics"],
    }
    assert two_after["evaluations"] - two_before["evaluations"] == 40


def test_the_prometheus_labels_are_the_flag_and_on_or_off(client, keys, make_flag):
    """One series per flag and result, whatever it serves: a label for the
    value would be a series for every value of every flag."""
    flag = make_flag(
        split=SPLIT,
        rollout_percentage=60,
        targeting_rules={**RULE, "value": "pro-copy"},
    )
    for user in USERS[:20]:
        for context in (PRO, FREE):
            _evaluate(client, keys["production"], flag.key, user, context)
    assert feature_flag_evaluations_total._labelnames == ("flag_key", "result")
    series = [
        sample.labels
        for family in REGISTRY.collect()
        if family.name == "feature_flag_evaluations"
        for sample in family.samples
        if sample.labels.get("flag_key") == flag.key
        and sample.name == "feature_flag_evaluations_total"
    ]
    assert sorted(series, key=lambda labels: labels["result"]) == [
        {"flag_key": flag.key, "result": "disabled"},
        {"flag_key": flag.key, "result": "enabled"},
    ]


# ---------------------------------------------------------------------------
# What the API accepts of such a flag
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_what_a_request_may_change_of_such_a_flag_is_kept_and_answered(
    client, db_session, owner, keys, make_flag
):
    """The same changes a task of the previous release makes during an upgrade.
    The split is kept; the answer follows what is stored, whatever the two
    values are; and nothing is a 500."""
    flag = make_flag(
        split=SPLIT,
        rollout_percentage=60,
        targeting_rules={**RULE, "value": "pro-copy"},
    )
    url, headers = f"{FLAGS}/{flag.id}", auth(owner)

    def put(body, status=200):
        response = client.put(url, json=body, headers=headers)
        assert response.status_code == status, response.text
        return response.json()

    def check(off: str, percentage: int, rule_value=None) -> None:
        for user in USERS[:40]:
            pro = _evaluate(client, keys["production"], flag.key, user, PRO)
            free = _evaluate(client, keys["production"], flag.key, user, FREE)
            on = hashing.flag_bucket(user, flag.key) < percentage
            pick = _pick(user, flag.key)
            assert (free["enabled"], free["value"]) == (on, pick if on else off)
            if rule_value is not None:
                assert (pro["enabled"], pro["value"]) == (True, rule_value)

    check("off", 60, "pro-copy")
    # The two values: any of their type, the split's own among them.
    put({"on_value": "another", "off_value": "red"})
    check("red", 60, "pro-copy")
    put({"rollout_percentage": 25})
    check("red", 25, "pro-copy")
    put({"description": "kept"})
    check("red", 25, "pro-copy")
    # Rules without a value of their own replace the ones that had one.
    put({"targeting_rules": RULE})
    for user in USERS[:40]:
        body = _evaluate(client, keys["production"], flag.key, user, PRO)
        assert body["reason"] == "targeting_rule"
        assert body["value"] == _pick(user, flag.key)
    put({"targeting_rules": None})
    check("red", 25)
    # Sending either sets it again (refused here before the write path took
    # them), and the stored rules without a value are what is kept below.
    put({"split": SPLIT})
    put({"targeting_rules": {**RULE, "value": "pro-copy"}})
    put({"targeting_rules": None})
    put({"is_active": False})
    for user in USERS[:5]:
        body = _evaluate(client, keys["production"], flag.key, user, PRO)
        assert (body["enabled"], body["reason"], body["value"]) == (
            False,
            "inactive",
            "red",
        )
    put({"is_active": True})
    check("red", 25)
    db_session.expire_all()
    stored = db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one()
    assert stored.split == SPLIT and stored.targeting_rules is None


def test_an_unencodable_user_id_never_reaches_a_split(client, keys, make_flag):
    """A lone surrogate cannot be hashed. A body that holds one is refused
    before the evaluation, and a URL cannot hold one, so a flag on for everyone,
    which hashes nothing for its rollout, is no 500 either."""
    flag = make_flag(split=SPLIT)
    response = client.post(
        f"{FLAGS}/evaluate/{flag.key}",
        content='{"user_id": "user-\\ud800"}',
        headers={**keys["production"], "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    response = client.get(
        f"{FLAGS}/evaluate/{flag.key}?user_id=user-%ED%A0%80",
        headers=keys["production"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["value"] in {"red", "green", "blue"}


def test_a_stored_split_outside_its_rules_is_answered_without_a_value(
    client, db_session, keys, make_flag
):
    """For every user, in the environment that stores it, and nowhere else."""
    flag = make_flag(split=_split(34, 33, 32))
    configure(db_session, flag, "staging", split=SPLIT)
    for user in USERS[:5]:
        body = _evaluate(client, keys["production"], flag.key, user)
        assert "value" not in body and body["enabled"] is True, body
        assert _evaluate(client, keys["staging"], flag.key, user)["value"] == _pick(
            user, flag.key
        )


# ---------------------------------------------------------------------------
# Staging and production
# ---------------------------------------------------------------------------


def test_staging_serves_its_own_split_and_production_does_not_move(
    client, db_session, keys, make_flag
):
    """Production 34/33/33, staging 50/25/25. Every user's value in each is the
    ground-truth copy's for that environment's weights, and production's are
    what they were before staging had any."""
    flag = make_flag(split=SPLIT)
    before = _values(client, keys["production"], flag.key)
    assert before == {user: _pick(user, flag.key) for user in USERS}
    half = _split(50, 25, 25)
    configure(db_session, flag, "staging", split=half)
    staging = _values(client, keys["staging"], flag.key)
    assert staging == {user: _pick(user, flag.key, half) for user in USERS}
    assert _values(client, keys["production"], flag.key) == before
    assert Counter(staging.values()) != Counter(before.values())
    # Development, which nothing configures, is off.
    development = _values(client, keys["development"], flag.key, USERS[:5])
    assert set(development.values()) == {"off"}
    # And the other way round: production's change leaves staging.
    flag.split = _split(0, 0, 100)
    db_session.commit()
    assert set(_values(client, keys["production"], flag.key).values()) == {"blue"}
    assert _values(client, keys["staging"], flag.key) == staging


def test_the_same_split_gives_a_user_the_same_value_in_every_environment(
    client, db_session, keys, make_flag
):
    """Over real keys: no environment enters the bucket, so a split copied to
    another environment gives the same users the same values."""
    flag = make_flag(split=SPLIT, rollout_percentage=60)
    for environment in ("staging", "development"):
        configure(db_session, flag, environment, split=SPLIT, rollout_percentage=60)
    production = _values(client, keys["production"], flag.key)
    assert set(production.values()) == {"red", "green", "blue", "off"}
    for environment in ("staging", "development"):
        assert _values(client, keys[environment], flag.key) == production


def test_each_environments_ruleset_says_what_it_serves(
    client, db_session, keys, make_flag
):
    """A split in staging alone makes staging's entry remote, and production's
    stays the local entry it was; and the other way round."""
    in_staging, in_production = make_flag(), make_flag(split=SPLIT)
    for flag, split in ((in_staging, SPLIT), (in_production, None)):
        configure(db_session, flag, "staging", split=split)

    def entry(environment: str, flag: FeatureFlag) -> Dict[str, Any]:
        served = client.get(RULESET, headers=keys[environment]).json()["flags"]
        return next(e for e in served if e["key"] == flag.key)

    for flag, remote_in, local_in in (
        (in_staging, "staging", "production"),
        (in_production, "production", "staging"),
    ):
        assert entry(remote_in, flag) == {
            "key": flag.key,
            "active": True,
            "evaluation": "remote",
        }
        local = entry(local_in, flag)
        assert (local["evaluation"], local["on_value"], local["off_value"]) == (
            "local",
            "on",
            "off",
        )
        assert "split" not in local
