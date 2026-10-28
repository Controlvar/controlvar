"""A known population of users, assigned and converted through the tracking API.

A ``traffic`` step sends an experiment the users a journey chooses, as an
application using an SDK would: each user is assigned with
``POST /api/v1/tracking/assign`` and, for the users that convert, the metric's
event is sent with ``POST /api/v1/tracking/track``, both with an API key
(``X-API-Key``). The journey says how many users each variant gets and how many
of them convert, so the numbers the results must show are known before the run.

Which variant a user lands in is not left to chance. Assignment is a
deterministic hash of the user and the experiment into a bucket in [0, 100),
the variants taking the buckets in their order, each as many as its traffic
allocation (``docs/architecture/technical-guide.md``, "Assignment Algorithm").
The hash is the cross-SDK one (``docs/sdk/javascript.md``, "Hash utilities";
pinned by ``tests/sdk-contract/golden-vectors.json``): the first four bytes of
``MD5("{user_id}:{experiment_key}")`` read as a little-endian unsigned integer
and divided by 2^32, times 100, its integer part the bucket. ``choose`` walks the user ids
``<prefix>-00001``, ``<prefix>-00002``, ... and keeps, per variant, the first
ones the hash puts there until each variant has its count. Every assignment the
API answers is then checked against that: a user answered with another variant,
or not assigned, fails the step. So the counts sent are exactly the counts
chosen, and a disagreement between the API's assignment and the documented hash
is a failure of its own, not a skewed count.

A step that gives a ``value`` (``model.TrafficValue``) sends one with each
converting user's event, and none with an assignment. It is never random, so
an oracle recomputes it from the journey: the ``i``-th converting user of a
variant (from 0, counted in each variant on its own, in the order ``choose``
chose them) sends ``base + step * (i mod modulo)`` (``ramp_values``). Without
a ``value``, no event carries one.

This module computes the hash itself (``hashlib``); it imports nothing from the
product, so an assignment that drifts from the contract is caught rather than
copied. Nothing here imports Playwright: the requests go through a ``fetch``
the runner passes in, and the unit job tests the rest with a fake one.

What a step writes (``NN-<step>.traffic.json``) holds counts, the first few
problems and, when the step gives a ``value``, the value it gives and every
value sent, per variant in the order sent; never the API key.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

#: 2^32, the divisor the SDK contract gives.
HASH_DIVISOR = 4294967296
#: How many candidate user ids ``choose`` tries per user it needs before giving up.
CANDIDATES_PER_USER = 50
#: How many problems a step lists; it stops sending once it has this many.
PROBLEMS_KEPT = 10

ASSIGN_PATH = "/api/v1/tracking/assign"
TRACK_PATH = "/api/v1/tracking/track"

#: ``fetch(method, path, body)``: the answer's status and its JSON (None if none).
Fetch = Callable[[str, str, Optional[Dict[str, Any]]], Tuple[int, Any]]


class TrafficError(Exception):
    """The population could not be sent as chosen; the message says why."""


def bucket(user_id: str, experiment_key: str) -> int:
    """The user's bucket in [0, 100) under the experiment's key (the SDK contract)."""
    digest = hashlib.md5(
        f"{user_id}:{experiment_key}".encode("utf-8"), usedforsecurity=False
    ).digest()
    (number,) = struct.unpack_from("<I", digest[:4])
    return int(number / HASH_DIVISOR * 100)


def variant_for(
    user_id: str, experiment_key: str, allocations: Sequence[Tuple[str, int]]
) -> str:
    """The variant the documented hash gives *user_id*.

    *allocations* is ``(variant name, traffic allocation in percent)`` in the
    experiment's order; a tail left when they add up to less than 100 goes to
    the last variant, as the server does.
    """
    if not allocations:
        raise TrafficError("the experiment has no variants")
    position = bucket(user_id, experiment_key)
    cumulative = 0
    for name, allocation in allocations:
        cumulative += allocation
        if position < cumulative:
            return name
    return allocations[-1][0]


def user_id(prefix: str, number: int) -> str:
    return f"{prefix}-{number:05d}"


def choose(
    experiment_key: str,
    allocations: Sequence[Tuple[str, int]],
    counts: Mapping[str, int],
    prefix: str,
) -> Dict[str, List[str]]:
    """Per variant, the first ``counts[variant]`` user ids the hash puts there.

    TrafficError when a variant of *counts* is not one of the experiment's, or
    when ``CANDIDATES_PER_USER`` candidates per user needed are not enough (a
    variant with a tiny allocation).
    """
    names = [name for name, _ in allocations]
    unknown = sorted(set(counts) - set(names))
    if unknown:
        raise TrafficError(
            f"the experiment has no variant {unknown[0]!r} (it has {', '.join(names)})"
        )
    chosen: Dict[str, List[str]] = {name: [] for name in counts}
    needed = sum(counts.values())
    limit = max(needed, 1) * CANDIDATES_PER_USER
    number = 0
    while any(len(chosen[name]) < counts[name] for name in counts):
        number += 1
        if number > limit:
            short = [n for n in counts if len(chosen[n]) < counts[n]]
            raise TrafficError(
                f"{limit} candidate users gave {short[0]!r} only"
                f" {len(chosen[short[0]])} of {counts[short[0]]}"
            )
        candidate = user_id(prefix, number)
        name = variant_for(candidate, experiment_key, allocations)
        if name in chosen and len(chosen[name]) < counts[name]:
            chosen[name].append(candidate)
    return chosen


Number = Union[int, float]


class Ramp(Protocol):
    """A traffic step's ``value`` (``model.TrafficValue``), read by its fields."""

    mode: str
    base: Number
    step: Number
    modulo: int


def ramp_values(ramp: Ramp, count: int) -> List[Number]:
    """The values a variant's first *count* converting users send, in order.

    The ``i``-th (from 0) sends ``base + step * (i mod modulo)``: base 10,
    step 1, modulo 7 gives 10, 11, 12, 13, 14, 15, 16, 10, 11, ...
    """
    if ramp.mode != "ramp":
        raise TrafficError(f"no value mode {ramp.mode!r}; ramp is the one mode")
    return [ramp.base + ramp.step * (i % ramp.modulo) for i in range(count)]


@dataclass
class Outcome:
    """What was sent and what came back, in counts, for the step's file."""

    experiment_key: str
    prefix: str
    variants: Dict[str, Dict[str, int]] = field(default_factory=dict)
    requests: int = 0
    problems: List[str] = field(default_factory=list)
    #: The step's ``value`` as given, or None (then no event carried one).
    value: Optional[Dict[str, Any]] = None
    #: Per variant, every value sent with an event, in the order sent.
    values_sent: Dict[str, List[Number]] = field(default_factory=dict)

    def record(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "experiment_key": self.experiment_key,
            "users": f"{self.prefix}-NNNNN",
            "variants": self.variants,
            "requests": self.requests,
            "problems": self.problems,
        }
        if self.value is not None:
            record["value"] = self.value
            record["values_sent"] = self.values_sent
        return record


def send(
    fetch: Fetch,
    *,
    experiment_key: str,
    event: str,
    chosen: Mapping[str, Sequence[str]],
    converted: Mapping[str, int],
    prefix: str,
    value: Optional[Ramp] = None,
) -> Outcome:
    """Assign every chosen user, then track *event* for each variant's first converters.

    Each assignment must answer 200 with ``assigned: true`` and the variant the
    user was chosen for; each track must answer 200. Sending stops once
    ``PROBLEMS_KEPT`` problems are found, and no event is tracked unless every
    assignment was as chosen. With a *value*, each event carries the next of
    its variant's ``ramp_values``, and the outcome lists every value sent.
    """
    outcome = Outcome(experiment_key=experiment_key, prefix=prefix)
    if value is not None:
        outcome.value = {
            "mode": value.mode,
            "base": value.base,
            "step": value.step,
            "modulo": value.modulo,
        }

    def problem(text: str) -> bool:
        outcome.problems.append(text)
        return len(outcome.problems) >= PROBLEMS_KEPT

    stop = False
    for name, users in chosen.items():
        counted = {"assigned": 0, "as_chosen": 0, "converted": 0}
        outcome.variants[name] = counted
        for user in users:
            if stop:
                break
            status, answer = fetch(
                "POST", ASSIGN_PATH, {"experiment_key": experiment_key, "user_id": user}
            )
            outcome.requests += 1
            if status != 200 or not isinstance(answer, dict):
                stop = problem(f"assigning {user} answered {status}")
                continue
            counted["assigned"] += 1
            if answer.get("assigned") is not True:
                stop = problem(
                    f"{user} was not assigned (reason {answer.get('reason')!r})"
                )
            elif answer.get("variant_name") != name:
                stop = problem(
                    f"{user} was assigned {answer.get('variant_name')!r}; the"
                    f" documented hash puts it in {name!r}"
                )
            else:
                counted["as_chosen"] += 1
    if outcome.problems:
        return outcome
    for name, users in chosen.items():
        converting = users[: converted.get(name, 0)]
        values = ramp_values(value, len(converting)) if value is not None else None
        if values is not None:
            outcome.values_sent[name] = []
        for index, user in enumerate(converting):
            body: Dict[str, Any] = {
                "event_type": event,
                "user_id": user,
                "experiment_key": experiment_key,
            }
            carried = ""
            if values is not None:
                body["value"] = values[index]
                outcome.values_sent[name].append(values[index])
                carried = f" (value {values[index]!r})"
            status, _ = fetch("POST", TRACK_PATH, body)
            outcome.requests += 1
            if status != 200:
                if problem(f"tracking {event} for {user}{carried} answered {status}"):
                    return outcome
                continue
            outcome.variants[name]["converted"] += 1
    return outcome


# ---------------------------------------------------------------------------
# Flag evaluations
# ---------------------------------------------------------------------------
#: ``GET`` this with ``user_id`` and, optionally, ``context`` (URL-encoded JSON).
EVALUATE_PATH = "/api/v1/feature-flags/evaluate/{flag}"


def flag_bucket(user_id: str, flag_key: str) -> int:
    """The user's rollout bucket in [0, 100) for a flag, as documented.

    ``docs/sdk/javascript.md`` ("Hash utilities") gives it: the whole MD5 digest
    of ``"{user_id}:{flag_key}"`` read as a big-endian integer, modulo 100
    (``md5-mod100-v1``). A user gets a flag at ``p`` percent when the bucket is
    below ``p``. It is not the assignment hash above: ``user-123`` and
    ``my-flag`` are bucket 79 here and 69 there.
    """
    digest = hashlib.md5(
        f"{user_id}:{flag_key}".encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return int(digest, 16) % 100


def expected_answer(
    user: str, flag_key: str, reason: str, rollout: Optional[int]
) -> Tuple[bool, str]:
    """(enabled, reason) the evaluation must give *user*."""
    if reason == "rollout":
        return flag_bucket(user, flag_key) < (rollout or 0), "rollout"
    return reason == "targeting_rule", reason


def evaluate(
    fetch: Fetch,
    *,
    flag_key: str,
    users: Sequence[str],
    reason: str,
    rollout: Optional[int],
    query: str = "",
) -> Outcome:
    """Evaluate the flag for each user; a problem per answer other than expected.

    *query* is appended to each request's ``user_id`` (the URL-encoded
    ``context``, say). Stops once ``PROBLEMS_KEPT`` problems are found.
    """
    outcome = Outcome(experiment_key=flag_key, prefix="")
    counts = {"evaluated": 0, "enabled": 0, "expected_enabled": 0}
    outcome.variants[flag_key] = counts
    path = EVALUATE_PATH.format(flag=flag_key)
    for user in users:
        enabled, why = expected_answer(user, flag_key, reason, rollout)
        counts["expected_enabled"] += int(enabled)
        status, answer = fetch("GET", f"{path}?user_id={user}{query}", None)
        outcome.requests += 1
        if status != 200 or not isinstance(answer, dict):
            if _full(outcome, f"evaluating for {user} answered {status}"):
                break
            continue
        counts["evaluated"] += 1
        counts["enabled"] += int(answer.get("enabled") is True)
        if answer.get("enabled") is not enabled or answer.get("reason") != why:
            if _full(
                outcome,
                f"{user}: enabled {answer.get('enabled')!r}, reason"
                f" {answer.get('reason')!r}; expected enabled {enabled}, reason {why!r}",
            ):
                break
    return outcome


def _full(outcome: Outcome, problem: str) -> bool:
    outcome.problems.append(problem)
    return len(outcome.problems) >= PROBLEMS_KEPT
