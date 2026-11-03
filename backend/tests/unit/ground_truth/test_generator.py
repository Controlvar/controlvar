"""The seeded generator: the same seed gives the same traffic, in any process.

* the plan is a function of the seed and the scenario alone: pinned by digest,
  and equal under two hash seeds in fresh interpreters;
* its randomness comes only from ``random.Random(seed).random()``;
* every value is a multiple of 2^-6, so any sum of them is exact;
* about 90% of events carry an ``event_id``, each id used once;
* every user is assigned once (a returning user twice, one at a time), and
  every event is sent after every assignment, within the API's batch sizes;
* a scenario the API could not take is refused.
"""

from __future__ import annotations

import ast
import math
import os
import random
import subprocess
import sys
from collections import Counter
from dataclasses import replace

import pytest

from backend.tests.unit.ground_truth.library import LIBRARY, LIBRARY_ROOT, generator

pytestmark = pytest.mark.unit

g = generator

SMALL = g.Scenario(
    name="small",
    experiment_key="gt-small",
    arms=(
        g.Arm("control", 30, 0.2, repeat=0.3, values=g.Values(3.0, 1.0)),
        g.Arm("treatment", 70, 0.3, repeat=0.3, values=g.Values(3.0, 1.0)),
    ),
    users=400,
    event_name="purchase",
    metrics=(
        g.Metric("revenue", "purchase", "revenue"),
        g.Metric("purchases", "purchase", "count"),
    ),
    assign_batch_size=150,
    event_batch_size=40,
)

#: The digest of ``simulate(11, SMALL)``. A change to the generator that moves
#: any operation changes it: a seed printed by a failing run must reproduce
#: the same traffic, so such a change is deliberate and updates this pin.
SMALL_DIGEST = "8271b85730f8021a93341cce46f5959106e3c4d299842a65c7a2ba9f4f27435b"

LARGE = replace(SMALL, users=20000, assign_batch_size=1000, event_batch_size=100)


def _events(plan: g.Plan) -> list[dict]:
    found = []
    for op in plan.ops:
        if op.kind == "track":
            found.append(dict(op.body))
        elif op.kind == "batch":
            found.extend(dict(e) for e in op.body["events"])
    return found


def test_the_same_seed_gives_the_same_plan():
    assert g.simulate(11, SMALL).digest() == g.simulate(11, SMALL).digest()
    assert g.simulate(11, SMALL).digest() != g.simulate(12, SMALL).digest()


def test_the_plan_is_pinned():
    assert g.simulate(11, SMALL).digest() == SMALL_DIGEST


_DIGEST_SCRIPT = f"""
import sys
sys.path.insert(0, {str(LIBRARY_ROOT)!r})
from ground_truth import generator as g
scenario = g.Scenario(
    name="small", experiment_key="gt-small",
    arms=(g.Arm("control", 30, 0.2, repeat=0.3, values=g.Values(3.0, 1.0)),
          g.Arm("treatment", 70, 0.3, repeat=0.3, values=g.Values(3.0, 1.0))),
    users=400, event_name="purchase",
    metrics=(g.Metric("revenue", "purchase", "revenue"),
             g.Metric("purchases", "purchase", "count")),
    assign_batch_size=150, event_batch_size=40,
)
print(g.simulate(11, scenario).digest())
"""


@pytest.mark.parametrize("hash_seed", ["0", "4242"])
def test_a_fresh_interpreter_makes_the_same_plan(hash_seed):
    result = subprocess.run(
        [sys.executable, "-I", "-c", _DIGEST_SCRIPT],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SMALL_DIGEST


#: Every way to get randomness other than ``Random(seed).random()``.
_OTHER_RANDOMNESS = {
    "betavariate",
    "binomialvariate",
    "choice",
    "choices",
    "expovariate",
    "gammavariate",
    "gauss",
    "getrandbits",
    "lognormvariate",
    "normalvariate",
    "paretovariate",
    "randbytes",
    "randint",
    "randrange",
    "sample",
    "setstate",
    "shuffle",
    "SystemRandom",
    "triangular",
    "uniform",
    "vonmisesvariate",
    "weibullvariate",
    "urandom",
    "token_bytes",
    "token_hex",
    "uuid4",
    "time",
    "time_ns",
    "now",
    "utcnow",
    "monotonic",
    "perf_counter",
}


def test_randomness_comes_only_from_the_seeded_random():
    tree = ast.parse((LIBRARY / "generator.py").read_text())
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {
        alias.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for alias in n.names
    }
    modules = {
        n.module.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module
    }
    assert attributes & _OTHER_RANDOMNESS == set()
    assert imported & _OTHER_RANDOMNESS == set()
    assert (imported | modules) & {"secrets", "uuid", "time", "datetime", "os"} == set()
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "Random"
    ]
    assert len(calls) == 1 and ast.unparse(calls[0]) == "random.Random(seed)"


def test_values_are_multiples_of_two_to_the_minus_six_and_sum_exactly():
    values = [e["value"] for e in _events(g.simulate(5, LARGE))]
    assert len(values) > 1000
    assert all((v * 64).is_integer() for v in values)
    shuffled = list(values)
    random.Random(1).shuffle(shuffled)
    assert sum(values) == sum(shuffled) == math.fsum(values)


def test_the_lognormal_values_have_the_planted_shape():
    """log(value) is mu + sigma z: its mean and spread are the planted ones."""
    values = [e["value"] for e in _events(g.simulate(5, LARGE)) if e["value"] > 0]
    logs = [math.log(v) for v in values]
    mean = math.fsum(logs) / len(logs)
    spread = math.sqrt(math.fsum((x - mean) ** 2 for x in logs) / (len(logs) - 1))
    assert abs(mean - 3.0) < 0.1 and abs(spread - 1.0) < 0.1, (mean, spread)


def test_about_ninety_percent_of_events_carry_an_id_each_used_once():
    events = _events(g.simulate(5, LARGE))
    ids = [e["event_id"] for e in events if "event_id" in e]
    assert 0.87 < len(ids) / len(events) < 0.93, len(ids) / len(events)
    assert len(set(ids)) == len(ids)


def test_every_user_is_assigned_and_every_event_follows_every_assignment():
    plan = g.simulate(11, SMALL)
    kinds = [op.kind for op in plan.ops]
    assert kinds[:2] == ["lifecycle", "lifecycle"]
    assert [op.body["action"] for op in plan.ops[:2]] == ["create", "start"]
    last_assign = max(i for i, k in enumerate(kinds) if k in ("assign", "assign_batch"))
    first_event = min(i for i, k in enumerate(kinds) if k in ("track", "batch"))
    assert last_assign < first_event

    batched = [
        u["user_id"]
        for op in plan.ops
        if op.kind == "assign_batch"
        for u in op.body["users"]
    ]
    single = Counter(op.body["user_id"] for op in plan.ops if op.kind == "assign")
    assert not set(batched) & set(single)
    assert len(batched) + len(single) == SMALL.users
    assert set(single.values()) == {1, 2}

    for op in plan.ops:
        if op.kind == "assign_batch":
            assert 1 <= len(op.body["users"]) <= SMALL.assign_batch_size
        if op.kind == "batch":
            assert 1 <= len(op.body["events"]) <= SMALL.event_batch_size
    tracked = {op.body["user_id"] for op in plan.ops if op.kind == "track"}
    in_batches = {
        e["user_id"] for op in plan.ops if op.kind == "batch" for e in op.body["events"]
    }
    assert tracked <= set(single) and in_batches <= set(batched)


def test_the_arms_get_the_planted_direction():
    """The planted truth sets the direction: the treatment converts more."""
    plan = g.simulate(5, LARGE)
    allocations = plan.allocations()
    converted = {
        g.hashing.variant_for(e["user_id"], LARGE.experiment_key, allocations)
        for e in _events(plan)
    }
    assert converted == {"control", "treatment"}
    users = Counter(
        g.hashing.variant_for(f"gt-5-{n:06d}", LARGE.experiment_key, allocations)
        for n in range(LARGE.users)
    )
    buyers = Counter(
        g.hashing.variant_for(u, LARGE.experiment_key, allocations)
        for u in {e["user_id"] for e in _events(plan)}
    )
    assert (
        buyers["treatment"] / users["treatment"] > buyers["control"] / users["control"]
    )


@pytest.mark.parametrize(
    "change, message",
    [
        ({"arms": (g.Arm("only", 100, 0.1),)}, "at least two arms"),
        ({"arms": (g.Arm("a", 50, 0.1), g.Arm("a", 50, 0.1))}, "repeat"),
        ({"arms": (g.Arm("a", 60, 0.1), g.Arm("b", 60, 0.1))}, "add up to 100"),
        ({"arms": (g.Arm("a", 100, 0.1), g.Arm("b", 0, 0.1))}, "positive"),
        ({"arms": (g.Arm("a", 50, 1.5), g.Arm("b", 50, 0.1))}, r"\[0, 1\]"),
        ({"metrics": (g.Metric("m", "e", "median"),)}, "metric type"),
        ({"correction_method": "holm"}, "correction method"),
        ({"confidence_level": 0.5}, "confidence level"),
        ({"assign_batch_size": 1001}, "assign_batch_size"),
        ({"event_batch_size": 101}, "event_batch_size"),
        ({"users": 0}, "at least one user"),
    ],
)
def test_a_scenario_the_api_cannot_take_is_refused(change, message):
    with pytest.raises(ValueError, match=message):
        g.simulate(1, replace(SMALL, **change))
