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

``local_proxy_findings`` and ``local_kill_findings`` look only at ruleset 200s
the proxy relayed (``fault is None``): a corrupt fault is a 200 too, and is
neither the SDK being behind the proxy nor a ruleset the server served.

Use case #4 (local evaluation through an outage; the proxy's faults, counted
from ``Record.fault``):

* ``outage_findings``: the refreshes during a 503 outage follow the
  documented backoff: exactly ``expected`` faulted ruleset requests, all
  answered the outage's status, consecutive among the ruleset requests; each
  gap from a failure's answer to the next request at least the backoff step
  (R, 2R, 4R, ...; ``SLACK`` for the clock), and the first unfaulted ruleset
  request after them at least the next step after the last. Lower bounds
  only: a timer never fires early, and a runner may be slow. None faulted is
  void. The same gate holds a corrupt ruleset after a load (``fault=``): it is
  a failed refresh, and backs off the same way.
* ``error_tally_findings``: the driver's ``on_error`` count for an operation
  equals the requests the proxy faulted on its path.
* ``last_known_findings``: every answer of every round is A0's (the answers
  before the outage), local, with no error.
* ``deferral_findings``: evaluate requests per flag key are the distinct users
  asked, for a flag the server evaluates, and 0 for a local one (the proxy
  keeps no query string, so per key over chosen users stands in for per pair).
* ``kept_etag_findings``: the first unfaulted ruleset request after the
  faulted ones is a 304 sent with ``If-None-Match``: the SDK kept the last
  good ruleset's ETag. A failing gate, never a report.
* ``hung_refresh_findings``: while a ruleset request was held (a blackhole),
  the SDK gave it up itself (``client_closed``), and at least ``minimum``
  polls were sent and answered inside the hold, each A0's answer, local.
* ``upstream_failure_findings``: no unfaulted 502 (the proxy's own answer
  when the upstream cannot be reached): an injected fault is always tagged.
* ``serving_findings``: every ``status()`` read through a fault window says
  the SDK answers locally from the ruleset it had before the faults, and one
  read after the last fault shows it saw the failure (``last_error``).

``retry_deadline`` is not a gate: it is how long a case waits for the SDK's
next ruleset request after n faulted ones (the n-th answer, plus the backoff
step after it, plus R), so a wait never ends before a retry the documented
backoff still allows.

``status()`` gives the ruleset's version as the document has it; the ruleset
route sends it as the ETag in double quotes (``ETag: "<version>"``), which is
what the proxy records. ``etag_of`` is the one place that turns one into the
other: compared raw, "the ETag differs from the old version" is always true.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from usecases.proxy import RULESET_PATH, Record

#: Every flag's evaluate route starts with this.
EVALUATE_PREFIX = "/api/v1/feature-flags/evaluate/"
#: Users named in a finding, at most.
SHOWN = 5
#: The keys of a driver's ``status()["errors"]``: ``on_error`` calls per
#: operation, any other operation counted as ``other``.
ERROR_KEYS = ("refresh", "flush", "evaluate", "other")
#: Both SDKs' local-mode backoff ceiling, in seconds.
BACKOFF_CAP = 300.0
#: How much earlier than its step a retry may seem to come on the runner's
#: clock before a gate calls it early.
SLACK = 0.05
#: A pair: (flag key, user id).
Pair = tuple[str, str]


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
    """A local-mode driver's proxy: at least one relayed ruleset 200 (else the
    SDK is not behind the proxy, and nothing it did was seen), no evaluate
    call."""
    found = []
    if not any(
        r.path == RULESET_PATH and r.status == 200 and r.fault is None for r in records
    ):
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
    the first false. Only the ruleset requests the proxy relayed count
    (``fault is None``). Then:

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
    ruleset = [
        record
        for record in records
        if record.path == RULESET_PATH and record.fault is None
    ]
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


# ---------------------------------------------------------------------------
# Use case #4: local evaluation through an outage
# ---------------------------------------------------------------------------
def _ruleset_requests(records: Sequence[Record]) -> list[Record]:
    return sorted(
        (record for record in records if record.path == RULESET_PATH),
        key=lambda record: record.seq,
    )


def backoff_step(refresh: float, failures: int) -> float:
    """The delay both SDKs wait after *failures* failed refreshes in a row:
    ``refresh * 2 ** (failures - 1)``, at most ``BACKOFF_CAP``, counted from
    the end of the failed request."""
    return min(refresh * 2 ** (failures - 1), BACKOFF_CAP)


def outage_findings(
    records: Sequence[Record],
    label: str,
    *,
    refresh: float = 5.0,
    expected: int = 5,
    status: int = 503,
    fault: str | None = None,
) -> list[str]:
    """One proxy's records over a ``status`` outage on every path, the SDK
    refreshing every *refresh* seconds: exactly *expected* faulted ruleset
    requests, all answered *status*, with no relayed ruleset request between
    them; after the k-th failure the next request at least
    ``backoff_step(refresh, k) - SLACK`` after that failure's answer; and the
    first relayed ruleset request after the last failure at least the step
    after it. None faulted is void: the outage never met the SDK.

    *fault*, when given, is the tag every faulted request must carry instead
    of ``status:<status>``: a ruleset that is not valid after a load
    (``corrupt:truncated``) is a failed refresh too, and backs off the same
    way."""
    ruleset = _ruleset_requests(records)
    faulted_at = [i for i, record in enumerate(ruleset) if record.fault is not None]
    if not faulted_at:
        return [f"void: {label}: the outage met no ruleset request (0 faulted)"]
    faulted = [ruleset[i] for i in faulted_at]
    found = []
    if len(faulted) != expected:
        found.append(
            f"{label}: {len(faulted)} faulted ruleset requests during the cut,"
            f" expected {expected}"
        )
    tag = f"status:{status}" if fault is None else fault
    wanted = f"{status} ({tag})" if fault is None else tag
    wrong = sorted({(r.status, str(r.fault)) for r in faulted if r.fault != tag})
    if wrong:
        found.append(
            f"{label}: faulted ruleset requests were answered {wrong}, expected"
            f" every one {wanted}"
        )
    between = [r for r in ruleset[faulted_at[0] : faulted_at[-1]] if r.fault is None]
    if between:
        found.append(
            f"{label}: {len(between)} relayed ruleset requests came between the"
            f" faulted ones (the first seq {between[0].seq}): not one outage"
        )
    early = []
    for k, (failed, retry) in enumerate(zip(faulted, faulted[1:]), start=1):
        gap = retry.t_recv - failed.t_answer
        step = backoff_step(refresh, k)
        if gap < step - SLACK:
            early.append((k, gap, step))
    if early:
        k, gap, step = early[0]
        found.append(
            f"{label}: {len(early)} retries came before their backoff step:"
            f" retry {k + 1} came {gap:.2f} s after failure {k}, expected at"
            f" least {step:g} s"
        )
    after = ruleset[faulted_at[-1] + 1 :]
    if not after:
        found.append(
            f"{label}: no relayed ruleset request after the outage: recovery was"
            " not seen"
        )
    else:
        wait = after[0].t_recv - faulted[-1].t_answer
        step = backoff_step(refresh, len(faulted))
        if wait < step - SLACK:
            found.append(
                f"{label}: the first ruleset request after the outage came"
                f" {wait:.2f} s after the last failure, expected at least {step:g} s"
            )
    return found


def retry_deadline(
    records: Sequence[Record], *, refresh: float, since: float, first_wait: float
) -> float:
    """When the SDK's next ruleset request is due at the latest, on the
    proxy's clock, from what one proxy recorded: after a streak of n faulted
    ruleset requests (the faulted ones since the last relayed one), the n-th
    one's answer plus ``backoff_step(refresh, n)`` plus *refresh* to spare;
    with no streak, *since* + *first_wait*. A fixed 3R would end before the
    retry after the third failure, which comes 4R after it."""
    streak: list[Record] = []
    for record in _ruleset_requests(records):
        streak = [*streak, record] if record.fault is not None else []
    if not streak:
        return since + first_wait
    return streak[-1].t_answer + backoff_step(refresh, len(streak)) + refresh


def serving_findings(
    statuses: Sequence[tuple[float, Any]],
    records: Sequence[Record],
    label: str,
    *,
    version: str | None,
) -> list[str]:
    """The driver's ``status()`` read through a fault window, each with the
    time it was read: every one says the SDK is answering locally (``ready``)
    from *version*, the ruleset it loaded before the faults (a discarded
    ruleset says ``ready`` false; a cache can hide that from the answers).
    And at least one read after the last faulted ruleset request was answered
    carries ``last_error``: the SDK had seen the failure and still answered
    locally. Without one the reads did not cover the faults (void)."""
    if not statuses:
        return [f"void: {label}: no status() was read through the faults"]
    found = []
    wrong = [
        status
        for _, status in statuses
        if not isinstance(status, Mapping)
        or status.get("ready") is not True
        or status.get("ruleset_version") != version
    ]
    if wrong:
        first = wrong[0] if isinstance(wrong[0], Mapping) else {}
        found.append(
            f"{label}: {len(wrong)} of {len(statuses)} status() reads were not"
            f" ready on version {version!r} (the first: ready"
            f" {first.get('ready')!r}, version {first.get('ruleset_version')!r})"
        )
    faulted = [r for r in _ruleset_requests(records) if r.fault is not None]
    if not faulted:
        found.append(f"void: {label}: no ruleset request was faulted")
        return found
    last = faulted[-1].t_answer
    seen = [
        status
        for read, status in statuses
        if read >= last
        and isinstance(status, Mapping)
        and status.get("last_error") is not None
    ]
    if not seen:
        found.append(
            f"void: {label}: no status() read after the last faulted ruleset request"
            " carried last_error: the reads did not cover the faults"
        )
    return found


def outage_report(
    records: Sequence[Record], *, t_cut: float, refresh: float = 5.0
) -> str:
    """Wall clock, reported and never gated: the first faulted ruleset
    request's time after the cut (f), each retry's excess over its backoff
    step, and the wait from the last failure to the next request."""
    ruleset = _ruleset_requests(records)
    faulted = [record for record in ruleset if record.fault is not None]
    if not faulted:
        return "no faulted ruleset request"
    excess = [
        f"{retry.t_recv - failed.t_answer - backoff_step(refresh, k):+.2f}"
        for k, (failed, retry) in enumerate(zip(faulted, faulted[1:]), start=1)
    ]
    later = [r for r in ruleset if r.seq > faulted[-1].seq]
    recovery = (
        f"{later[0].t_recv - faulted[-1].t_answer:.2f} s"
        if later
        else "no request after"
    )
    return (
        f"f {faulted[0].t_recv - t_cut:.2f} s after the cut; retry excess over"
        f" the step [{', '.join(excess)}] s; next request after the last failure"
        f" {recovery}"
    )


def error_tally_findings(
    errors: Any,
    records: Sequence[Record],
    label: str,
    *,
    operation: str = "refresh",
    path: str = RULESET_PATH,
) -> list[str]:
    """*errors*, a driver's ``status()["errors"]``, has exactly ``ERROR_KEYS``,
    and its count for *operation* equals the requests the proxy faulted on
    *path*. None faulted is void: the tally would compare nothing."""
    if not isinstance(errors, Mapping) or sorted(errors) != sorted(ERROR_KEYS):
        keys = sorted(errors) if isinstance(errors, Mapping) else errors
        return [
            f"{label}: the driver's status() carries no error tally ({keys!r}),"
            f" expected the keys {', '.join(ERROR_KEYS)}"
        ]
    faulted = sum(1 for r in records if r.path == path and r.fault is not None)
    if faulted == 0:
        return [f"void: {label}: the proxy faulted no request on {path}"]
    counted = errors[operation]
    if isinstance(counted, bool) or counted != faulted:
        return [
            f"{label}: on_error({operation}) {counted!r}, the proxy faulted {faulted}"
        ]
    return []


def last_known_findings(
    a0: Mapping[Pair, Any], rounds: Sequence[Mapping[Pair, Poll]], label: str
) -> list[str]:
    """Every round asked exactly A0's pairs, and every answer is A0's (the
    answer before the outage), local, with no error."""
    if not a0 or not rounds:
        return [f"void: {label}: {len(a0)} pairs in A0, {len(rounds)} rounds polled"]
    found = []
    for number, answers in enumerate(rounds, start=1):
        missing = [pair for pair in a0 if pair not in answers]
        extra = [pair for pair in answers if pair not in a0]
        if missing or extra:
            found.append(
                f"{label}: round {number} asked {len(answers)} pairs, A0 has"
                f" {len(a0)} ({len(missing)} missing, {len(extra)} not in A0)"
            )
        changed = [
            (pair, poll.enabled)
            for pair, poll in answers.items()
            if pair in a0 and poll.enabled is not a0[pair]
        ]
        if changed:
            shown = ", ".join(
                f"{pair!r} answered {enabled!r}" for pair, enabled in changed[:SHOWN]
            )
            found.append(
                f"{label}: round {number}: {len(changed)} of {len(a0)} answers"
                f" differ from A0 ({shown})"
            )
        remote = [
            poll
            for poll in answers.values()
            if poll.source != "local" or poll.error is not None
        ]
        if remote:
            found.append(
                f"{label}: round {number}: {len(remote)} answers were not local or"
                f" carried an error (sources {sorted({str(p.source) for p in remote})})"
            )
    return found


def deferral_findings(
    records: Sequence[Record],
    label: str,
    *,
    remote: Mapping[str, Iterable[str]],
    local: Collection[str],
) -> list[str]:
    """Evaluate requests per flag key: for each key in *remote* (the flags
    the server evaluates, each with the users the case asked about it),
    exactly the number of distinct users; for each key in *local*, none; and
    none for a flag the case did not name. Keys are compared as the path
    carries them (the cases' keys need no encoding)."""
    if not remote and not local:
        return [f"void: {label}: no flag to count evaluate requests for"]
    counts = Counter(
        r.path[len(EVALUATE_PREFIX) :]
        for r in records
        if r.path.startswith(EVALUATE_PREFIX)
    )
    found = []
    for key, users in sorted(remote.items()):
        want = len(set(users))
        if counts.get(key, 0) != want:
            found.append(
                f"{label}: {counts.get(key, 0)} evaluate requests for the remote flag"
                f" {key!r}, expected {want} (one per distinct user asked)"
            )
    for key in sorted(set(local)):
        if counts.get(key, 0):
            found.append(
                f"{label}: {counts[key]} evaluate requests for the local flag"
                f" {key!r}, expected 0"
            )
    unnamed = sorted(set(counts) - set(remote) - set(local))
    if unnamed:
        found.append(
            f"{label}: evaluate requests for flags the case did not name: {unnamed}"
        )
    return found


def kept_etag_findings(records: Sequence[Record], label: str) -> list[str]:
    """The first relayed ruleset request after the last faulted one is a 304
    to a request that carried ``If-None-Match``: the SDK kept the last good
    ruleset's ETag through the faults. A failing gate: with a long cache a
    discarded ruleset can hide in the answers, and this is what sees it."""
    ruleset = _ruleset_requests(records)
    faulted_at = [i for i, record in enumerate(ruleset) if record.fault is not None]
    if not faulted_at:
        return [f"void: {label}: no ruleset request was faulted"]
    after = ruleset[faulted_at[-1] + 1 :]
    if not after:
        return [
            f"{label}: no ruleset request after the faulted ones: whether the SDK"
            " kept its ruleset was not seen"
        ]
    first = after[0]
    if (first.status, first.if_none_match) != (304, True):
        sent = "with" if first.if_none_match else "without"
        return [
            f"{label}: the first ruleset answer after the faulted ones was"
            f" {first.status} to a request {sent} If-None-Match, expected a 304 to"
            " If-None-Match: the SDK did not keep its last good ruleset's ETag"
        ]
    return []


def hung_refresh_findings(
    records: Sequence[Record],
    a0: Mapping[Pair, Any],
    polls: Sequence[tuple[Pair, Poll]],
    label: str,
    *,
    minimum: int = 10,
) -> list[str]:
    """The first held (blackhole) ruleset request: the SDK gave it up itself
    (``client_closed``), and at least *minimum* of *polls* were sent after it
    arrived and answered before it ended, each A0's answer, local, with no
    error: evaluation never waited on the refresh."""
    held = [r for r in _ruleset_requests(records) if r.fault == "blackhole"]
    if not held:
        return [f"void: {label}: no ruleset request was held"]
    hold = held[0]
    found = []
    if not hold.client_closed:
        found.append(
            f"{label}: the SDK did not give up the held ruleset request (it ended"
            f" after {hold.t_answer - hold.t_recv:.2f} s with the hold or the"
            " proxy): its timeout did not fire"
        )
    inside = [
        (pair, poll)
        for pair, poll in polls
        if poll.sent >= hold.t_recv and poll.answered <= hold.t_answer
    ]
    if len(inside) < minimum:
        found.append(
            f"{label}: {len(inside)} polls were sent and answered while the ruleset"
            f" request was held, expected at least {minimum}"
        )
    wrong = [
        pair
        for pair, poll in inside
        if pair not in a0
        or poll.enabled is not a0[pair]
        or poll.source != "local"
        or poll.error is not None
    ]
    if wrong:
        found.append(
            f"{label}: {len(wrong)} of the polls during the hold were not A0's local"
            f" answer ({', '.join(map(repr, wrong[:SHOWN]))})"
        )
    return found


def upstream_failure_findings(records: Sequence[Record], label: str) -> list[str]:
    """No unfaulted 502: that is the proxy's own answer when the upstream
    cannot be reached or breaks off, which a gate counting tagged faults
    would otherwise not see."""
    failed = [r for r in records if r.fault is None and r.status == 502]
    if not failed:
        return []
    paths = sorted({r.path for r in failed})
    return [
        f"{label}: {len(failed)} unfaulted 502 answers ({', '.join(paths)}): the"
        " upstream failed; an injected fault is always tagged"
    ]
