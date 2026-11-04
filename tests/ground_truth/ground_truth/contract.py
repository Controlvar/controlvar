"""The written event contract, applied to a ledger: ``valid_events(records)``.

``docs/guides/how-events-count.md`` says which of the events an application
sends count in an experiment's results; ``docs/api/endpoints.md`` ("Tracking
Endpoints") says what each tracking route stores. ``valid_events`` applies
those rules, R0 to R8, to the ledger's records in the order the server
received them, and returns the exact numbers ``GET /api/v1/results/{id}`` must
show: per experiment and variant, the assigned users and, per event name, each
user's count of counting events and the total of their values; and per
experiment the stored events, counted or not.

``valid_events(records, without=...)`` applies every rule but the ones named
(``WITHOUT``): the reference a fault scenario's sensitivity check compares
with, to show the fault moved what that rule decides. Nothing else may pass
it (``backend/tests/unit/ground_truth/test_sensitivity.py`` pins that only
``sensitivity()`` does): a gate compared with a reference missing a rule
would pass a server missing it too.

Each rule below is stated once, in the function that applies it, and its
docstring quotes the sentence of the docs that makes it the contract, as a
paragraph starting ``Contract (<page>):``. A unit test holds every quoted
sentence to its page.

When a rule needs a field the stream lacks (the ledger's optional fields among
them), ``valid_events`` raises ``ContractRefused`` naming the rule and the
field, and never guesses.

The records are taken to be in the order the server received them: a driver
that sends concurrently must record in that order or not at all.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import hashing

#: The ``event_type`` values the server writes for an exposure (a user saw a
#: variant); no metric matches them.
EXPOSURE_TYPES = ("exposure", "experiment_exposure")
#: The largest value, in size, that adds to a metric.
VALUE_BOUND = 1e12
ACTIVE = "active"
#: An experiment's status after a lifecycle action the server accepted.
STATUS_AFTER = {
    "create": "draft",
    "start": ACTIVE,
    "resume": ACTIVE,
    "pause": "paused",
    "complete": "completed",
    "archive": "archived",
}
#: How each metric type turns a user's counting events into their value.
METRIC_TYPES = ("conversion", "revenue", "count", "duration", "custom")
#: The rules ``valid_events(records, without=...)`` can leave out, and what is
#: done instead: ``R2-order``, an event received before its user's assignment
#: is tagged with that assignment; ``R2-status``, an event received while the
#: experiment is not ACTIVE is tagged as if it were; ``R4``, every repeat is
#: stored; ``R5``, every value adds, whatever its size; ``R8``, an ``/events``
#: event counts for the variant it names, whatever the user's assignment.
WITHOUT = ("R2-order", "R2-status", "R4", "R5", "R8")
#: The route of an event that names its experiment and variant by id.
BY_ID = "/events"


class ContractRefused(Exception):
    """A rule needs something the ledger does not say; the message names both."""


@dataclass(frozen=True)
class Mismatch:
    """An assignment answer that disagrees with O-ASSIGN or with an earlier one."""

    record: int
    user: str
    answered: str
    expected: str


@dataclass(frozen=True)
class ExperimentExpected:
    """What ``GET /api/v1/results/{id}`` must show for one experiment."""

    key: str
    #: In creation order; the first is the control.
    variants: tuple[str, ...]
    allocations: tuple[float, ...]
    #: Per variant, its assigned users in the order they were assigned.
    users: Mapping[str, tuple[str, ...]]
    #: Every stored event of the experiment: counted, untagged and exposures.
    total_events: int
    #: Stored events that count in no variant (R2, R8).
    untagged_events: int
    #: Exposure events stored by per-user ``/assign`` answers (R1).
    exposures: int
    #: Assignment answers that are not O-ASSIGN's variant (G0's evidence).
    variant_mismatches: tuple[Mismatch, ...]
    #: Later answers naming another variant than the user's first.
    variant_conflicts: tuple[Mismatch, ...]
    #: (variant, event name) -> user -> (counting events, their counting values).
    counting: Mapping[tuple[str, str], Mapping[str, tuple[int, tuple[float, ...]]]]
    #: Stored ``/events`` events (R8), counted in a variant or not.
    by_id_events: int = 0

    def sample_size(self, variant: str) -> int:
        return len(self.users[variant])

    @property
    def total_users(self) -> int:
        return sum(len(users) for users in self.users.values())

    def counts(self, variant: str, event_name: str) -> list[int]:
        """Per assigned user, in ``users`` order: their counting events."""
        return _r6_per_user(self, variant, event_name, lambda c, _: c)

    def value_totals(self, variant: str, event_name: str) -> list[float]:
        """Per assigned user: the exact total of their counting values."""
        return _r6_per_user(self, variant, event_name, lambda _, v: math.fsum(v))

    def converting_users(self, variant: str, event_name: str) -> int:
        """Assigned users with at least one counting event (``conversions``)."""
        entries = self.counting.get((variant, event_name), {})
        return sum(1 for count, _ in entries.values() if count > 0)

    def per_user(self, variant: str, event_name: str, metric_type: str) -> list[float]:
        """The per-user values a metric of *metric_type* analyses.

        A conversion metric: 1 for a user who converted, else 0. A count
        metric: their counting events. A revenue, duration or custom metric:
        the total of their counting values.
        """
        if metric_type not in METRIC_TYPES:
            raise ContractRefused(
                f"metric type {metric_type!r} is not one of {METRIC_TYPES}"
            )
        if metric_type == "conversion":
            return _r6_per_user(
                self, variant, event_name, lambda c, _: 1.0 if c else 0.0
            )
        if metric_type == "count":
            return [float(count) for count in self.counts(variant, event_name)]
        return self.value_totals(variant, event_name)

    def mean(self, variant: str, event_name: str, metric_type: str) -> float:
        """The metric's ``mean`` for *variant*: exact when values are quantised."""
        values = self.per_user(variant, event_name, metric_type)
        return math.fsum(values) / len(values)

    def total_converting_users(self, event_names: Iterable[str]) -> int:
        """Assigned users with a counting event of any of *event_names*, once each."""
        names = set(event_names)
        users = {
            user
            for (_, name), entries in self.counting.items()
            if name in names
            for user, (count, _) in entries.items()
            if count > 0
        }
        return len(users)


@dataclass(frozen=True)
class Expected:
    experiments: Mapping[str, ExperimentExpected]
    #: Records answered 429: nothing was stored for them.
    rate_limited: int
    #: Event records the server did not accept (R0), the 429s among them.
    rejected_events: int
    #: The ledger lines (the first record is line 1) of the accepted events R4
    #: does not store: what a server that stores an event once answers
    #: ``duplicate: true`` for on ``/track`` and ``/events``, and counts in a
    #: ``/batch``'s ``duplicate_count``. Empty without R4.
    repeats: frozenset[int] = frozenset()


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
def _ok(status: int) -> bool:
    return 200 <= status < 300


def _r0_accepted(
    record: Mapping[str, Any], number: int, batches: Mapping[int, Mapping[str, Any]]
) -> bool:
    """R0 Accepted. An event is stored only when its answer is a 2xx; an event
    sent in a ``/batch`` only when the batch's answer is a 2xx and its position
    is not among the answer's ``errors``. A schema error refuses the whole batch
    (a 422), so none of its events is stored. An ``/events`` event, like a
    ``/track`` one, only when its answer is a 2xx.

    Contract (docs/api/endpoints.md): An entry with no key is stored as history,
    not refused; an entry whose key is not found is reported in `errors`.

    Contract (docs/api/endpoints.md): 422 above 100 events, or one entry invalid
    (its `event_id` among them), and none is stored

    Contract (docs/api/endpoints.md): 404 an id names no stored row, and nothing
    is stored

    Contract (docs/guides/how-events-count.md): the server stores nothing for a
    `429`.
    """
    route = record.get("route")
    if route is None:
        raise ContractRefused(
            f"R0 needs `route` for an event (record {number}): whether it was"
            " stored depends on the route it was sent to"
        )
    if route in ("/track", BY_ID):
        return _ok(record["status"])
    if route == "/batch":
        if "batch_index" not in record:
            raise ContractRefused(
                f"R0 needs `batch_index` for a /batch event (record {number})"
            )
        if "seq" not in record:
            raise ContractRefused(
                f"R0 needs `seq` for a /batch event (record {number})"
            )
        batch = batches.get(record["seq"])
        if batch is None:
            raise ContractRefused(
                f"R0 needs the answer to the /batch of seq {record['seq']} before"
                f" its event (record {number})"
            )
        return _ok(batch["status"]) and record["batch_index"] not in (
            batch["errors"] or ()
        )
    raise ContractRefused(
        f"R0 has no rule for an event sent to {route} (record {number})"
    )


def _r1_assign(state: _State, record: Mapping[str, Any], number: int) -> None:
    """R1 Assignment. A user's variant is their first ``assigned: true`` answer,
    and every answer must be O-ASSIGN's variant (``hashing``, from the
    allocation and order in ``start``). Each such answer of a per-user
    ``/assign`` stores one exposure event, sticky calls included;
    ``/assign/batch`` stores none. An answer with ``assigned: false`` stores
    nothing.

    Contract (docs/api/endpoints.md): Every call for an enrolled user records a
    view event (the user saw the experiment): the first call and every later
    sticky call alike.

    Contract (docs/api/endpoints.md): Only assignments are recorded: the user's
    first `POST /api/v1/tracking/assign` (from an SDK, when they actually see the
    experiment) records the view.

    Contract (docs/api/endpoints.md): A user who already has an assignment keeps
    it.
    """
    if not _ok(record["status"]) or record["assigned"] is not True:
        return
    user, variant = record["user"], record["variant"]
    if variant not in state.users:
        raise ContractRefused(
            f"R1: record {number} answers variant {variant!r}, which start does not"
            f" list for experiment {state.key!r}"
        )
    expected = hashing.variant_for(user, state.key, state.allocations)
    if variant != expected:
        state.mismatches.append(Mismatch(number, user, variant, expected))
    current = state.assigned.get(user)
    if current is None:
        state.assigned[user] = variant
        state.users[variant].append(user)
    elif current != variant:
        state.conflicts.append(Mismatch(number, user, variant, current))
    route = record.get("route")
    if route == "/assign":
        state.exposures += 1
        state.total_events += 1
    elif route != "/assign/batch":
        raise ContractRefused(
            f"R1 needs `route` for an assign record (record {number}): a per-user"
            " /assign stores an exposure and /assign/batch does not"
        )


def _r2_status(
    states: Mapping[str, _State], record: Mapping[str, Any], number: int
) -> None:
    """R2, the experiment's status: what a lifecycle action the server accepted
    makes it (``STATUS_AFTER``).

    Contract (docs/guides/how-events-count.md): An event received while the
    experiment is a draft, paused, completed or archived is stored with no
    variant, and counts in no variant's results, even after the experiment is
    started again.
    """
    if not _ok(record["status"]) or record["exp"] is None:
        return
    state = _state(states, record["exp"], "R2", number)
    if record["action"] not in STATUS_AFTER:
        raise ContractRefused(
            f"R2 has no rule for action {record['action']!r} (record {number})"
        )
    state.status = STATUS_AFTER[record["action"]]


def _r2_variant_at_receipt(
    state: _State, record: Mapping[str, Any], number: int
) -> str | None:
    """R2 Attribution on arrival. A stored event is tagged with its user's
    variant if, when the server receives it, the experiment is ACTIVE and the
    user is already assigned. Otherwise it is stored untagged: it counts in the
    experiment's total events and in no variant.

    Contract (docs/guides/how-events-count.md): An event sent to `POST
    /api/v1/tracking/track` or `POST /api/v1/tracking/batch` counts for the
    variant the user is assigned to when the server receives it.

    Contract (docs/guides/how-events-count.md): An event received by `/track`
    or `/batch` before the user's assignment is stored with no variant, and counts in no variant's
    results, even after the user is assigned.

    Contract (docs/guides/how-events-count.md): The results summary's Total
    Events counts every stored event of the experiment, the ones that count in
    no variant included.
    """
    if state.status is None:
        raise ContractRefused(
            f"R2 needs the status of experiment {state.key!r} when record {number}"
            " was received: give `status` in start's experiment, or record the"
            " `action` that set it"
        )
    if state.status != ACTIVE:
        return None
    return state.assigned.get(record["user"])


def _event_name(record: Mapping[str, Any]) -> str:
    name = record["event_name"]
    return record["event_type"] if name is None else name


def _r3_refuse_empty_name(record: Mapping[str, Any], number: int) -> None:
    """R3, an empty ``event_name``: refused, not modelled. The server stores
    ``event_name or event_type``, so an empty name becomes the event type, and
    this module follows the docs' ``event_name`` defaulting to ``event_type``
    only for a missing or null name. A stream that carries ``""`` says
    something the contract does not model, so it is refused by name."""
    if record.get("event_name") == "":
        raise ContractRefused(
            f'R3: record {number} carries `event_name` ""; the server stores'
            " `event_name or event_type`, so an empty name is not modelled"
        )


def _r3_metric_name(record: Mapping[str, Any]) -> str | None:
    """R3 Metric match. A metric counts the events of its ``event_name``, which
    defaults to the event's ``event_type``; an exposure counts for no metric.

    Contract (docs/api/endpoints.md): `event_type` is free text (SDKs send the
    event name); `event_name` defaults to `event_type`. Metrics count events by
    `event_name`

    Contract (docs/sdk-guide.md): Conversions are matched to experiment metrics
    by `event_name` (exposures excluded)
    """
    if record["event_type"] in EXPOSURE_TYPES:
        return None
    return _event_name(record)


def _r4_repeat(
    record: Mapping[str, Any],
    number: int,
    dedup: bool | None,
    stored: set[tuple[Any, ...]],
) -> bool:
    """R4 Dedup, only when the run declares the server has it (start's
    ``declared["event_dedup"]``). A later accepted event with the same user,
    experiment, flag, event name and ``event_id`` is not stored; the first one
    sent is kept, whatever happened to it. No ``event_id``, null and ``""`` are
    none.

    Contract (docs/guides/how-events-count.md): The server does not store an
    event when it already has one with the same `event_id`, user, experiment,
    flag and event name.

    Contract (docs/guides/how-events-count.md): Leaving it out, `null` and `""`
    all mean no `event_id`.

    Contract (docs/guides/how-events-count.md): An event first received while
    its experiment was paused is not stored again when it is sent again after
    the experiment is started, and stays out of every variant's results.

    Contract (docs/api/endpoints.md): It takes `event_id` as `/track` does, and
    the same event sent through either route is stored once.
    """
    if dedup is False:
        return False
    if "event_id" not in record:
        raise ContractRefused(
            f"R4 needs `event_id` (null when none was sent) for an event (record"
            f" {number})"
            + ("" if dedup else ", or `event_dedup` in start's `declared`")
        )
    event_id = record["event_id"]
    if event_id is None or event_id == "":
        return False
    if dedup is None:
        raise ContractRefused(
            f"R4 needs `event_dedup` in start's `declared` to count an event sent"
            f" with an `event_id` (record {number})"
        )
    key = (record["user"], record["exp"], record["flag"], _event_name(record), event_id)
    if key in stored:
        return True
    stored.add(key)
    return False


def _r5_value(record: Mapping[str, Any]) -> float | None:
    """R5 Values. A finite value of at most 10^12 in size adds to a revenue,
    duration or custom metric; any other adds nothing, and its user still
    converts. A count metric counts every counting event, whatever its value.

    Contract (docs/guides/how-events-count.md): Its user counts as converting;
    the value adds nothing

    Contract (docs/getting-started/concepts.md): for a count metric each user's
    value is the number of the metric's events they sent, whatever their `value`
    """
    value = record["value"]
    if value is None or not math.isfinite(value) or abs(value) > VALUE_BOUND:
        return None
    return float(value)


def _r6_per_user(
    expected: ExperimentExpected, variant: str, event_name: str, pick: Any
) -> list[Any]:
    """R6 Unit. Each assigned user is one unit, scored 0 when they have no
    counting event.

    Contract (docs/getting-started/concepts.md): a user with none counts as 0.
    """
    entries = expected.counting.get((variant, event_name), {})
    return [pick(*entries.get(user, (0, ()))) for user in expected.users[variant]]


def _r7_no_timestamp(record: Mapping[str, Any], number: int) -> None:
    """R7 Time. No number of ``GET /api/v1/results/{id}`` reads an event's
    ``timestamp``, so nothing here reads one, and the ledger does not record it.

    Contract (docs/guides/how-events-count.md): `GET
    /api/v1/results/{experiment_id}` does not read it.
    """
    if "timestamp" in record:
        raise ContractRefused(
            f"R7: record {number} carries a `timestamp`, which no rule reads"
        )


def _r8_named_variant(state: _State, record: Mapping[str, Any], number: int) -> None:
    """R8, the variant an ``/events`` event names: one of the experiment's, or
    none. A record that does not say (no ``variant``) is refused, never
    guessed."""
    if "variant" not in record:
        raise ContractRefused(
            f"R8 needs `variant` for an /events event (record {number}): the variant"
            " it names decides where it counts"
        )
    variant = record["variant"]
    if variant is not None and variant not in state.users:
        raise ContractRefused(
            f"R8: record {number} names variant {variant!r}, which start does not"
            f" list for experiment {state.key!r}"
        )


def _r8_attribution_by_id(state: _State, record: Mapping[str, Any]) -> str | None:
    """R8 Attribution by id. An accepted ``/events`` event is stored with the
    variant it names, whatever the experiment's status, and counts in the
    experiment's total events. It counts for that variant only when the user's
    first ``assigned: true`` answer (R1), anywhere in the ledger, is that
    variant: decided when the results are read, after every record, not when
    the event arrives. Otherwise it counts in no variant. (Measured against the
    server: an ``/events`` event sent before the user is assigned counts once
    the user is assigned to the variant it names.)

    Contract (docs/guides/how-events-count.md): `POST /api/v1/tracking/events`
    stores the `variant_id` it is sent, whatever the experiment's status; the
    event counts for that variant once the user is assigned to it, whether the
    assignment came before or after the event, and in no variant while the user
    is assigned to another or to none.

    Contract (docs/api/endpoints.md): It stores the `variant_id` it is given,
    whatever the experiment's status.
    """
    variant = record["variant"]
    if variant is not None and state.assigned.get(record["user"]) == variant:
        return variant
    return None


# ---------------------------------------------------------------------------
# Applying them
# ---------------------------------------------------------------------------
class _State:
    def __init__(self, entry: Mapping[str, Any]) -> None:
        self.key: str = entry["exp"]
        self.allocations = [(v["name"], v["allocation"]) for v in entry["variants"]]
        self.status: str | None = entry.get("status")
        self.assigned: dict[str, str] = {}
        self.users: dict[str, list[str]] = {name: [] for name, _ in self.allocations}
        self.counting: dict[tuple[str, str], dict[str, list[Any]]] = {}
        self.total_events = 0
        self.untagged = 0
        self.exposures = 0
        self.by_id = 0
        self.mismatches: list[Mismatch] = []
        self.conflicts: list[Mismatch] = []

    def freeze(self) -> ExperimentExpected:
        return ExperimentExpected(
            key=self.key,
            variants=tuple(name for name, _ in self.allocations),
            allocations=tuple(float(a) for _, a in self.allocations),
            users={v: tuple(u) for v, u in self.users.items()},
            total_events=self.total_events,
            untagged_events=self.untagged,
            exposures=self.exposures,
            variant_mismatches=tuple(self.mismatches),
            variant_conflicts=tuple(self.conflicts),
            counting={
                key: {user: (c, tuple(v)) for user, (c, v) in entries.items()}
                for key, entries in self.counting.items()
            },
            by_id_events=self.by_id,
        )


def _state(states: Mapping[str, _State], exp: str, rule: str, number: int) -> _State:
    if exp not in states:
        raise ContractRefused(
            f"{rule}: record {number} names experiment {exp!r}, which start does not"
            " list"
        )
    return states[exp]


#: An event whose variant is decided after every record (R8, or R2 without
#: R2-order): the experiment, the record and its number.
_Later = tuple[_State, Mapping[str, Any], int]


def _count(
    state: _State, variant: str, record: Mapping[str, Any], without: frozenset[str]
) -> None:
    """A stored event that counts for *variant*: R3 picks the metric, R5 the
    value (every value, without R5)."""
    name = _r3_metric_name(record)
    if name is None:
        return
    entry = state.counting.setdefault((variant, name), {}).setdefault(
        record["user"], [0, []]
    )
    entry[0] += 1
    if "R5" in without:
        value = None if record["value"] is None else float(record["value"])
    else:
        value = _r5_value(record)
    if value is not None:
        entry[1].append(value)


def _on_arrival(
    state: _State, record: Mapping[str, Any], number: int, without: frozenset[str]
) -> str | None | bool:
    """The variant R2 tags an event with when it arrives, None for none, or
    True when R2-order is left out and the user's assignment decides it later."""
    if "R2-status" in without:
        variant, running = state.assigned.get(record["user"]), True
    else:
        variant = _r2_variant_at_receipt(state, record, number)
        running = state.status == ACTIVE
    if variant is None and running and "R2-order" in without:
        return True
    return variant


def _event(
    states: Mapping[str, _State],
    batches: Mapping[int, Mapping[str, Any]],
    record: Mapping[str, Any],
    number: int,
    dedup: bool | None,
    stored: set[tuple[Any, ...]],
    without: frozenset[str],
    later: list[_Later],
    repeats: set[int],
) -> bool:
    """Apply R0 to R5, and R8 to an ``/events`` event, to one event; whether
    the server accepted it. An event decided after every record goes to
    *later*; one R4 does not store, to *repeats*."""
    if not _r0_accepted(record, number, batches):
        return False
    if record["exp"] is None:
        return True  # history: stored, and in no experiment
    state = _state(states, record["exp"], "R2", number)
    by_id = record.get("route") == BY_ID
    if by_id:
        _r8_named_variant(state, record, number)
    if "R4" not in without and _r4_repeat(record, number, dedup, stored):
        repeats.add(number)
        return True
    state.total_events += 1
    if by_id:
        state.by_id += 1
        later.append((state, record, number))
        return True
    variant = _on_arrival(state, record, number, without)
    if variant is True:
        later.append((state, record, number))
    elif variant is None:
        state.untagged += 1
    else:
        _count(state, variant, record, without)
    return True


def _decide_later(later: Sequence[_Later], without: frozenset[str]) -> None:
    """The events whose variant the whole ledger decides, now it is read."""
    for state, record, _ in later:
        if record.get("route") == BY_ID:
            if "R8" in without:
                variant = record["variant"]
            else:
                variant = _r8_attribution_by_id(state, record)
        else:  # R2 without R2-order: the user's first assignment, if any
            variant = state.assigned.get(record["user"])
        if variant is None:
            state.untagged += 1
        else:
            _count(state, variant, record, without)


def _rules_left_out(without: Iterable[str]) -> frozenset[str]:
    names = frozenset(without)
    unknown = sorted(names - set(WITHOUT))
    if unknown:
        raise ContractRefused(
            f"without= names {unknown}; it can leave out only {list(WITHOUT)}"
        )
    return names


def valid_events(
    records: Sequence[Mapping[str, Any]], *, without: Iterable[str] = ()
) -> Expected:
    """The exact expectation for the run whose ledger is *records*, as
    ``ledger.read`` returns them. ``flag`` and ``alias`` records store no
    experiment event and are passed over.

    *without* names rules (``WITHOUT``) to leave out, for a fault scenario's
    sensitivity check only; every gate uses the default, every rule."""
    if not records or records[0].get("kind") != "start":
        raise ContractRefused("valid_events needs a ledger that starts with start")
    left_out = _rules_left_out(without)
    start = records[0]
    states = {entry["exp"]: _State(entry) for entry in start["experiments"]}
    dedup = start["declared"].get("event_dedup")
    if dedup is not None and not isinstance(dedup, bool):
        raise ContractRefused(
            "R4: start's `declared.event_dedup` must be true or false"
        )
    batches: dict[int, Mapping[str, Any]] = {}
    stored: set[tuple[Any, ...]] = set()
    later: list[_Later] = []
    repeats: set[int] = set()
    rate_limited = rejected = 0
    for number, record in enumerate(records[1:], 2):
        _r7_no_timestamp(record, number)
        kind = record["kind"]
        if kind == "event":
            _r3_refuse_empty_name(record, number)
        if record.get("status") == 429:
            rate_limited += 1
        if kind == "action":
            _r2_status(states, record, number)
        elif kind == "assign":
            if _ok(record["status"]) and record["assigned"] is True:
                _r1_assign(_state(states, record["exp"], "R1", number), record, number)
        elif kind == "batch":
            if "seq" in record:
                batches[record["seq"]] = record
        elif kind == "event":
            accepted = _event(
                states, batches, record, number, dedup, stored, left_out, later, repeats
            )
            if not accepted:
                rejected += 1
        elif kind == "end":
            break
    _decide_later(later, left_out)
    return Expected(
        experiments={key: state.freeze() for key, state in states.items()},
        rate_limited=rate_limited,
        rejected_events=rejected,
        repeats=frozenset(repeats),
    )
