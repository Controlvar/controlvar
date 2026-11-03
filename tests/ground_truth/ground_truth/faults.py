"""Faults planted in a seeded plan (#1544 part D): ``inject(plan, *faults)``.

A fault is a ``Plan -> Plan`` injector. ``inject`` applies fault definitions
to a plan ``generator.simulate`` made and returns the plan a driver sends; the
clean plan is never changed, so ``simulate`` and the clean scenarios stay
byte-for-byte what they were, and a fault scenario is a clean scenario plus
its fault.

**Every count a fault plants is a named integer of its definition**, never a
share or a random draw. The definition says how many repeats, untagged events
or lost users the run must show. The sensitivity check
(``tests/ground_truth/sensitivity.py``) compares the ledger with the integers
named in the table below, so a fault that reached the server short, or not at
all, fails the scenario instead of leaving its gates passing on a server that
has the defect. The others (``no_id_repeats``, ``paused_assigns``,
``completed_assigns``, ``paused_resends``) are pinned at plan level, by
``test_fault_library.py``, not against the ledger. Which
events or users carry the fault is decided from the plan alone, spread evenly
over the eligible ones in plan order (which the seed fixes), so the same seed
gives the same plan. A count below 1 is refused when the definition is made,
and a plan with fewer eligible events or users than a count needs is refused
by the injector; both raise ``ValueError`` naming the fault.

=====  ===================  =========  =============================================
Fault  What                 Rule       Named integer(s) the sensitivity check reads
=====  ===================  =========  =============================================
F1     duplicates           R4         ``r4_drops`` (``no_id_repeats``: a control)
F3     late events          R2-status  ``r2_status_untags``
F5     events before        R2-order   ``r2_order_untags``
       assignment
F6     variant mismatch     R8         ``other_arm + never_assigned`` counted
       on /events                      nowhere; ``tagged_then_assigned + own_arm``
                                       counted (positive controls)
F7     exposure loss        none       ``lost_users``
F9     hostile payloads     none       ``per_class`` sends of each class
F4     clock skew           none (R7)  ``future_users_per_arm``
=====  ===================  =========  =============================================

Each count is exact when its fault is the only one in the plan besides F4,
which adds timestamps and nothing else.

Ops
---
Beside ``simulate``'s kinds, a fault plan has the lifecycle actions ``pause``,
``resume`` (sent as ``POST /api/v1/experiments/{id}/start``) and
``complete``; ``events`` ops for ``POST /api/v1/tracking/events``, whose body
names the experiment by ``experiment_key`` and the variant by arm name
(``variant``, None for none) where the request carries ids
(``events_request`` maps them); and ``hostile`` ops, raw request text no
ledger records (``{"class": ..., "content": ...}``), each naming the
scenario's experiment so a stored one would show in its counts.

Every op of a plan ``inject`` returns has an ``expect`` (``EXPECTS``): the
answer it must get. ``unexpected`` compares an answer with it, and
``expected_answer`` is the answer it describes.

Imports only the standard library and this library.
"""

from __future__ import annotations

import io
import itertools
import json
import math
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from . import contract, hashing, ledger, reference
from .generator import Op, Plan

# ---------------------------------------------------------------------------
# What an op expects
# ---------------------------------------------------------------------------
#: A lifecycle step: a 2xx.
OK = "ok"
#: An assignment: a 2xx whose every answer says ``assigned: true``.
ASSIGNED = "assigned"
#: ``/assign`` while the experiment is not running: a 2xx, ``assigned: false``.
UNASSIGNED = "unassigned"
#: An event: a 2xx, and for a ``/batch`` no position in ``errors``.
ACCEPTED = "accepted"
#: ``item-errors:1,3``: a 2xx ``/batch`` whose ``errors`` are exactly those
#: positions. A three-digit status (``"422"``) expects exactly that status.
ITEM_ERRORS = "item-errors:"
#: The expectations each kind of op may carry; a status is allowed on any
#: kind but ``lifecycle``.
EXPECTS = {
    "lifecycle": (OK,),
    "assign": (ASSIGNED, UNASSIGNED),
    "assign_batch": (ASSIGNED,),
    "track": (ACCEPTED,),
    "batch": (ACCEPTED, ITEM_ERRORS),
    "events": (ACCEPTED,),
    "hostile": (),
}
#: What an op expects when nothing says otherwise; a hostile op says.
DEFAULT_EXPECT = {
    "lifecycle": OK,
    "assign": ASSIGNED,
    "assign_batch": ASSIGNED,
    "track": ACCEPTED,
    "batch": ACCEPTED,
    "events": ACCEPTED,
}
LIFECYCLE = ("create", "start", "pause", "resume", "complete")
EVENTS = ledger.EVENTS
#: What the fault scenarios declare about the server: an event sent again
#: with its ``event_id`` is stored once (#1543), as the clean replay declares.
DECLARED = {"event_dedup": True}


def _item_errors(expect: str) -> list[int]:
    text = expect[len(ITEM_ERRORS) :]
    return sorted(int(part) for part in text.split(",")) if text else []


def check_expect(op: Op) -> str:
    """*op*'s expectation, its kind's default when it has none; ``ValueError``
    for one its kind cannot carry."""
    expect = op.expect if op.expect is not None else DEFAULT_EXPECT.get(op.kind)
    if expect is None:
        raise ValueError(f"a {op.kind} op needs an expect")
    allowed = EXPECTS.get(op.kind)
    if allowed is None:
        raise ValueError(f"no op has the kind {op.kind!r}")
    if op.kind == "lifecycle" and op.body.get("action") not in LIFECYCLE:
        raise ValueError(f"a lifecycle op's action is one of {LIFECYCLE}")
    if expect.isdigit() and len(expect) == 3 and op.kind != "lifecycle":
        if 100 <= int(expect) <= 599:
            return expect
    if expect.startswith(ITEM_ERRORS):
        if ITEM_ERRORS in allowed:
            try:
                positions = _item_errors(expect)
            except ValueError:
                positions = []
            size = len(op.body.get("events", ()))
            if positions and all(0 <= p < size for p in positions):
                if len(set(positions)) == len(positions):
                    return expect
    elif expect in allowed:
        return expect
    raise ValueError(f"a {op.kind} op cannot expect {expect!r}")


def unexpected(op: Op, status: int, body: Any) -> str | None:
    """Why the answer (*status*, *body*) is not the one *op* expects; None
    when it is."""
    expect = check_expect(op)
    if expect.isdigit():
        return None if status == int(expect) else f"answered {status}, want {expect}"
    if not 200 <= status < 300:
        return f"answered {status}, want a 2xx ({expect})"
    data = body if isinstance(body, Mapping) else {}
    if expect in (ASSIGNED, UNASSIGNED):
        want = expect == ASSIGNED
        if op.kind == "assign":
            answers = [data]
        else:
            answers = data.get("assignments")
            answers = answers if isinstance(answers, list) else []
            if len(answers) != len(op.body["users"]):
                return f"answered {len(answers)} assignments for {len(op.body['users'])} users"
        wrong = sum(
            1
            for a in answers
            if not isinstance(a, Mapping) or a.get("assigned") is not want
        )
        if wrong:
            return (
                f"{wrong} of {len(answers)} answers not assigned: {str(want).lower()}"
            )
        return None
    if op.kind != "batch":
        return None
    reported = data.get("errors") or []
    positions = sorted(
        e.get("index")
        for e in reported
        if isinstance(e, Mapping) and isinstance(e.get("index"), int)
    )
    want = _item_errors(expect) if expect.startswith(ITEM_ERRORS) else []
    if positions != want or len(positions) != len(reported):
        return f"errors at positions {positions}, want {want}"
    return None


def events_request(
    body: Mapping[str, Any], *, experiment_id: str, variant_ids: Mapping[str, str]
) -> dict[str, Any]:
    """The ``POST /api/v1/tracking/events`` body for an ``events`` op's body:
    the experiment and the variant named by id instead of key and arm name."""
    request = {k: v for k, v in body.items() if k not in ("experiment_key", "variant")}
    request["experiment_id"] = experiment_id
    if body.get("variant") is not None:
        request["variant_id"] = variant_ids[body["variant"]]
    return request


def expected_answer(
    op: Op, allocations: Sequence[tuple[str, int]], *, status: str = "active"
) -> tuple[int, Any]:
    """The answer *op*'s expectation describes, as a server would give it
    (variant ids ``id-<name>``). *status* is the experiment's, for the reason
    of an ``/assign`` that does not assign. Says nothing about repeats."""
    expect = check_expect(op)
    if expect.isdigit():
        return int(expect), {"detail": "refused as the plan expects"}
    key = op.body.get("experiment_key")
    if op.kind == "lifecycle":
        return 200, None
    if op.kind == "assign":
        user = op.body["user_id"]
        if expect == UNASSIGNED:
            variant, assigned, reason = allocations[0][0], False, status
        else:
            variant = hashing.variant_for(user, key, allocations)
            assigned, reason = True, "assigned"
        return 200, {
            "experiment_key": key,
            "user_id": user,
            "variant_id": f"id-{variant}",
            "variant_name": variant,
            "is_control": variant == allocations[0][0],
            "configuration": {},
            "assigned": assigned,
            "reason": reason,
        }
    if op.kind == "assign_batch":
        return 200, {
            "experiment_key": key,
            "variants": {f"id-{name}": {"name": name} for name, _ in allocations},
            "assignments": [
                {
                    "user_id": u["user_id"],
                    "variant_id": f"id-{hashing.variant_for(u['user_id'], key, allocations)}",
                    "assigned": True,
                    "reason": "assigned",
                }
                for u in op.body["users"]
            ],
        }
    if op.kind == "batch":
        size = len(op.body["events"])
        errors = _item_errors(expect) if expect.startswith(ITEM_ERRORS) else []
        return 200, {
            "success_count": size - len(errors),
            "failure_count": len(errors),
            "errors": [{"index": i, "error": "refused as planned"} for i in errors]
            or None,
        }
    return 200, {}


def expected_records(
    plan: Plan, declared: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The ledger of *plan* sent to a server that answers every op as its
    ``expect`` says (``expected_answer``); hostile ops are in no ledger."""
    stream = io.StringIO()
    writer = ledger.Writer(stream, **plan.start(declared))
    allocations = plan.allocations()
    key = plan.scenario.experiment_key
    variant_ids = {name: f"id-{name}" for name, _ in allocations}
    status = "draft"
    for op in plan.ops:
        if op.kind == "hostile":
            continue
        if op.kind == "lifecycle":
            seq = writer.send_action(op.body["action"], op.body["experiment_key"])
        elif op.kind == "events":
            seq = writer.send(
                EVENTS,
                events_request(
                    op.body, experiment_id=f"id-{key}", variant_ids=variant_ids
                ),
                exp=op.body["experiment_key"],
                variant=op.body["variant"],
            )
        else:
            seq = writer.send(op.route, op.body)
        answer, body = expected_answer(op, allocations, status=status)
        writer.answer(seq, answer, body)
        if op.kind == "lifecycle" and 200 <= answer < 300:
            status = contract.STATUS_AFTER[op.body["action"]]
    writer.end()
    return ledger.read(io.StringIO(stream.getvalue()))


# ---------------------------------------------------------------------------
# Reading and rewriting a plan
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Event:
    """An event of a ``track`` op (``item`` None) or a ``batch`` op."""

    op: int
    item: int | None
    body: Mapping[str, Any]

    @property
    def user(self) -> str:
        return self.body["user_id"]

    @property
    def single(self) -> bool:
        return self.item is None


def _events(plan: Plan) -> list[_Event]:
    found: list[_Event] = []
    for index, op in enumerate(plan.ops):
        if op.kind == "track":
            found.append(_Event(index, None, op.body))
        elif op.kind == "batch":
            found.extend(_Event(index, i, e) for i, e in enumerate(op.body["events"]))
    return found


def _assigned(plan: Plan) -> dict[str, bool]:
    """Each user an op assigns, in plan order: True when one at a time."""
    found: dict[str, bool] = {}
    for op in plan.ops:
        if op.kind == "assign" and check_expect(op) == ASSIGNED:
            found.setdefault(op.body["user_id"], True)
        elif op.kind == "assign_batch":
            for entry in op.body["users"]:
                found.setdefault(entry["user_id"], False)
    return found


def _arm(plan: Plan, user: str) -> str:
    """O-ASSIGN's variant for *user*."""
    return hashing.variant_for(user, plan.scenario.experiment_key, plan.allocations())


def _arms(plan: Plan) -> list[str]:
    return [arm.name for arm in plan.scenario.arms]


def _spread(items: Sequence[Any], count: int, fault: str, what: str) -> list[Any]:
    """*count* of *items*, evenly spread over them in order; refused when
    there are fewer."""
    if len(items) < count:
        raise ValueError(f"{fault} needs {count} {what}; the plan has {len(items)}")
    return [items[(i * len(items)) // count] for i in range(count)]


def _chunks(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _without(ops: Sequence[Op], picked: set[tuple[int, int | None]]) -> list[Op]:
    """*ops* with the picked events taken out, and any op left empty."""
    kept: list[Op] = []
    for index, op in enumerate(ops):
        if op.kind == "track" and (index, None) in picked:
            continue
        if op.kind == "batch":
            events = [
                e for i, e in enumerate(op.body["events"]) if (index, i) not in picked
            ]
            if not events:
                continue
            if len(events) != len(op.body["events"]):
                op = replace(op, body={**op.body, "events": events})
        kept.append(op)
    return kept


def _sends(events: Sequence[_Event], size: int) -> list[Op]:
    """Ops sending *events* as they were sent: alone on ``/track``, or in
    ``/batch`` requests of at most *size*."""
    ops = [Op("track", ledger.TRACK, dict(e.body)) for e in events if e.single]
    batched = [dict(e.body) for e in events if not e.single]
    ops += [Op("batch", ledger.BATCH, {"events": c}) for c in _chunks(batched, size)]
    return ops


def _after_start(ops: Sequence[Op]) -> int:
    """Where the first request after the create and start steps goes."""
    for index, op in enumerate(ops):
        if op.kind != "lifecycle":
            return index
    return len(ops)


def _after_assignments(ops: Sequence[Op]) -> int:
    last = [i for i, op in enumerate(ops) if op.kind in ("assign", "assign_batch")]
    return last[-1] + 1 if last else _after_start(ops)


def _lifecycle(plan: Plan, action: str) -> Op:
    return Op(
        "lifecycle",
        None,
        {"action": action, "experiment_key": plan.scenario.experiment_key},
    )


def _by_id(plan: Plan, event: Mapping[str, Any], variant: str | None) -> dict[str, Any]:
    """An ``events`` op's body for *event* (a ``/track`` body), naming *variant*."""
    body: dict[str, Any] = {
        "event_name": event.get("event_name") or event["event_type"],
        "event_type": event["event_type"],
        "user_id": event["user_id"],
        "experiment_key": plan.scenario.experiment_key,
        "variant": variant,
    }
    for field in ("value", "event_id", "timestamp"):
        if field in event:
            body[field] = event[field]
    return body


def _one_event_users(plan: Plan, events: Sequence[_Event]) -> list[_Event]:
    """The events whose user is assigned and sends no other: moving one moves
    whether its user converts, so no metric can hide it."""
    per_user = Counter(e.user for e in events)
    assigned = _assigned(plan)
    return [e for e in events if per_user[e.user] == 1 and e.user in assigned]


def _positive(fault: str, counts: Mapping[str, Any]) -> None:
    for name, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                f"{fault}: {name} must be a whole number of at least 1, not {value!r}:"
                " a fault that plants nothing tests nothing"
            )


# ---------------------------------------------------------------------------
# The faults
# ---------------------------------------------------------------------------
#: F1's kinds of repeat, in the order its drops are dealt to them.
F1_KINDS = ("track", "value", "later-batch", "same-batch", "track-in-batch", "events")
#: The ``timestamp`` every other F1 ``/track`` repeat carries: a repeat is
#: one whatever its time.
RESEND_TIMESTAMP = "2026-01-15T09:30:00Z"


@dataclass(frozen=True)
class Duplicates:
    """F1: events sent again. ``r4_drops`` repeats of an event already stored
    with its ``event_id`` (R4 stores none of them), dealt in turn to six
    kinds: on ``/track`` again; again with another ``value`` (the first is
    kept); in a later ``/batch``; twice in one ``/batch``; a ``/track`` event
    again in a ``/batch``; through ``/events``. And ``no_id_repeats`` events
    with no ``event_id`` sent again, both stored and both counted."""

    r4_drops: int = 60
    no_id_repeats: int = 10
    NAME = "F1"
    RULE = "R4"

    def __post_init__(self) -> None:
        _positive(
            self.NAME, {"r4_drops": self.r4_drops, "no_id_repeats": self.no_id_repeats}
        )

    def apply(self, plan: Plan) -> Plan:
        kinds = len(F1_KINDS)
        if self.r4_drops < kinds:
            raise ValueError(
                f"F1 needs r4_drops of at least {kinds}, one of each kind of repeat;"
                f" it is {self.r4_drops}"
            )
        size = plan.scenario.event_batch_size
        if size < 2:
            raise ValueError(
                "F1 needs event_batch_size of at least 2 for a repeat in one /batch"
            )
        n = {
            kind: len(range(i, self.r4_drops, kinds)) for i, kind in enumerate(F1_KINDS)
        }
        events = _events(plan)
        singles = _spread(
            [e for e in events if e.single and e.body.get("event_id")],
            n["track"] + n["value"] + n["track-in-batch"],
            "F1",
            "/track events that carry an event_id",
        )
        batched = _spread(
            [e for e in events if not e.single and e.body.get("event_id")],
            n["later-batch"] + n["same-batch"] + n["events"],
            "F1",
            "/batch events that carry an event_id",
        )
        plain = _spread(
            [e for e in events if not e.body.get("event_id")],
            self.no_id_repeats,
            "F1",
            "events sent without an event_id",
        )
        track_at = n["track"]
        value_at = track_at + n["value"]
        again = singles[:track_at]
        other_value = singles[track_at:value_at]
        in_batch = singles[value_at:]
        later_at = n["later-batch"]
        same_at = later_at + n["same-batch"]
        later = batched[:later_at]
        same = batched[later_at:same_at]
        by_id = batched[same_at:]

        ops = _without(plan.ops, {(e.op, e.item) for e in same})
        for i, e in enumerate(again):
            body = (
                dict(e.body)
                if i % 2 == 0
                else {**e.body, "timestamp": RESEND_TIMESTAMP}
            )
            ops.append(Op("track", ledger.TRACK, body))
        for e in other_value:
            value = (e.body.get("value") or 0.0) + 1.0
            ops.append(Op("track", ledger.TRACK, {**e.body, "value": value}))
        ops += [Op("track", ledger.TRACK, dict(e.body)) for e in plain if e.single]
        resent = [dict(e.body) for e in (*later, *in_batch)]
        resent += [dict(e.body) for e in plain if not e.single]
        ops += [Op("batch", ledger.BATCH, {"events": c}) for c in _chunks(resent, size)]
        pairs = [dict(e.body) for e in same for _ in range(2)]
        ops += [
            Op("batch", ledger.BATCH, {"events": c})
            for c in _chunks(pairs, size - size % 2)
        ]
        ops += [
            Op("events", EVENTS, _by_id(plan, e.body, _arm(plan, e.user)))
            for e in by_id
        ]
        return replace(plan, ops=tuple(ops))

    def findings(self, plan, records, expected, without, hostile_sends) -> list[str]:
        got = without.total_events - expected.total_events
        return _planted(self, self.r4_drops, got, "R4 drops (repeats not stored)")


@dataclass(frozen=True)
class LateEvents:
    """F3: events received while the experiment is paused or completed.

    ``r2_status_untags`` events, each its user's only one, sent while paused
    (every other one) or after completing (the rest), so R2 stores them with
    no variant. Around them: ``paused_assigns`` and ``completed_assigns``
    assigned users asking ``/assign`` again (``assigned: false``, nothing
    recorded), and ``paused_resends`` of the paused events sent again after
    the resume with their ``event_id`` (R4: not stored, so still in no
    variant). The other events are sent half before the pause, half after the
    resume."""

    r2_status_untags: int = 40
    paused_assigns: int = 20
    completed_assigns: int = 10
    paused_resends: int = 10
    NAME = "F3"
    RULE = "R2-status"

    def __post_init__(self) -> None:
        _positive(
            self.NAME,
            {
                "r2_status_untags": self.r2_status_untags,
                "paused_assigns": self.paused_assigns,
                "completed_assigns": self.completed_assigns,
                "paused_resends": self.paused_resends,
            },
        )

    def apply(self, plan: Plan) -> Plan:
        late = _spread(
            _one_event_users(plan, _events(plan)),
            self.r2_status_untags,
            "F3",
            "events whose user is assigned and sends no other",
        )
        paused, completed = late[0::2], late[1::2]
        resent = _spread(
            [e for e in paused if e.body.get("event_id")],
            self.paused_resends,
            "F3",
            "events sent while paused that carry an event_id, to send again after the resume",
        )
        asking = _spread(
            list(_assigned(plan)),
            self.paused_assigns + self.completed_assigns,
            "F3",
            "assigned users, to ask for their assignment again",
        )
        key = plan.scenario.experiment_key

        def again(user: str) -> Op:
            body = {"experiment_key": key, "user_id": user}
            return Op("assign", ledger.ASSIGN, body, UNASSIGNED)

        ops = _without(plan.ops, {(e.op, e.item) for e in late})
        at = _after_assignments(ops)
        head, rest = ops[:at], ops[at:]
        middle = len(rest) // 2
        size = plan.scenario.event_batch_size
        ops = [
            *head,
            *rest[:middle],
            _lifecycle(plan, "pause"),
            *_sends(paused, size),
            *[again(u) for u in asking[: self.paused_assigns]],
            _lifecycle(plan, "resume"),
            *rest[middle:],
            *_sends(resent, size),
            _lifecycle(plan, "complete"),
            *_sends(completed, size),
            *[again(u) for u in asking[self.paused_assigns :]],
        ]
        return replace(plan, ops=tuple(ops))

    def findings(self, plan, records, expected, without, hostile_sends) -> list[str]:
        got = expected.untagged_events - without.untagged_events
        return _planted(self, self.r2_status_untags, got, "events R2 untags by status")


@dataclass(frozen=True)
class EarlyEvents:
    """F5: ``r2_order_untags`` events, each its user's only one, sent before
    any assignment (right after the experiment starts), so R2 stores them with
    no variant even once their user is assigned."""

    r2_order_untags: int = 40
    NAME = "F5"
    RULE = "R2-order"

    def __post_init__(self) -> None:
        _positive(self.NAME, {"r2_order_untags": self.r2_order_untags})

    def apply(self, plan: Plan) -> Plan:
        early = _spread(
            _one_event_users(plan, _events(plan)),
            self.r2_order_untags,
            "F5",
            "events whose user is assigned and sends no other",
        )
        ops = _without(plan.ops, {(e.op, e.item) for e in early})
        at = _after_start(ops)
        ops[at:at] = _sends(early, plan.scenario.event_batch_size)
        return replace(plan, ops=tuple(ops))

    def findings(self, plan, records, expected, without, hostile_sends) -> list[str]:
        got = expected.untagged_events - without.untagged_events
        return _planted(self, self.r2_order_untags, got, "events R2 untags by order")


@dataclass(frozen=True)
class VariantMismatch:
    """F6: ``POST /api/v1/tracking/events`` naming a variant the user is not
    assigned to (R8). Counted in no variant: ``other_arm`` assigned users'
    events naming another arm (case i) and ``never_assigned`` events of users
    never assigned (case ii). Counted, as positive controls:
    ``tagged_then_assigned`` events naming the user's arm sent before the
    user is assigned (case ii-b) and ``own_arm`` assigned users' events naming
    their arm (case iii). The scenario needs a conversion metric and a count
    metric on the event: the server checks the assignment separately for
    converting users and for counts and values, so a defect in one check moves
    one kind of metric only."""

    other_arm: int = 40
    never_assigned: int = 40
    tagged_then_assigned: int = 20
    own_arm: int = 20
    NAME = "F6"
    RULE = "R8"

    def __post_init__(self) -> None:
        _positive(
            self.NAME,
            {
                "other_arm": self.other_arm,
                "never_assigned": self.never_assigned,
                "tagged_then_assigned": self.tagged_then_assigned,
                "own_arm": self.own_arm,
            },
        )

    def apply(self, plan: Plan) -> Plan:
        scenario = plan.scenario
        name = scenario.event_name
        types = {m.type for m in scenario.metrics if m.event_name == name}
        missing = [t for t in ("conversion", "count") if t not in types]
        if missing:
            raise ValueError(
                f"F6 needs a conversion metric and a count metric on {name!r}; the"
                f" scenario has no {' or '.join(missing)} metric on it"
            )
        assigned = _assigned(plan)
        early = _spread(
            [u for u, single in assigned.items() if not single],
            self.tagged_then_assigned,
            "F6",
            "users assigned in a batch, to name their arm before it",
        )
        taken = set(early)
        later = _spread(
            [u for u in assigned if u not in taken],
            self.other_arm + self.own_arm,
            "F6",
            "other assigned users, to name an arm after their assignment",
        )
        arms = _arms(plan)
        serial = itertools.count()

        def event(user: str, variant: str) -> Op:
            body = {
                "event_name": name,
                "event_type": name,
                "user_id": user,
                "experiment_key": scenario.experiment_key,
                "variant": variant,
                "value": 1.0,
                "event_id": f"gt-{plan.seed}-f6-e{next(serial):06d}",
            }
            return Op("events", EVENTS, body)

        def another(arm: str) -> str:
            return arms[(arms.index(arm) + 1) % len(arms)]

        before = [event(u, _arm(plan, u)) for u in early]
        after = [event(u, another(_arm(plan, u))) for u in later[: self.other_arm]]
        after += [
            event(f"gt-{plan.seed}-f6-never-{n:06d}", arms[n % len(arms)])
            for n in range(self.never_assigned)
        ]
        after += [event(u, _arm(plan, u)) for u in later[self.other_arm :]]
        ops = list(plan.ops)
        at = _after_start(ops)
        ops[at:at] = before
        ops += after
        return replace(plan, ops=tuple(ops))

    def findings(self, plan, records, expected, without, hostile_sends) -> list[str]:
        nowhere = expected.untagged_events - without.untagged_events
        found = _planted(
            self,
            self.other_arm + self.never_assigned,
            nowhere,
            "/events events counted in no variant (cases i and ii)",
        )
        counted = expected.by_id_events - nowhere
        want = self.tagged_then_assigned + self.own_arm
        if counted != want:
            found.append(
                f"F6 (R8): the definition plants {want} /events events counted in"
                f" their variant (cases ii-b and iii, the positive controls); the"
                f" ledger shows {counted}"
            )
        return found


@dataclass(frozen=True)
class ExposureLoss:
    """F7: ``lost_users`` users of one arm (``arm``, the first after the
    control when None) never ask for an assignment; their events are still
    sent, and stored with no variant. The sample-ratio check must fire."""

    lost_users: int = 1000
    arm: str | None = None
    NAME = "F7"
    RULE = None

    def __post_init__(self) -> None:
        _positive(self.NAME, {"lost_users": self.lost_users})

    def apply(self, plan: Plan) -> Plan:
        arms = _arms(plan)
        arm = arms[1] if self.arm is None else self.arm
        if arm not in arms:
            raise ValueError(f"F7: the scenario has no arm {arm!r}; it has {arms}")
        lost = set(
            _spread(
                [u for u in _assigned(plan) if _arm(plan, u) == arm],
                self.lost_users,
                "F7",
                f"users assigned to {arm!r}",
            )
        )
        ops: list[Op] = []
        for op in plan.ops:
            if op.kind == "assign" and op.body["user_id"] in lost:
                continue
            if op.kind == "assign_batch":
                users = [u for u in op.body["users"] if u["user_id"] not in lost]
                if not users:
                    continue
                if len(users) != len(op.body["users"]):
                    op = replace(op, body={**op.body, "users": users})
            ops.append(op)
        return replace(plan, ops=tuple(ops))

    def findings(self, plan, records, expected, without, hostile_sends) -> list[str]:
        got = plan.scenario.users - expected.total_users
        found = _planted(self, self.lost_users, got, "users never assigned")
        sizes = [expected.sample_size(v) for v in expected.variants]
        p = reference.srm_p(sizes, expected.allocations)
        if not p < SRM_ALARM:
            found.append(
                f"F7: the reference's sample-ratio p-value on the ledger's sample sizes"
                f" {sizes} is {p:.3g}, not below {SRM_ALARM}: the loss does not show"
            )
        return found


#: The p-value below which the sample-ratio check warns (``srm_service.py``).
SRM_ALARM = 0.001

#: The two edge timestamps of #1684: valid as sent, past the range in UTC.
EDGE_TIMESTAMPS = {
    "max": "9999-12-31T23:59:59-01:00",
    "min": "0001-01-01T00:00:00+01:00",
}
#: The deepest ``metadata`` the API stores is 64 levels (#1685).
METADATA_MAX_DEPTH = 64
#: The largest request body the API reads (``backend/app/main.py``).
MAX_BODY_BYTES = 5 * 1024 * 1024
#: Hostile but valid: assigned and stored, so each counts in a variant.
F9_VALID = (
    "emoji-user-id",
    "nfc-and-nfd-user-id",
    "longest-user-id",
    "longest-event-id",
    "longest-event-type",
)
#: Refused, through the ledger: the class and the answer it must get (#1686's
#: regression tests for the edge timestamps and the deep metadata).
F9_REFUSED = {
    "nul-user-id": "422",
    "nul-event-id": "422",
    "nul-metadata": "422",
    "user-id-over-255": "422",
    "event-id-over-255": "422",
    "event-type-over-100": "422",
    "batch-of-101": "422",
    "batch-with-a-nul-item": "422",
    "batch-with-unknown-keys": "item-errors:1,3",
    "edge-max-track": "422",
    "edge-min-track": "422",
    "edge-max-events": "422",
    "edge-min-events": "422",
    "edge-max-batch": "item-errors:0",
    "edge-min-batch": "item-errors:0",
    "deep-metadata-track": "422",
    "deep-metadata-events": "422",
    "deep-metadata-batch": "item-errors:0",
}
#: Refused, outside the ledger (it cannot record them): raw request text.
F9_HOSTILE = {
    "body-over-5-mib": "413",
    "json-nested-5000-deep": "400",
    "lone-surrogate": "422",
    "invalid-json": "422",
}
#: Sends of each class per unit of ``per_class``: the NFC and NFD forms of
#: one id are two users.
F9_SENDS = {"nfc-and-nfd-user-id": 2}


def _nested(levels: int) -> Any:
    value: Any = 1
    for _ in range(levels):
        value = {"a": value}
    return value


def f9_class(user: str) -> str | None:
    """The F9 class whose send *user* belongs to, or None."""
    if "-f9~" not in user:
        return None
    return user.split("-f9~", 1)[1].split("~", 1)[0]


@dataclass(frozen=True)
class Hostile:
    """F9: ``per_class`` sends of each hostile class. The valid ones
    (``F9_VALID``) are assigned and stored, so each counts in a variant; the
    refused ones (``F9_REFUSED``) go through the ledger and must be refused as
    listed, storing nothing; the rest (``F9_HOSTILE``) are raw text outside
    the ledger. Every send names the scenario's experiment (but the two
    unknown keys of ``batch-with-unknown-keys``), so a stored one shows in its
    counts, and its user (``gt-<seed>-f9~<class>~<n>``) names its class."""

    per_class: int = 1
    NAME = "F9"
    RULE = None

    def __post_init__(self) -> None:
        _positive(self.NAME, {"per_class": self.per_class})

    def apply(self, plan: Plan) -> Plan:
        assigns: list[Op] = []
        tracks: list[Op] = []
        refused: list[Op] = []
        hostile: list[Op] = []
        for n in range(self.per_class):
            for user, body in self._valid(plan, n):
                assign = {
                    "experiment_key": plan.scenario.experiment_key,
                    "user_id": user,
                }
                assigns.append(Op("assign", ledger.ASSIGN, assign, ASSIGNED))
                tracks.append(Op("track", ledger.TRACK, body, ACCEPTED))
            refused += self._refused(plan, n)
            hostile += [
                Op(
                    "hostile",
                    ledger.TRACK,
                    {"class": cls, "content": text},
                    F9_HOSTILE[cls],
                )
                for cls, text in self._hostile(plan, n)
            ]
        return replace(plan, ops=(*plan.ops, *assigns, *tracks, *refused, *hostile))

    @staticmethod
    def _user(plan: Plan, cls: str, n: int) -> str:
        return f"gt-{plan.seed}-f9~{cls}~{n}"

    def _valid(self, plan: Plan, n: int) -> list[tuple[str, dict[str, Any]]]:
        name, key = plan.scenario.event_name, plan.scenario.experiment_key

        def track(user: str, **extra: Any) -> tuple[str, dict[str, Any]]:
            return user, {
                "event_type": name,
                "user_id": user,
                "experiment_key": key,
                **extra,
            }

        # One text in its two Unicode forms: NFC U+00E9, NFD U+0065 U+0301.
        forms = self._user(plan, "nfc-and-nfd-user-id", n) + "~caf"
        return [
            track(self._user(plan, "emoji-user-id", n) + "~\U0001f9ea\U0001f98a"),
            track(forms + chr(0xE9)),
            track(forms + "e" + chr(0x301)),
            track((self._user(plan, "longest-user-id", n) + "~").ljust(255, "x")),
            track(
                self._user(plan, "longest-event-id", n),
                event_id=(self._user(plan, "longest-event-id", n) + "~id~").ljust(
                    255, "x"
                ),
            ),
            track(
                self._user(plan, "longest-event-type", n),
                event_type=f"gt-f9-{n}-".ljust(100, "t"),
                event_name=name,
            ),
        ]

    def _refused(self, plan: Plan, n: int) -> list[Op]:
        name, key = plan.scenario.event_name, plan.scenario.experiment_key
        first = _arms(plan)[0]

        def event(cls: str, **extra: Any) -> dict[str, Any]:
            return {
                "event_type": name,
                "user_id": self._user(plan, cls, n),
                "experiment_key": key,
                **extra,
            }

        def track(cls: str, **extra: Any) -> Op:
            return Op("track", ledger.TRACK, event(cls, **extra), F9_REFUSED[cls])

        def batch(cls: str, events: list[dict[str, Any]]) -> Op:
            return Op("batch", ledger.BATCH, {"events": events}, F9_REFUSED[cls])

        def by_id(cls: str, **extra: Any) -> Op:
            body = {**_by_id(plan, event(cls), first), **extra}
            return Op("events", EVENTS, body, F9_REFUSED[cls])

        def long_text(cls: str, size: int) -> str:
            return (self._user(plan, cls, n) + "~").ljust(size, "x")

        deep = _nested(METADATA_MAX_DEPTH + 1)
        ops = [
            track("nul-user-id", user_id=self._user(plan, "nul-user-id", n) + "\u0000"),
            track("nul-event-id", event_id="gt-f9\u0000id"),
            track("nul-metadata", metadata={"note": "a\u0000b"}),
            track("user-id-over-255", user_id=long_text("user-id-over-255", 256)),
            track("event-id-over-255", event_id=long_text("event-id-over-255", 256)),
            track("event-type-over-100", event_type=f"gt-f9-{n}-".ljust(101, "t")),
            batch("batch-of-101", [event("batch-of-101") for _ in range(101)]),
            batch(
                "batch-with-a-nul-item",
                [
                    event("batch-with-a-nul-item"),
                    event(
                        "batch-with-a-nul-item",
                        user_id=self._user(plan, "batch-with-a-nul-item", n) + "\u0000",
                    ),
                ],
            ),
            batch(
                "batch-with-unknown-keys",
                [
                    event("batch-with-unknown-keys"),
                    event("batch-with-unknown-keys", experiment_key=f"{key}-unknown"),
                    event("batch-with-unknown-keys"),
                    event("batch-with-unknown-keys", experiment_key=f"{key}-unknown"),
                ],
            ),
        ]
        for edge, stamp in EDGE_TIMESTAMPS.items():
            ops.append(track(f"edge-{edge}-track", timestamp=stamp))
            ops.append(by_id(f"edge-{edge}-events", timestamp=stamp))
            ops.append(
                batch(
                    f"edge-{edge}-batch", [event(f"edge-{edge}-batch", timestamp=stamp)]
                )
            )
        ops.append(track("deep-metadata-track", metadata=deep))
        ops.append(by_id("deep-metadata-events", metadata=deep))
        ops.append(
            batch("deep-metadata-batch", [event("deep-metadata-batch", metadata=deep)])
        )
        return ops

    def _hostile(self, plan: Plan, n: int) -> list[tuple[str, str]]:
        name, key = plan.scenario.event_name, plan.scenario.experiment_key

        def text(cls: str, **extra: Any) -> str:
            body = {
                "event_type": name,
                "user_id": self._user(plan, cls, n),
                "experiment_key": key,
            }
            return json.dumps({**body, **extra}, separators=(",", ":"))

        empty = text("body-over-5-mib", metadata={"pad": ""})
        pad = MAX_BODY_BYTES + 1 - len(empty.encode("utf-8"))
        nested = text("json-nested-5000-deep")[:-1] + ',"metadata":'
        lone = json.dumps(
            {
                "event_type": name,
                "user_id": self._user(plan, "lone-surrogate", n) + "\ud800",
                "experiment_key": key,
            }
        )
        return [
            ("body-over-5-mib", text("body-over-5-mib", metadata={"pad": "x" * pad})),
            ("json-nested-5000-deep", nested + '{"a":' * 5000 + "1" + "}" * 5000 + "}"),
            ("lone-surrogate", lone),
            ("invalid-json", text("invalid-json")[:-1] + ',"value":'),
        ]

    def findings(self, plan, records, expected, without, hostile_sends) -> list[str]:
        sends: dict[str, set[tuple[Any, Any]]] = {}
        for number, record in enumerate(records):
            if record["kind"] != "event":
                continue
            cls = f9_class(record["user"])
            if cls is not None:
                sends.setdefault(cls, set()).add(
                    (record.get("route"), record.get("seq", number))
                )
        counts = {cls: len(seen) for cls, seen in sends.items()}
        counts.update(hostile_sends or {})
        found = []
        for cls in (*F9_VALID, *F9_REFUSED, *F9_HOSTILE):
            want = self.per_class * F9_SENDS.get(cls, 1)
            if counts.get(cls, 0) != want:
                found.append(
                    f"F9: class {cls!r}: the definition plants {want} sends; the run"
                    f" shows {counts.get(cls, 0)}"
                )
        counted = {
            user
            for entries in expected.counting.values()
            for user, (count, _) in entries.items()
            if count and f9_class(user) in F9_VALID
        }
        if not counted:
            found.append(
                "F9: no hostile but valid event counts in a variant: the valid classes"
                " did not reach the results"
            )
        return found


#: The ``timestamp`` F4 gives every event of the users it picks.
FUTURE = "2099-12-31T23:59:59Z"
#: A ``timestamp`` in this year or later is future-dated; no other F4
#: timestamp reaches it.
FUTURE_YEAR = 2099


def _skew(serial: int) -> str:
    """A skewed ``timestamp`` from the event's serial number, never the RNG:
    far past, the widest offsets, no zone, or up to 400 days either side of
    2026-01-01. Never a range edge (F9's) and never ``FUTURE_YEAR``."""
    form = (serial // 3) % 6
    if form == 0:
        return "1970-01-01T00:00:00Z"
    if form == 1:
        return "2026-01-01T00:00:00+14:00"
    if form == 2:
        return "2026-01-01T00:00:00-12:00"
    if form == 3:
        return "2026-01-01T00:00:00"
    days = (serial // 18) % 801 - 400
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_bodies(op: Op) -> list[Mapping[str, Any]]:
    if op.kind in ("track", "events"):
        return [op.body]
    if op.kind == "batch":
        return list(op.body["events"])
    return []


def _future(stamp: Any) -> bool:
    if not isinstance(stamp, str):
        return False
    try:
        return datetime.fromisoformat(stamp).year >= FUTURE_YEAR
    except ValueError:
        return False


def _counting_users(plan: Plan) -> dict[str, set[str]]:
    """Per arm, the users with a counting event when every op is answered as
    it expects."""
    records = expected_records(plan, DECLARED)
    experiment = contract.valid_events(records).experiments[
        plan.scenario.experiment_key
    ]
    found: dict[str, set[str]] = {arm: set() for arm in _arms(plan)}
    for (variant, _), entries in experiment.counting.items():
        found[variant].update(user for user, (count, _) in entries.items() if count)
    return found


def future_dated_users(plan: Plan) -> dict[str, int]:
    """F4's count: per arm, the users with a counting event whose every event
    is future-dated."""
    events: dict[str, list[Mapping[str, Any]]] = {}
    for op in plan.ops:
        for body in _event_bodies(op):
            events.setdefault(body["user_id"], []).append(body)
    return {
        arm: sum(
            1 for u in users if all(_future(e.get("timestamp")) for e in events[u])
        )
        for arm, users in _counting_users(plan).items()
    }


@dataclass(frozen=True)
class ClockSkew:
    """F4: client clocks that are wrong. In every arm,
    ``future_users_per_arm`` users with a counting event get ``FUTURE`` on
    every event they send, so a results reader that drops future-dated events
    loses them; every third other event that is accepted gets a skewed
    ``timestamp`` from its serial number. ``GET /api/v1/results`` reads no
    timestamp (R7), so no number may move: F4 has no runtime check of its own
    (its sensitivity is its planted defect, in a later part)."""

    future_users_per_arm: int = 3
    NAME = "F4"
    RULE = None

    def __post_init__(self) -> None:
        _positive(self.NAME, {"future_users_per_arm": self.future_users_per_arm})

    def apply(self, plan: Plan) -> Plan:
        order: dict[str, bool] = {}
        stamped: set[str] = set()
        for op in plan.ops:
            for body in _event_bodies(op):
                order.setdefault(body["user_id"], True)
                if "timestamp" in body:
                    stamped.add(body["user_id"])
        chosen: set[str] = set()
        for arm, users in _counting_users(plan).items():
            eligible = [u for u in order if u in users and u not in stamped]
            chosen.update(
                _spread(
                    eligible,
                    self.future_users_per_arm,
                    "F4",
                    f"users of {arm!r} with a counting event and no timestamp",
                )
            )
        serial = itertools.count()

        def stamp(body: Mapping[str, Any], skew: bool) -> Mapping[str, Any]:
            number = next(serial)
            if body["user_id"] in chosen:
                return {**body, "timestamp": FUTURE}
            if skew and "timestamp" not in body and number % 3 == 0:
                return {**body, "timestamp": _skew(number)}
            return body

        ops: list[Op] = []
        for op in plan.ops:
            skew = check_expect(op) == ACCEPTED if op.kind in DEFAULT_EXPECT else False
            if op.kind in ("track", "events"):
                op = replace(op, body=stamp(op.body, skew))
            elif op.kind == "batch":
                op = replace(
                    op, body={"events": [stamp(e, skew) for e in op.body["events"]]}
                )
            ops.append(op)
        return replace(plan, ops=tuple(ops))

    def findings(self, plan, records, expected, without, hostile_sends) -> list[str]:
        return []


#: The order faults are applied in, whatever order they are given.
ORDER = ("F7", "F5", "F6", "F3", "F1", "F9", "F4")
Fault = (
    Duplicates
    | LateEvents
    | EarlyEvents
    | VariantMismatch
    | ExposureLoss
    | Hostile
    | ClockSkew
)


def inject(plan: Plan, *faults: Fault) -> Plan:
    """*plan* with each fault planted, in ``ORDER``, and every op's
    ``expect`` set (its kind's default when a fault set none)."""
    names = [fault.NAME for fault in faults]
    if len(set(names)) != len(names):
        raise ValueError(f"a fault is given twice: {names}")
    for fault in sorted(faults, key=lambda f: ORDER.index(f.NAME)):
        plan = fault.apply(plan)
    ops = tuple(replace(op, expect=check_expect(op)) for op in plan.ops)
    return replace(plan, ops=ops)


# ---------------------------------------------------------------------------
# Sensitivity: did the fault reach the ledger as planted?
# ---------------------------------------------------------------------------
def _label(fault: Fault) -> str:
    return fault.NAME if fault.RULE is None else f"{fault.NAME} ({fault.RULE})"


def _planted(fault: Fault, want: int, got: int, what: str) -> list[str]:
    if got == want:
        return []
    return [
        f"{_label(fault)}: the definition plants {want} {what}; the ledger shows {got}"
        + (" (the fault touched nothing)" if got == 0 else "")
    ]


def projection(
    experiment: contract.ExperimentExpected, plan: Plan
) -> tuple[tuple[Any, ...], ...]:
    """What the gates compare, per variant and metric: the sample size, the
    converting users and the total of the per-user values; and the total
    events."""
    rows: list[tuple[Any, ...]] = [("total_events", experiment.total_events)]
    for variant in experiment.variants:
        rows.append((variant, "sample_size", experiment.sample_size(variant)))
        for metric in plan.scenario.metrics:
            per_user = experiment.per_user(variant, metric.event_name, metric.type)
            rows.append(
                (
                    variant,
                    metric.name,
                    experiment.converting_users(variant, metric.event_name),
                    math.fsum(per_user),
                )
            )
    return tuple(rows)


def sensitivity_findings(
    fault: Fault,
    plan: Plan,
    records: Sequence[Mapping[str, Any]],
    expected: contract.Expected,
    without: contract.Expected | None,
    *,
    hostile_sends: Mapping[str, int] | None = None,
) -> list[str]:
    """Why *fault* did not reach the ledger as its definition plants it; empty
    when it did. *expected* is ``valid_events(records)``; *without* is the
    same with the fault's rule left out (None for a fault with no rule).
    *hostile_sends* counts the sends of each ``F9_HOSTILE`` class the driver
    made and had answered."""
    key = plan.scenario.experiment_key
    mine = expected.experiments[key]
    theirs = None if without is None else without.experiments[key]
    if (fault.RULE is None) != (theirs is None):
        raise ValueError(
            f"{fault.NAME}: without= is for a fault with a rule, and only one"
        )
    found = fault.findings(plan, records, mine, theirs, hostile_sends)
    if theirs is not None and projection(mine, plan) == projection(theirs, plan):
        found.append(
            f"{fault.NAME} ({fault.RULE}) moves no number the gates compare: without"
            f" {fault.RULE} the reference has the same sample sizes, converting users,"
            " value totals and total events"
        )
    return found


def hostile_counts(plan: Plan) -> dict[str, int]:
    """The hostile ops of *plan* per class: what a driver that sent them all
    and had each answered reports as ``hostile_sends``."""
    return dict(Counter(op.body["class"] for op in plan.ops if op.kind == "hostile"))
