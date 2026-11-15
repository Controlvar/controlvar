"""G1: a percentage rollout keeps each user's answer as the percentage moves.

Use case #1 (D210): a flag is ramped up, down and back, and each user stays on
the side of the percentage the flag's hash puts them on. Every answer comes
from the server's public ``FeatureFlagService.evaluate_flag_detailed`` (no
database: the metrics write is stubbed) and is compared, user by user, with
O-FLAG (``ground_truth.hashing.flag_bucket``), which imports nothing from the
product. Monotonicity alone is not enough: a server that bucketed on the
experiment hash, or on the flag's id instead of its key, would still only add
users as the percentage rises, but it would give them to the wrong users, and
only the comparison with O-FLAG sees that.

* G1-1a: 100,000 ids, one flag ramped 10, 50, 10, 60, 100, 0. At every step
  the enabled set is exactly O-FLAG's; the users enabled at 10% are enabled at
  50% and at 60%, those at 60% at 100%; the second 10% is the first; 100% is
  everyone and 0% no one.
* G1-2 (in process; the HTTP half is
  ``backend/tests/integration/api/test_flag_rule_rollout_api.py``): a
  dashboard rule ``country in [US, CA]`` whose own percentage ramps 30, 60, 30
  over a global 10%, 20,000 seeded ids. A user the rule matches is enabled iff
  O-FLAG is below the rule's percentage, with reason ``targeting_rule`` and
  the rule's id; anyone else iff O-FLAG is below 10, with reason ``rollout``.

``docs/feature-flags/create.md`` (Step 4) and ``docs/guides/user-guide.md``
cite the first test for the sentences that say a user always lands on the
same side of the percentage.
"""

from __future__ import annotations

import random
import uuid

import pytest

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.metrics_service import MetricsService
from backend.tests.unit.ground_truth.library import hashing

pytestmark = pytest.mark.unit

RAMP = (10, 50, 10, 60, 100, 0)
RAMP_KEY = "uc-g1-ramp"
RAMP_USERS = [f"u-{number:06d}" for number in range(1, 100001)]
#: O-FLAG's enabled counts on RAMP_USERS at each step of RAMP: if the oracle
#: moved, the comparison below would prove nothing.
RAMP_ORACLE_COUNTS = [9939, 49739, 9939, 59818, 100000, 0]

RULE_KEY = "uc-g12-rule"
RULE_RAMP = (30, 60, 30)
GLOBAL_PERCENTAGE = 10
RULE_USERS = [f"g12-{number:05d}" for number in range(20000)]
#: 40/20/15/15/10, so about 60% of the users match the rule.
COUNTRY_SLOTS = ("US",) * 8 + ("CA",) * 4 + ("GB",) * 3 + ("DE",) * 3 + ("FR",) * 2
MATCHED = frozenset({"US", "CA"})


def _rule(percentage: int) -> dict:
    """The dashboard editor's shape, the rule carrying its own percentage."""
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "in", "value": ["US", "CA"]}
                ],
            }
        ],
        "rollout_percentage": percentage,
    }


def _flag(key: str, percentage: int, rules: dict | None = None) -> FeatureFlag:
    return FeatureFlag(
        id=uuid.uuid5(uuid.NAMESPACE_URL, key),
        key=key,
        name=key,
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=percentage,
        targeting_rules=rules,
    )


@pytest.fixture
def service(monkeypatch) -> FeatureFlagService:
    """The server's evaluator with its metrics writes stubbed (no database)."""
    monkeypatch.setattr(
        MetricsService, "record_flag_evaluation", staticmethod(lambda **_: None)
    )
    monkeypatch.setattr(MetricsService, "log_error", staticmethod(lambda **_: None))
    return FeatureFlagService(db=None)


def test_a_user_keeps_their_side_as_the_rollout_moves(service):
    flag = _flag(RAMP_KEY, 0)
    buckets = [hashing.flag_bucket(user, RAMP_KEY) for user in RAMP_USERS]
    total = len(RAMP_USERS)
    problems: list[str] = []
    enabled: list[set[str]] = []
    oracle_counts: list[int] = []
    for percentage in RAMP:
        flag.rollout_percentage = percentage
        server: set[str] = set()
        wrong_reason = 0
        for user in RAMP_USERS:
            answer = service.evaluate_flag_detailed(flag, user)
            if answer["enabled"]:
                server.add(user)
            if answer["reason"] != "rollout" or answer["rule_id"] is not None:
                wrong_reason += 1
        oracle = {u for u, b in zip(RAMP_USERS, buckets) if b < percentage}
        oracle_counts.append(len(oracle))
        if server != oracle:
            problems.append(
                f"server != oracle at {percentage}%: {len(server ^ oracle)} users "
                f"differ (server {len(server)}, oracle {len(oracle)})"
            )
        if wrong_reason:
            problems.append(
                f"{wrong_reason} answers at {percentage}% not reason rollout"
            )
        enabled.append(server)

    first_10, at_50, second_10, at_60, at_100, at_0 = enabled
    for low, high, smaller, larger in (
        (10, 50, first_10, at_50),
        (10, 60, first_10, at_60),
        (60, 100, at_60, at_100),
    ):
        lost = len(smaller - larger)
        if lost:
            problems.append(
                f"monotonicity: {lost} users enabled at {low}% are disabled at {high}%"
            )
    if second_10 != first_10:
        problems.append(
            f"returning to 10%: {len(second_10 ^ first_10)} users differ from the "
            "first 10%"
        )
    if len(at_100) != total:
        problems.append(f"100%: {len(at_100)} of {total} users enabled, expected all")
    if at_0:
        problems.append(f"0%: {len(at_0)} users enabled, expected none")
    if oracle_counts != RAMP_ORACLE_COUNTS:
        problems.append(
            f"the oracle moved: {oracle_counts}, expected {RAMP_ORACLE_COUNTS}"
        )
    print(f"G1-1a enabled per step {[len(s) for s in enabled]}")
    assert problems == [], "\n".join(problems)


def test_a_matched_rule_decides_with_its_own_percentage_as_it_moves(service):
    draw = random.Random(1003)
    countries = [draw.choice(COUNTRY_SLOTS) for _ in RULE_USERS]
    buckets = [hashing.flag_bucket(user, RULE_KEY) for user in RULE_USERS]
    matched_total = sum(country in MATCHED for country in countries)
    # Both populations, so either half of the decision can fail.
    assert 0 < matched_total < len(RULE_USERS), matched_total

    flag = _flag(RULE_KEY, GLOBAL_PERCENTAGE)
    problems: list[str] = []
    for rule_percentage in RULE_RAMP:
        flag.targeting_rules = _rule(rule_percentage)
        matched_enabled_wrong = matched_reason_wrong = 0
        other_enabled_wrong = other_reason_wrong = 0
        for user, country, bucket in zip(RULE_USERS, countries, buckets):
            answer = service.evaluate_flag_detailed(flag, user, {"country": country})
            if country in MATCHED:
                if answer["enabled"] != (bucket < rule_percentage):
                    matched_enabled_wrong += 1
                if (answer["reason"], answer["rule_id"]) != (
                    "targeting_rule",
                    "dashboard",
                ):
                    matched_reason_wrong += 1
            else:
                if answer["enabled"] != (bucket < GLOBAL_PERCENTAGE):
                    other_enabled_wrong += 1
                if (answer["reason"], answer["rule_id"]) != ("rollout", None):
                    other_reason_wrong += 1
        if matched_enabled_wrong:
            problems.append(
                f"{matched_enabled_wrong} of {matched_total} matched users enabled "
                f"!= O-FLAG at {rule_percentage}%"
            )
        if matched_reason_wrong:
            problems.append(
                f"{matched_reason_wrong} users reason wrong at {rule_percentage}% "
                "(expected targeting_rule, rule dashboard)"
            )
        if other_enabled_wrong:
            problems.append(
                f"{other_enabled_wrong} of {len(RULE_USERS) - matched_total} "
                f"unmatched users enabled != O-FLAG at {GLOBAL_PERCENTAGE}% "
                f"(rule at {rule_percentage}%)"
            )
        if other_reason_wrong:
            problems.append(
                f"{other_reason_wrong} unmatched users reason wrong at "
                f"{rule_percentage}% (expected rollout)"
            )
    print(f"G1-2 matched {matched_total} of {len(RULE_USERS)}")
    assert problems == [], "\n".join(problems)
