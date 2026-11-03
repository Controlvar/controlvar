"""A seeded plan of realistic traffic: ``simulate(seed, scenario) -> Plan``.

A plan is an ordered list of operations a driver sends through the public API:
lifecycle steps (create, then start the experiment), ``assign_batch``
(``POST /api/v1/tracking/assign/batch``, up to 1,000 users), ``assign``
(``POST /api/v1/tracking/assign``, one user), ``track``
(``POST /api/v1/tracking/track``, one event) and ``batch``
(``POST /api/v1/tracking/batch``, up to 100 events).

* **Randomness** comes only from ``random.Random(seed).random()``; a normal
  draw is Box-Muller over two of those, a lognormal value ``exp`` of one. The
  same seed and scenario give the same plan, operation for operation, in any
  process (``Plan.digest`` pins it).
* **Values are quantised to multiples of 2^-6**, so every sum of them is exact
  in binary64 and a mean can be compared with ``==``.
* **``event_id``** is sent on about 90% of events (``event_id_share``) and left
  out of the rest. Each id is used once, so a server that drops repeats stores
  every event of the plan, and so does one that ignores the field.
* **Who goes where:** about 10% of users (``single_share``) are assigned one at
  a time and send their events one at a time; a quarter of those
  (``sticky_share``) come back for a second, sticky assignment. Everyone else
  is assigned in batches and sends their events in batches. Every event is
  sent after every assignment, so each one counts for its user's variant.

The planted truth (each arm's conversion, repeat and value distribution) only
sets a scenario's direction. What the results must show is computed from the
ledger of what the server answered (``contract``), never from this plan.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import hashing
from .ledger import ASSIGN, ASSIGN_BATCH, BATCH, TRACK

#: The API's own limits per request (docs/api/endpoints.md, "Tracking Endpoints").
MAX_ASSIGN_BATCH = 1000
MAX_EVENT_BATCH = 100

METRIC_TYPES = ("conversion", "revenue", "count", "duration", "custom")
CORRECTION_METHODS = ("none", "bonferroni", "benjamini_hochberg")

#: Values are multiples of 1 / QUANTUM.
QUANTUM = 64


@dataclass(frozen=True)
class Values:
    """A lognormal value per event: ``exp(mu + sigma * z)``, quantised."""

    mu: float
    sigma: float


@dataclass(frozen=True)
class Arm:
    """One variant, in creation order; the first arm of a scenario is the control.

    ``conversion`` is the share of the arm's users who send the event at least
    once; after each event a user sends another with probability ``repeat``.
    """

    name: str
    allocation: int
    conversion: float
    repeat: float = 0.0
    values: Values | None = None


@dataclass(frozen=True)
class Metric:
    name: str
    event_name: str
    type: str


@dataclass(frozen=True)
class Scenario:
    name: str
    experiment_key: str
    arms: tuple[Arm, ...]
    users: int
    #: The event every converting user sends; each metric reads it by name.
    event_name: str
    metrics: tuple[Metric, ...]
    correction_method: str = "benjamini_hochberg"
    confidence_level: float = 0.95
    single_share: float = 0.10
    sticky_share: float = 0.25
    event_id_share: float = 0.90
    assign_batch_size: int = MAX_ASSIGN_BATCH
    event_batch_size: int = MAX_EVENT_BATCH
    max_events_per_user: int = 50


@dataclass(frozen=True)
class Op:
    """One request. ``kind`` is ``lifecycle``, ``assign``, ``assign_batch``,
    ``track`` or ``batch``; ``route`` is the tracking path (None for a
    lifecycle step); ``body`` is the JSON body as sent, or for a lifecycle step
    ``{"action": "create" | "start", "experiment_key": ...}``.

    ``simulate`` plans only those. ``faults`` adds the lifecycle actions
    ``pause``, ``resume`` and ``complete``, the kind ``events`` (``POST
    /api/v1/tracking/events``, the variant named by arm) and the kind
    ``hostile`` (raw bytes no ledger records), and sets ``expect``, the answer
    the op must get (``faults.EXPECTS``). ``expect`` is not part of
    ``Plan.digest``: a plan's traffic is its kinds, routes and bodies."""

    kind: str
    route: str | None
    body: Mapping[str, Any]
    expect: str | None = None


@dataclass(frozen=True)
class Plan:
    seed: int
    scenario: Scenario
    ops: tuple[Op, ...]

    def allocations(self) -> list[tuple[str, int]]:
        return [(arm.name, arm.allocation) for arm in self.scenario.arms]

    def start(self, declared: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The fields of the ledger's ``start`` record for this plan.

        *declared* is the state the run declares about the server, for example
        ``{"event_dedup": True}`` when it stores an event sent again with the
        same ``event_id`` once (``contract`` R4).
        """
        scenario = self.scenario
        return {
            "source": f"ground_truth.generator:{scenario.name}",
            "seed": self.seed,
            "experiments": [
                {
                    "exp": scenario.experiment_key,
                    "variants": [
                        {"name": name, "allocation": allocation}
                        for name, allocation in self.allocations()
                    ],
                    "metrics": [
                        {"name": m.name, "event_name": m.event_name, "type": m.type}
                        for m in scenario.metrics
                    ],
                    "correction_method": scenario.correction_method,
                    "confidence_level": scenario.confidence_level,
                }
            ],
            "flags": [],
            # A list of entries, not a dict keyed by arm name: an arm's name is
            # data, and a key named like a header would be refused as one.
            "planted": [
                {
                    "name": arm.name,
                    "conversion": arm.conversion,
                    "repeat": arm.repeat,
                    "values": None
                    if arm.values is None
                    else {"mu": arm.values.mu, "sigma": arm.values.sigma},
                }
                for arm in scenario.arms
            ],
            "declared": dict(declared or {}),
        }

    def digest(self) -> str:
        """SHA-256 of the operations as JSON: equal plans, equal digests."""
        text = json.dumps(
            [[op.kind, op.route, op.body] for op in self.ops],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode()).hexdigest()


def _check(scenario: Scenario) -> None:
    def share(name: str, value: float) -> None:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0, 1], not {value!r}")

    if len(scenario.arms) < 2:
        raise ValueError("a scenario has at least two arms")
    names = [arm.name for arm in scenario.arms]
    if len(set(names)) != len(names):
        raise ValueError(f"arm names repeat: {names}")
    if any(arm.allocation <= 0 for arm in scenario.arms):
        raise ValueError("every arm needs a positive allocation")
    if sum(arm.allocation for arm in scenario.arms) != 100:
        raise ValueError("the allocations must add up to 100")
    for arm in scenario.arms:
        share(f"{arm.name}.conversion", arm.conversion)
        share(f"{arm.name}.repeat", arm.repeat)
    if scenario.users < 1:
        raise ValueError("a scenario has at least one user")
    if not scenario.metrics:
        raise ValueError("a scenario has at least one metric")
    for metric in scenario.metrics:
        if metric.type not in METRIC_TYPES:
            raise ValueError(
                f"metric type {metric.type!r} is not one of {METRIC_TYPES}"
            )
    if scenario.correction_method not in CORRECTION_METHODS:
        raise ValueError(f"correction method {scenario.correction_method!r} is unknown")
    if not 0.8 <= scenario.confidence_level <= 0.99:
        raise ValueError("the confidence level must be in [0.80, 0.99]")
    share("single_share", scenario.single_share)
    share("sticky_share", scenario.sticky_share)
    share("event_id_share", scenario.event_id_share)
    if not 1 <= scenario.assign_batch_size <= MAX_ASSIGN_BATCH:
        raise ValueError(f"assign_batch_size must be 1 to {MAX_ASSIGN_BATCH}")
    if not 1 <= scenario.event_batch_size <= MAX_EVENT_BATCH:
        raise ValueError(f"event_batch_size must be 1 to {MAX_EVENT_BATCH}")
    if scenario.max_events_per_user < 1:
        raise ValueError("max_events_per_user must be at least 1")


def _normal(draw: Callable[[], float]) -> float:
    """A standard normal draw: Box-Muller over two uniform draws."""
    radius = math.sqrt(-2.0 * math.log(1.0 - draw()))
    return radius * math.cos(2.0 * math.pi * draw())


def quantise(value: float) -> float:
    """*value* rounded to the nearest multiple of 2^-6."""
    return round(value * QUANTUM) / QUANTUM


def _shuffle(items: list[Any], draw: Callable[[], float]) -> None:
    """Fisher-Yates in place, drawing only from *draw*."""
    for i in range(len(items) - 1, 0, -1):
        j = int(draw() * (i + 1))
        items[i], items[j] = items[j], items[i]


def _chunks(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def simulate(seed: int, scenario: Scenario) -> Plan:
    """The plan for *scenario* under *seed*; the same inputs, the same plan."""
    _check(scenario)
    draw = random.Random(seed).random
    key = scenario.experiment_key
    allocations = [(arm.name, arm.allocation) for arm in scenario.arms]
    arms = {arm.name: arm for arm in scenario.arms}

    users = [f"gt-{seed}-{number:06d}" for number in range(scenario.users)]
    _shuffle(users, draw)

    singles: list[str] = []
    returning: list[str] = []
    batched: list[str] = []
    single_events: list[dict[str, Any]] = []
    batched_events: list[dict[str, Any]] = []
    serial = 0
    for user in users:
        arm = arms[hashing.variant_for(user, key, allocations)]
        single = draw() < scenario.single_share
        if single:
            singles.append(user)
            if draw() < scenario.sticky_share:
                returning.append(user)
        else:
            batched.append(user)
        if draw() >= arm.conversion:
            continue
        count = 1
        while count < scenario.max_events_per_user and draw() < arm.repeat:
            count += 1
        for _ in range(count):
            serial += 1
            event: dict[str, Any] = {
                "event_type": scenario.event_name,
                "user_id": user,
                "experiment_key": key,
            }
            if arm.values is not None:
                z = _normal(draw)
                event["value"] = quantise(
                    math.exp(arm.values.mu + arm.values.sigma * z)
                )
            if draw() < scenario.event_id_share:
                event["event_id"] = f"gt-{seed}-e{serial:07d}"
            (single_events if single else batched_events).append(event)
    _shuffle(batched_events, draw)

    ops = [
        Op("lifecycle", None, {"action": "create", "experiment_key": key}),
        Op("lifecycle", None, {"action": "start", "experiment_key": key}),
    ]
    for chunk in _chunks(batched, scenario.assign_batch_size):
        body = {"experiment_key": key, "users": [{"user_id": u} for u in chunk]}
        ops.append(Op("assign_batch", ASSIGN_BATCH, body))
    for user in [*singles, *returning]:
        ops.append(Op("assign", ASSIGN, {"experiment_key": key, "user_id": user}))
    for event in single_events:
        ops.append(Op("track", TRACK, event))
    for chunk in _chunks(batched_events, scenario.event_batch_size):
        ops.append(Op("batch", BATCH, {"events": chunk}))
    return Plan(seed=seed, scenario=scenario, ops=tuple(ops))
