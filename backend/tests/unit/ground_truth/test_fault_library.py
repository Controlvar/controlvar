"""The fault library (#1544 part D): ``ground_truth.faults``, with no server.

* injecting a fault never changes the clean plan, its digest or ``simulate``;
* every count a fault plants is a named integer of its definition: a count
  below 1 is refused when the definition is made, and a plan with too few
  eligible events or users is refused by the injector, each naming the fault;
* the same seed gives the same fault plan;
* every op of a fault plan carries an ``expect``, the answer it describes
  meets it, and a different answer does not;
* each fault plants what its docstring says, where it says (F3's lifecycle,
  F5's early events, F6's four cases, F7's lost users, F9's classes and raw
  payloads, F2's bot volume and values beyond the bound, F4's timestamps from
  the event serial);
* ``inject`` plants one fault besides F4; ``combine`` places them all, each on
  users no other touched, inside F3's pause and ``complete``.

That each planted count reaches the ledger exactly is
``test_sensitivity.py``'s.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import replace

import pytest

from backend.tests.unit.ground_truth.library import contract, faults, generator, hashing

pytestmark = pytest.mark.unit

g = generator
SEED = 20261103
SCENARIO = g.Scenario(
    name="faults",
    experiment_key="gt-faults",
    arms=(
        g.Arm("control", 50, 0.3, repeat=0.3, values=g.Values(3.0, 1.0)),
        g.Arm("treatment", 50, 0.3, repeat=0.3, values=g.Values(3.0, 1.0)),
    ),
    users=600,
    event_name="purchase",
    metrics=(
        g.Metric("buyers", "purchase", "conversion"),
        g.Metric("purchases", "purchase", "count"),
        g.Metric("revenue", "purchase", "revenue"),
    ),
    event_batch_size=50,
)
CLEAN = g.simulate(SEED, SCENARIO)
#: Each fault, with counts this small scenario has room for.
SMALL = {
    "F1": faults.Duplicates(r4_drops=12, no_id_repeats=3),
    "F3": faults.LateEvents(
        r2_status_untags=8, paused_assigns=3, completed_assigns=2, paused_resends=2
    ),
    "F5": faults.EarlyEvents(r2_order_untags=8),
    "F6": faults.VariantMismatch(
        other_arm=5, never_assigned=4, tagged_then_assigned=3, own_arm=2
    ),
    "F7": faults.ExposureLoss(lost_users=150),
    "F9": faults.Hostile(per_class=1),
    "F2": faults.BotTraffic(bots=4, events_per_bot=30, huge_values=6),
    "F4": faults.ClockSkew(future_users_per_arm=2),
}
#: Every named count of every definition.
COUNTS = [
    (faults.Duplicates, "r4_drops"),
    (faults.Duplicates, "no_id_repeats"),
    (faults.LateEvents, "r2_status_untags"),
    (faults.LateEvents, "paused_assigns"),
    (faults.LateEvents, "completed_assigns"),
    (faults.LateEvents, "paused_resends"),
    (faults.EarlyEvents, "r2_order_untags"),
    (faults.VariantMismatch, "other_arm"),
    (faults.VariantMismatch, "never_assigned"),
    (faults.VariantMismatch, "tagged_then_assigned"),
    (faults.VariantMismatch, "own_arm"),
    (faults.ExposureLoss, "lost_users"),
    (faults.Hostile, "per_class"),
    (faults.BotTraffic, "bots"),
    (faults.BotTraffic, "events_per_bot"),
    (faults.BotTraffic, "huge_values"),
    (faults.ClockSkew, "future_users_per_arm"),
]


def planted(name: str, plan: g.Plan = CLEAN) -> g.Plan:
    return faults.inject(plan, SMALL[name])


def _bodies(plan: g.Plan, kinds=("track", "batch", "events")) -> list[dict]:
    found = []
    for op in plan.ops:
        if op.kind not in kinds:
            continue
        found.extend(op.body["events"] if op.kind == "batch" else [op.body])
    return found


def _assigned_users(plan: g.Plan) -> list[str]:
    users = []
    for op in plan.ops:
        if op.kind == "assign" and op.expect in (None, faults.ASSIGNED):
            users.append(op.body["user_id"])
        elif op.kind == "assign_batch":
            users.extend(u["user_id"] for u in op.body["users"])
    return users


def _first(plan: g.Plan, kinds: tuple[str, ...]) -> int:
    return min(i for i, op in enumerate(plan.ops) if op.kind in kinds)


# ---------------------------------------------------------------------------
# The clean plan stays clean
# ---------------------------------------------------------------------------
def test_injecting_never_changes_the_clean_plan_or_its_digest():
    digest = CLEAN.digest()
    for name in SMALL:
        assert planted(name).digest() != digest, name
    assert CLEAN.digest() == digest == g.simulate(SEED, SCENARIO).digest()
    assert faults.inject(CLEAN).digest() == digest  # expects are not traffic
    assert all(op.expect is None for op in CLEAN.ops)


@pytest.mark.parametrize("name", sorted(SMALL))
def test_the_same_seed_gives_the_same_fault_plan(name):
    assert planted(name).digest() == planted(name).digest()
    other = g.simulate(SEED + 1, SCENARIO)
    assert planted(name, other).digest() != planted(name).digest()


# ---------------------------------------------------------------------------
# Named integers, never zero, never more than the plan holds
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fault, field", COUNTS, ids=[f"{f.NAME}-{name}" for f, name in COUNTS]
)
@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_a_count_below_one_or_not_a_whole_number_is_refused(fault, field, bad):
    with pytest.raises(
        ValueError, match=rf"^{fault.NAME}: {field} must be a whole number"
    ):
        fault(**{field: bad})


@pytest.mark.parametrize(
    "fault, scenario, message",
    [
        (faults.Duplicates(r4_drops=5), SCENARIO, "F1 needs r4_drops of at least 6"),
        (
            faults.Duplicates(r4_drops=600),
            SCENARIO,
            r"F1 needs 300 /track events that carry an event_id; the plan has \d+",
        ),
        (
            faults.Duplicates(r4_drops=12, no_id_repeats=5000),
            SCENARIO,
            "F1 needs 5000 events sent without an event_id",
        ),
        (
            faults.LateEvents(r2_status_untags=5000),
            SCENARIO,
            "F3 needs 5000 events whose user is assigned and sends no other",
        ),
        (
            faults.LateEvents(r2_status_untags=4, paused_resends=3),
            SCENARIO,
            "F3 needs 3 events sent while paused that carry an event_id",
        ),
        (faults.EarlyEvents(r2_order_untags=5000), SCENARIO, "F5 needs 5000 events"),
        (
            faults.VariantMismatch(tagged_then_assigned=5000),
            SCENARIO,
            "F6 needs 5000 users assigned in a batch",
        ),
        (
            faults.VariantMismatch(),
            replace(SCENARIO, metrics=(g.Metric("buyers", "purchase", "conversion"),)),
            "F6 needs a conversion metric and a count metric on 'purchase'",
        ),
        (
            faults.ExposureLoss(lost_users=5000),
            SCENARIO,
            "F7 needs 5000 users assigned",
        ),
        (
            faults.ExposureLoss(arm="ghost"),
            SCENARIO,
            "F7: the scenario has no arm 'ghost'",
        ),
        (
            faults.ClockSkew(future_users_per_arm=5000),
            SCENARIO,
            "F4 needs 5000 users of 'control' with a counting event",
        ),
        (
            faults.BotTraffic(bots=5000),
            SCENARIO,
            "F2 needs 5000 users assigned in a batch",
        ),
        (
            faults.BotTraffic(),
            replace(SCENARIO, metrics=SCENARIO.metrics[:2]),
            "F2 needs a revenue, duration or custom metric on 'purchase'",
        ),
    ],
    ids=[
        "F1-kinds",
        "F1-track",
        "F1-no-id",
        "F3-events",
        "F3-resends",
        "F5",
        "F6-users",
        "F6-metrics",
        "F7-users",
        "F7-arm",
        "F4",
        "F2-users",
        "F2-metrics",
    ],
)
def test_a_plan_with_too_few_eligible_items_is_refused_naming_the_fault(
    fault, scenario, message
):
    with pytest.raises(ValueError, match=message):
        faults.inject(g.simulate(SEED, scenario), fault)


def test_a_fault_given_twice_is_refused():
    with pytest.raises(ValueError, match="a fault is given twice"):
        faults.inject(CLEAN, SMALL["F5"], faults.EarlyEvents(r2_order_untags=2))
    with pytest.raises(ValueError, match="a fault is given twice"):
        faults.combine(CLEAN, SMALL["F5"], faults.EarlyEvents(r2_order_untags=2))


def test_inject_plants_one_fault_besides_f4():
    """Stacked by ``inject``, two faults' counts are not exact (F3's resends
    are R4 drops, one fault takes another's event): ``combine`` places them."""
    with pytest.raises(ValueError, match=r"not \['F1', 'F3'\].*combine"):
        faults.inject(CLEAN, SMALL["F3"], SMALL["F1"], SMALL["F4"])


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"bots": 2, "events_per_bot": 3, "huge_values": 7}, "more than the 6 bot"),
        ({"value": float("nan")}, "F9: value must be None or a finite number"),
        ({"value": 2e12}, "F9: value must be None or a finite number"),
        ({"value": True}, "F9: value must be None or a finite number"),
    ],
    ids=["F2-more-values-than-events", "F9-nan", "F9-beyond", "F9-bool"],
)
def test_a_definition_that_cannot_plant_what_it_says_is_refused(fields, message):
    kind = faults.BotTraffic if "bots" in fields else faults.Hostile
    with pytest.raises(ValueError, match=message):
        kind(**fields)


# ---------------------------------------------------------------------------
# Every op expects an answer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(SMALL))
def test_every_op_expects_an_answer_and_the_answer_it_describes_meets_it(name):
    plan = planted(name)
    allocations = plan.allocations()
    for op in plan.ops:
        assert op.expect is not None and faults.check_expect(op) == op.expect
        if op.kind != "hostile":
            status, body = faults.expected_answer(op, allocations)
            assert faults.unexpected(op, status, body) is None, op


@pytest.mark.parametrize(
    "op, status, body, message",
    [
        (g.Op("track", "/t", {}, "accepted"), 422, {}, "answered 422, want a 2xx"),
        (g.Op("track", "/t", {}, "422"), 200, {}, "answered 200, want 422"),
        (
            g.Op("assign", "/a", {}, "unassigned"),
            200,
            {"assigned": True},
            "not assigned: false",
        ),
        (
            g.Op("assign_batch", "/b", {"users": [{"user_id": "u"}]}),
            200,
            {"assignments": []},
            "answered 0 assignments for 1 users",
        ),
        (
            g.Op("batch", "/b", {"events": [{}, {}]}, "item-errors:1"),
            200,
            {"errors": None},
            r"errors at positions \[\], want \[1\]",
        ),
        (
            g.Op("batch", "/b", {"events": [{}, {}]}),
            200,
            {"errors": [{"index": 0}]},
            r"errors at positions \[0\], want \[\]",
        ),
        (g.Op("hostile", "/t", {}, "413"), 200, {}, "answered 200, want 413"),
    ],
    ids=["refused", "stored", "assigned", "batch-short", "no-errors", "errors", "raw"],
)
def test_an_answer_other_than_the_expected_one_is_named(op, status, body, message):
    found = faults.unexpected(op, status, body)
    assert found is not None and re.search(message, found), found


@pytest.mark.parametrize(
    "op",
    [
        g.Op("lifecycle", None, {"action": "start"}, "422"),
        g.Op("assign_batch", "/b", {"users": []}, "unassigned"),
        g.Op("track", "/t", {}, "item-errors:0"),
        g.Op("batch", "/b", {"events": [{}]}, "item-errors:1"),
        g.Op("hostile", "/t", {}),
        g.Op("events", "/e", {}, "stored"),
        g.Op("batch", "/b", {"events": [{}]}, "item-errors:"),
        g.Op("lifecycle", None, {"action": "archive"}),
    ],
    ids=[
        "lifecycle-status",
        "batch-unassigned",
        "track-items",
        "out-of-range",
        "raw",
        "typo",
        "no-positions",
        "unsent-action",
    ],
)
def test_an_expect_its_kind_cannot_carry_is_refused(op):
    with pytest.raises(ValueError):
        faults.check_expect(op)
    assert faults.check_expect(g.Op("lifecycle", None, {"action": "pause"})) == "ok"


def test_an_events_body_is_sent_with_ids():
    body = {
        "event_name": "p",
        "event_type": "p",
        "user_id": "u",
        "experiment_key": "k",
        "variant": "treatment",
        "event_id": "e",
    }
    request = faults.events_request(
        body, experiment_id="X", variant_ids={"treatment": "T"}
    )
    assert request == {
        "event_name": "p",
        "event_type": "p",
        "user_id": "u",
        "event_id": "e",
        "experiment_id": "X",
        "variant_id": "T",
    }
    unnamed = faults.events_request(
        {**body, "variant": None}, experiment_id="X", variant_ids={}
    )
    assert "variant_id" not in unnamed


# ---------------------------------------------------------------------------
# Each fault plants what it says, where it says
# ---------------------------------------------------------------------------
def test_f1_sends_each_kind_of_repeat():
    plan = planted("F1")
    first = {}
    repeats = Counter()
    for index, op in enumerate(plan.ops):
        for body in _bodies(g.Plan(plan.seed, plan.scenario, (op,))):
            event_id = body.get("event_id")
            if event_id is None:
                continue
            if event_id in first:
                repeats[op.kind] += 1
            else:
                first[event_id] = index
    # 12 drops over six kinds: 2 each; on /track: again and another value; in
    # a /batch: a later batch, a /track event, and the pairs; through /events.
    assert repeats == {"track": 4, "batch": 6, "events": 2}
    same = [op for op in plan.ops if op.kind == "batch" and len(op.body["events"]) >= 2]
    pairs = [
        op
        for op in same
        if all(
            op.body["events"][i] == op.body["events"][i + 1]
            for i in range(0, len(op.body["events"]), 2)
        )
    ]
    assert len(pairs) == 1 and len(pairs[0].body["events"]) == 4
    stamped = [b for b in _bodies(plan, ("track",)) if b.get("timestamp")]
    assert [b["timestamp"] for b in stamped] == [faults.RESEND_TIMESTAMP]

    # no_id_repeats is pinned here, not by the sensitivity check: exactly that
    # many events with no event_id are sent again, each in a /track or a /batch.
    def no_id(p: g.Plan) -> list[dict]:
        return [b for b in _bodies(p) if not b.get("event_id")]

    assert len(no_id(plan)) - len(no_id(CLEAN)) == SMALL["F1"].no_id_repeats == 3


def test_f3_pauses_resumes_and_completes_around_the_late_events():
    plan = planted("F3")
    steps = [
        (i, op.body["action"])
        for i, op in enumerate(plan.ops)
        if op.kind == "lifecycle"
    ]
    assert [action for _, action in steps] == [
        "create",
        "start",
        "pause",
        "resume",
        "complete",
    ]
    (pause, _), (resume, _), (complete, _) = steps[2:]
    asking = [i for i, op in enumerate(plan.ops) if op.expect == faults.UNASSIGNED]
    assert [i for i in asking if pause < i < resume] == asking[:3]
    assert all(i > complete for i in asking[3:]) and len(asking) == 5

    # paused_resends is pinned here, not by the sensitivity check: exactly that
    # many events sent while paused are sent again, with their event_id, after
    # the resume and before the completion.
    def ids(lo: int, hi: int) -> list[str]:
        window = g.Plan(plan.seed, plan.scenario, tuple(plan.ops[lo:hi]))
        return [b["event_id"] for b in _bodies(window) if b.get("event_id")]

    paused = set(ids(pause, resume))
    resent = [event_id for event_id in ids(resume, complete) if event_id in paused]
    assert len(resent) == SMALL["F3"].paused_resends == 2
    assert pause > max(i for i, op in enumerate(plan.ops) if op.kind == "assign_batch")


def test_f5_sends_its_events_before_any_assignment():
    plan = planted("F5")
    before = plan.ops[2 : _first(plan, ("assign", "assign_batch"))]
    assert sum(len(_bodies(g.Plan(SEED, SCENARIO, (op,)))) for op in before) == 8
    assert {op.kind for op in before} <= {"track", "batch"}


def test_f6_names_the_other_arm_no_arm_and_its_own_arm():
    plan = planted("F6")
    allocations = plan.allocations()
    key = SCENARIO.experiment_key
    events = [(i, op.body) for i, op in enumerate(plan.ops) if op.kind == "events"]
    first_assign = _first(plan, ("assign", "assign_batch"))
    before = [b for i, b in events if i < first_assign]
    after = [b for i, b in events if i > first_assign]
    assigned = set(_assigned_users(plan))
    own = [b for b in after if b["user_id"] in assigned]
    arm = {
        b["user_id"]: hashing.variant_for(b["user_id"], key, allocations) for b in own
    }
    assert len(before) == 3
    assert all(
        b["variant"] == hashing.variant_for(b["user_id"], key, allocations)
        for b in before
    )
    assert sum(1 for b in own if b["variant"] != arm[b["user_id"]]) == 5
    assert sum(1 for b in own if b["variant"] == arm[b["user_id"]]) == 2
    never = [b for b in after if b["user_id"] not in assigned]
    assert len(never) == 4 and all(b["variant"] is not None for b in never)
    assert len({b["event_id"] for _, b in events}) == len(events) == 14


def test_f7_loses_users_of_one_arm_and_keeps_their_events():
    plan = planted("F7")
    key, allocations = SCENARIO.experiment_key, plan.allocations()
    lost = set(_assigned_users(CLEAN)) - set(_assigned_users(plan))
    assert len(lost) == 150
    assert {hashing.variant_for(u, key, allocations) for u in lost} == {"treatment"}
    sent = Counter(b["user_id"] for b in _bodies(plan))
    assert sent == Counter(b["user_id"] for b in _bodies(CLEAN))


def test_f9_sends_every_class_once_per_unit_each_naming_the_experiment():
    plan = planted("F9")
    classes = Counter()
    for op in plan.ops:
        if op.kind == "hostile":
            classes[op.body["class"]] += 1
            assert SCENARIO.experiment_key in op.body["content"]
            continue
        users = {b["user_id"] for b in _bodies(g.Plan(SEED, SCENARIO, (op,)))}
        found = {faults.f9_class(u) for u in users} - {None}
        if op.kind in ("track", "batch", "events") and found:
            (cls,) = found
            classes[cls] += 1
            want = faults.F9_REFUSED.get(cls, faults.ACCEPTED)
            assert op.expect == want, (cls, op.expect)
    expected = {
        cls: faults.F9_SENDS.get(cls, 1)
        for cls in (*faults.F9_VALID, *faults.F9_REFUSED, *faults.F9_HOSTILE)
    }
    assert classes == expected


def test_f9_raw_payloads_are_what_their_class_names():
    raw = {
        op.body["class"]: op.body["content"]
        for op in planted("F9").ops
        if op.kind == "hostile"
    }
    assert len(raw["body-over-5-mib"].encode("utf-8")) == faults.MAX_BODY_BYTES + 1
    assert raw["json-nested-5000-deep"].count('{"a":') == 5000
    assert raw["lone-surrogate"].isascii() and "\\ud800" in raw["lone-surrogate"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw["invalid-json"])
    assert (
        json.loads(raw["body-over-5-mib"])["experiment_key"] == SCENARIO.experiment_key
    )


def test_f9_valid_ids_are_at_the_limits_and_the_two_unicode_forms_differ():
    users = {
        op.body["user_id"]
        for op in planted("F9").ops
        if op.kind == "assign" and op.expect == faults.ASSIGNED
    }
    forms = sorted(u for u in users if faults.f9_class(u) == "nfc-and-nfd-user-id")
    # The same text in its two Unicode forms: two users unless a server
    # normalises them into one.
    assert len(forms) == 2 and forms[0] != forms[1]
    assert len({unicodedata.normalize("NFC", u) for u in forms}) == 1
    assert max(len(u) for u in users) == 255


def test_f4_dates_every_event_of_its_users_in_the_future_and_skews_by_serial():
    plan = planted("F4")
    assert faults.future_dated_users(plan) == {"control": 2, "treatment": 2}
    assert faults.future_dated_users(faults.inject(CLEAN)) == {
        "control": 0,
        "treatment": 0,
    }
    chosen = {
        b["user_id"] for b in _bodies(plan) if b.get("timestamp") == faults.FUTURE
    }
    serial = 0
    skewed = 0
    for op in plan.ops:
        for body in _bodies(g.Plan(SEED, SCENARIO, (op,))):
            stamp = body.get("timestamp")
            if body["user_id"] in chosen:
                assert stamp == faults.FUTURE
            elif stamp is not None:
                assert stamp == faults._skew(serial), (serial, stamp)
                assert not stamp.startswith(("9999", "0001", "2099"))
                skewed += 1
            serial += 1
    assert skewed > 10


def test_f4_composes_with_another_fault_and_keeps_its_timestamps():
    plan = faults.inject(CLEAN, SMALL["F9"], SMALL["F4"])
    edges = [
        b
        for b in _bodies(plan)
        if b.get("timestamp") in faults.EDGE_TIMESTAMPS.values()
    ]
    assert len(edges) == 6  # F9's edge timestamps on /track, /events and /batch
    assert faults.future_dated_users(plan) == {"control": 2, "treatment": 2}


def test_f2_sends_its_bot_volume_and_values_beyond_the_bound():
    """The volume is pinned here, not by the sensitivity check: ``bots``
    users assigned in a batch, ``events_per_bot`` events each through
    ``/batch`` after their assignment, each with its own ``event_id``;
    ``huge_values`` of them beyond 10^12 in size, of both signs, the rest
    ``BOT_VALUE``."""
    fault = SMALL["F2"]
    plan = planted("F2")
    bodies = [b for b in _bodies(plan) if "-f2-e" in (b.get("event_id") or "")]
    assert len(bodies) == fault.bots * fault.events_per_bot == 120
    per_bot = Counter(b["user_id"] for b in bodies)
    assert sorted(per_bot.values()) == [fault.events_per_bot] * fault.bots
    assert len({b["event_id"] for b in bodies}) == len(bodies)
    batched = {
        u["user_id"]
        for op in CLEAN.ops
        if op.kind == "assign_batch"
        for u in op.body["users"]
    }
    assert set(per_bot) <= batched
    first = min(
        i
        for i, op in enumerate(plan.ops)
        for b in _bodies(g.Plan(SEED, SCENARIO, (op,)))
        if "-f2-e" in (b.get("event_id") or "")
    )
    assert first > max(i for i, op in enumerate(plan.ops) if op.kind == "assign")
    assert {op.kind for op in plan.ops[first:]} == {"batch"}
    beyond = [b["value"] for b in bodies if abs(b["value"]) > contract.VALUE_BOUND]
    assert len(beyond) == fault.huge_values == 6
    assert beyond == [faults.BEYOND_BOUND[i % 2] for i in range(6)]
    assert all(
        b["value"] == faults.BOT_VALUE for b in bodies if b["value"] not in beyond
    )
    # The API refuses a value beyond the bound in a /batch entry (#1630): each
    # batch expects exactly those positions in its errors, and no other.
    positions = 0
    for op in plan.ops[first:]:
        events = op.body["events"]
        refused = [
            i for i, e in enumerate(events) if abs(e["value"]) > contract.VALUE_BOUND
        ]
        want = "item-errors:" + ",".join(map(str, refused)) if refused else "accepted"
        assert faults.check_expect(op) == want, (want, faults.check_expect(op))
        positions += len(refused)
    assert positions == fault.huge_values


def test_f9_valid_sends_carry_the_value_it_is_given():
    plan = faults.inject(CLEAN, faults.Hostile(per_class=1, value=1.0))
    valid = [
        op.body
        for op in plan.ops
        if op.kind == "track" and faults.f9_class(op.body["user_id"]) in faults.F9_VALID
    ]
    assert len(valid) == 6 and all(body["value"] == 1.0 for body in valid)
    assert all(
        "value" not in b
        for b in _bodies(planted("F9"))
        if faults.f9_class(b["user_id"]) in faults.F9_VALID
    )


# ---------------------------------------------------------------------------
# Every fault at once: combine
# ---------------------------------------------------------------------------
COMBINED = faults.combine(CLEAN, *SMALL.values())


def _fault_users(plan: g.Plan) -> dict[str, set[str]]:
    """Who carries each fault in a combined plan, read from the plan alone."""
    actions = {
        op.body["action"]: i for i, op in enumerate(plan.ops) if op.kind == "lifecycle"
    }
    first_assign = _first(plan, ("assign", "assign_batch"))
    window = [
        *plan.ops[actions["pause"] + 1 : actions["resume"]],
        *plan.ops[actions["complete"] + 1 :],
    ]
    seen = Counter(b["event_id"] for b in _bodies(plan) if b.get("event_id"))
    defined = set(g.user_ids(SEED, SCENARIO.users))
    return {
        "F1": {
            b["user_id"] for b in _bodies(plan) if seen.get(b.get("event_id"), 0) > 1
        }
        - {
            b["user_id"]
            for op in window
            for b in _bodies(g.Plan(SEED, SCENARIO, (op,)))
        },
        "F2": {
            b["user_id"] for b in _bodies(plan) if "-f2-e" in (b.get("event_id") or "")
        },
        "F3": {
            b["user_id"]
            for op in window
            for b in _bodies(g.Plan(SEED, SCENARIO, (op,)))
        },
        "F5": {
            b["user_id"]
            for op in plan.ops[:first_assign]
            for b in _bodies(g.Plan(SEED, SCENARIO, (op,)), ("track", "batch"))
        },
        "F6": {
            b["user_id"]
            for b in _bodies(plan, ("events",))
            if "-f6-e" in (b.get("event_id") or "")
        },
        "F7": defined - set(_assigned_users(plan)),
        "F9": {b["user_id"] for b in _bodies(plan) if faults.f9_class(b["user_id"])},
        "F4": {
            b["user_id"] for b in _bodies(plan) if b.get("timestamp") == faults.FUTURE
        },
    }


def test_combine_places_each_fault_on_users_no_other_fault_touched():
    users = _fault_users(COMBINED)
    assert all(users.values()), {name: len(found) for name, found in users.items()}
    overlaps = {
        (a, b): sorted(users[a] & users[b])[:3]
        for a in users
        for b in users
        if a < b and users[a] & users[b]
    }
    assert overlaps == {}


def test_combine_encloses_every_other_send_in_f3_s_pause_and_complete():
    """Between the pause and the resume, and after the ``complete``, only
    F3's late events and its users asking again; everything else between
    the start and the ``complete``."""
    plan = COMBINED
    actions = [op.body["action"] for op in plan.ops if op.kind == "lifecycle"]
    assert actions == ["create", "start", "pause", "resume", "complete"]
    at = {
        op.body["action"]: i for i, op in enumerate(plan.ops) if op.kind == "lifecycle"
    }
    late = _fault_users(plan)["F3"]
    for lo, hi in ((at["pause"], at["resume"]), (at["complete"], len(plan.ops))):
        for op in plan.ops[lo + 1 : hi]:
            if op.kind == "assign":
                assert op.expect == faults.UNASSIGNED, op
            else:
                assert op.kind in ("track", "batch"), op
                assert {
                    b["user_id"] for b in _bodies(g.Plan(SEED, SCENARIO, (op,)))
                } <= late
    assert (
        plan.ops[0].body["action"] == "create" and plan.ops[1].body["action"] == "start"
    )


def test_the_expected_ledger_of_a_fault_plan_reads_and_counts():
    plan = faults.inject(CLEAN, SMALL["F6"])
    records = faults.expected_records(plan, faults.DECLARED)
    by_id = [r for r in records if r["kind"] == "event" and r.get("route") == "/events"]
    assert len(by_id) == 14 and all("variant" in r for r in by_id)
    experiment = contract.valid_events(records).experiments[SCENARIO.experiment_key]
    assert experiment.by_id_events == 14
