"""G0b, the split within groups (#1897, S5 of the live replay), with no
server: S5 sent to a fake server that balances as #448 does (each group's
next place in its blocks, from the product's own ``balanced_blocks``), whose
ledger and balanced groups every G0b gate accepts; then the defects the live
run plants in the product, applied to the fake instead, which their gates
refuse with the lines the live run prints:

* each place keyed on the user's hash, not the group's seq: G0b (1),
  ``S5 group US: treatment <n> != 1,400 at n=2,000``;
* the context dropped before the group is read: G0b (3),
  ``S5: unknown has 6,000 users, expected 600``;
* no counter step ever free (every user "busy", assigned by the hash): G0b (3),
  ``S5: users_outside_blocks 6,000, expected 0``.

Also each gate's edges: the bound at 30/70 (2.1, exact at every tenth user),
a returning user answered another variant, and a server that assigned fewer
users than the plan sends.
"""

from __future__ import annotations

import importlib
import io
import re
from collections import Counter
from dataclasses import replace

import pytest

from backend.app.core import balanced_blocks
from backend.tests.unit.ground_truth.library import (
    contract,
    generator,
    hashing,
    ledger,
    replay,
)

suite = importlib.import_module("test_replay")

pytestmark = pytest.mark.unit

SEED = 20261103
S5 = suite.scenarios(SEED, 1)["S5"]
#: The fake server's ``balance_seed``: any key gives the same guarantees.
BALANCE_SEED = "unit-balance-seed"


class FakeBalancedServer:
    """Answers a balanced plan one request at a time, as a server with #448
    does: a new user's group from the top level of their context, the group's
    next place in its blocks, and a returning user their stored variant.
    *plant* is one of the live run's defects, or None."""

    def __init__(self, plan: generator.Plan, plant: str | None = None) -> None:
        assert plan.scenario.balance is not None
        self.setting = plan.scenario.balance.setting()
        self.allocations = plan.allocations()
        self.shares = [share for _, share in self.allocations]
        self.key = plan.scenario.experiment_key
        self.plant = plant
        self.next_seq: Counter[str] = Counter()
        #: user -> (variant, group, seq or None)
        self.stored: dict[str, tuple[str, str, int | None]] = {}

    def variant(self, user: str, context: object) -> str:
        if user in self.stored:
            return self.stored[user][0]
        if self.plant == "dropped context":
            context = None
        group = balanced_blocks.group_of(self.setting, context)
        if self.plant == "busy":
            name, seq = hashing.variant_for(user, self.key, self.allocations), None
        else:
            seq = self.next_seq[group]
            self.next_seq[group] += 1
            place = hashing.bucket(user, self.key) if self.plant == "hash" else seq
            index = balanced_blocks.slot_variant(
                BALANCE_SEED, group, place, self.shares
            )
            name = self.allocations[index][0]
        self.stored[user] = (name, group, seq)
        return name

    def answer(self, op, default):
        status, body = default
        if op.kind == "assign":
            name = self.variant(op.body["user_id"], op.body.get("context"))
            return status, {
                **body,
                "variant_id": f"id-{name}",
                "variant_name": name,
                "is_control": name == self.allocations[0][0],
            }
        if op.kind == "assign_batch":
            rows = [
                {
                    **row,
                    "variant_id": f"id-{self.variant(u['user_id'], u.get('context'))}",
                }
                for row, u in zip(body["assignments"], op.body["users"], strict=True)
            ]
            return status, {**body, "assignments": rows}
        return default

    def balanced_groups(self) -> dict:
        """``GET .../balanced-groups``'s group rows and fallback count."""
        sizes = Counter(group for _, group, _ in self.stored.values())
        return {
            "groups": {
                "segments": [
                    {"segment_value": group, "sample_size": users}
                    for group, users in sizes.items()
                ]
            },
            "users_outside_blocks": sum(
                1 for _, _, seq in self.stored.values() if seq is None
            ),
        }


def _sent(scenario, plant=None):
    """The plan sent to the fake: (plan, records, experiment, fake)."""
    plan = generator.simulate(SEED, scenario)
    fake = FakeBalancedServer(plan, plant)
    text = replay(plan, declared=suite.DECLARED, answer=fake.answer)
    records = ledger.read(io.StringIO(text))
    experiment = contract.valid_events(records).experiments[scenario.experiment_key]
    return plan, records, experiment, fake


def _gates(scenario, plant=None):
    """Every G0b gate's lines for *scenario* sent to the fake with *plant*."""
    plan, records, experiment, fake = _sent(scenario, plant)
    groups = generator.user_groups(plan)
    found = suite.arrivals(
        records, scenario.experiment_key, groups, scenario.balance.groups()
    )
    planned = Counter(groups.values())
    return {
        "balance": suite.g0b_balance_lines(
            scenario.name, found, plan.allocations(), dict(planned)
        ),
        "returning": suite.g0b_returning_lines(
            scenario.name,
            experiment,
            suite.returning_answers(records, scenario.experiment_key),
            suite.returning_sends(plan),
        ),
        "groups": suite.g0b_groups_lines(scenario.name, fake.balanced_groups(), found),
    }


@pytest.fixture(scope="module")
def honest():
    return _gates(S5)


# ---------------------------------------------------------------------------
# A correct server passes; each planted defect is refused with its line
# ---------------------------------------------------------------------------
def test_a_correct_balanced_server_passes_every_g0b_gate(honest):
    assert honest == {"balance": [], "returning": [], "groups": []}


def test_g0b1_a_place_keyed_on_the_user_s_hash_is_refused():
    lines = _gates(S5, "hash")
    us = [
        line
        for line in lines["balance"]
        if re.fullmatch(r"S5 group US: treatment [\d,]+ != 1,400 at n=2,000", line)
    ]
    assert len(us) == 1 and us[0] != "S5 group US: treatment 1,400 != 1,400 at n=2,000"
    assert lines["groups"] == []  # the groups are right; their split is not


def test_g0b3_a_dropped_context_puts_everyone_in_unknown():
    lines = _gates(S5, "dropped context")
    assert lines["groups"] == [
        "S5: US has 0 users, expected 2,000",
        "S5: DE has 0 users, expected 1,500",
        "S5: FR has 0 users, expected 1,400",
        "S5: other has 0 users, expected 500",
        "S5: unknown has 6,000 users, expected 600",
    ]


def test_g0b3_every_user_busy_is_outside_the_blocks():
    lines = _gates(S5, "busy")
    assert lines["groups"] == ["S5: users_outside_blocks 6,000, expected 0"]
    assert any(line.startswith("S5 group US: ") for line in lines["balance"])


# ---------------------------------------------------------------------------
# The edges of each gate
# ---------------------------------------------------------------------------
ARMS = [("control", 30), ("treatment", 70)]


def test_the_bound_at_30_70_is_2_1_and_exact_at_every_tenth_user():
    # A block's three control places first: 3 at n=3 is 2.1 from 0.9.
    worst = ["control"] * 3 + ["treatment"] * 7
    assert suite.g0b_balance_lines("S5", {"US": worst * 3}, ARMS, {"US": 30}) == []
    # Four in a row is 2.8 from 1.2 (and the treatment's 0 from 2.8): refused
    # there, and the tenth is not 3.
    four = ["control"] * 4 + ["treatment"] * 6
    assert suite.g0b_balance_lines("S5", {"US": four}, ARMS, {"US": 10}) == [
        "S5 group US: control 4 at n=4 is 2.8 from 1.2 (at most 2.1)",
        "S5 group US: treatment 0 at n=4 is 2.8 from 2.8 (at most 2.1)",
        "S5 group US: control 4 != 3 at n=10",
        "S5 group US: treatment 6 != 7 at n=10",
    ]
    # Within 2.1 everywhere, but not exact at the tenth user.
    off_by_one = ["control", "treatment"] * 2 + ["treatment"] * 6
    assert suite.g0b_balance_lines("S5", {"US": off_by_one}, ARMS, {"US": 10}) == [
        "S5 group US: control 2 != 3 at n=10",
        "S5 group US: treatment 8 != 7 at n=10",
    ]


def test_a_group_with_fewer_users_than_the_plan_sends_is_named():
    lines = suite.g0b_balance_lines("S5", {"US": []}, ARMS, {"US": 2000, "DE": 10})
    assert lines == [
        "S5 group US: 0 users answered, the plan sends 2,000",
        "S5 group DE: 0 users answered, the plan sends 10",
    ]


def test_g0b2_a_returning_user_answered_another_variant_is_refused():
    small = replace(
        S5,
        users=100,
        sticky_share=1.0,
        balance=replace(
            S5.balance,
            cohorts=(generator.Cohort("US", {"country": "US"}, 100),),
        ),
    )
    plan = generator.simulate(SEED, small)
    assert suite.returning_sends(plan) == 100
    flipped = 0

    def flip(op, default):
        nonlocal flipped
        status, body = default
        user = op.body.get("user_id")
        if op.kind == "assign" and user in fake.stored and flipped == 0:
            flipped += 1
            first = fake.stored[user][0]
            other = "control" if first == "treatment" else "treatment"
            return status, {**body, "variant_id": f"id-{other}", "variant_name": other}
        return fake.answer(op, default)

    fake = FakeBalancedServer(plan)
    text = replay(plan, declared=suite.DECLARED, answer=flip)
    records = ledger.read(io.StringIO(text))
    experiment = contract.valid_events(records).experiments[small.experiment_key]
    (line,) = suite.g0b_returning_lines(
        "S5",
        experiment,
        suite.returning_answers(records, small.experiment_key),
        suite.returning_sends(plan),
    )
    assert re.fullmatch(
        r"S5: user gt-\d+-\d{6} \(ledger line \d+\) answered '(control|treatment)',"
        r" first '(control|treatment)'",
        line,
    )
    assert suite.g0b_returning_lines("S5", experiment, 99, 100)[0] == (
        "S5: 99 returning users answered, the plan sends 100"
    )


def test_g0b3_an_answer_without_rows_or_a_count_is_refused():
    found = {"US": ["control"] * 3 + ["treatment"] * 7}
    assert suite.g0b_groups_lines("S5", None, found) == [
        "S5: balanced-groups answered no group rows",
        "S5: US has 0 users, expected 10",
        "S5: users_outside_blocks None, expected 0",
    ]


def test_s5_sends_every_user_with_a_context_and_its_documented_group():
    plan = generator.simulate(SEED, S5)
    groups = generator.user_groups(plan)
    assert Counter(groups.values()) == {
        "US": 2000,
        "DE": 1500,
        "FR": 1400,
        "other": 500,
        "unknown": 600,
    }
    assigns = [op for op in plan.ops if op.kind == "assign"]
    assert {op.kind for op in plan.ops} == {"lifecycle", "assign", "track"}
    # Every user, then about one in ten again.
    returning = suite.returning_sends(plan)
    assert len(assigns) == 6000 + returning and 500 < returning < 700
    assert all("context" in op.body for op in assigns)
    # The server's own reading of each context agrees with the docs' group.
    setting = S5.balance.setting()
    assert all(
        balanced_blocks.group_of(setting, op.body["context"])
        == groups[op.body["user_id"]]
        for op in assigns
    )
