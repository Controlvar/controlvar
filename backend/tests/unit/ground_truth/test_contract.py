"""K1: each rule of the event contract on a small stream, against numbers worked
out by hand.

Every stream is a ledger the reader accepts (``ledger.read``), so what
``valid_events`` is given here is what a real run gives it. The last tests
replay generated plans through a fake server and check that every planted
event is counted, and that an answer other than the documented hash's is
reported.

Tagging an event received before its user's assignment (R2) moves the counts
of ``test_r2_*`` and fails it.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

import pytest

from backend.tests.unit.ground_truth.library import (
    contract,
    generator,
    hashing,
    ledger,
    records,
    replay,
)

pytestmark = pytest.mark.unit

g = generator
EXP = "k1"
ARMS = [("control", 50), ("treatment", 50)]
Mismatch = contract.Mismatch


def _users(variant: str, count: int) -> list[str]:
    found = []
    number = 0
    while len(found) < count:
        user = f"k1-user-{number}"
        if hashing.variant_for(user, EXP, ARMS) == variant:
            found.append(user)
        number += 1
    return found


C0, C1 = _users("control", 2)
T0, T1 = _users("treatment", 2)


def start(
    status: str | None = None, dedup: bool | None = None, extra: tuple[str, ...] = ()
) -> list[dict]:
    """A start listing EXP and, when given, the other experiments in *extra*."""
    experiments = []
    for key in (EXP, *extra):
        experiment = {
            "exp": key,
            "variants": [{"name": n, "allocation": a} for n, a in ARMS],
        }
        if status is not None:
            experiment["status"] = status
        experiments.append(experiment)
    return [
        {
            "kind": "start",
            "source": "k1",
            "seed": 0,
            "experiments": experiments,
            "flags": [],
            "planted": [],
            "declared": {} if dedup is None else {"event_dedup": dedup},
        }
    ]


def action(name: str, status: int = 200) -> list[dict]:
    return [
        {"kind": "action", "action": name, "exp": EXP, "status": status, "attempts": 1}
    ]


def assign(
    user,
    *,
    route="/assign",
    status=200,
    assigned=True,
    variant=None,
    reason="assigned",
    seq=None,
    index=None,
    exp=EXP,
) -> list[dict]:
    record = {
        "kind": "assign",
        "user": user,
        "exp": exp,
        "status": status,
        "assigned": assigned if status == 200 else None,
        "variant": (variant or hashing.variant_for(user, exp, ARMS))
        if status == 200
        else None,
        "reason": reason if status == 200 else None,
        "attempts": 1,
        "route": route,
    }
    if seq is not None:
        record.update(seq=seq, batch_index=index)
    return [record]


def _event(user, event_type, event_name, value, status, event_id, exp, route, flag):
    return {
        "kind": "event",
        "user": user,
        "exp": exp,
        "flag": flag,
        "event_type": event_type,
        "event_name": event_name,
        "value": value,
        "status": status,
        "attempts": 1,
        "route": route,
        "event_id": event_id,
    }


def track(
    user,
    *,
    event_type="purchase",
    event_name=None,
    value=None,
    status=200,
    event_id=None,
    exp=EXP,
    route="/track",
    flag=None,
) -> list[dict]:
    return [
        _event(user, event_type, event_name, value, status, event_id, exp, route, flag)
    ]


def batch(seq: int, events: list[dict], *, status=200, errors=None) -> list[dict]:
    """A /batch answer and its events; *events* are ``track(...)[0]`` shapes."""
    ok = status == 200
    failed = len(errors or [])
    head = {
        "kind": "batch",
        "size": len(events),
        "status": status,
        "success_count": len(events) - failed if ok else None,
        "failure_count": failed if ok else None,
        "errors": (errors or None) if ok else None,
        "attempts": 1,
        "route": "/batch",
        "seq": seq,
    }
    members = [
        {**e, "status": status, "route": "/batch", "seq": seq, "batch_index": i}
        for i, e in enumerate(events)
    ]
    return [head, *members]


def ledger_of(*parts: list[dict]) -> list[dict]:
    stream = [record for part in parts for record in part]
    counts = dict.fromkeys(ledger.COUNTED, 0) | {"429": 0}
    for record in stream[1:]:
        counts[record["kind"]] += 1
        counts["429"] += record["status"] == 429
    stream.append({"kind": "end", "counts": counts, "errors": []})
    return ledger.read(ledger.dumps(r) for r in stream)


def k1(*parts: list[dict]) -> contract.ExperimentExpected:
    return contract.valid_events(ledger_of(*parts)).experiments[EXP]


# ---------------------------------------------------------------------------
# The rules, one by one
# ---------------------------------------------------------------------------
def test_r0_only_an_accepted_event_is_stored():
    parts = [
        start("active"),
        assign(C0),
        track(C0, value=0.5, status=404),
        track(C0, value=2.0),
        batch(
            1,
            [track(C0, value=1.0)[0], track(C0, value=4.0)[0], track(C0, value=8.0)[0]],
            errors=[1],
        ),
        batch(2, [track(C0, value=16.0)[0]], status=422),
        track(C0, value=32.0, status=429),
    ]
    expected = contract.valid_events(ledger_of(*parts))
    e = expected.experiments[EXP]
    assert e.counts("control", "purchase") == [3]
    assert e.value_totals("control", "purchase") == [11.0]
    assert e.total_events == 1 + 3  # the exposure and the three stored events
    assert expected.rejected_events == 4  # the 404, position 1, the 422's, the 429
    assert expected.rate_limited == 1


def test_r1_the_first_assignment_and_an_exposure_per_per_user_answer():
    e = k1(
        start("active"),
        assign(C0),
        assign(C0),  # sticky: another exposure
        assign(T0, route="/assign/batch", seq=1, index=0),  # no exposure
        assign(C1, assigned=False, variant="control", reason="holdout"),
        assign(T1, status=404),
    )
    assert e.users == {"control": (C0,), "treatment": (T0,)}
    assert (e.exposures, e.total_events, e.total_users) == (2, 2, 2)
    assert e.variant_mismatches == () and e.variant_conflicts == ()


def test_r1_an_answer_other_than_the_hash_is_reported_and_the_first_one_stays():
    e = k1(
        start("active"), assign(C0, variant="treatment"), assign(C0, variant="control")
    )
    assert e.users == {"control": (), "treatment": (C0,)}
    assert e.variant_mismatches == (Mismatch(2, C0, "treatment", "control"),)
    assert e.variant_conflicts == (Mismatch(3, C0, "control", "treatment"),)


def test_r2_an_event_counts_for_the_variant_its_user_has_when_it_arrives():
    e = k1(
        start(),
        action("create"),
        track(C0, value=1.0),  # a draft: untagged
        action("start"),
        track(C0, value=2.0),  # before the assignment: untagged
        assign(C0),
        track(C0, value=4.0),  # counts
        action("pause"),
        track(C0, value=8.0),  # paused: untagged
        action("start"),
        track(C0, value=16.0),  # counts
        track(C0, value=64.0, exp=None),  # no experiment: history
    )
    assert e.counts("control", "purchase") == [2]
    assert e.value_totals("control", "purchase") == [20.0]
    assert e.untagged_events == 3
    assert e.total_events == 1 + 5  # the exposure, two counted, three untagged


def test_r3_a_metric_matches_by_event_name_and_never_an_exposure():
    e = k1(
        start("active"),
        assign(C0),
        track(C0, event_type="purchase"),
        track(C0, event_type="conversion", event_name="purchase"),
        track(C0, event_type="checkout"),
        track(C0, event_type="exposure"),
        track(C0, event_type="experiment_exposure", event_name="purchase"),
    )
    assert e.counts("control", "purchase") == [2]
    assert e.counts("control", "checkout") == [1]
    assert e.counts("control", "conversion") == [0]
    assert e.counts("control", "exposure") == [0]
    assert e.total_events == 1 + 5


def test_r4_an_event_sent_again_with_its_id_is_stored_once_the_first_kept():
    e = k1(
        start("active", dedup=True),
        assign(C0),
        track(C0, value=1.0, event_id="o-1"),
        track(C0, value=99.0, event_id="o-1"),  # again: not stored, 1.0 kept
        track(C0, event_type="checkout", value=2.0, event_id="o-1"),  # another name
        track(C0, value=4.0, event_id=""),  # "" is no id: both stored
        track(C0, value=4.0, event_id=""),
        batch(
            1,
            [
                track(C0, value=8.0, event_id="o-2")[0],
                track(C0, value=8.0, event_id="o-2")[0],
            ],
        ),  # again in one batch
        track(C0, value=16.0, event_id="o-3", status=422),  # refused: no key kept
        track(C0, value=32.0, event_id="o-3"),
    )
    assert e.counts("control", "purchase") == [5]
    assert e.value_totals("control", "purchase") == [1.0 + 4.0 + 4.0 + 8.0 + 32.0]
    assert e.counts("control", "checkout") == [1]
    assert e.total_events == 1 + 6


def test_r4_the_whole_key_counts_user_experiment_flag_and_event_name():
    """The same ``event_id`` for another user, experiment, flag or event name is
    another event; only all five equal is a repeat."""
    other = "k1b"
    found = contract.valid_events(
        ledger_of(
            start("active", dedup=True, extra=(other,)),
            assign(C0),
            assign(C1),
            assign(C0, exp=other),
            track(C0, value=1.0, event_id="same"),
            track(C0, value=1.0, event_id="same"),  # repeat: not stored
            track(C1, value=2.0, event_id="same"),  # another user
            track(C0, value=4.0, event_id="same", flag="f1"),  # another flag
            track(C0, value=4.0, event_id="same", flag="f1"),  # repeat: not stored
            track(C0, value=8.0, event_id="same", flag="f2"),  # a third flag
            track(C0, event_type="checkout", value=16.0, event_id="same"),  # a name
            track(C0, value=32.0, event_id="same", exp=other),  # another experiment
            track(C0, value=32.0, event_id="same", exp=other),  # repeat: not stored
        )
    )
    e = found.experiments[EXP]
    assert e.counts("control", "purchase") == [3, 1]
    assert e.value_totals("control", "purchase") == [1.0 + 4.0 + 8.0, 2.0]
    assert e.counts("control", "checkout") == [1, 0]
    assert e.total_events == 2 + 5
    b =found.experiments[other]
    arm = hashing.variant_for(C0, other, ARMS)
    assert b.counts(arm, "purchase") == [1]
    assert b.total_events == 1 + 1


def test_r4_a_repeat_of_an_event_first_received_while_paused_stays_out():
    e = k1(
        start(dedup=True),
        action("start"),
        assign(C0),
        action("pause"),
        track(C0, value=5.0, event_id="late-1"),
        action("start"),
        track(C0, value=5.0, event_id="late-1"),
    )
    assert e.counts("control", "purchase") == [0]
    assert (e.untagged_events, e.total_events) == (1, 2)


def test_r4_without_dedup_every_repeat_is_stored():
    e = k1(
        start("active", dedup=False),
        assign(C0),
        track(C0, value=1.0, event_id="o-1"),
        track(C0, value=1.0, event_id="o-1"),
    )
    assert e.counts("control", "purchase") == [2]
    assert e.value_totals("control", "purchase") == [2.0]


def test_r5_a_value_over_ten_to_the_twelve_adds_nothing_its_user_converts():
    e = k1(
        start("active"),
        assign(C0),
        assign(T0),
        track(C0, value=2e12),
        track(T0, value=1e12),  # exactly the bound: adds
        track(T0, value=-1.5e12),  # over it in size: adds nothing
        track(T0, value=0.5),
        track(T0),  # no value: counts, adds nothing
    )
    assert e.counts("control", "purchase") == [1]
    assert e.value_totals("control", "purchase") == [0.0]
    assert e.converting_users("control", "purchase") == 1
    assert e.per_user("control", "purchase", "conversion") == [1.0]
    assert e.per_user("control", "purchase", "revenue") == [0.0]
    assert e.per_user("treatment", "purchase", "count") == [4.0]
    assert e.per_user("treatment", "purchase", "revenue") == [1e12 + 0.5]


def test_r6_every_assigned_user_is_one_unit_scored_zero_without_events():
    e = k1(
        start("active"),
        assign(C0),
        assign(C1),
        assign(T0),
        track(C1, value=3.0),
        track(C1, value=3.0),
    )
    assert (e.sample_size("control"), e.sample_size("treatment")) == (2, 1)
    assert e.counts("control", "purchase") == [0, 2]
    assert e.value_totals("control", "purchase") == [0.0, 6.0]
    assert e.per_user("control", "purchase", "conversion") == [0.0, 1.0]
    assert e.mean("control", "purchase", "revenue") == 3.0
    assert e.per_user("treatment", "purchase", "duration") == [0.0]
    assert e.total_converting_users(["purchase", "checkout"]) == 1


def test_r7_no_rule_reads_a_timestamp():
    found = ledger_of(start("active"), assign(C0), track(C0))
    found[3]["timestamp"] = "2099-01-01T00:00:00Z"
    with pytest.raises(
        contract.ContractRefused, match="R7: record 4 carries a `timestamp`"
    ):
        contract.valid_events(found)


@pytest.mark.parametrize(
    "parts, message",
    [
        (
            (start(), assign(C0), track(C0)),
            "R2 needs the status of experiment 'k1' when record 3 was received",
        ),
        (
            (start("active"), track(C0, exp="other")),
            "R2: record 2 names experiment 'other', which start does not list",
        ),
        (
            (start("active"), assign(C0, variant="ghost")),
            "R1: record 2 answers variant 'ghost'",
        ),
        ((start(), action("restart")), "R2 has no rule for action 'restart'"),
        (
            (start("active"), assign(C0), track(C0, event_id="o-1")),
            "R4 needs `event_dedup` in start's `declared`",
        ),
        (
            (start("active"), track(C0, route="/events")),
            "R0 has no rule for an event sent to /events",
        ),
        (
            (start("active"), assign(C0), track(C0, event_name="")),
            'R3: record 3 carries `event_name` ""; the server stores `event_name or'
            " event_type`, so an empty name is not modelled",
        ),
    ],
    ids=["status", "experiment", "variant", "action", "dedup", "route", "empty-name"],
)
def test_a_rule_that_needs_what_the_ledger_does_not_say_refuses_by_name(parts, message):
    with pytest.raises(contract.ContractRefused, match=message.replace("(", r"\(")):
        contract.valid_events(ledger_of(*parts))


def test_valid_events_needs_a_start():
    with pytest.raises(contract.ContractRefused, match="starts with start"):
        contract.valid_events([])


# ---------------------------------------------------------------------------
# A whole plan, through a fake server
# ---------------------------------------------------------------------------
THREE = g.Scenario(
    name="k1-plan",
    experiment_key="gt-k1-plan",
    arms=(
        g.Arm("control", 34, 0.10, repeat=0.3, values=g.Values(3.0, 1.0)),
        g.Arm("lifted", 33, 0.15, repeat=0.3, values=g.Values(3.0, 1.0)),
        g.Arm("other", 33, 0.10, repeat=0.3, values=g.Values(3.0, 1.0)),
    ),
    users=3000,
    event_name="purchase",
    metrics=(
        g.Metric("revenue", "purchase", "revenue"),
        g.Metric("purchases", "purchase", "count"),
    ),
)


def test_a_replayed_plan_counts_every_planted_event():
    plan = g.simulate(9, THREE)
    key, allocations = THREE.experiment_key, plan.allocations()
    expected = contract.valid_events(
        records(replay(plan, declared={"event_dedup": True}))
    )
    e = expected.experiments[key]

    variant_of = {
        u["user_id"]: hashing.variant_for(u["user_id"], key, allocations)
        for op in plan.ops
        if op.kind == "assign_batch"
        for u in op.body["users"]
    } | {
        op.body["user_id"]: hashing.variant_for(op.body["user_id"], key, allocations)
        for op in plan.ops
        if op.kind == "assign"
    }
    events = [op.body for op in plan.ops if op.kind == "track"] + [
        ev for op in plan.ops if op.kind == "batch" for ev in op.body["events"]
    ]
    count: Counter = Counter(ev["user_id"] for ev in events)
    values: dict = defaultdict(list)
    for ev in events:
        values[ev["user_id"]].append(ev["value"])
    for variant, _ in allocations:
        users = [u for u, v in variant_of.items() if v == variant]
        assert sorted(e.users[variant]) == sorted(users)
        order = e.users[variant]
        assert e.counts(variant, "purchase") == [count[u] for u in order]
        totals = [math.fsum(values[u]) for u in order]
        assert e.value_totals(variant, "purchase") == totals
        assert e.mean(variant, "purchase", "revenue") == math.fsum(totals) / len(order)
        assert e.converting_users(variant, "purchase") == sum(
            1 for u in order if count[u]
        )
    singles = sum(1 for op in plan.ops if op.kind == "assign")
    assert (e.exposures, e.untagged_events) == (singles, 0)
    assert e.total_events == singles + len(events)
    assert e.variant_mismatches == () and e.variant_conflicts == ()
    assert expected.rejected_events == expected.rate_limited == 0


def test_an_answer_other_than_the_hash_is_reported_user_by_user():
    """G0's evidence: a server that moves one user is caught by the ledger alone."""
    plan = g.simulate(9, THREE)
    moved = []

    def answer(op, default):
        status, body = default
        if op.kind == "assign_batch" and not moved:
            first = body["assignments"][0]
            moved.append(first["user_id"])
            wrong = "id-other" if first["variant_id"] != "id-other" else "id-control"
            body = {
                **body,
                "assignments": [
                    {**first, "variant_id": wrong},
                    *body["assignments"][1:],
                ],
            }
        return status, body

    expected = contract.valid_events(
        records(replay(plan, declared={"event_dedup": True}, answer=answer))
    )
    mismatches = expected.experiments[THREE.experiment_key].variant_mismatches
    assert [m.user for m in mismatches] == moved
