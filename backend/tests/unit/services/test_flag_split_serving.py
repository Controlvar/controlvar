"""What a flag with a split, or with a rule's own value, answers (#2012).

Through the flag service's evaluation and the route's answer builder, without a
database: the flag is a transient row. Every number below was worked out by the
ground-truth copy of the hash (``ground_truth.hashing``), for the key and the
ids written here, and each test holds the server to it and the copy to the
literal, so neither can drift with the other.

* The same user is served the same value by every service, process and call.
* Exact counts over 10,000 ids, and among the users the flag is on for.
* The value does not depend on the rollout: every value is reachable below
  100%, and raising the percentage changes nobody's value.
* Changing the weights moves exactly the users between the moved boundaries.
* No environment enters the bucket.
* The answer's keys are the ones every flag's answer has.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

import pytest

from backend.app.api.v1.endpoints.feature_flags import evaluation_answer
from backend.app.core.flag_environments import flag_in_environment
from backend.app.models import register_core_models
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.tests.unit.ground_truth.library import REPO_ROOT, hashing

pytestmark = pytest.mark.unit

register_core_models()

KEY = "my-flag"
USERS = [f"user-{n}" for n in range(10_000)]
MORE_USERS = [f"user-{n}" for n in range(20_000)]


#: In an order nothing sorts them into.
VALUES = ("red", "green", "blue", "amber")


def _split(*weights: int) -> List[Dict[str, Any]]:
    """``red``, ``green``, ``blue`` ... with these weights, in this order."""
    return [{"value": v, "weight": w} for v, w in zip(VALUES, weights)]


THREE = _split(34, 33, 33)


def _flag(**fields) -> FeatureFlag:
    base = {
        "id": uuid.uuid4(),
        "key": KEY,
        "name": KEY,
        "status": FeatureFlagStatus.ACTIVE,
        "rollout_percentage": 100,
        "targeting_rules": None,
        "value_type": "string",
        "on_value": "on",
        "off_value": "off",
        "split": None,
    }
    return FeatureFlag(**{**base, **fields})


def answer(
    flag: Any,
    user: str,
    context: Optional[Dict[str, Any]] = None,
    service: Optional[FeatureFlagService] = None,
) -> Dict[str, Any]:
    """The evaluate route's body: the service's evaluation, the route's builder."""
    evaluation = (service or FeatureFlagService(db=None)).evaluate_flag_detailed(
        flag, user, context, record_metrics=False
    )
    return evaluation_answer(flag.key, flag, evaluation, user)


def values(flag: Any, users: Iterable[str]) -> Dict[str, Any]:
    """``{user: value}`` for the users the flag is on for."""
    service = FeatureFlagService(db=None)
    answers = {user: answer(flag, user, service=service) for user in users}
    return {user: body["value"] for user, body in answers.items() if body["enabled"]}


# ---------------------------------------------------------------------------
# The same answer, every time
# ---------------------------------------------------------------------------

#: Prints the sha256 of what USERS are served, in a process of its own.
_IN_A_PROCESS = """
import hashlib, json
from backend.tests.unit.services import test_flag_split_serving as t
served = t.values(t._flag(split=t.THREE, rollout_percentage=60), t.USERS)
print("DIGEST", hashlib.sha256(json.dumps(served, sort_keys=True).encode()).hexdigest())
"""


def test_the_same_user_is_served_the_same_value_by_every_service_and_call():
    flag = _flag(split=THREE, rollout_percentage=60)
    again = _flag(split=json.loads(json.dumps(THREE)), rollout_percentage=60)
    first, second = values(flag, USERS), values(again, USERS)
    differ = [
        f"{user}: {first.get(user)} then {second.get(user)}"
        for user in USERS
        if first.get(user) != second.get(user)
    ]
    assert differ == []
    assert values(flag, reversed(USERS)) == first


def test_the_same_user_is_served_the_same_value_by_every_process():
    """Each with another hash seed, so a value that followed Python's own hash,
    or one drawn when the process starts, differs."""
    served = values(_flag(split=THREE, rollout_percentage=60), USERS)
    here = hashlib.sha256(json.dumps(served, sort_keys=True).encode()).hexdigest()
    digests = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
        run = subprocess.run(
            [sys.executable, "-c", _IN_A_PROCESS],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        digests += [
            line.split()[1]
            for line in run.stdout.splitlines()
            if line.startswith("DIGEST ")
        ]
    assert digests == [here, here]


def test_the_exact_distribution_over_10000_ids():
    """34/33/33 over user-0 .. user-9999, on for everyone."""
    expected = {"red": 3416, "green": 3368, "blue": 3216}
    assert Counter(hashing.split_value(u, KEY, THREE) for u in USERS) == expected
    served = values(_flag(split=THREE), USERS)
    assert len(served) == 10_000
    assert Counter(served.values()) == expected


# ---------------------------------------------------------------------------
# The split and the rollout are independent
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_every_value_is_reachable_while_the_flag_is_on_for_some():
    """At 30%, over user-0 .. user-19999: were the split to share the rollout's
    bucket, all 6020 enabled users would get the first value."""
    expected = {"red": 2024, "green": 2040, "blue": 1956}
    enabled = [u for u in MORE_USERS if hashing.flag_bucket(u, KEY) < 30]
    assert len(enabled) == 6020
    assert Counter(hashing.split_value(u, KEY, THREE) for u in enabled) == expected
    served = values(_flag(split=THREE, rollout_percentage=30), MORE_USERS)
    assert set(served) == set(enabled)
    assert Counter(served.values()) == expected


def test_raising_the_rollout_changes_nobodys_value():
    low = values(_flag(split=THREE, rollout_percentage=10), USERS)
    high = values(_flag(split=THREE, rollout_percentage=50), USERS)
    assert (len(low), len(high)) == (1015, 5004)
    changed = [user for user, value in low.items() if high.get(user) != value]
    assert changed == [], f"{len(changed)} users changed value when the rollout rose"


# ---------------------------------------------------------------------------
# Changing the weights
# ---------------------------------------------------------------------------

#: ``(before, after, the users of user-0 .. user-19999 who moved)``, worked out
#: by the ground-truth copy. The first two are a re-weighting and an appended
#: value; then each way of moving one boundary, and one that moves two.
RE_WEIGHTINGS = [
    ((34, 33, 33), (50, 25, 25), {"green>red": 3338, "blue>green": 1497}),
    ((34, 33, 33), (34, 33, 23, 10), {"blue>amber": 2037}),
    ((34, 33, 33), (34, 43, 23), {"blue>green": 1937}),
    ((34, 33, 33), (34, 23, 43), {"green>blue": 2032}),
    ((34, 33, 33), (44, 23, 33), {"green>red": 2105}),
    ((50, 30, 20), (50, 20, 30), {"green>blue": 2017}),
    ((34, 33, 33), (44, 33, 23), {"green>red": 2105, "blue>green": 1937}),
    ((50, 30, 20), (100, 0, 0), {"green>red": 5970, "blue>red": 3949}),
]


def _moves(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, int]:
    assert before.keys() == after.keys()
    return dict(
        Counter(
            f"{before[user]}>{after[user]}"
            for user in before
            if before[user] != after[user]
        )
    )


@pytest.mark.parametrize("before, after, moved", RE_WEIGHTINGS)
def test_changing_the_weights_moves_exactly_these_users(before, after, moved):
    by_copy = [
        {u: hashing.split_value(u, KEY, _split(*weights)) for u in MORE_USERS}
        for weights in (before, after)
    ]
    assert _moves(*by_copy) == moved
    served = [
        values(_flag(split=_split(*weights)), MORE_USERS) for weights in (before, after)
    ]
    assert _moves(*served) == moved


@pytest.mark.parametrize("before, after, moved", RE_WEIGHTINGS)
def test_the_transitions_are_the_ones_the_cumulative_ranges_say(before, after, moved):
    """What a weight change moves can be read off the two lists of weights: a
    bucket that is one value's before and another's after."""

    def owner(weights) -> List[str]:
        return [v for v, weight in zip(VALUES, weights) for _ in range(weight)]

    buckets = Counter(hashing.split_bucket(u, KEY) for u in MORE_USERS)
    predicted: Counter = Counter()
    for bucket, (old, new) in enumerate(zip(owner(before), owner(after))):
        if old != new:
            predicted[f"{old}>{new}"] += buckets[bucket]
    assert dict(predicted) == moved


@pytest.mark.parametrize(
    "before, after, raised",
    [
        ((50, 30, 20), (50, 20, 30), "blue"),
        ((34, 33, 33), (34, 43, 23), "green"),
        ((34, 33, 33), (44, 23, 33), "red"),
        ((34, 33, 33), (34, 33, 23, 10), "amber"),
    ],
)
def test_moving_one_boundary_leaves_the_raised_values_users_where_they_are(
    before, after, raised
):
    """Taking from a neighbour: the only users who move are the neighbour's, to
    the raised value, about as many of every hundred as the weight moved."""
    old = values(_flag(split=_split(*before)), MORE_USERS)
    new = values(_flag(split=_split(*after)), MORE_USERS)
    moved = _moves(old, new)
    assert len(moved) == 1
    ((transition, count),) = moved.items()
    assert transition.endswith(f">{raised}")
    assert 0.095 < count / len(MORE_USERS) < 0.106


def test_taking_from_a_value_that_is_no_neighbour_moves_the_ones_between():
    """34/33/33 to 44/33/23: green's weight does not change and its users do."""
    old = values(_flag(split=_split(34, 33, 33)), MORE_USERS)
    new = values(_flag(split=_split(44, 33, 23)), MORE_USERS)
    assert set(_moves(old, new)) == {"green>red", "blue>green"}


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


def _in_staging(flag: FeatureFlag, **setting) -> Any:
    base = {
        "environment": "staging",
        "feature_flag_id": flag.id,
        "is_active": True,
        "rollout_percentage": 100,
        "targeting_rules": None,
        "split": None,
    }
    return flag_in_environment(flag, SimpleNamespace(**{**base, **setting}), "staging")


def test_the_same_split_serves_a_user_the_same_value_in_every_environment():
    flag = _flag(split=THREE, rollout_percentage=60)
    staging = _in_staging(flag, split=THREE, rollout_percentage=60)
    production_values = values(flag, USERS)
    staging_values = values(staging, USERS)
    differ = [
        f"{user}: {production_values.get(user)} in production, "
        f"{staging_values.get(user)} in staging"
        for user in USERS
        if production_values.get(user) != staging_values.get(user)
    ]
    assert differ == []
    assert len(production_values) > 5000


def test_each_environment_serves_its_own_split_and_rule_values():
    """And neither reads the other's: staging's change leaves production's
    counts exactly as they were."""
    flag = _flag(split=THREE)
    before = Counter(values(flag, USERS).values())
    staging = _in_staging(flag, split=_split(50, 25, 25))
    changed = Counter(values(staging, USERS).values())
    assert changed == {"red": 5096, "green": 2425, "blue": 2479}
    after = Counter(values(flag, USERS).values())
    assert after == before == {"red": 3416, "green": 3368, "blue": 3216}
    # A flag with no split in one of them serves its on value there.
    assert set(values(_in_staging(flag), USERS[:50]).values()) == {"on"}
    none = _flag()
    with_one = _in_staging(none, split=THREE)
    assert set(values(with_one, USERS[:50]).values()) == {"red", "green", "blue"}
    assert set(values(none, USERS[:50]).values()) == {"on"}
    # A rule's own value rides in the environment's rules.
    ruled = _in_staging(flag, targeting_rules={**EVERYONE, "value": "staging-copy"})
    assert answer(ruled, "user-1")["value"] == "staging-copy"
    assert answer(flag, "user-1")["value"] in {"red", "green", "blue"}


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------

#: A rule every user matches: none of them sends ``plan``.
EVERYONE = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "is_null", "value": None}],
        }
    ],
}
KEYS = ["key", "enabled", "value", "value_type", "config", "reason"]
#: ``user-123``'s split bucket under my-flag is 49: the second value's.
ONE_USER = "user-123"
TYPED = {
    "string": ("on", "off", ["red", "green", "blue"]),
    "number": (10, 2.5, [1, 20, 30.5]),
    "json": ({"a": 1}, [], [{"a": 2}, {"a": 3}, []]),
}


def _typed(value_type: str, **fields) -> FeatureFlag:
    on_value, off_value, split = TYPED[value_type]
    fields.setdefault(
        "split", [{"value": v, "weight": w} for v, w in zip(split, (34, 33, 33))]
    )
    return _flag(
        value_type=value_type, on_value=on_value, off_value=off_value, **fields
    )


def _body(value, value_type, enabled=True, reason="rollout") -> Dict[str, Any]:
    return {
        "key": KEY,
        "enabled": enabled,
        "value": value,
        "value_type": value_type,
        "config": None,
        "reason": reason,
    }


@pytest.mark.regression
def test_every_answer_has_exactly_the_keys_it_had():
    """Boolean and two-value flags, and flags with a split or a rule's own
    value: same keys, same order, one of the four types, one of the four
    reasons. The route's response model is an open object, so the OpenAPI
    document would not show one more."""
    boolean = _flag(value_type="boolean", on_value=None, off_value=None)
    asked = [(boolean, True, "boolean", "rollout")]
    boolean_off = _flag(
        value_type="boolean", on_value=None, off_value=None, rollout_percentage=0
    )
    asked.append((boolean_off, False, "boolean", "rollout"))
    for value_type, (on_value, off_value, split) in TYPED.items():
        ruled = {**EVERYONE, "value": split[0]}
        asked += [
            (_typed(value_type, split=None), on_value, value_type, "rollout"),
            (
                _typed(value_type, split=None, rollout_percentage=0),
                off_value,
                value_type,
                "rollout",
            ),
            (_typed(value_type), split[1], value_type, "rollout"),
            (
                _typed(value_type, rollout_percentage=0),
                off_value,
                value_type,
                "rollout",
            ),
            (
                _typed(value_type, targeting_rules=ruled),
                split[0],
                value_type,
                "targeting_rule",
            ),
            (
                _typed(value_type, split=None, targeting_rules=ruled),
                split[0],
                value_type,
                "targeting_rule",
            ),
            (
                _typed(value_type, status=FeatureFlagStatus.INACTIVE),
                off_value,
                value_type,
                "inactive",
            ),
            (
                _typed(value_type, status=FeatureFlagStatus.ARCHIVED),
                off_value,
                value_type,
                "inactive",
            ),
        ]
    assert len(asked) == 26
    for flag, value, value_type, reason in asked:
        body = answer(flag, ONE_USER)
        assert list(body) == KEYS, body
        off_value = flag.off_value if value_type != "boolean" else False
        assert body == _body(value, value_type, value != off_value, reason)
        assert type(body["value"]) is type(value)


def test_an_unservable_split_is_answered_without_a_value():
    """As any other stored value outside the rules: no ``value`` key."""
    flag = _flag(split=_split(34, 33, 32))
    body = answer(flag, ONE_USER)
    assert list(body) == [key for key in KEYS if key != "value"]
    assert (body["enabled"], body["reason"]) == (True, "rollout")


HALF = {
    "rules": [
        {
            "id": "half",
            "rollout_percentage": 50,
            "priority": 0,
            "rule": {
                "operator": "and",
                "conditions": [{"attribute": "plan", "operator": "eq", "value": "pro"}],
            },
            "value": "pro-copy",
        },
        {
            "id": "team",
            "rollout_percentage": 100,
            "priority": 1,
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "plan", "operator": "eq", "value": "team"}
                ],
            },
        },
    ]
}


def test_the_exact_answer_for_each_way_a_rule_decides():
    """Matched and inside the rule's percentage: the rule's own value, or with
    none the split's. Matched and outside: the off value, never the split's and
    never the rollout's. Not matched: the flag's percentage, then the split."""
    flag = _flag(split=THREE, rollout_percentage=40, targeting_rules=HALF)
    users = USERS[:2000]
    service = FeatureFlagService(db=None)
    inside = {u for u in users if hashing.flag_bucket(u, KEY) < 50}
    rolled_out = {u for u in users if hashing.flag_bucket(u, KEY) < 40}
    assert (len(inside), len(rolled_out)) == (993, 781)
    for user in users:
        pick = hashing.split_value(user, KEY, THREE)
        for plan, on, value, reason in (
            ("pro", user in inside, "pro-copy", "targeting_rule"),
            ("team", True, pick, "targeting_rule"),
            ("free", user in rolled_out, pick, "rollout"),
        ):
            body = answer(flag, user, {"plan": plan}, service)
            expected = _body(value if on else "off", "string", on, reason)
            assert body == expected, (user, plan)


def test_a_rule_without_a_value_serves_the_on_value_when_there_is_no_split():
    flag = _flag(targeting_rules=HALF)
    body = answer(flag, ONE_USER, {"plan": "team"})
    assert body == _body("on", "string", True, "targeting_rule")


def test_what_an_older_release_may_write_is_answered():
    """The previous release writes the two values, the percentage and the rules
    of such a flag, and knows neither its split nor a rule's value: nothing
    assumes the on value is one of the split's, or that a rule has kept its
    value."""
    flag = _flag(split=THREE, rollout_percentage=40, targeting_rules=HALF)
    flag.on_value, flag.off_value = "another", "red"
    assert answer(flag, ONE_USER, {"plan": "team"})["value"] == "green"
    off = next(u for u in USERS if hashing.flag_bucket(u, KEY) >= 50)
    assert answer(flag, off, {"plan": "pro"})["value"] == "red"
    flag.targeting_rules = {"rules": [HALF["rules"][1]]}
    assert answer(flag, off, {"plan": "pro"})["reason"] == "rollout"
    flag.targeting_rules = None
    assert answer(flag, off) == _body("red", "string", False, "rollout")
    flag.rollout_percentage = 0
    assert answer(flag, ONE_USER) == _body("red", "string", False, "rollout")
