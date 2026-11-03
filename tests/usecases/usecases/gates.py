"""The over-the-wire gates (SPEC 2 of the D210 v3 plan), as pure functions of
what the proxy recorded and what the drivers answered, on the runner's one
clock (``time.monotonic``: the proxy runs in the runner's process).

A case collects the facts: the proxy's ``Record`` list and its own ``Poll``
list, each poll with the time its command was written and the time its answer
was read. These functions say what is wrong with them: a list of findings,
empty when the gate holds. Being pure, each gate is checked in the backend's
unit job (``backend/tests/unit/usecases/test_gates.py``) against synthetic
traces, the defect it exists to catch included, with no server and no SDK.

* ``pacing_findings``: ``429 == 0`` over every record. A pacing guard: the
  cases pace their own requests and the SDKs' refreshes are seconds apart, so
  a 429 means the pacing did not keep the rate limiter quiet. It says nothing
  about how the limiter keys its counters. (The JS SDK's local mode backs off
  silently on a 429, so without it a step would stall rather than fail.)
* ``local_proxy_findings``: a local-mode driver fetched the ruleset through
  its proxy (the positive control: the SDK is behind the proxy) and never
  called the evaluate route.
* ``o_flag_findings``: every answer of one step is O-FLAG's
  (``ground_truth.hashing.flag_bucket``), for every user.
* ``remote_refetch_findings`` (G2-1): a server-mode SDK asks the API about a
  flag again only once its cache lifetime has ended, and the first call made
  after that asks.
* ``local_kill_findings`` (G2-2): a local-mode SDK answers a flag turned off
  as off from the first ruleset that carries the change, and only from it.

``status()`` gives the ruleset's version as the document has it; the ruleset
route sends it as the ETag in double quotes (``ETag: "<version>"``), which is
what the proxy records. ``etag_of`` is the one place that turns one into the
other: compared raw, "the ETag differs from the old version" is always true.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from usecases.proxy import RULESET_PATH, Record

#: Every flag's evaluate route starts with this.
EVALUATE_PREFIX = "/api/v1/feature-flags/evaluate/"
#: Users named in a finding, at most.
SHOWN = 5


@dataclass(frozen=True)
class Poll:
    """One ``evaluate`` command: when it was written and when its answer was
    read, on the runner's clock, and what it answered."""

    sent: float
    answered: float
    enabled: Any
    source: Any = None
    reason: Any = None
    error: Any = None


def etag_of(version: str | None) -> str | None:
    """The ETag the ruleset route sends for *version*: the version in double
    quotes."""
    return None if version is None else f'"{version}"'


def pacing_findings(records: Sequence[Record], label: str) -> list[str]:
    """``429 == 0`` over every record of one proxy."""
    limited = [record for record in records if record.status == 429]
    if not limited:
        return []
    paths = sorted({record.path for record in limited})
    return [
        f"{label}: {len(limited)} answers were 429 ({', '.join(paths)}): the"
        " pacing did not keep the rate limiter quiet"
    ]


def local_proxy_findings(records: Sequence[Record], label: str) -> list[str]:
    """A local-mode driver's proxy: at least one ruleset 200 (else the SDK is
    not behind the proxy, and nothing it did was seen), no evaluate call."""
    found = []
    if not any(r.path == RULESET_PATH and r.status == 200 for r in records):
        found.append(
            f"void: {label} is not behind its proxy (no ruleset 200 went through it)"
        )
    calls = [r for r in records if r.path.startswith(EVALUATE_PREFIX)]
    if calls:
        found.append(
            f"{label}: {len(calls)} calls to the evaluate route in local mode,"
            " expected 0"
        )
    return found


def o_flag_findings(
    label: str,
    flag_key: str,
    percentage: int,
    answers: Sequence[tuple[str, Any]],
    users: Sequence[str],
    bucket: Callable[[str, str], int],
) -> list[str]:
    """Each of *users* answered exactly once, in order, and ``enabled`` is
    ``bucket(user, flag_key) < percentage`` for every one."""
    if [user for user, _ in answers] != list(users):
        return [
            f"{label} at {percentage}%: {len(answers)} answers for"
            f" {len(users)} users, or not in their order"
        ]
    wrong = [
        (user, enabled)
        for user, enabled in answers
        if enabled is not (bucket(user, flag_key) < percentage)
    ]
    if not wrong:
        return []
    shown = ", ".join(
        f"{user!r} answered {enabled!r}" for user, enabled in wrong[:SHOWN]
    )
    return [
        f"{label} at {percentage}%: {len(wrong)} of {len(users)} answers are not"
        f" O-FLAG's ({shown})"
    ]


def remote_refetch_findings(
    records: Sequence[Record],
    path: str,
    *,
    t1: float,
    a1: float,
    ttl: float,
    polls: Sequence[Poll],
) -> list[str]:
    """G2-1. Command 1 was written at *t1* and answered at *a1*; the flag was
    then turned off, and *polls* followed until the first false. With a cache
    lifetime of *ttl*:

    * the first request to *path* is command 1's, made between t1 and a1;
    * none follows it before t1 + ttl (the SDK cannot have cached before t1);
    * exactly one is made from t1 + ttl on, during a poll that answered false,
      every earlier poll answering true from the cache;
    * it is made no later than the first poll written at or after a1 + ttl
      (the SDK cached by the time it answered, so its entry has expired then):
      the cache does not outlive its lifetime.
    """
    found = []
    on_path = [record for record in records if record.path == path]
    if not on_path:
        return [
            "void: the SDK is not behind its proxy (no request to the flag's route)"
        ]
    first = on_path[0]
    if not t1 <= first.t_recv <= a1:
        found.append(
            "the first request to the flag's route was not command 1's"
            f" ({first.t_recv - t1:+.3f} s from it)"
        )
    early = [record for record in on_path[1:] if record.t_recv < t1 + ttl]
    if early:
        found.append(
            f"asked the API again {early[0].t_recv - t1:.2f} s after command 1,"
            f" before its {ttl:g} s cache lifetime ended ({len(early)} such requests)"
        )
    late = [record for record in on_path if record.t_recv >= t1 + ttl]
    if len(late) != 1:
        found.append(
            f"{len(late)} requests to the flag's route from command 1 + {ttl:g} s"
            " to the end, expected exactly 1"
        )
    errors = [poll for poll in polls if poll.error is not None]
    if errors:
        found.append(f"{len(errors)} polls answered an error")
    if not any(poll.enabled is False for poll in polls):
        found.append("no poll answered false")
    if not late:
        return found
    refetch = late[0]
    during = [
        index
        for index, poll in enumerate(polls)
        if poll.sent <= refetch.t_recv <= poll.answered
    ]
    if len(during) != 1:
        found.append(
            "the request after the cache lifetime was not made during one poll"
            f" ({refetch.t_recv - t1:.2f} s after command 1)"
        )
    else:
        index = during[0]
        if polls[index].enabled is not False:
            found.append(
                "the poll that asked the API again answered"
                f" {polls[index].enabled!r}, not false"
            )
        cached = [poll.enabled for poll in polls[:index]]
        if any(enabled is not True for enabled in cached):
            found.append(
                "a poll before the API was asked again did not answer true from"
                f" the cache: {cached}"
            )
    due = [poll for poll in polls if poll.sent >= a1 + ttl]
    if due and refetch.t_recv > due[0].answered:
        found.append(
            f"the first poll written {ttl:g} s after command 1 was answered"
            f" (+{due[0].sent - a1:.2f} s) did not ask the API: it asked"
            f" {refetch.t_recv - a1:.2f} s after, so the cache outlived its"
            f" {ttl:g} s lifetime"
        )
    return found


def local_kill_findings(
    records: Sequence[Record],
    *,
    version_before: str | None,
    toggle_sent: float,
    version_after: str | None,
    polls: Sequence[Poll],
) -> list[str]:
    """G2-2. Before the toggle the driver's ``status()`` said
    *version_before*; the toggle was written at *toggle_sent*; then the server
    served *version_after* (read straight from it), and *polls* followed until
    the first false. Then:

    * the last ruleset 200 the proxy answered before the toggle carries
      ``etag_of(version_before)``: status() and the wire agree on V1;
    * the server's version moved, and no ruleset 200 answered after the toggle
      still carries V1; the first one carries ``etag_of(version_after)``;
    * no evaluate call went through the proxy;
    * every poll is answered locally, true until the first false, which is
      answered after that first new 200 (the SDK cannot know before it has
      it) with the reason ``inactive``.
    """
    found = []
    v1, v2 = etag_of(version_before), etag_of(version_after)
    ruleset = [record for record in records if record.path == RULESET_PATH]
    before = [r for r in ruleset if r.status == 200 and r.t_answer < toggle_sent]
    if not before:
        found.append("void: no ruleset 200 went through the proxy before the toggle")
    elif before[-1].etag != v1:
        found.append(
            f"the last ruleset 200 before the toggle carries ETag {before[-1].etag},"
            f" but status() says version {version_before!r} (ETag {v1})"
        )
    if version_after == version_before:
        found.append("the server's ruleset version did not change with the toggle")
    after = [r for r in ruleset if r.status == 200 and r.t_answer > toggle_sent]
    stale = [r for r in after if r.etag == v1]
    if stale:
        found.append(f"{len(stale)} ruleset 200s after the toggle still carry {v1}")
    if not after:
        found.append("no ruleset 200 went through the proxy after the toggle")
    elif after[0].etag != v2:
        found.append(
            f"the first ruleset 200 after the toggle carries {after[0].etag}, the"
            f" server serves {v2}"
        )
    calls = [r for r in records if r.path.startswith(EVALUATE_PREFIX)]
    if calls:
        found.append(f"{len(calls)} evaluate calls went through the proxy, expected 0")
    if any(poll.source != "local" or poll.error is not None for poll in polls):
        found.append(
            "a poll was not answered locally: sources"
            f" {sorted({str(poll.source) for poll in polls})}"
        )
    off = [index for index, poll in enumerate(polls) if poll.enabled is False]
    if not off:
        found.append("no poll answered false")
        return found
    index = off[0]
    if any(poll.enabled is not True for poll in polls[:index]):
        found.append("a poll before the first false did not answer true")
    if polls[index].reason != "inactive":
        found.append(f"the first false has the reason {polls[index].reason!r}")
    if after and polls[index].answered <= after[0].t_answer:
        found.append(
            "the first false was answered before the first new ruleset went"
            " through the proxy"
        )
    return found
